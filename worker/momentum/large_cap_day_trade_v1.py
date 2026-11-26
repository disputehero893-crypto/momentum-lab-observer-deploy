"""``large_cap_day_trade_v1``: Derek's BOT/v1 large-cap day-trade rules (paper only, OFF by default).

Source: ChatGPT chat "Day Trading Strategies Comparison" (the bot's custom
large-cap adaptation, not a published Ross Cameron ruleset) and draft PR #14.
Universe AAPL/MSFT/SPY, long only, regular-session entries, two setups:
15-minute opening-range breakout with retest, and trend-aligned VWAP pullback.

This module is a deterministic engine. It never talks to a broker. It turns
completed 1-minute bars into entry intents and exit decisions, and keeps the
per-day counters that must survive a restart. The current default strategy
(``liquid_large_cap_iex_v2``) is unchanged; this profile only runs when
``DAY_TRADE_PROFILE=large_cap_day_trade_v1`` is set, the new reviewed config
hash is approved, and the runtime wiring (follow-up to PR #57) exists.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any, Mapping

from .day_trade_core import (DTBar, DayCounters, SessionClock, SessionSeries, ZERO, bar_is_fresh,
                             cents_down, cents_up, cost_allowance, d, local, size_shares)
from .models import Decision, config_fingerprint, plain

PROFILE_ID = "large_cap_day_trade_v1"
UNIVERSE = ("AAPL", "MSFT", "SPY")
SPY_ALIGNED = ("AAPL", "MSFT")
# Main's RiskManager allows one open equity position or order at a time.
GLOBAL_MAX_OPEN_STOCK_POSITIONS = 1

UP, DOWN, CHOP, NOT_READY = "UP", "DOWN", "CHOP_OR_UNCLEAR", "NOT_READY"


@dataclass(frozen=True)
class LargeCapV1Params:
    """Every number in the BOT/v1 spec. Changing any of them changes config_id."""
    universe: tuple = UNIVERSE
    warmup_bars: int = 15
    atr_period: int = 14
    buffer_floor: Decimal = Decimal("0.02")          # b = max($0.02, 0.10 x A0)
    buffer_atr: Decimal = Decimal("0.10")
    # Regime
    efficiency_bars: int = 10
    efficiency_min: Decimal = Decimal("0.30")
    vwap_slope_bars: int = 5
    vwap_slope_atr: Decimal = Decimal("0.10")
    closes_vs_vwap_bars: int = 10
    closes_vs_vwap_needed: int = 8
    # ORB retest
    opening_range_bars: int = 15                     # 09:30-09:44
    orb_retest_bars: int = 5
    orb_retest_upper_atr: Decimal = Decimal("0.25")
    orb_cancel_atr: Decimal = Decimal("0.50")
    orb_deadline_bars: int = 6
    max_orb_fills_per_symbol: int = 1
    # VWAP pullback
    vwap_extension_lookback: int = 10
    vwap_extension_atr: Decimal = Decimal("1.0")
    pullback_min_bars: int = 2
    pullback_max_bars: int = 5
    vwap_touch_atr: Decimal = Decimal("0.25")
    vwap_cancel_atr: Decimal = Decimal("0.50")
    vwap_entry_ceiling_atr: Decimal = Decimal("1.25")
    vwap_max_lifetime_bars: int = 6
    # Entry order
    entry_offset_atr: Decimal = Decimal("0.20")
    limit_ttl_seconds: int = 60
    stop_min_atr: Decimal = Decimal("0.75")
    stop_max_atr: Decimal = Decimal("2.50")
    # Exits
    target_r: Decimal = Decimal("2")
    no_progress_seconds: int = 600
    no_progress_r: Decimal = Decimal("0.5")
    max_hold_seconds: int = 2700
    partials_enabled: bool = False
    breakeven_enabled: bool = False
    trailing_enabled: bool = False
    scale_in_enabled: bool = False
    # Sizing, as fractions of E_ref = min(session-start equity, $100,000)
    reference_equity_cap: Decimal = Decimal("100000")
    risk_per_campaign_pct: Decimal = Decimal("0.00025")    # $25
    symbol_notional_pct: Decimal = Decimal("0.025")        # $2,500
    gross_notional_pct: Decimal = Decimal("0.05")          # $5,000
    committed_risk_pct: Decimal = Decimal("0.0005")        # $50
    daily_loss_pct: Decimal = Decimal("0.00075")           # $75
    max_simultaneous_campaigns: int = 2
    # Caps and cooldowns
    max_campaigns_per_symbol: int = 3
    max_campaigns_per_day: int = 6
    disable_symbol_after_losses: int = 2
    cooldown_after_exit_seconds: int = 600
    cooldown_after_loss_seconds: int = 900
    # Costs
    cost_bps_per_side: Decimal = Decimal("0.5")
    max_round_trip_cost_fraction_of_stop: Decimal = Decimal("0.20")
    # Data
    max_bar_receipt_lag_seconds: int = 10
    regular_session_only: bool = True

    def __post_init__(self) -> None:
        for name in ("partials_enabled", "breakeven_enabled", "trailing_enabled", "scale_in_enabled"):
            if getattr(self, name):
                raise ValueError(f"{name} is disabled in large_cap_day_trade_v1")
        if not self.regular_session_only:
            raise ValueError("large_cap_day_trade_v1 is regular-session only")
        if tuple(self.universe) != UNIVERSE:
            raise ValueError("large_cap_day_trade_v1 trades AAPL, MSFT and SPY only")

    @property
    def config_id(self) -> str:
        return config_fingerprint(self)


@dataclass(frozen=True)
class EffectiveCaps:
    """The profile's caps after taking the stricter of its spec and the global RiskConfig."""
    reference_equity: Decimal
    per_campaign_risk: Decimal
    symbol_notional: Decimal
    gross_notional: Decimal
    committed_risk: Decimal
    daily_loss: Decimal
    max_simultaneous: int
    max_shares: int
    cost_floor: Decimal

    def public(self) -> dict:
        return plain(asdict(self))


