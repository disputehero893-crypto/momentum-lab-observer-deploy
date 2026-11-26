"""``small_cap_candle_patterns_iex_v1``: small-cap momentum on FREE data (paper only, OFF by default).

Derek (2026-11-26): no $99/mo SIP feed; "see what we can do with what we have and
by watching candles for patterns". This profile keeps Ross Cameron's stock
selection and risk rules but swaps the parts that need the full tape:

* Scanner gate: price $2-$20, float under 20M (FMP Starter), up >= 10% on the
  day, and an IEX-relative volume check instead of an absolute 5x RVOL. The
  movers scanner's RVOL is already today's IEX volume / the symbol's own IEX
  daily average; this profile divides it by the share of a normal day that has
  traded so far (``cumulative_volume_fraction``) and asks for >= 3x pace.
* News catalyst when available: a fresh Alpaca news headline is logged and
  preferred; with ``catalyst_policy="require"`` it becomes mandatory.
* Entry needs a candle pattern from ``candle_patterns`` (bull flag, first
  pullback, micro pullback, 5-minute opening-range breakout, VWAP reclaim,
  higher-low breakout) on 1-minute or 5-minute bars, a candle confirmation and
  an in-session volume surge (bar vs the symbol's own recent IEX bars).
* Stop one tick under the pattern low; sell half at 2R and move the stop to
  entry; the runner exits on a close under the 9-EMA; flat by the session cut.
  Three losing trades in a row ends the day. Global caps always win ($25 risk
  per trade, $75 daily loss, $1,000 notional, one open stock position).

IEX limits are reported, not hidden: thin IEX minutes are skipped (bar coverage
gate) and the scanner cannot see premarket gappers before 09:30 ET.

Allowed with ``ALPACA_DATA_FEED=iex`` (and ``sip``). Nothing calls this engine
yet; the paper loop wiring comes after PR #57, like the other day-trade profiles.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any, Mapping

from . import candle_patterns as cp
from .day_trade_core import (DTBar, DayCounters, SessionClock, SessionSeries, ZERO, bar_is_fresh,
                             cents_down, cents_up, cost_allowance, d, local, size_shares)
from .models import Decision, config_fingerprint, plain
from .ross_cameron_small_cap_v1 import (Catalyst, CatalystProvider, FloatProvider, GainersScanner, ProviderSet,
                                        ScannerRow)

PROFILE_ID = "small_cap_candle_patterns_iex_v1"
GLOBAL_MAX_OPEN_STOCK_POSITIONS = 1
ALLOWED_FEEDS = ("iex", "sip")


def provider_blockers(providers: ProviderSet | None, data_feed: str) -> list[str]:
    """Float and a gainers scanner are required; news is optional (catalyst when available)."""
    providers = providers or ProviderSet()
    reasons = []
    if not isinstance(providers.float_provider, FloatProvider):
        reasons.append("float_provider_missing")
    if not isinstance(providers.gainers_scanner, GainersScanner):
        reasons.append("gainers_scanner_missing")
    if data_feed not in ALLOWED_FEEDS:
        reasons.append("data_feed_must_be_iex_or_sip")
    return reasons


@dataclass(frozen=True)
class CandleIexV1Params:
    # Scanner gate (Ross's five factors, RVOL made IEX-relative)
    min_price: Decimal = Decimal("2")
    max_price: Decimal = Decimal("20")
    max_float_exclusive: int = 20_000_000
    min_gain_pct: Decimal = Decimal("10")
    min_iex_pace_rvol: Decimal = Decimal("3")
    catalyst_policy: str = "prefer"            # "prefer" (log only) or "require"
    catalyst_max_age_hours: int = 24
    # Patterns
    timeframes: tuple[str, ...] = ("1m", "5m")
    enabled_patterns: tuple[str, ...] = cp.PATTERN_KINDS
    one_minute: cp.PatternParams = cp.ONE_MINUTE
    five_minute: cp.PatternParams = cp.FIVE_MINUTE
    entry_start: str = "09:35"
    entry_end: str = "11:30"                   # morning momentum only; midday chop excluded
    coverage_window_minutes: int = 10          # IEX gap guard: share of recent minutes with a bar
    min_bar_coverage: Decimal = Decimal("0.6")
    # Orders and stops
    tick: Decimal = Decimal("0.01")
    max_stop_pct: Decimal = Decimal("4")       # stop distance cap as % of entry (1-minute patterns)
    max_stop_pct_5m: Decimal = Decimal("8")    # 5-minute pattern lows sit further away; size shrinks to keep $ risk
    min_stop_per_share: Decimal = Decimal("0.03")
    max_chase_r: Decimal = Decimal("1.0")      # close-confirmed entries buy above the trigger; cap at 1R
    limit_ttl_seconds: int = 60
    # Exits
    first_target_r: Decimal = Decimal("2")
    partial_fraction: Decimal = Decimal("0.5")
    move_stop_to_entry_after_partial: bool = True
    runner_exit_ema_period: int = 9            # after the partial: exit on a close under this EMA
    # Risk (clamped by the global RiskConfig)
    per_trade_risk: Decimal = Decimal("25")
    daily_max_loss: Decimal = Decimal("75")
    max_consecutive_losses: int = 3
    max_trades_per_day: int = 5
    max_bar_receipt_lag_seconds: int = 10
    regular_session_only: bool = True

    def __post_init__(self) -> None:
        if not self.regular_session_only:
            raise ValueError(f"{PROFILE_ID} is regular-session only in paper")
        if self.min_price <= 0 or self.max_price <= self.min_price:
            raise ValueError("invalid price band")
        if self.catalyst_policy not in {"prefer", "require"}:
            raise ValueError("catalyst_policy must be prefer or require")
        if not self.timeframes or not set(self.timeframes) <= {"1m", "5m"}:
            raise ValueError("timeframes must be a subset of 1m, 5m")
        if not set(self.enabled_patterns) <= set(cp.PATTERN_KINDS):
            raise ValueError("unknown pattern")

    @property
    def config_id(self) -> str:
        return config_fingerprint(self)


def _t(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class CandleCaps:
    per_trade_risk: Decimal
    daily_loss: Decimal
    max_notional: Decimal
    max_simultaneous: int
    max_shares: int
    cost_floor: Decimal

    def public(self) -> dict:
        return plain(asdict(self))


def candle_caps(params: CandleIexV1Params, risk_config: Any) -> CandleCaps:
    return CandleCaps(
        per_trade_risk=min(params.per_trade_risk, d(risk_config.per_trade_risk)),
        daily_loss=min(params.daily_max_loss, d(risk_config.max_daily_loss)),
        max_notional=d(risk_config.max_notional),
        max_simultaneous=GLOBAL_MAX_OPEN_STOCK_POSITIONS,
        max_shares=int(risk_config.max_shares),
        cost_floor=max(Decimal("0.02"), d(risk_config.execution_allowance_per_share)),
    )


def candidate_gate(row: ScannerRow, float_shares: int | None, catalyst: Catalyst | None, as_of: datetime,
                   params: CandleIexV1Params) -> tuple[bool, list[str], dict]:
    """Price, float, gain, IEX pace RVOL and (policy-dependent) catalyst."""
    reasons = []
    if not (params.min_price <= row.last <= params.max_price):
        reasons.append("price_outside_2_20")
    if float_shares is None:
        reasons.append("float_unknown")
    elif float_shares >= params.max_float_exclusive:
        reasons.append("float_not_under_20m")
    if row.pct_change < params.min_gain_pct:
        reasons.append("not_up_10pct")
    pace = row.rvol / cp.cumulative_volume_fraction(as_of) if row.rvol is not None else ZERO
    if pace < params.min_iex_pace_rvol:
        reasons.append("iex_pace_rvol_below_3x")
    fresh = catalyst is not None and \
        timedelta(0) <= as_of - catalyst.published_at <= timedelta(hours=params.catalyst_max_age_hours)
    if params.catalyst_policy == "require" and not fresh:
        reasons.append("no_fresh_news_catalyst")
    info = {"rank": row.rank, "float_shares": float_shares, "iex_rvol_raw": row.rvol, "iex_pace_rvol": pace,
            "fresh_catalyst": fresh,
            "catalyst": None if catalyst is None else {"headline": catalyst.headline, "source": catalyst.source}}
    return not reasons, reasons, info


class SmallCapCandlePatternsIexV1:
    """Stateful engine. Feed it completed 1-minute bars (it builds 5-minute bars itself),
    or 5-minute bars directly with ``timeframe="5m"`` (offline replay)."""

    profile_id = PROFILE_ID

    def __init__(self, risk_config: Any, providers: ProviderSet | None = None, *, data_feed: str = "iex",
                 params: CandleIexV1Params | None = None, state: Mapping[str, Any] | None = None) -> None:
        self.params = params or CandleIexV1Params()
        self.providers = providers or ProviderSet()
        self.data_feed = data_feed
        self.blockers = provider_blockers(self.providers, data_feed)
        self.caps = candle_caps(self.params, risk_config)
        self.counters = DayCounters.from_state((state or {}).get("counters"))
        self.positions: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self.series: dict[tuple[str, str], SessionSeries] = {}
        self.candidates: dict[str, ScannerRow] = {}
        self.catalysts: dict[str, bool] = {}
        self.scan_log: dict[str, dict] = {}
        self.clock: SessionClock | None = None

    @property
    def can_trade(self) -> bool:
        return not self.blockers

    def start_session(self, day: date, close: time | None = None) -> None:
        self.clock = SessionClock.for_day(day, close)
        self.counters.roll(day)
        self.series, self.candidates, self.scan_log, self.catalysts = {}, {}, {}, {}

    def refresh_candidates(self, as_of: datetime) -> dict[str, dict]:
        if self.blockers:
            return {}
        p = self.providers
        for row in p.gainers_scanner.scan(as_of):
            catalyst = None
            if isinstance(p.catalyst_provider, CatalystProvider):
                catalyst = p.catalyst_provider.latest_catalyst(row.symbol, as_of)
            ok, reasons, info = candidate_gate(row, p.float_provider.float_shares(row.symbol, as_of),
                                               catalyst, as_of, self.params)
            self.scan_log[row.symbol] = plain({"passed": ok, "reasons": reasons, **info})
            if ok:
                self.candidates[row.symbol] = row
                self.catalysts[row.symbol] = bool(info["fresh_catalyst"])
            else:
                self.candidates.pop(row.symbol, None)
        return dict(self.scan_log)

    def _reject(self, reason: str, **metrics: Any) -> Decision:
        return Decision(False, (reason,), metrics=plain(metrics), profile=PROFILE_ID)

    def _series(self, symbol: str, tf: str) -> SessionSeries:
        return self.series.setdefault((symbol, tf), SessionSeries(symbol, self.clock.session_date))

    def _coverage_ok(self, series: SessionSeries, tf: str, now: datetime) -> bool:
        """IEX gap guard: enough of the recent minutes actually printed a bar (no forward fill)."""
        step = 1 if tf == "1m" else 5
        window = max(step, self.params.coverage_window_minutes)
        elapsed = int((now - series.bars[0].start).total_seconds() // (60 * step)) if series.bars else 0
        expected = max(1, min(window // step, elapsed))
        since = now - timedelta(minutes=expected * step)
        have = sum(1 for b in series.bars[-expected:] if b.start >= since)
        return Decimal(have) / Decimal(expected) >= self.params.min_bar_coverage

    def on_bar(self, bar: DTBar, timeframe: str = "1m") -> list[Decision]:
        if self.blockers:
            return [self._reject("profile_blocked", blockers=self.blockers)]
        if self.clock is None:
            raise RuntimeError("start_session first")
        if timeframe not in {"1m", "5m"}:
            raise ValueError("timeframe must be 1m or 5m")
        if not self.clock.in_regular_session(bar.start):
            return [self._reject("outside_regular_session", symbol=bar.symbol)]
        series = self._series(bar.symbol, timeframe)
        if not series.add(bar):
            return [self._reject("duplicate_or_out_of_order_bar", symbol=bar.symbol)]
        out = self._manage_bar(bar, series)
        if out:
            return out
        evaluations = [(timeframe, series, bar)]
        if timeframe == "1m" and "5m" in self.params.timeframes:
            for five in self._roll_five(bar.symbol, series):
                evaluations.append(("5m", self._series(bar.symbol, "5m"), five))
        decisions = []
        for tf, s, b in evaluations:
            if tf not in self.params.timeframes:
                continue
            decisions.append(self._evaluate(tf, s, b))
            if decisions[-1].accepted:
                return [decisions[-1]]
        return decisions or [self._reject("timeframe_not_enabled", symbol=bar.symbol)]

    def _roll_five(self, symbol: str, one: SessionSeries) -> list[DTBar]:
        """Build 5-minute bars from 1-minute bars as their buckets close.

        A bucket closes on its last minute (:x4/:x9), or late when IEX skipped
        that minute and a bar from a later bucket arrives. A late bucket's
        ``received_at`` is before its end, so ``bar_is_fresh`` blocks entries on it.
        """
        last = one.bars[-1]
        cur = cp.bucket_start(last.start)
        s5 = self._series(symbol, "5m")
        built = s5.bars[-1].start if s5.bars else None
        out = []
        earlier = [b for b in one.bars[:-1] if cp.bucket_start(b.start) < cur]
        if earlier:
            prev = cp.bucket_start(earlier[-1].start)
            if built is None or prev > built:
                group = [b for b in earlier if cp.bucket_start(b.start) == prev]
                bars5 = cp.aggregate_bars(group, include_partial=True)
                if bars5 and s5.add(bars5[-1]):
                    out.append(bars5[-1])
        if local(last.start).minute % 5 == 4:
            group = [b for b in one.bars[-5:] if cp.bucket_start(b.start) == cur]
            bars5 = cp.aggregate_bars(group)
            if bars5 and s5.add(bars5[-1]):
                out.append(bars5[-1])
        return out

    def _evaluate(self, tf: str, series: SessionSeries, bar: DTBar) -> Decision:
        sym = bar.symbol
        if sym not in self.candidates:
            return self._reject("not_a_candidate", symbol=sym, timeframe=tf)
        now = bar.end
        lt = local(now).time()
        if not (_t(self.params.entry_start) <= lt <= _t(self.params.entry_end)):
            return self._reject("outside_entry_window", symbol=sym, timeframe=tf)
        if not self._coverage_ok(series, tf, now):
            return self._reject("iex_bar_coverage_too_thin", symbol=sym, timeframe=tf)
        pp = self.params.one_minute if tf == "1m" else self.params.five_minute
        i = len(series) - 1
        sig, reasons = cp.scan_patterns(series.bars, series.vwaps, i, pp, self.params.enabled_patterns)
        if sig is None:
            return self._reject("no_pattern", symbol=sym, timeframe=tf, reasons=reasons)
        return self.admit(sig, bar)

    def admit(self, signal: cp.PatternSignal, bar: DTBar) -> Decision:
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
        trigger = signal.trigger
        stop = cents_down(signal.stop_low - p.tick)
        entry = cents_up(max(trigger, bar.close))
        risk_ps = trigger - stop
        if risk_ps < p.min_stop_per_share:
            return self._reject("stop_too_tight_for_iex_noise", symbol=sym, risk_per_share=risk_ps)
        max_pct = p.max_stop_pct_5m if signal.timeframe == "5m" else p.max_stop_pct
        if (entry - stop) / entry * 100 > max_pct:
            return self._reject("stop_wider_than_max_pct", symbol=sym, risk_per_share=entry - stop)
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
        setup_id = sha256(f"{PROFILE_ID}|{p.config_id}|{sym}|{signal.kind}|{signal.timeframe}|"
                          f"{bar.start.isoformat()}".encode()).hexdigest()[:24]
        intent = plain({
            "symbol": sym, "side": "buy", "mode": "paper", "profile": PROFILE_ID, "config_id": p.config_id,
            "setup_class": signal.kind, "reason": signal.kind, "pattern": signal.kind,  # same field as PR #69 paper_events
            "timeframe": signal.timeframe, "setup_id": setup_id,
            "confirmations": list(signal.confirmations), "entry_limit": entry, "limit_price": entry,
            "stop_price": stop, "target_price": target, "qty": qty, "order_type": "limit", "time_in_force": "day",
            "extended_hours": False, "limit_ttl_seconds": p.limit_ttl_seconds, "observed_at": now,
            "paper_only": True, "data_feed": self.data_feed, "partial_at_target_fraction": p.partial_fraction,
            "fresh_catalyst": self.catalysts.get(sym, False), "pattern_metrics": signal.metrics,
        })
        self.pending[sym] = {"stop": stop, "entry": entry, "qty": qty, "kind": signal.kind,
                             "expires_at": now + timedelta(seconds=p.limit_ttl_seconds)}
        return Decision(True, (signal.kind,), intent, plain({"caps": self.caps.public()}), profile=PROFILE_ID)

    def pending_actions(self, now: datetime) -> list[Decision]:
        out = []
        for sym in list(self.pending):
            if self.counters.halted_reason or now >= self.pending[sym]["expires_at"] or \
                    (self.clock is not None and not self.clock.entries_allowed(now)):
                kind = self.pending.get(sym, {}).get("kind", "unknown")
                self.pending.pop(sym)
                out.append(Decision(True, ("cancel_entry",), {"symbol": sym, "action": "cancel_entry", "pattern": kind},
                                    profile=PROFILE_ID))
        return out

    def on_entry_filled(self, symbol: str, qty: int, price: Decimal | str, at: datetime) -> None:
        pend = self.pending.pop(symbol)
        entry = d(price)
        r = entry - pend["stop"]
        self.positions[symbol] = {"qty": int(qty), "entry": entry, "stop": pend["stop"], "r": r,
                                  "initial_qty": int(qty), "kind": pend["kind"],
                                  "target": entry + self.params.first_target_r * r, "partial_done": False,
                                  "realized": ZERO, "opened_at": at}
        self.counters.campaigns_total += 1
        self.counters.campaigns_by_symbol[symbol] = self.counters.campaigns_by_symbol.get(symbol, 0) + 1

    def _exit(self, sym: str, qty: int, reason: str, price_hint: Decimal | None = None) -> Decision:
        pattern = (self.positions.get(sym) or {}).get("kind", "unknown")
        return Decision(True, ("exit", reason), plain({"symbol": sym, "side": "sell", "action": "exit", "qty": qty,
                                                       "reason": reason, "pattern": pattern, "mode": "paper",
                                                       "profile": PROFILE_ID, "price_hint": price_hint}),
                        profile=PROFILE_ID)

    def _manage_bar(self, bar: DTBar, series: SessionSeries) -> list[Decision]:
        """Stop, half at +2R then stop to entry, runner out on a close under the 9-EMA."""
        pos = self.positions.get(bar.symbol)
        if pos is None:
            return []
        p = self.params
        if bar.low <= pos["stop"]:
            return [self._exit(bar.symbol, pos["qty"], "breakeven_stop" if pos["partial_done"] else "stop",
                               pos["stop"])]
        if not pos["partial_done"] and bar.high >= pos["target"]:
            half = int(pos["qty"] * p.partial_fraction)
            if half >= 1:
                return [self._exit(bar.symbol, half, "first_target_half", pos["target"])]
            return [self._exit(bar.symbol, pos["qty"], "first_target_full", pos["target"])]
        if pos["partial_done"]:
            ema = series.ema(p.runner_exit_ema_period)
            if ema is not None and bar.close < ema:
                return [self._exit(bar.symbol, pos["qty"], "runner_close_below_ema9", bar.close)]
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
                      "blockers": list(self.blockers), "caps": self.caps.public(), "data_feed": self.data_feed,
                      "candidates": sorted(self.candidates), "scan": self.scan_log,
                      "counters": self.counters.to_state(), "open_positions": sorted(self.positions)})
