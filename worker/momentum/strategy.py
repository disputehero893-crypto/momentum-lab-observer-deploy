"""Causal first-pullback research baseline, with no brokerage capability.

The default preset is liquid_large_cap_iex_v2 (Lab-retuned free-IEX
pullback for AAPL, MSFT and SPY). v1 remains loadable. The small-cap
float/news profile remains a gated preset. See worker/STRATEGY_PRESETS.md.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
import re
from typing import Any, Mapping

from .models import (Bar, Decision, InputError, NY, Quote, config_fingerprint,
                     integer_value, plain, positive_decimal, session_date, timestamp)
from .presets import (LIQUID_IEX_UNIVERSE, PRESET_LIQUID_IEX, PRESET_LIQUID_IEX_V1,
                      PRESET_LIQUID_IEX_V2, PRESET_SMALL_CAP_CATALYST,
                      PROFILE_LIQUID_IEX, SYNTHETIC_FEEDS, PresetSpec, get_preset)

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")


@dataclass(frozen=True)
class StrategyConfig:
    """Defaults are liquid_large_cap_iex_v2 (free IEX, Lab-retuned).

    v1 remains available via liquid_iex_v1(). The small-cap float/news preset is
    constructed with small_cap_catalyst() and stays inert until
    enable_gated_presets is true. Paper mode is the only mode.
    """

    profile: str = PROFILE_LIQUID_IEX
    preset: str = PRESET_LIQUID_IEX
    mode: str = "paper"
    min_price: Decimal = Decimal("5")
    max_price: Decimal = Decimal("1000")
    min_gain_pct: Decimal = Decimal("0.10")
    min_rvol: Decimal = Decimal("1.2")
    max_float_exclusive: int = 20_000_000
    rvol_sessions: int = 30
    impulse_bars: int = 3
    min_pullback_bars: int = 1
    max_pullback_bars: int = 3
    min_impulse_gain_pct: Decimal = Decimal("0.10")
    min_impulse_mean_volume: int = 1  # unused when impulse_vol_vs_same_clock_median is set
    min_impulse_body_fraction: Decimal = Decimal("0.25")
    max_pullback_volume_ratio: Decimal = Decimal("1.0")
    max_retracement: Decimal = Decimal("0.65")
    tick_size: Decimal = Decimal("0.01")
    max_entry_chase: Decimal = Decimal("0.03")
    target_r: Decimal = Decimal("2")
    max_quote_age_seconds: Decimal = Decimal("2")
    max_spread: Decimal = Decimal("0.08")
    max_spread_bps: Decimal = Decimal("40")
    max_float_age_days: int = 7
    max_catalyst_age_hours: int = 24
    max_hold_seconds: int = 600
    trade_start_et: str = "09:35"
    trade_end_et: str = "15:45"
    requires_float_catalyst: bool = False
    allowed_symbols: tuple = LIQUID_IEX_UNIVERSE
    enable_gated_presets: bool = False
    # --- IEX retune shape / volume (v1 defaults preserve PR#9 when loaded) ---
    min_green_impulse_bars: int = 2
    require_strict_rising: bool = False
    require_target_within_impulse_peak: bool = False
    impulse_vol_vs_same_clock_median: Decimal | None = Decimal("1.2")

    @property
    def config_id(self) -> str:
        return config_fingerprint(self)

    @property
    def preset_spec(self) -> PresetSpec:
        return get_preset(self.preset)

    @classmethod
    def liquid_iex(cls, **overrides: Any) -> "StrategyConfig":
        """Default free-IEX preset (v2 retune)."""
        return load_strategy({"preset": PRESET_LIQUID_IEX, **overrides})

    @classmethod
    def liquid_iex_v1(cls, **overrides: Any) -> "StrategyConfig":
        """Legacy PR#9 liquid thresholds (0 historical IEX trades)."""
        return load_strategy({"preset": PRESET_LIQUID_IEX_V1, **overrides})

    @classmethod
    def liquid_iex_v2(cls, **overrides: Any) -> "StrategyConfig":
        return load_strategy({"preset": PRESET_LIQUID_IEX_V2, **overrides})

    @classmethod
    def small_cap_catalyst(cls, **overrides: Any) -> "StrategyConfig":
        """Gated small-cap float/news preset. Not the default."""
        return load_strategy({"preset": PRESET_SMALL_CAP_CATALYST, **overrides})

    def __post_init__(self) -> None:
        if self.mode != "paper":
            raise ValueError("Only paper mode is supported")
        if type(self.requires_float_catalyst) is not bool or type(self.enable_gated_presets) is not bool:
            raise ValueError("Preset flags must be booleans")
        if type(self.require_strict_rising) is not bool or type(self.require_target_within_impulse_peak) is not bool:
            raise ValueError("Impulse shape flags must be booleans")
        try:
            spec = get_preset(self.preset)
        except ValueError:
            raise ValueError(f"Unsupported strategy preset: {self.preset}") from None
        if self.profile != spec.profile:
            raise ValueError("preset/profile mismatch")
        if self.requires_float_catalyst != spec.requires_float_catalyst:
            raise ValueError("preset/requires_float_catalyst mismatch")
        symbols = self.allowed_symbols
        if isinstance(symbols, list):
            symbols = tuple(symbols)
        if not isinstance(symbols, tuple) or len(symbols) > 10 or any(type(symbol) is not str or not _SYMBOL.fullmatch(symbol) for symbol in symbols):
            raise ValueError("allowed_symbols must be at most 10 uppercase symbols")
        if len(set(symbols)) != len(symbols):
            raise ValueError("allowed_symbols must be unique")
        if spec.preset_id in {PRESET_LIQUID_IEX_V1, PRESET_LIQUID_IEX_V2} and not symbols:
            symbols = LIQUID_IEX_UNIVERSE
        object.__setattr__(self, "allowed_symbols", symbols)
        for name in ("min_price", "max_price", "min_gain_pct", "min_rvol", "min_impulse_gain_pct",
                     "min_impulse_body_fraction", "max_pullback_volume_ratio", "max_retracement",
                     "tick_size", "max_entry_chase", "target_r", "max_quote_age_seconds", "max_spread", "max_spread_bps"):
            object.__setattr__(self, name, positive_decimal(getattr(self, name), name))
        if self.impulse_vol_vs_same_clock_median is not None:
            object.__setattr__(
                self, "impulse_vol_vs_same_clock_median",
                positive_decimal(self.impulse_vol_vs_same_clock_median, "impulse_vol_vs_same_clock_median"),
            )
        for name in ("max_float_exclusive", "rvol_sessions", "impulse_bars", "min_pullback_bars",
                     "max_pullback_bars", "min_impulse_mean_volume", "max_float_age_days",
                     "max_catalyst_age_hours", "max_hold_seconds", "min_green_impulse_bars"):
            object.__setattr__(self, name, integer_value(getattr(self, name), name, minimum=1))
        if not 1 <= self.min_green_impulse_bars <= self.impulse_bars:
            raise ValueError("min_green_impulse_bars must be within the impulse window")
        if self.min_price > self.max_price or self.min_pullback_bars > self.max_pullback_bars:
            raise ValueError("Invalid ordered bounds")
        if any(getattr(self, name) > 1 for name in ("min_impulse_body_fraction", "max_pullback_volume_ratio", "max_retracement")):
            raise ValueError("Pattern fractions must be <= 1")
        if self.target_r < 2:
            raise ValueError("Strategy requires at least 2R target room")
        if self.rvol_sessions != 30 or self.impulse_bars != 3:
            raise ValueError("Changing the RVOL lookback or impulse definition requires a new profile")
        try:
            begin, end = time.fromisoformat(self.trade_start_et), time.fromisoformat(self.trade_end_et)
        except (TypeError, ValueError):
            raise ValueError("Invalid trading window") from None
        if begin.tzinfo or end.tzinfo or not time(4) <= begin < end <= time(16):
            raise ValueError("Invalid trading window")