def effective_caps(params: LargeCapV1Params, risk_config: Any, session_start_equity: Decimal) -> EffectiveCaps:
    e_ref = min(d(session_start_equity), params.reference_equity_cap)
    rc_risk, rc_loss = d(risk_config.per_trade_risk), d(risk_config.max_daily_loss)
    rc_notional = d(risk_config.max_notional)
    return EffectiveCaps(
        reference_equity=e_ref,
        per_campaign_risk=min(params.risk_per_campaign_pct * e_ref, rc_risk),
        symbol_notional=min(params.symbol_notional_pct * e_ref, rc_notional),
        gross_notional=min(params.gross_notional_pct * e_ref, rc_notional * GLOBAL_MAX_OPEN_STOCK_POSITIONS),
        committed_risk=min(params.committed_risk_pct * e_ref, rc_risk * GLOBAL_MAX_OPEN_STOCK_POSITIONS),
        daily_loss=min(params.daily_loss_pct * e_ref, rc_loss),
        max_simultaneous=min(params.max_simultaneous_campaigns, GLOBAL_MAX_OPEN_STOCK_POSITIONS),
        max_shares=int(risk_config.max_shares),
        cost_floor=max(Decimal("0.02"), d(risk_config.execution_allowance_per_share)),
    )


def buffer(params: LargeCapV1Params, atr0: Decimal) -> Decimal:
    return max(params.buffer_floor, params.buffer_atr * atr0)


