"""``ross_cameron_small_cap_v1``: Ross Cameron-style small-cap momentum (paper only, OFF and hard-gated).

Source: Ross Cameron public material as summarised in the ChatGPT chat "Day
Trading Strategies Comparison" (docs/handbook, research note). Rules:

* Five-factor scanner gate: price $2-$20, float under 20M shares, relative
  volume >= 5x, up >= 10% on the day, and a fresh news catalyst. Being a top-5
  gainer is preferred and logged, not required.
* Gap and Go (09:30-10:00 ET, gap > 4%): enter on a break of the premarket high
  or a 1-minute opening-range breakout (stop at the first candle's low).
* Bull flag (09:30-11:30 ET): strong green impulse, 2-3 red pullback candles
  (4+ rejected), retrace <= 50%, pullback holds VWAP (9-EMA break is a logged
  warning); enter over the last red candle's high, stop at the pullback low.
* About 2:1: sell half at +2R and move the stop to entry; the first red 1-minute
  candle close exits; stop for the day after 3 consecutive losing trades.
* Our global caps always win: $25 risk per trade, $75 daily loss, $1,000
  notional and one open stock position (stricter of Ross and RiskConfig).

HARD GATE: free IEX data has no share float, no news catalysts and no
full-market gainers scan. This module defines provider interfaces only and
ships with NO paid implementation. Without all three providers (and a SIP /
full-tape feed) the profile refuses to trade and reports why.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .day_trade_core import (DTBar, DayCounters, SessionClock, SessionSeries, ZERO, bar_is_fresh,
                             cents_down, cents_up, cost_allowance, d, local, size_shares)
from .models import Decision, config_fingerprint, plain

PROFILE_ID = "ross_cameron_small_cap_v1"
GLOBAL_MAX_OPEN_STOCK_POSITIONS = 1


# ---- data providers (interfaces only; no paid implementation ships) -----------
@dataclass(frozen=True)
class Catalyst:
    symbol: str
    headline: str
    published_at: datetime
    source: str


@dataclass(frozen=True)
class ScannerRow:
    """One full-market gainers row. ``rvol`` must come from full consolidated volume."""
    symbol: str
    last: Decimal
    prior_close: Decimal
    pct_change: Decimal
    rvol: Decimal
    premarket_high: Decimal | None = None
    rank: int | None = None


@runtime_checkable
class FloatProvider(Protocol):
    name: str

    def float_shares(self, symbol: str, as_of: datetime) -> int | None: ...


@runtime_checkable
class CatalystProvider(Protocol):
    name: str

    def latest_catalyst(self, symbol: str, as_of: datetime) -> Catalyst | None: ...


@runtime_checkable
class GainersScanner(Protocol):
    name: str

    def scan(self, as_of: datetime) -> Sequence[ScannerRow]: ...


@dataclass(frozen=True)
class ProviderSet:
    float_provider: FloatProvider | None = None
    catalyst_provider: CatalystProvider | None = None
    gainers_scanner: GainersScanner | None = None


def provider_blockers(providers: ProviderSet | None, data_feed: str) -> list[str]:
    """Every reason the profile must refuse to trade. Empty only when all data exists."""
    providers = providers or ProviderSet()
    reasons = []
    if not isinstance(providers.float_provider, FloatProvider):
        reasons.append("float_provider_missing")
    if not isinstance(providers.catalyst_provider, CatalystProvider):
        reasons.append("catalyst_provider_missing")
    if not isinstance(providers.gainers_scanner, GainersScanner):
        reasons.append("gainers_scanner_missing")
    if data_feed != "sip":
        reasons.append("requires_sip_or_full_tape_feed")
    return reasons


# ---- parameters ----------------------------------------------------------------
@dataclass(frozen=True)
class RossV1Params:
    # Five-factor scanner gate
    min_price: Decimal = Decimal("2")
    max_price: Decimal = Decimal("20")
    max_float_exclusive: int = 20_000_000
    min_rvol: Decimal = Decimal("5")
    min_gain_pct: Decimal = Decimal("10")
    catalyst_max_age_hours: int = 24
    preferred_top_gainers: int = 5            # logged only
    # Gap and Go
    min_gap_pct: Decimal = Decimal("4")
    gap_and_go_start: str = "09:30"
    gap_and_go_end: str = "10:00"
    # Bull flag
    bull_flag_start: str = "09:30"
    bull_flag_end: str = "11:30"
    min_impulse_green_bars: int = 2
    min_impulse_pct: Decimal = Decimal("2")
    min_red_pullback_bars: int = 2
    max_red_pullback_bars: int = 3            # 4-5 red candles are "concerning": rejected
    max_retrace: Decimal = Decimal("0.50")
    ema_period: int = 9                       # break is a logged warning only
    # Orders and stops
    tick: Decimal = Decimal("0.01")
    max_stop_per_share: Decimal = Decimal("0.20")   # Ross example: limit wide stops to $0.20
    max_chase_r: Decimal = Decimal("0.5")
    limit_ttl_seconds: int = 60
    # Exits
    first_target_r: Decimal = Decimal("2")
    partial_fraction: Decimal = Decimal("0.5")
    move_stop_to_entry_after_partial: bool = True
    first_red_candle_exit: bool = True
    # Risk (Ross's own numbers are clamped by the global RiskConfig)
    per_trade_risk: Decimal = Decimal("25")
    daily_max_loss: Decimal = Decimal("75")
    max_consecutive_losses: int = 3
    max_trades_per_day: int = 5               # BOT safety cap; Ross: 3-5 trades/day target
    max_bar_receipt_lag_seconds: int = 10
    regular_session_only: bool = True

    def __post_init__(self) -> None:
        if not self.regular_session_only:
            raise ValueError("ross_cameron_small_cap_v1 is regular-session only in paper")
        if self.min_price <= 0 or self.max_price <= self.min_price:
            raise ValueError("invalid price band")

    @property
    def config_id(self) -> str:
        return config_fingerprint(self)


def _t(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class RossCaps:
    per_trade_risk: Decimal
    daily_loss: Decimal
    max_notional: Decimal
    max_simultaneous: int
    max_shares: int
    cost_floor: Decimal

    def public(self) -> dict:
        return plain(asdict(self))


def ross_caps(params: RossV1Params, risk_config: Any) -> RossCaps:
    return RossCaps(
        per_trade_risk=min(params.per_trade_risk, d(risk_config.per_trade_risk)),
        daily_loss=min(params.daily_max_loss, d(risk_config.max_daily_loss)),
        max_notional=d(risk_config.max_notional),
        max_simultaneous=GLOBAL_MAX_OPEN_STOCK_POSITIONS,
        max_shares=int(risk_config.max_shares),
        cost_floor=max(Decimal("0.02"), d(risk_config.execution_allowance_per_share)),
    )


# ---- scanner gate --------------------------------------------------------------
def five_factor_gate(row: ScannerRow, float_shares: int | None, catalyst: Catalyst | None,
                     as_of: datetime, params: RossV1Params) -> tuple[bool, list[str], dict]:
    reasons = []
    if not (params.min_price <= row.last <= params.max_price):
        reasons.append("price_outside_2_20")
    if float_shares is None:
        reasons.append("float_unknown")
    elif float_shares >= params.max_float_exclusive:
        reasons.append("float_not_under_20m")
    if row.rvol < params.min_rvol:
        reasons.append("rvol_below_5x")
    if row.pct_change < params.min_gain_pct:
        reasons.append("not_up_10pct")
    if catalyst is None:
        reasons.append("no_news_catalyst")
    elif not (timedelta(0) <= as_of - catalyst.published_at <= timedelta(hours=params.catalyst_max_age_hours)):
        reasons.append("catalyst_not_fresh")
    info = {"top_gainer_preferred": row.rank is not None and row.rank <= params.preferred_top_gainers,
            "rank": row.rank, "float_shares": float_shares,
            "catalyst": None if catalyst is None else {"headline": catalyst.headline, "source": catalyst.source}}
    return not reasons, reasons, info


# ---- pattern detectors (pure) --------------------------------------------------
def gap_pct(row: ScannerRow, open_price: Decimal) -> Decimal:
    return (open_price - row.prior_close) / row.prior_close * Decimal(100)


def detect_gap_and_go(series: SessionSeries, i: int, row: ScannerRow, params: RossV1Params) -> dict | None:
    """Break of the premarket high, or a 1-minute opening-range breakout (stop at first candle's low)."""
    bars = series.bars
    if not bars or local(bars[0].start).time() != time(9, 30):
        return None
    t_end = local(bars[i].end).time()
    if not (_t(params.gap_and_go_start) < t_end <= _t(params.gap_and_go_end)):
        return None
    if gap_pct(row, bars[0].open) <= params.min_gap_pct:
        return None
    bar = bars[i]
    prev_close = bars[i - 1].close if i > 0 else row.prior_close
    pmh = row.premarket_high
    if pmh is not None and bar.close > pmh and prev_close <= pmh:
        return {"kind": "gap_and_go_pmh_break", "trigger": pmh + params.tick, "stop_low": bar.low}
    if i >= 1:
        first = bars[0]
        if bar.close > first.high and bars[i - 1].close <= first.high:
            return {"kind": "gap_and_go_1m_orb", "trigger": first.high + params.tick, "stop_low": first.low}
    return None


def detect_bull_flag(series: SessionSeries, i: int, params: RossV1Params) -> tuple[dict | None, str]:
    """Bar ``i`` breaks the last red pullback candle's high after a strong impulse."""
    bars = series.bars
    t_end = local(bars[i].end).time()
    if not (_t(params.bull_flag_start) < t_end <= _t(params.bull_flag_end)):
        return None, "bull_flag_outside_window"
    red = 0
    k = i - 1
    while k >= 0 and bars[k].red:
        red += 1
        k -= 1
    if red < params.min_red_pullback_bars:
        return None, "bull_flag_no_pullback"
    if red > params.max_red_pullback_bars:
        return None, "bull_flag_too_many_red_candles"
    green_end = k
    green = 0
    while k >= 0 and bars[k].green:
        green += 1
        k -= 1
    if green < params.min_impulse_green_bars:
        return None, "bull_flag_no_impulse"
    impulse = bars[green_end - green + 1:green_end + 1]
    imp_low = min(b.low for b in impulse)
    imp_high = max(b.high for b in impulse)
    if imp_low <= 0 or (imp_high - imp_low) / imp_low * 100 < params.min_impulse_pct:
        return None, "bull_flag_impulse_too_small"
    pullback = bars[green_end + 1:i]
    pb_low = min(b.low for b in pullback)
    retrace = (imp_high - pb_low) / (imp_high - imp_low)
    if retrace > params.max_retrace:
        return None, "bull_flag_retrace_over_50pct"
    if any(b.low < series.vwaps[green_end + 1 + n] for n, b in enumerate(pullback)):
        return None, "bull_flag_broke_vwap"
    trigger_high = pullback[-1].high
    bar = bars[i]
    if not (bar.high > trigger_high and bar.close > trigger_high):
        return None, "bull_flag_waiting_break"
    ema = series.ema(params.ema_period, i - 1)
    return {"kind": "bull_flag", "trigger": trigger_high + params.tick, "stop_low": pb_low,
            "retrace": retrace, "red_candles": red,
            "ema9_break_warning": ema is not None and pb_low < ema}, "bull_flag_break"


# ---- engine --------------------------------------------------------------------
class RossCameronSmallCapV1:
    """Stateful engine. Refuses every entry while any data provider is missing."""

    profile_id = PROFILE_ID

    def __init__(self, risk_config: Any, providers: ProviderSet | None = None, *, data_feed: str = "iex",
                 params: RossV1Params | None = None, state: Mapping[str, Any] | None = None) -> None:
        self.params = params or RossV1Params()
        self.providers = providers or ProviderSet()
        self.blockers = provider_blockers(self.providers, data_feed)
        self.caps = ross_caps(self.params, risk_config)
        self.counters = DayCounters.from_state((state or {}).get("counters"))
        self.positions: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self.series: dict[str, SessionSeries] = {}
        self.candidates: dict[str, ScannerRow] = {}
        self.scan_log: dict[str, dict] = {}
        self.clock: SessionClock | None = None

    @property
    def can_trade(self) -> bool:
        return not self.blockers

    def start_session(self, day: date, close: time | None = None) -> None:
        self.clock = SessionClock.for_day(day, close)
        self.counters.roll(day)
        self.series, self.candidates, self.scan_log = {}, {}, {}

    def refresh_candidates(self, as_of: datetime) -> dict[str, dict]:
        """Run the five-factor gate over the full-market gainers scan."""
        if self.blockers:
            return {}
        p = self.providers
        for row in p.gainers_scanner.scan(as_of):
            ok, reasons, info = five_factor_gate(
                row, p.float_provider.float_shares(row.symbol, as_of),
                p.catalyst_provider.latest_catalyst(row.symbol, as_of), as_of, self.params)
            self.scan_log[row.symbol] = plain({"passed": ok, "reasons": reasons, **info})
            if ok:
                self.candidates[row.symbol] = row
            else:
                self.candidates.pop(row.symbol, None)
        return dict(self.scan_log)

    def _reject(self, reason: str, **metrics: Any) -> Decision:
        return Decision(False, (reason,), metrics=plain(metrics), profile=PROFILE_ID)

    def on_bar(self, bar: DTBar) -> list[Decision]:
        if self.blockers:
            return [self._reject("profile_blocked", blockers=self.blockers)]
        if self.clock is None:
            raise RuntimeError("start_session first")
        if not self.clock.in_regular_session(bar.start):
            return [self._reject("outside_regular_session", symbol=bar.symbol)]
        series = self.series.setdefault(bar.symbol, SessionSeries(bar.symbol, self.clock.session_date))
        if not series.add(bar):
            return [self._reject("duplicate_or_out_of_order_bar", symbol=bar.symbol)]
        i = len(series) - 1
        out = self._manage_bar(bar)
        if out:
            return out
        row = self.candidates.get(bar.symbol)
        if row is None:
            return [self._reject("not_a_five_factor_candidate", symbol=bar.symbol)]
        signal = detect_gap_and_go(series, i, row, self.params)
        why = "gap_and_go_none"
        if signal is None:
            signal, why = detect_bull_flag(series, i, self.params)
        if signal is None:
            return [self._reject(why, symbol=bar.symbol)]
        return [self.admit(signal, bar)]

    def admit(self, signal: dict, bar: DTBar) -> Decision:
        p, c, sym = self.params, self.counters, bar.symbol
        now = bar.end
        if c.halted_reason:
            return self._reject(c.halted_reason, symbol=sym)
        if not bar_is_fresh(bar, p.max_bar_receipt_lag_seconds):
            return self._reject("stale_bar_blocks_entry", symbol=sym)
        if not self.clock.entries_allowed(now):
            return self._reject("after_entry_cutoff", symbol=sym)
        if c.campaigns_total >= p.max_trades_per_day:
            return self._reject("daily_trade_cap", symbol=sym)
        if sym in self.positions or sym in self.pending or \
                len(self.positions) + len(self.pending) >= self.caps.max_simultaneous:
            return self._reject("one_position_at_a_time", symbol=sym)
        trigger = signal["trigger"]
        stop = cents_down(signal["stop_low"] - p.tick)
        entry = cents_up(max(trigger, bar.close))
        risk_ps = trigger - stop
        if risk_ps <= 0:
            return self._reject("invalid_stop", symbol=sym)
        if risk_ps > p.max_stop_per_share:
            return self._reject("stop_wider_than_max", symbol=sym, risk_per_share=risk_ps)
        if entry - trigger > p.max_chase_r * risk_ps:
            return self._reject("entry_extended_past_trigger", symbol=sym)
        if not (p.min_price <= entry <= p.max_price):
            return self._reject("price_outside_2_20", symbol=sym)
        cost = cost_allowance(entry, self.caps.cost_floor)
        loss_left = self.caps.daily_loss + d(c.realized_pnl)
        qty = size_shares(price=entry, stop=stop, cost=cost, risk_left=min(self.caps.per_trade_risk, loss_left),
                          portfolio_risk_left=self.caps.per_trade_risk, symbol_notional_left=self.caps.max_notional,
                          gross_notional_left=self.caps.max_notional, max_shares=self.caps.max_shares)
        if qty < 1:
            return self._reject("zero_quantity_under_limits", symbol=sym)
        r = entry - stop
        target = entry + p.first_target_r * r
        setup_id = sha256(f"{PROFILE_ID}|{p.config_id}|{sym}|{signal['kind']}|{bar.start.isoformat()}".encode()).hexdigest()[:24]
        intent = plain({
            "symbol": sym, "side": "buy", "mode": "paper", "profile": PROFILE_ID, "config_id": p.config_id,
            "setup_class": signal["kind"], "reason": signal["kind"], "setup_id": setup_id,
            "entry_limit": entry, "limit_price": entry, "stop_price": stop, "target_price": target,
            "qty": qty, "order_type": "limit", "time_in_force": "day", "extended_hours": False,
            "limit_ttl_seconds": p.limit_ttl_seconds, "observed_at": now, "paper_only": True,
            "partial_at_target_fraction": p.partial_fraction, "ema9_break_warning": signal.get("ema9_break_warning", False),
        })
        self.pending[sym] = {"stop": stop, "entry": entry, "qty": qty, "expires_at": now + timedelta(seconds=p.limit_ttl_seconds)}
        return Decision(True, (signal["kind"],), intent, plain({"caps": self.caps.public()}), profile=PROFILE_ID)

    def pending_actions(self, now: datetime) -> list[Decision]:
        out = []
        for sym in list(self.pending):
            if self.counters.halted_reason or now >= self.pending[sym]["expires_at"] or \
                    (self.clock is not None and not self.clock.entries_allowed(now)):
                self.pending.pop(sym)
                out.append(Decision(True, ("cancel_entry",), {"symbol": sym, "action": "cancel_entry"}, profile=PROFILE_ID))
        return out

    def on_entry_filled(self, symbol: str, qty: int, price: Decimal | str, at: datetime) -> None:
        pend = self.pending.pop(symbol)
        entry = d(price)
        r = entry - pend["stop"]
        self.positions[symbol] = {"qty": int(qty), "entry": entry, "stop": pend["stop"], "r": r,
                                  "target": entry + self.params.first_target_r * r, "partial_done": False,
                                  "realized": ZERO, "opened_at": at}
        self.counters.campaigns_total += 1
        self.counters.campaigns_by_symbol[symbol] = self.counters.campaigns_by_symbol.get(symbol, 0) + 1

    def _exit(self, sym: str, qty: int, reason: str) -> Decision:
        return Decision(True, ("exit", reason), plain({"symbol": sym, "side": "sell", "action": "exit", "qty": qty,
                                                       "reason": reason, "mode": "paper", "profile": PROFILE_ID}),
                        profile=PROFILE_ID)

    def _manage_bar(self, bar: DTBar) -> list[Decision]:
        """Candle-based exits: stop, half at first target (+2R) then stop to entry, first red candle."""
        pos = self.positions.get(bar.symbol)
        if pos is None:
            return []
        p = self.params
        if bar.low <= pos["stop"]:
            return [self._exit(bar.symbol, pos["qty"], "stop" if not pos["partial_done"] else "breakeven_stop")]
        if not pos["partial_done"] and bar.high >= pos["target"]:
            half = int(pos["qty"] * p.partial_fraction)
            if half >= 1:
                return [self._exit(bar.symbol, half, "first_target_half")]
            return [self._exit(bar.symbol, pos["qty"], "first_target_full")]
        if p.first_red_candle_exit and bar.red:
            return [self._exit(bar.symbol, pos["qty"], "first_red_candle")]
        return []

    def manage(self, now: datetime, marks: Mapping[str, Any]) -> list[Decision]:
        out = self.pending_actions(now)
        unreal = sum(((d(marks.get(s, x["entry"])) - x["entry"]) * x["qty"] for s, x in self.positions.items()), ZERO)
        if d(self.counters.realized_pnl) + unreal <= -self.caps.daily_loss and not self.counters.halted_reason:
            self.counters.halted_reason = "daily_loss_limit"
        for sym, pos in self.positions.items():
            if self.counters.halted_reason:
                out.append(self._exit(sym, pos["qty"], self.counters.halted_reason))
            elif self.clock is not None and self.clock.must_flatten(now):
                out.append(self._exit(sym, pos["qty"], "session_flatten"))
        return out

    def on_exit_filled(self, symbol: str, qty: int, price: Decimal | str, at: datetime) -> Decimal | None:
        """Record a sell fill. Returns the trade's P&L once the position is fully closed."""
        pos = self.positions[symbol]
        qty = int(qty)
        pnl = (d(price) - pos["entry"]) * qty
        pos["realized"] += pnl
        pos["qty"] -= qty
        c = self.counters
        c.realized_pnl = str(d(c.realized_pnl) + pnl)
        if pos["qty"] > 0:
            pos["partial_done"] = True
            if self.params.move_stop_to_entry_after_partial:
                pos["stop"] = pos["entry"]
            return None
        self.positions.pop(symbol)
        total = pos["realized"]
        if total < 0:
            c.consecutive_losses += 1
            c.losses_by_symbol[symbol] = c.losses_by_symbol.get(symbol, 0) + 1
        else:
            c.consecutive_losses = 0
        if c.consecutive_losses >= self.params.max_consecutive_losses:
            c.halted_reason = "three_consecutive_losses"
        if d(c.realized_pnl) <= -self.caps.daily_loss:
            c.halted_reason = "daily_loss_limit"
        return total

    def diagnostics(self) -> dict:
        return plain({"profile": PROFILE_ID, "config_id": self.params.config_id, "can_trade": self.can_trade,
                      "blockers": list(self.blockers), "caps": self.caps.public(),
                      "candidates": sorted(self.candidates), "scan": self.scan_log,
                      "counters": self.counters.to_state(), "open_positions": sorted(self.positions)})