def load_strategy(overrides: Mapping[str, Any] | None = None) -> StrategyConfig:
    """Build a paper StrategyConfig, applying the named preset's defaults first.

    An empty mapping is the liquid IEX preset. A mapping that sets `preset` to
    the small-cap id still starts from that preset's thresholds; it does not
    keep the liquid price band underneath a small-cap label.
    """
    raw = dict(overrides or {})
    preset_id = raw.get("preset", PRESET_LIQUID_IEX)
    merged = dict(get_preset(preset_id).strategy_defaults)
    merged.update(raw)
    return StrategyConfig(**merged)


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



def lookup_same_clock_volume_median(
    snapshot: Mapping[str, Any], impulse_bar: "Bar", feed: str,
) -> Decimal:
    """Read the adapter-supplied same-clock IEX bar-volume median for the impulse clock.

    Expected snapshot shape::

        "same_clock_volume_median": {
            "feed": "iex",
            "medians": {"11:12:00": 2400, ...},  # America/New_York clock -> median volume
        }

    Medians must be fitted on prior sessions only (no lookahead). Missing or
    non-positive entries fail closed.
    """
    data = snapshot.get("same_clock_volume_median")
    if not isinstance(data, Mapping):
        raise InputError("same_clock_volume_median_missing")
    if data.get("feed") != feed:
        raise InputError("mixed_feed", "same_clock_volume_median.feed")
    medians = data.get("medians")
    if not isinstance(medians, Mapping) or not medians:
        raise InputError("same_clock_volume_median_missing")
    clock = impulse_bar.start.astimezone(NY).time().replace(tzinfo=None)
    keys = (
        clock.strftime("%H:%M:%S"),
        clock.strftime("%H:%M"),
        f"{clock.hour:02d}:{clock.minute:02d}:00",
    )
    raw = None
    for key in keys:
        if key in medians:
            raw = medians[key]
            break
    if raw is None:
        raise InputError("same_clock_volume_median_missing")
    return positive_decimal(raw, "same_clock_volume_median")