def regime_at(series: SessionSeries, i: int, params: LargeCapV1Params) -> str:
    """UP / DOWN / CHOP_OR_UNCLEAR / NOT_READY for the bar at index ``i``."""
    if i < 0 or i + 1 < params.warmup_bars or i < params.efficiency_bars or i < params.vwap_slope_bars:
        return NOT_READY
    atr = series.atr(i)
    if atr is None:
        return NOT_READY
    closes = [b.close for b in series.bars]
    n = params.efficiency_bars
    path = sum(abs(closes[k] - closes[k - 1]) for k in range(i - n + 1, i + 1))
    if path == 0:
        return CHOP
    eff = (closes[i] - closes[i - n]) / path
    slope = series.vwaps[i] - series.vwaps[i - params.vwap_slope_bars]
    window = range(i - params.closes_vs_vwap_bars + 1, i + 1)
    above = sum(1 for k in window if closes[k] > series.vwaps[k])
    below = sum(1 for k in window if closes[k] < series.vwaps[k])
    if (eff >= params.efficiency_min and slope >= params.vwap_slope_atr * atr
            and closes[i] > series.vwaps[i] and above >= params.closes_vs_vwap_needed):
        return UP
    if (eff <= -params.efficiency_min and slope <= -params.vwap_slope_atr * atr
            and closes[i] < series.vwaps[i] and below >= params.closes_vs_vwap_needed):
        return DOWN
    return CHOP


def spy_aligned(spy: SessionSeries | None, minute: datetime, params: LargeCapV1Params) -> tuple[bool, str]:
    """Fresh same-minute SPY close above SPY VWAP and SPY VWAP >= its value 5 minutes earlier."""
    if spy is None or not spy.bars:
        return False, "spy_missing"
    index = {b.start: k for k, b in enumerate(spy.bars)}
    i = index.get(minute)
    if i is None:
        return False, "spy_same_minute_bar_missing"
    if not bar_is_fresh(spy.bars[i], params.max_bar_receipt_lag_seconds):
        return False, "spy_bar_stale"
    j = index.get(minute - timedelta(minutes=params.vwap_slope_bars))
    if j is None:
        return False, "spy_vwap_history_missing"
    if spy.bars[i].close <= spy.vwaps[i]:
        return False, "spy_below_vwap"
    if spy.vwaps[i] < spy.vwaps[j]:
        return False, "spy_vwap_falling"
    return True, "spy_aligned"


def orb_window(t: time) -> bool:
    """ORB entries 09:45-10:45 ET, end-exclusive."""
    return time(9, 45) <= t < time(10, 45)


def vwap_window(t: time) -> bool:
    """VWAP entries 09:50-11:30 and 13:30-15:00 ET (11:30-13:30 pause, nothing after 15:00)."""
    return time(9, 50) <= t < time(11, 30) or time(13, 30) <= t < time(15, 0)


def window_label(t: time) -> str:
    orb, vwap = orb_window(t), vwap_window(t)
    if orb and vwap:
        return "orb_or_vwap"
    if orb:
        return "orb_only"
    if vwap:
        return "vwap_only"
    return "none"


@dataclass
class OrbSetup:
    breakout_index: int
    atr0: Decimal
    buf: Decimal
    orh: Decimal
    retest_index: int | None = None


@dataclass
class VwapSetup:
    start_index: int
    atr0: Decimal
    buf: Decimal
    touched: bool = False


@dataclass
class SymbolState:
    series: SessionSeries
    orb: OrbSetup | None = None
    vwap: VwapSetup | None = None
    vwap_last_end: int = -1
    orh: Decimal | None = None
    last_regime: str = NOT_READY
    last_event: str = ""


def _signal(kind: str, symbol: str, bar: DTBar, atr0: Decimal, buf: Decimal, stop_low: Decimal,
            params: LargeCapV1Params, extra: dict | None = None) -> dict:
    limit = cents_up(bar.close + params.entry_offset_atr * atr0)
    stop = cents_down(stop_low - buf)
    return {"kind": kind, "symbol": symbol, "signal_close": bar.close, "entry_limit": limit,
            "stop_price": stop, "atr0": atr0, "buffer": buf, "bar_start": bar.start, **(extra or {})}


