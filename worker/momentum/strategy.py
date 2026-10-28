"""Causal first-pullback research baseline, with no brokerage capability.

The source-backed scanner thresholds are combined with explicitly experimental
pattern, RVOL, entry and exit definitions. This is not an exact behavioral clone.
See worker/STRATEGY_NOTES.md for the distinction and input contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any, Mapping

from .models import (Bar, Decision, InputError, NY, PROFILE, Quote, config_fingerprint,
                     integer_value, plain, positive_decimal, session_date, timestamp)


@dataclass(frozen=True)
class StrategyConfig:
    profile: str = PROFILE
    mode: str = "paper"
    min_price: Decimal = Decimal("2")
    max_price: Decimal = Decimal("20")
    min_gain_pct: Decimal = Decimal("10")
    min_rvol: Decimal = Decimal("5")
    max_float_exclusive: int = 20_000_000
    rvol_sessions: int = 30
    impulse_bars: int = 3
    min_pullback_bars: int = 2
    max_pullback_bars: int = 3
    min_impulse_gain_pct: Decimal = Decimal("2")
    min_impulse_mean_volume: int = 10_000
    min_impulse_body_fraction: Decimal = Decimal("0.5")
    max_pullback_volume_ratio: Decimal = Decimal("0.6")
    max_retracement: Decimal = Decimal("0.5")
    tick_size: Decimal = Decimal("0.01")
    max_entry_chase: Decimal = Decimal("0.03")
    target_r: Decimal = Decimal("2")
    max_quote_age_seconds: Decimal = Decimal("2")
    max_spread: Decimal = Decimal("0.05")
    max_spread_bps: Decimal = Decimal("100")
    max_float_age_days: int = 7
    max_catalyst_age_hours: int = 24
    max_hold_seconds: int = 600
    trade_start_et: str = "07:00"
    trade_end_et: str = "10:00"

    @property
    def config_id(self) -> str:
        return config_fingerprint(self)

    def __post_init__(self) -> None:
        if self.mode != "paper" or self.profile != PROFILE:
            raise ValueError("Only paper experimental_v1 is supported")
        for name in ("min_price", "max_price", "min_gain_pct", "min_rvol", "min_impulse_gain_pct",
                     "min_impulse_body_fraction", "max_pullback_volume_ratio", "max_retracement",
                     "tick_size", "max_entry_chase", "target_r", "max_quote_age_seconds", "max_spread", "max_spread_bps"):
            object.__setattr__(self, name, positive_decimal(getattr(self, name), name))
        for name in ("max_float_exclusive", "rvol_sessions", "impulse_bars", "min_pullback_bars",
                     "max_pullback_bars", "min_impulse_mean_volume", "max_float_age_days",
                     "max_catalyst_age_hours", "max_hold_seconds"):
            object.__setattr__(self, name, integer_value(getattr(self, name), name, minimum=1))
        if self.min_price > self.max_price or self.min_pullback_bars > self.max_pullback_bars:
            raise ValueError("Invalid ordered bounds")
        if any(getattr(self, name) > 1 for name in ("min_impulse_body_fraction", "max_pullback_volume_ratio", "max_retracement")):
            raise ValueError("Pattern fractions must be <= 1")
        if self.target_r < 2:
            raise ValueError("experimental_v1 requires at least 2R target room")
        if self.rvol_sessions != 30 or self.impulse_bars != 3:
            raise ValueError("Changing the RVOL lookback or impulse definition requires a new profile")
        try:
            begin, end = time.fromisoformat(self.trade_start_et), time.fromisoformat(self.trade_end_et)
        except (TypeError, ValueError):
            raise ValueError("Invalid trading window") from None
        if begin.tzinfo or end.tzinfo or not time(4) <= begin < end <= time(16):
            raise ValueError("Invalid trading window")


def _input_error(error: Exception) -> Decision:
    if isinstance(error, InputError):
        return Decision.invalid(error)
    return Decision.reject("invalid_snapshot", metrics={"error_type": type(error).__name__})


def compute_rvol(data: Mapping[str, Any], now: datetime | str, feed: str,
                 sessions: int = 30) -> Decimal:
    """04:00 ET cumulative volume / prior 30-session same-clock mean.

    The adapter must supply the exchange calendar's exact preceding 30 sessions.
    This function verifies their order, uniqueness, causal availability, clock
    and feed consistency; it does not infer exchange holidays from weekdays.
    """
    if not isinstance(data, Mapping):
        raise InputError("invalid_rvol")
    now = timestamp(now, "now")
    if sessions != 30:
        raise InputError("unsupported_rvol_definition")
    as_of = timestamp(data.get("as_of"), "rvol.as_of")
    today = now.astimezone(NY).date()
    cutoff = as_of.astimezone(NY)
    if as_of > now or cutoff.date() != today or cutoff.time() < time(4):
        raise InputError("invalid_rvol_cutoff")
    if now - as_of >= timedelta(minutes=1):
        raise InputError("stale_rvol")
    if data.get("feed") != feed:
        raise InputError("mixed_feed", "rvol.feed")
    if data.get("session_start_et", "04:00") != "04:00":
        raise InputError("mixed_rvol_session_definition")
    numerator = integer_value(data.get("current_volume"), "rvol.current_volume")
    calendar = [session_date(x, "rvol.prior_sessions") for x in data.get("prior_sessions", [])]
    if len(calendar) != sessions or len(set(calendar)) != sessions or calendar != sorted(calendar):
        raise InputError("incomplete_rvol_history")
    if any(day >= today for day in calendar):
        raise InputError("lookahead_rvol_history")
    rows = data.get("history", [])
    if not isinstance(rows, (list, tuple)) or len(rows) != sessions:
        raise InputError("incomplete_rvol_history")
    expected_clock = cutoff.time().replace(tzinfo=None)
    by_date: dict[Any, int] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise InputError("invalid_rvol_history_row")
        day = session_date(row.get("session"), "rvol.history.session")
        if day >= today:
            raise InputError("lookahead_rvol_history")
        if day in by_date:
            raise InputError("duplicate_rvol_session")
        if row.get("feed") != feed:
            raise InputError("mixed_feed", "rvol.history.feed")
        if row.get("session_start_et", "04:00") != "04:00":
            raise InputError("mixed_rvol_session_definition")
        try:
            row_clock = time.fromisoformat(str(row.get("clock")))
        except ValueError:
            raise InputError("invalid_rvol_clock") from None
        if row_clock != expected_clock or row_clock.tzinfo is not None:
            raise InputError("mismatched_rvol_clock")
        known = timestamp(row.get("known_at"), "rvol.history.known_at")
        measurement = datetime.combine(day, row_clock, NY)
        if known < measurement or known > now:
            raise InputError("lookahead_rvol_availability")
        by_date[day] = integer_value(row.get("volume"), "rvol.history.volume")
    if set(by_date) != set(calendar):
        raise InputError("incomplete_rvol_history")
    denominator = Decimal(sum(by_date.values())) / Decimal(sessions)
    if denominator <= 0:
        raise InputError("zero_rvol_denominator")
    return Decimal(numerator) / denominator


def _market(snapshot: Mapping[str, Any], cfg: StrategyConfig) -> tuple[datetime, str, str, Quote]:
    if not isinstance(snapshot, Mapping):
        raise InputError("invalid_snapshot")
    now = timestamp(snapshot.get("now"), "now")
    symbol = snapshot.get("symbol")
    feed = snapshot.get("feed")
    if not isinstance(symbol, str) or not symbol or len(symbol) > 24:
        raise InputError("missing_or_invalid_symbol")
    if not isinstance(feed, str) or not feed:
        raise InputError("missing_feed")
    quote_data = snapshot.get("quote", {})
    if not isinstance(quote_data, Mapping):
        raise InputError("invalid_quote")
    if quote_data.get("symbol", symbol) != symbol:
        raise InputError("quote_symbol_mismatch")
    quote = Quote.from_dict(quote_data, feed)
    quote.check_time(now, cfg.max_quote_age_seconds)
    return now, symbol, feed, quote


def evaluate_setup(snapshot: Mapping[str, Any], config: StrategyConfig | None = None) -> Decision:
    """Evaluate one causal observation and tag every result with its config hash."""
    cfg = config or StrategyConfig()
    result = _evaluate_setup(snapshot, cfg)
    return replace(result, metrics={**result.metrics, "config_id": cfg.config_id})


def _evaluate_setup(snapshot: Mapping[str, Any], cfg: StrategyConfig) -> Decision:
    try:
        now, symbol, feed, quote = _market(snapshot, cfg)
        local = now.astimezone(NY)
        if not time.fromisoformat(cfg.trade_start_et) <= local.time() < time.fromisoformat(cfg.trade_end_et):
            return Decision.reject("outside_entry_window")
        if snapshot.get("mode", "paper") != "paper":
            return Decision.reject("live_mode_forbidden")
        session = snapshot.get("session", {})
        if not isinstance(session, Mapping) or session.get("is_trading_day") is not True:
            return Decision.reject("session_eligibility_unconfirmed")
        if session_date(session.get("date")) != local.date() or local.weekday() >= 5:
            return Decision.reject("session_ineligible")
        spread = quote.ask - quote.bid
        if spread > cfg.max_spread or spread / quote.bid * 10000 > cfg.max_spread_bps:
            return Decision.reject("spread_too_wide")
        trade = snapshot.get("last_trade", {})
        if not isinstance(trade, Mapping):
            return Decision.reject("invalid_trade")
        if trade.get("feed") != feed:
            return Decision.reject("mixed_feed", metrics={"field": "last_trade.feed"})
        if trade.get("symbol") != symbol:
            return Decision.reject("trade_symbol_mismatch")
        last = positive_decimal(trade.get("price"), "last_trade.price")
        trade_time = timestamp(trade.get("timestamp"), "last_trade.timestamp")
        trade_received = timestamp(trade.get("received_at"), "last_trade.received_at")
        if trade_time > now or trade_received > now or trade_received < trade_time:
            return Decision.reject("future_or_invalid_trade")
        if Decimal(str((now - trade_time).total_seconds())) > cfg.max_quote_age_seconds:
            return Decision.reject("stale_trade")
        if not cfg.min_price <= last <= cfg.max_price or not cfg.min_price <= quote.ask <= cfg.max_price:
            return Decision.reject("price_outside_profile")
        facts = snapshot.get("facts", {})
        if not isinstance(facts, Mapping):
            return Decision.reject("invalid_facts")
        previous_close = positive_decimal(facts.get("previous_close"), "previous_close")
        gain = (last / previous_close - 1) * 100
        if gain < cfg.min_gain_pct:
            return Decision.reject("gain_below_threshold", metrics={"gain_pct": gain})
        if facts.get("float_shares") is None:
            return Decision.reject("float_unconfirmed")
        shares_float = integer_value(facts["float_shares"], "float_shares", minimum=1)
        effective = timestamp(facts.get("float_effective_at"), "float_effective_at")
        known = timestamp(facts.get("float_known_at"), "float_known_at")
        if effective > now or known > now or known < effective:
            return Decision.reject("float_not_known_at_decision")
        if now - effective > timedelta(days=cfg.max_float_age_days):
            return Decision.reject("float_too_old")
        if shares_float >= cfg.max_float_exclusive:
            return Decision.reject("float_above_threshold")
        if facts.get("catalyst_confirmed") is not True:
            return Decision.reject("catalyst_unconfirmed")
        published = timestamp(facts.get("catalyst_published_at"), "catalyst_published_at")
        catalyst_known = timestamp(facts.get("catalyst_known_at"), "catalyst_known_at")
        if published > now or catalyst_known > now or catalyst_known < published:
            return Decision.reject("catalyst_not_known_at_decision")
        if now - published > timedelta(hours=cfg.max_catalyst_age_hours):
            return Decision.reject("catalyst_too_old")
        rvol = compute_rvol(snapshot.get("rvol", {}), now, feed, cfg.rvol_sessions)
        if rvol < cfg.min_rvol:
            return Decision.reject("rvol_below_threshold", metrics={"rvol": rvol})
        raw_bars = snapshot.get("bars", [])
        if not isinstance(raw_bars, (list, tuple)) or len(raw_bars) < cfg.impulse_bars + cfg.min_pullback_bars:
            return Decision.reject("insufficient_completed_bars")
        bars = [Bar.from_dict(row, feed) for row in raw_bars]
        for i, bar in enumerate(bars):
            if bar.end > now or bar.available_at > now:
                return Decision.reject("lookahead_bar")
            if bar.start.astimezone(NY).date() != local.date() or bar.start.astimezone(NY).time() < time(4):
                return Decision.reject("bar_outside_session")
            if i and bar.start != bars[i - 1].end:
                return Decision.reject("noncontiguous_bars")
        if not bars[-1].end <= trade_time < bars[-1].end + timedelta(minutes=1):
            return Decision.reject("trigger_not_current_intrabar")
        if not bars[-1].end <= now < bars[-1].end + timedelta(minutes=1):
            return Decision.reject("stale_completed_bars")
        if quote.timestamp < bars[-1].end:
            return Decision.reject("quote_before_trigger_bar")
        pullback_count = 0
        for bar in reversed(bars):
            if bar.close >= bar.open:
                break
            pullback_count += 1
        if not cfg.min_pullback_bars <= pullback_count <= cfg.max_pullback_bars:
            return Decision.reject("pullback_length_invalid")
        if len(bars) < pullback_count + cfg.impulse_bars:
            return Decision.reject("insufficient_impulse_bars")
        pullback = bars[-pullback_count:]
        impulse = bars[-pullback_count - cfg.impulse_bars:-pullback_count]
        for i, bar in enumerate(impulse):
            if bar.close <= bar.open or bar.high == bar.low:
                return Decision.reject("impulse_not_rising")
            if (bar.close - bar.open) / (bar.high - bar.low) < cfg.min_impulse_body_fraction:
                return Decision.reject("impulse_body_too_small")
            if i and (bar.close <= impulse[i - 1].close or bar.high <= impulse[i - 1].high):
                return Decision.reject("impulse_not_rising")
        impulse_gain = (impulse[-1].close / impulse[0].open - 1) * 100
        if impulse_gain < cfg.min_impulse_gain_pct:
            return Decision.reject("impulse_too_small")
        peak, base = max(b.high for b in impulse), min(b.low for b in impulse)
        low = min(b.low for b in pullback)
        if any(b.high > peak for b in pullback) or any(
            pullback[i].high > pullback[i - 1].high or pullback[i].close > pullback[i - 1].close
            for i in range(1, len(pullback))
        ):
            return Decision.reject("pullback_shape_invalid")
        retracement = (peak - low) / (peak - base)
        if retracement <= 0 or retracement > cfg.max_retracement:
            return Decision.reject("pullback_too_deep", metrics={"retracement": retracement})
        impulse_volume = Decimal(sum(b.volume for b in impulse)) / len(impulse)
        pullback_volume = Decimal(sum(b.volume for b in pullback)) / len(pullback)
        if impulse_volume < cfg.min_impulse_mean_volume:
            return Decision.reject("impulse_volume_too_low")
        if pullback_volume / impulse_volume > cfg.max_pullback_volume_ratio:
            return Decision.reject("pullback_volume_too_high")
        trigger = pullback[-1].high + cfg.tick_size
        if last < trigger:
            return Decision.reject("waiting_for_breakout", metrics={"trigger": trigger, "last": last})
        entry = quote.ask
        if entry < trigger or entry > trigger + cfg.max_entry_chase or last > trigger + cfg.max_entry_chase:
            return Decision.reject("entry_extended_or_inconsistent")
        stop = low - cfg.tick_size
        risk_distance = entry - stop
        if stop <= 0 or risk_distance <= 0:
            return Decision.reject("invalid_stop_distance")
        target = entry + cfg.target_r * risk_distance
        if target > peak:
            return Decision.reject("insufficient_known_resistance_room")
        identity = f"{cfg.profile}|{cfg.config_id}|{symbol}|{feed}|{impulse[0].start.isoformat()}|{pullback[0].start.isoformat()}"
        setup_id = sha256(identity.encode()).hexdigest()[:24]
        intent = plain({"symbol": symbol, "side": "buy", "mode": "paper", "profile": cfg.profile,
                        "config_id": cfg.config_id,
                        "entry_limit": entry, "limit_price": entry, "stop_price": stop, "target_price": target,
                        "setup_id": setup_id, "observed_at": now, "feed": feed,
                        "liquidity_volume": pullback[-1].volume, "reason": "first_pullback_breakout"})
        return Decision(True, ("first_pullback_breakout",), intent,
                        plain({"gain_pct": gain, "rvol": rvol, "retracement": retracement,
                               "trigger": trigger, "known_resistance": peak,
                               "impulse_mean_volume": impulse_volume, "pullback_mean_volume": pullback_volume,
                               "pullback_bars": pullback_count, "reduced_fidelity": "price_and_volume_only_no_depth"}))
    except (InputError, ValueError, TypeError, KeyError, ArithmeticError) as error:
        return _input_error(error)


def evaluate_exit(position: Mapping[str, Any], snapshot: Mapping[str, Any],
                  config: StrategyConfig | None = None, *, force_reason: str | None = None) -> Decision:
    """Produce a paper sell intention; only a confirmed fill establishes flatness.

    Exits use the executable bid, not an assumed stop/target fill. A stale quote
    blocks price construction even for forced exits and must alert the operator.
    """
    cfg = config or StrategyConfig()
    result = _evaluate_exit(position, snapshot, cfg, force_reason=force_reason)
    return replace(result, metrics={**result.metrics, "config_id": cfg.config_id})


def _evaluate_exit(position: Mapping[str, Any], snapshot: Mapping[str, Any],
                   cfg: StrategyConfig, *, force_reason: str | None = None) -> Decision:
    try:
        now, symbol, feed, quote = _market(snapshot, cfg)
        if not isinstance(position, Mapping):
            return Decision.reject("invalid_position")
        if position.get("symbol") != symbol:
            return Decision.reject("position_symbol_mismatch")
        if position.get("mode", "paper") != "paper" or snapshot.get("mode", "paper") != "paper":
            return Decision.reject("live_mode_forbidden")
        qty = integer_value(position.get("qty"), "position.qty", minimum=1)
        entry = positive_decimal(position.get("entry_price"), "position.entry_price")
        stop = positive_decimal(position.get("stop_price"), "position.stop_price")
        target = positive_decimal(position.get("target_price"), "position.target_price")
        opened = timestamp(position.get("opened_at"), "position.opened_at")
        if not stop < entry < target or opened > now:
            return Decision.reject("invalid_position_risk")
        if force_reason is not None and (not isinstance(force_reason, str) or not force_reason.strip()):
            return Decision.reject("invalid_force_reason")
        reason = force_reason
        if reason is None:
            if quote.bid <= stop:
                reason = "stop_reached"
            elif quote.bid >= target:
                reason = "target_reached"
            elif now.astimezone(NY).date() > opened.astimezone(NY).date() or now.astimezone(NY).time() >= time.fromisoformat(cfg.trade_end_et):
                reason = "session_end"
            elif now - opened >= timedelta(seconds=cfg.max_hold_seconds):
                reason = "maximum_holding_time"
        if reason is None:
            return Decision.reject("hold", metrics={"mark_bid": quote.bid})
        intent = plain({"symbol": symbol, "side": "sell", "mode": "paper", "profile": cfg.profile,
                        "config_id": cfg.config_id,
                        "qty": qty, "entry_limit": quote.bid, "limit_price": quote.bid,
                        "observed_at": now, "reason": reason, "feed": feed})
        return Decision(True, (reason,), intent, {"position_flat": False, "fill_confirmation_required": True})
    except (InputError, ValueError, TypeError, KeyError, ArithmeticError) as error:
        return _input_error(error)