def evaluate_setup(snapshot: Mapping[str, Any], config: StrategyConfig | None = None) -> Decision:
    """Evaluate one causal observation and tag every result with its config hash."""
    cfg = config or StrategyConfig()
    result = _evaluate_setup(snapshot, cfg)
    return replace(result, profile=cfg.profile, metrics={**result.metrics, "config_id": cfg.config_id})


def _evaluate_setup(snapshot: Mapping[str, Any], cfg: StrategyConfig) -> Decision:
    try:
        spec = cfg.preset_spec
        if spec.gated and not cfg.enable_gated_presets:
            return Decision.reject("gated_preset_disabled", metrics={
                "preset": cfg.preset, "gate_reasons": list(spec.gate_reasons)})
        now, symbol, feed, quote = _market(snapshot, cfg)
        if cfg.allowed_symbols and symbol not in cfg.allowed_symbols:
            return Decision.reject("symbol_not_in_preset_universe", metrics={"symbol": symbol, "preset": cfg.preset})
        if spec.required_feed == "iex" and feed != "iex":
            return Decision.reject("liquid_preset_requires_iex", metrics={"feed": feed})
        if spec.required_feed == "sip" and feed != "sip" and feed not in SYNTHETIC_FEEDS:
            return Decision.reject("small_cap_requires_sip_feed", metrics={"feed": feed, "required_feed": "sip"})
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
        if cfg.requires_float_catalyst:
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
        if cfg.require_strict_rising:
            for i, bar in enumerate(impulse):
                if bar.close <= bar.open or bar.high == bar.low:
                    return Decision.reject("impulse_not_rising")
                if (bar.close - bar.open) / (bar.high - bar.low) < cfg.min_impulse_body_fraction:
                    return Decision.reject("impulse_body_too_small")
                if i and (bar.close <= impulse[i - 1].close or bar.high <= impulse[i - 1].high):
                    return Decision.reject("impulse_not_rising")
        else:
            greens = 0
            for bar in impulse:
                if bar.close > bar.open:
                    greens += 1
                    if bar.high == bar.low or (bar.close - bar.open) / (bar.high - bar.low) < cfg.min_impulse_body_fraction:
                        return Decision.reject("impulse_body_too_small")
            if greens < cfg.min_green_impulse_bars:
                return Decision.reject("impulse_not_rising")
            if impulse[-1].close <= impulse[0].close:
                return Decision.reject("impulse_not_rising")
        impulse_gain = (impulse[-1].close / impulse[0].open - 1) * 100
        if impulse_gain < cfg.min_impulse_gain_pct:
            return Decision.reject("impulse_too_small")
        peak, base = max(b.high for b in impulse), min(b.low for b in impulse)
        low = min(b.low for b in pullback)
        if any(b.high > peak for b in pullback):
            return Decision.reject("pullback_shape_invalid")
        for i in range(1, len(pullback)):
            if pullback[i].close > pullback[i - 1].close:
                return Decision.reject("pullback_shape_invalid")
            if cfg.require_strict_rising and pullback[i].high > pullback[i - 1].high:
                return Decision.reject("pullback_shape_invalid")
        retracement = (peak - low) / (peak - base)
        if retracement <= 0 or retracement > cfg.max_retracement:
            return Decision.reject("pullback_too_deep", metrics={"retracement": retracement})
        impulse_volume = Decimal(sum(b.volume for b in impulse)) / len(impulse)
        pullback_volume = Decimal(sum(b.volume for b in pullback)) / len(pullback)
        if cfg.impulse_vol_vs_same_clock_median is not None:
            try:
                median = lookup_same_clock_volume_median(snapshot, impulse[-1], feed)
            except InputError as error:
                return Decision.reject(error.code, metrics={"field": error.field_name})
            if impulse_volume < cfg.impulse_vol_vs_same_clock_median * median:
                return Decision.reject(
                    "impulse_volume_too_low",
                    metrics={
                        "impulse_mean_volume": impulse_volume,
                        "same_clock_median": median,
                        "required_multiple": cfg.impulse_vol_vs_same_clock_median,
                    },
                )
        elif impulse_volume < cfg.min_impulse_mean_volume:
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
        if cfg.require_target_within_impulse_peak and target > peak:
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
    return replace(result, profile=cfg.profile, metrics={**result.metrics, "config_id": cfg.config_id})


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