def step_orb(state: SymbolState, i: int, params: LargeCapV1Params, regime: str) -> tuple[dict | None, str]:
    """Advance the ORB-retest machine by bar ``i``. Returns (signal, event)."""
    s = state.series
    bar = s.bars[i]
    t_end = local(bar.end).time()
    if state.orh is None:
        opening = [b for b in s.bars if time(9, 30) <= local(b.start).time() < time(9, 45)]
        if len(opening) == params.opening_range_bars and t_end >= time(9, 45):
            state.orh = max(b.high for b in opening)
        else:
            return None, "opening_range_not_ready"
    setup = state.orb
    if setup is not None:
        k = setup.breakout_index
        if regime == DOWN:
            state.orb = None
            return None, "orb_cancel_regime_down"
        if bar.close < setup.orh - params.orb_cancel_atr * setup.atr0:
            state.orb = None
            return None, "orb_cancel_close_below_orh"
        if not orb_window(t_end):
            state.orb = None
            return None, "orb_cancel_window_closed"
        if i - k > params.orb_deadline_bars:
            state.orb = None
            return None, "orb_expired_deadline"
        if setup.retest_index is None:
            if i - k > params.orb_retest_bars:
                state.orb = None
                return None, "orb_expired_no_retest"
            if setup.orh - setup.buf <= bar.low <= setup.orh + params.orb_retest_upper_atr * setup.atr0:
                setup.retest_index = i
                return None, "orb_retest"
            return None, "orb_waiting_retest"
        prev = s.bars[i - 1]
        if (bar.close > setup.orh + setup.buf and bar.close > prev.high + setup.buf
                and bar.close > s.vwaps[i] + setup.buf):
            lowest = min(b.low for b in s.bars[k + 1:i + 1])  # after breakout through confirmation
            state.orb = None
            return _signal("orb_retest", s.symbol, bar, setup.atr0, setup.buf, lowest, params,
                           {"orh": setup.orh, "breakout_index": k}), "orb_confirmed"
        return None, "orb_waiting_confirmation"
    if not orb_window(t_end) or regime != UP or i < 1:
        return None, "orb_idle"
    atr = s.atr(i)
    if atr is None:
        return None, "orb_idle"
    buf = buffer(params, atr)
    threshold = state.orh + buf
    if bar.close > threshold and s.bars[i - 1].close <= threshold:
        state.orb = OrbSetup(breakout_index=i, atr0=atr, buf=buf, orh=state.orh)
        return None, "orb_breakout"
    return None, "orb_idle"


def step_vwap(state: SymbolState, i: int, params: LargeCapV1Params, regime: str) -> tuple[dict | None, str]:
    """Advance the VWAP-pullback machine by bar ``i``. Returns (signal, event)."""
    s = state.series
    bar = s.bars[i]
    t_end = local(bar.end).time()
    setup = state.vwap
    if setup is not None:
        a0, vwap = setup.atr0, s.vwaps[i]
        if regime == DOWN:
            state.vwap, state.vwap_last_end = None, i
            return None, "vwap_cancel_regime_down"
        if bar.close < vwap - params.vwap_cancel_atr * a0:
            state.vwap, state.vwap_last_end = None, i
            return None, "vwap_cancel_close_below_band"
        pullback_bars = i - setup.start_index
        if (pullback_bars >= params.pullback_min_bars and setup.touched and vwap_window(t_end)
                and bar.close > vwap + setup.buf
                and bar.close > s.bars[i - 1].high + setup.buf
                and bar.close > s.bars[i - 2].high + setup.buf):
            limit = cents_up(bar.close + params.entry_offset_atr * a0)
            lowest = min(b.low for b in s.bars[setup.start_index:i + 1])
            state.vwap, state.vwap_last_end = None, i
            if limit > vwap + params.vwap_entry_ceiling_atr * a0:
                return None, "vwap_skip_entry_above_ceiling"
            return _signal("vwap_pullback", s.symbol, bar, a0, setup.buf, lowest, params,
                           {"vwap": vwap, "pullback_bars": pullback_bars}), "vwap_confirmed"
        # Max lifetime 6 bars = up to 5 pullback bars plus one confirmation bar.
        if pullback_bars + 1 > min(params.pullback_max_bars, params.vwap_max_lifetime_bars - 1):
            state.vwap, state.vwap_last_end = None, i
            return None, "vwap_expired"
        if _touches(bar, vwap, a0, params):
            setup.touched = True
        return None, "vwap_pullback_bar"
    if i < 1 or bar.close >= s.bars[i - 1].close or i - 1 <= state.vwap_last_end:
        return None, "vwap_idle"
    if regime_at(s, i - 1, params) != UP:
        return None, "vwap_idle"
    a0 = s.atr(i - 1)
    if a0 is None:
        return None, "vwap_idle"
    lo = max(0, i - params.vwap_extension_lookback)
    if not any(s.bars[k].close - s.vwaps[k] >= params.vwap_extension_atr * a0 for k in range(lo, i)):
        return None, "vwap_no_extension"
    vwap = s.vwaps[i]
    if bar.close < vwap - params.vwap_cancel_atr * a0:
        state.vwap_last_end = i
        return None, "vwap_cancel_close_below_band"
    state.vwap = VwapSetup(start_index=i, atr0=a0, buf=buffer(params, a0),
                           touched=_touches(bar, vwap, a0, params))
    return None, "vwap_pullback_start"


def _touches(bar: DTBar, vwap: Decimal, a0: Decimal, params: LargeCapV1Params) -> bool:
    band = params.vwap_touch_atr * a0
    return bar.low <= vwap + band and bar.high >= vwap - band


class LargeCapDayTradeV1:
    """Stateful per-session engine. The caller owns orders, fills and persistence."""

    profile_id = PROFILE_ID

    def __init__(self, risk_config: Any, params: LargeCapV1Params | None = None, *,
                 session_start_equity: Decimal | str = "100000", state: Mapping[str, Any] | None = None) -> None:
        self.params = params or LargeCapV1Params()
        self.risk_config = risk_config
        self.caps = effective_caps(self.params, risk_config, d(session_start_equity))
        self.counters = DayCounters.from_state((state or {}).get("counters"))
        self.positions: dict[str, dict] = {k: _load_position(v) for k, v in (state or {}).get("positions", {}).items()}
        self.pending: dict[str, dict] = {k: _load_pending(v) for k, v in (state or {}).get("pending", {}).items()}
        self.symbols: dict[str, SymbolState] = {}
        self.clock: SessionClock | None = None
        self.last_reasons: dict[str, str] = {}

    # ---- session / persistence -------------------------------------------------
    def start_session(self, day: date, close: time | None = None) -> None:
        self.clock = SessionClock.for_day(day, close)
        self.counters.roll(day)
        self.symbols = {sym: SymbolState(SessionSeries(sym, day, self.params.atr_period)) for sym in self.params.universe}

    def to_state(self) -> dict:
        return plain({"profile": PROFILE_ID, "config_id": self.params.config_id,
                      "counters": self.counters.to_state(), "positions": self.positions,
                      "pending": self.pending})

    # ---- bars and entries --------------------------------------------------------
    def on_bar(self, bar: DTBar) -> list[Decision]:
        """Feed one completed regular-session bar. Returns accepted entry intents (or rejections)."""
        if self.clock is None:
            raise RuntimeError("start_session first")
        if bar.symbol not in self.symbols:
            return [Decision.reject("symbol_outside_universe", metrics={"symbol": bar.symbol})]
        if not self.clock.in_regular_session(bar.start):
            return [Decision.reject("outside_regular_session", metrics={"symbol": bar.symbol})]
        st = self.symbols[bar.symbol]
        prev = st.series.bars[-1] if st.series.bars else None
        if not st.series.add(bar):
            return [Decision.reject("duplicate_or_out_of_order_bar", metrics={"symbol": bar.symbol})]
        i = len(st.series) - 1
        out: list[Decision] = []
        gap = prev is not None and (bar.start - prev.start) > timedelta(seconds=60)
        if gap:
            # No forward fill: a missing minute drops any setup in progress, and the
            # VWAP pullback needs a fresh trend bar after the gap before it re-arms.
            st.orb, st.vwap = None, None
            st.vwap_last_end = i
        self._track_open(bar)
        pend = self.pending.get(bar.symbol)
        if pend and bar.close <= d(pend["stop_price"]):
            out.append(self._cancel_pending(bar.symbol, "entry_invalidated_close_at_stop"))
        regime = regime_at(st.series, i, self.params)
        st.last_regime = regime
        orb_sig, orb_evt = step_orb(st, i, self.params, regime)
        vwap_sig, vwap_evt = step_vwap(st, i, self.params, regime)
        st.last_event = f"{orb_evt},{vwap_evt}"
        signal = orb_sig or vwap_sig  # ORB has priority when both fire on one bar
        if signal is None:
            return out
        if orb_sig and vwap_sig:
            signal["also_vwap_signal"] = True
        out.append(self.admit(signal, bar))
        return out

    def _reject(self, symbol: str, reason: str, **metrics: Any) -> Decision:
        self.last_reasons[symbol] = reason
        return Decision.reject(reason, metrics=plain({"symbol": symbol, **metrics}))

    def admit(self, signal: dict, bar: DTBar) -> Decision:
        """Apply data, clock, caps, cooldowns, stop band, cost and sizing rules to a setup signal."""
        p, c, sym = self.params, self.counters, signal["symbol"]
        now = bar.end
        if c.halted_reason:
            return self._reject(sym, c.halted_reason)
        if not bar_is_fresh(bar, p.max_bar_receipt_lag_seconds):
            return self._reject(sym, "stale_bar_blocks_entry")
        if not self.clock.entries_allowed(now):
            return self._reject(sym, "after_entry_cutoff")
        if sym in SPY_ALIGNED:
            ok, why = spy_aligned(self.symbols["SPY"].series, bar.start, p)
            if not ok:
                return self._reject(sym, why)
        if sym in c.disabled_symbols:
            return self._reject(sym, "symbol_disabled_after_losses")
        if c.in_cooldown(sym, now):
            return self._reject(sym, "symbol_cooldown")
        if sym in self.positions or sym in self.pending:
            return self._reject(sym, "symbol_has_active_campaign")
        if c.campaigns_by_symbol.get(sym, 0) >= p.max_campaigns_per_symbol:
            return self._reject(sym, "symbol_campaign_cap")
        if signal["kind"] == "orb_retest" and c.orb_fills_by_symbol.get(sym, 0) >= p.max_orb_fills_per_symbol:
            return self._reject(sym, "orb_already_filled_today")
        if c.campaigns_total >= p.max_campaigns_per_day:
            return self._reject(sym, "daily_campaign_cap")
        if len(self.positions) + len(self.pending) >= self.caps.max_simultaneous:
            return self._reject(sym, "simultaneous_campaign_cap")
        price, stop, a0 = signal["entry_limit"], signal["stop_price"], signal["atr0"]
        distance = price - stop
        if not (p.stop_min_atr * a0 <= distance <= p.stop_max_atr * a0):
            return self._reject(sym, "stop_distance_outside_atr_band", distance=distance, atr0=a0)
        round_trip = Decimal(2) * p.cost_bps_per_side / Decimal(10000) * price
        if round_trip > p.max_round_trip_cost_fraction_of_stop * distance:
            return self._reject(sym, "round_trip_cost_too_large_for_stop")
        cost = cost_allowance(price, self.caps.cost_floor)
        committed = sum(d(x["risk"]) for x in [*self.positions.values(), *self.pending.values()])
        notional = sum(d(x["notional"]) for x in [*self.positions.values(), *self.pending.values()])
        loss_left = self.caps.daily_loss + d(c.realized_pnl)
        qty = size_shares(price=price, stop=stop, cost=cost,
                          risk_left=min(self.caps.per_campaign_risk, loss_left),
                          portfolio_risk_left=self.caps.committed_risk - committed,
                          symbol_notional_left=self.caps.symbol_notional,
                          gross_notional_left=self.caps.gross_notional - notional,
                          max_shares=self.caps.max_shares)
        if qty < 1:
            return self._reject(sym, "zero_quantity_under_limits")
        target = price + p.target_r * (price - stop)
        setup_id = sha256(f"{PROFILE_ID}|{p.config_id}|{sym}|{signal['kind']}|{bar.start.isoformat()}".encode()).hexdigest()[:24]
        intent = plain({
            "symbol": sym, "side": "buy", "mode": "paper", "profile": PROFILE_ID, "config_id": p.config_id,
            "setup_class": signal["kind"], "reason": signal["kind"], "setup_id": setup_id,
            "entry_limit": price, "limit_price": price, "stop_price": stop, "target_price": target,
            "qty": qty, "order_type": "limit", "time_in_force": "day", "extended_hours": False,
            "limit_ttl_seconds": p.limit_ttl_seconds, "observed_at": now, "atr0": a0,
            "cost_allowance_per_share": cost, "planned_risk": (price - stop + cost) * qty,
            "liquidity_volume": max(1, bar.volume), "rvol_log_only": True, "paper_only": True,
        })
        self.pending[sym] = {"stop_price": stop, "entry_limit": price, "qty": qty, "kind": signal["kind"],
                             "expires_at": now + timedelta(seconds=p.limit_ttl_seconds),
                             "risk": (price - stop + cost) * qty, "notional": price * qty}
        self.last_reasons[sym] = "entry_intent"
        return Decision(True, (signal["kind"],), intent, plain({"caps": self.caps.public()}), profile=PROFILE_ID)

    # ---- pending orders ---------------------------------------------------------
    def _cancel_pending(self, symbol: str, reason: str) -> Decision:
        self.pending.pop(symbol, None)
        return Decision(True, ("cancel_entry", reason), {"symbol": symbol, "action": "cancel_entry", "reason": reason},
                        profile=PROFILE_ID)

    def pending_actions(self, now: datetime) -> list[Decision]:
        """Cancel unfilled entries after 60 s, at the entry cutoff, or when the day is halted."""
        out = []
        for sym in list(self.pending):
            pend = self.pending[sym]
            if self.counters.halted_reason:
                out.append(self._cancel_pending(sym, self.counters.halted_reason))
            elif now >= pend["expires_at"]:
                out.append(self._cancel_pending(sym, "limit_ttl_expired"))
            elif self.clock is not None and not self.clock.entries_allowed(now):
                out.append(self._cancel_pending(sym, "entry_window_closed"))
        return out

    def on_entry_filled(self, symbol: str, qty: int, price: Decimal | str, at: datetime) -> None:
        """Record a (possibly partial) fill. Protective exits always cover actual filled shares."""
        price = d(price)
        pend = self.pending.get(symbol)
        pos = self.positions.get(symbol)
        if pos is None:
            if pend is None:
                raise ValueError("fill without a pending entry")
            stop = d(pend["stop_price"])
            pos = {"qty": 0, "entry": ZERO, "stop": stop, "first_fill_at": at, "max_high": price,
                   "kind": pend["kind"], "session_date": local(at).date().isoformat()}
            self.positions[symbol] = pos
            c = self.counters
            c.campaigns_total += 1
            c.campaigns_by_symbol[symbol] = c.campaigns_by_symbol.get(symbol, 0) + 1
            if pend["kind"] == "orb_retest":
                c.orb_fills_by_symbol[symbol] = c.orb_fills_by_symbol.get(symbol, 0) + 1
        total = pos["qty"] + int(qty)
        pos["entry"] = (pos["entry"] * pos["qty"] + price * int(qty)) / Decimal(total)
        pos["qty"] = total
        r = pos["entry"] - pos["stop"]
        pos["r"] = r
        pos["target"] = pos["entry"] + self.params.target_r * r
        pos["risk"] = max(ZERO, r) * total
        pos["notional"] = pos["entry"] * total
        pos["max_high"] = max(pos["max_high"], price)
        if pend is not None:
            pend["qty"] = int(pend["qty"]) - int(qty)
            if pend["qty"] <= 0:
                self.pending.pop(symbol, None)

    def _track_open(self, bar: DTBar) -> None:
        pos = self.positions.get(bar.symbol)
        if pos is not None and bar.end > pos["first_fill_at"]:
            pos["max_high"] = max(pos["max_high"], bar.high)

    # ---- exits ------------------------------------------------------------------
    def manage(self, now: datetime, marks: Mapping[str, Any]) -> list[Decision]:
        """Exit decisions for open positions. Daily loss and session flatten override everything."""
        out: list[Decision] = []
        unreal = ZERO
        for sym, pos in self.positions.items():
            mark = d(marks.get(sym, pos["entry"]))
            pos["max_high"] = max(pos["max_high"], mark)
            unreal += (mark - pos["entry"]) * pos["qty"]
        net = d(self.counters.realized_pnl) + unreal
        if net <= -self.caps.daily_loss and not self.counters.halted_reason:
            self.counters.halted_reason = "daily_loss_limit"
        out.extend(self.pending_actions(now))
        today = local(now).date().isoformat()
        for sym, pos in list(self.positions.items()):
            mark = d(marks.get(sym, pos["entry"]))
            held = (now - pos["first_fill_at"]).total_seconds()
            reason = None
            if self.counters.halted_reason:
                reason = self.counters.halted_reason
            elif pos["session_date"] != today:
                reason = "prior_day_liquidation"
            elif self.clock is not None and self.clock.must_flatten(now):
                reason = "session_flatten"
            elif mark <= pos["stop"]:
                reason = "stop"
            elif mark >= pos["target"]:
                reason = "target_2r"
            elif held >= self.params.max_hold_seconds:
                reason = "max_hold_45m"
            elif (held >= self.params.no_progress_seconds
                  and pos["max_high"] < pos["entry"] + self.params.no_progress_r * pos["r"]):
                reason = "no_progress_10m"
            if reason:
                out.append(Decision(True, ("exit", reason), plain({
                    "symbol": sym, "side": "sell", "action": "exit", "qty": pos["qty"], "reason": reason,
                    "mode": "paper", "profile": PROFILE_ID}), profile=PROFILE_ID))
        return out

    def on_exit_filled(self, symbol: str, price: Decimal | str, at: datetime) -> Decimal:
        pos = self.positions.pop(symbol)
        pnl = (d(price) - pos["entry"]) * pos["qty"]
        c = self.counters
        c.realized_pnl = str(d(c.realized_pnl) + pnl)
        if pnl < 0:
            c.losses_by_symbol[symbol] = c.losses_by_symbol.get(symbol, 0) + 1
            c.consecutive_losses += 1
            wait = self.params.cooldown_after_loss_seconds
            if c.losses_by_symbol[symbol] >= self.params.disable_symbol_after_losses and symbol not in c.disabled_symbols:
                c.disabled_symbols.append(symbol)
        else:
            c.consecutive_losses = 0
            wait = self.params.cooldown_after_exit_seconds
        c.cooldown_until[symbol] = (at + timedelta(seconds=wait)).isoformat()
        if d(c.realized_pnl) <= -self.caps.daily_loss:
            c.halted_reason = "daily_loss_limit"
        return pnl

    def diagnostics(self) -> dict:
        return plain({
            "profile": PROFILE_ID, "config_id": self.params.config_id, "caps": self.caps.public(),
            "session": None if self.clock is None else {
                "date": self.clock.session_date, "close": self.clock.close.isoformat(),
                "early_close": self.clock.early_close, "entry_cutoff": self.clock.entry_cutoff.isoformat(),
                "flatten_at": self.clock.flatten_at.isoformat()},
            "counters": self.counters.to_state(),
            "symbols": {k: {"bars": len(v.series), "regime": v.last_regime, "orh": v.orh,
                            "orb_active": v.orb is not None, "vwap_active": v.vwap is not None,
                            "last_event": v.last_event, "last_reason": self.last_reasons.get(k)}
                        for k, v in self.symbols.items()},
            "open_positions": sorted(self.positions), "pending_entries": sorted(self.pending),
        })


def _dt(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _load_position(raw: Mapping[str, Any]) -> dict:
    pos = dict(raw)
    for k in ("entry", "stop", "max_high", "r", "target", "risk", "notional"):
        if k in pos:
            pos[k] = d(pos[k])
    pos["qty"] = int(pos["qty"])
    pos["first_fill_at"] = _dt(pos["first_fill_at"])
    return pos


def _load_pending(raw: Mapping[str, Any]) -> dict:
    pend = dict(raw)
    for k in ("stop_price", "entry_limit", "risk", "notional"):
        pend[k] = d(pend[k])
    pend["qty"] = int(pend["qty"])
    pend["expires_at"] = _dt(pend["expires_at"])
    return pend
