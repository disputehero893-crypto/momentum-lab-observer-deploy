"""Read-only selection requirements for the configured observation watchlist.

This projection does not call the setup/risk engines or construct an intention.
It checks the range of BOTH a fresh observed last trade and a fresh observed
ask. It does not independently establish trade-condition eligibility or fills.
The current Market Watch contract contains no evidence for the other four
selection pillars, so every overall review remains incomplete.

S10/S12 in STRATEGY_NOTES.md identify a particular instructional 2024 variant:
$2-$20, gain >=10%, RVOL >=5 and float <20 million. The comparison below applies
only to those five numeric configuration fields. The 30-session same-clock RVOL,
freshness policy, seven-day float, 24-hour catalyst, and entry window are explicit
experimental choices. A numeric source match never labels the whole experiment
as an exact reproduction of the source trader's method.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, time, timezone
from decimal import Decimal

from .activities import timestamp_parts
from .market_observation import sanitize_market_watch
from .models import NY
from .strategy import StrategyConfig


SCHEMA_VERSION = "1.0"
PRICE_REASONS = frozenset({
    "quote_missing", "quote_invalid", "quote_stale", "trade_missing", "trade_invalid", "trade_stale",
    "both_prices_in_range", "last_trade_outside_range", "ask_outside_range", "both_prices_outside_range",
})
SOURCE_THRESHOLD_FIELDS = ("min_price", "max_price", "min_gain_pct", "min_rvol", "max_float_exclusive")
SOURCE_THRESHOLDS = {"min_price": Decimal("2"), "max_price": Decimal("20"),
                     "min_gain_pct": Decimal("10"), "min_rvol": Decimal("5"),
                     "max_float_exclusive": 20_000_000}
EXPERIMENTAL_FIELDS = (
    "profile", "mode", "min_price", "max_price", "min_gain_pct", "min_rvol", "max_float_exclusive",
    "rvol_sessions", "impulse_bars", "min_pullback_bars", "max_pullback_bars", "min_impulse_gain_pct",
    "min_impulse_mean_volume", "min_impulse_body_fraction", "max_pullback_volume_ratio", "max_retracement",
    "tick_size", "max_entry_chase", "target_r", "max_quote_age_seconds", "max_spread", "max_spread_bps",
    "max_float_age_days", "max_catalyst_age_hours", "max_hold_seconds", "trade_start_et", "trade_end_et",
)
SETTING_FIELDS = ("min_price", "max_price", "min_gain_pct", "min_rvol", "max_float_exclusive",
                  "rvol_sessions", "max_float_age_days", "max_catalyst_age_hours", "max_quote_age_seconds",
                  "trade_start_et", "trade_end_et")


def _setting(value):
    # Only the fixed dataclass fields above reach publication. Scientific
    # notation is permitted for Decimal settings; consumers must parse it as a
    # bounded positive decimal, never assume every setting is an integer.
    if isinstance(value, Decimal):
        value = str(value)
    if type(value) is int:
        if not 0 < value <= 10**18:
            raise ValueError("Selection setting exceeds the bounded contract")
        return value
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("Selection setting exceeds the bounded contract")
    return value


def _nanoseconds_floor(seconds):
    """Floor a positive, at-most-five-second limit without Decimal rounding.

    The integer part after shifting nine decimal places needs at most ten
    digits. Reading only those coefficient digits avoids ambient precision
    and avoids constructing a huge power of ten for tiny scientific values.
    """
    if not seconds.is_finite() or not 0 < seconds <= 5:
        raise ValueError("Freshness limit exceeds the bounded contract")
    _, digits, exponent = seconds.as_tuple()
    whole_digits = len(digits) + exponent + 9
    if whole_digits <= 0:
        return 0
    if whole_digits > 10:
        raise ValueError("Freshness limit exceeds the bounded contract")
    result = 0
    for index in range(whole_digits):
        result = result * 10 + (digits[index] if index < len(digits) else 0)
    return result


def _freshness(source, receipt, *, collected_ns, now_ns, maximum_ns):
    """Exact nanosecond validation, including a stricter strategy age limit."""
    try:
        _, source_ns = timestamp_parts(source)
        _, receipt_ns = timestamp_parts(receipt)
        if not source_ns <= receipt_ns <= collected_ns <= now_ns:
            return "invalid"
        if now_ns - source_ns > maximum_ns or now_ns - receipt_ns > maximum_ns:
            return "stale"
    except (ValueError, TypeError, ArithmeticError):
        return "invalid"
    return "fresh"


def _price_review(row, cfg, *, collected_ns, now_ns, maximum_ns):
    status = row["status"]
    if status != "fresh":
        reason = {"missing": "quote_missing", "stale": "quote_stale"}.get(status, "quote_invalid")
        return {"state": "unavailable", "reason": reason}
    quote = _freshness(row["source_at"], row["received_at"], collected_ns=collected_ns,
                       now_ns=now_ns, maximum_ns=maximum_ns)
    if quote != "fresh":
        return {"state": "unavailable", "reason": "quote_" + quote}
    trade = row["last_trade"]
    if trade is None:
        code = row.get("last_trade_error_code")
        reason = ("trade_invalid" if code in {"trade_invalid", "trade_timestamp_invalid"} else
                  "trade_stale" if code == "trade_stale" else "trade_missing")
        return {"state": "unavailable", "reason": reason}
    if trade["status"] != "fresh":
        return {"state": "unavailable", "reason": "trade_stale"}
    trade_freshness = _freshness(trade["source_at"], trade["received_at"], collected_ns=collected_ns,
                               now_ns=now_ns, maximum_ns=maximum_ns)
    if trade_freshness != "fresh":
        return {"state": "unavailable", "reason": "trade_" + trade_freshness}
    # Market Watch already validated finite positive prices and a noncrossed
    # quote. Its values stay there; this report contains no duplicate prices.
    ask_in_range = cfg.min_price <= Decimal(row["ask"]) <= cfg.max_price
    trade_in_range = cfg.min_price <= Decimal(trade["price"]) <= cfg.max_price
    if ask_in_range and trade_in_range:
        return {"state": "pass", "reason": "both_prices_in_range"}
    reason = ("both_prices_outside_range" if not ask_in_range and not trade_in_range else
              "ask_outside_range" if not ask_in_range else "last_trade_outside_range")
    return {"state": "price_outside_range", "reason": reason}


def build_strategy_review(market_watch, config, *, now):
    """Project selection requirements from Market Watch and current settings.

    The publisher supplies its already-sanitized owner/watchlist/feed-scoped
    Market Watch and validated current StrategyConfig. Revalidation here keeps
    this pure function safe for independent callers and later publication times.
    Both row and trade must already be reported fresh; revalidation never
    promotes a prior missing, erroneous or stale observation.
    """
    if not isinstance(config, StrategyConfig):
        raise ValueError("A validated current StrategyConfig is required")
    # Revalidate the actual field values rather than trusting a mutable mapping
    # or an object whose frozen dataclass attributes were bypassed by a caller.
    cfg = StrategyConfig(**asdict(config))
    if not isinstance(market_watch, dict) or not isinstance(market_watch.get("collected_at"), str):
        raise ValueError("A recorded Market Watch observation is required")
    evaluated, now_ns = timestamp_parts(now)
    if isinstance(now, datetime):
        # The publisher heartbeat uses this exact UTC microsecond format. Source
        # timestamps remain untouched in Market Watch, including nanoseconds.
        evaluated = now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    watch = sanitize_market_watch(market_watch, allowed_symbols=market_watch.get("requested_symbols"),
                                  feed=market_watch.get("feed"), now=now,
                                  max_age_seconds=market_watch.get("max_age_seconds"))
    _, collected_ns = timestamp_parts(watch["collected_at"])
    maximum_ns = _nanoseconds_floor(min(cfg.max_quote_age_seconds, Decimal(str(watch["max_age_seconds"]))))
    local = datetime.fromisoformat(evaluated.replace("Z", "+00:00")).astimezone(NY)
    inside = time.fromisoformat(cfg.trade_start_et) <= local.time() < time.fromisoformat(cfg.trade_end_et)
    threshold_changes = [field for field in SOURCE_THRESHOLD_FIELDS
                         if getattr(cfg, field) != SOURCE_THRESHOLDS[field]]
    defaults = StrategyConfig()
    experimental_changes = [field for field in EXPERIMENTAL_FIELDS if getattr(cfg, field) != getattr(defaults, field)]
    settings = {field: _setting(getattr(cfg, field)) for field in SETTING_FIELDS}
    settings.update(rvol_session_start_et="04:00", catalyst_required=True)
    rows = []
    for row in watch["rows"]:
        rows.append({"symbol": row["symbol"], "overall": "incomplete",
                     "price": _price_review(row, cfg, collected_ns=collected_ns, now_ns=now_ns, maximum_ns=maximum_ns),
                     "gain": {"state": "unavailable", "reason": "previous_close_missing"},
                     "rvol": {"state": "unavailable", "reason": "same_clock_history_missing"},
                     "float": {"state": "unavailable", "reason": "point_in_time_float_missing"},
                     "catalyst": {"state": "unavailable", "reason": "catalyst_evidence_missing"}})
    return {"schema_version": SCHEMA_VERSION, "kind": "selection_requirements", "observation_only": True,
            "profile": cfg.profile, "config_id": cfg.config_id,
            "source_basis": {"id": "instructional_2024_selection", "source_ids": ["S10", "S12"],
                             "threshold_fields": list(SOURCE_THRESHOLD_FIELDS), "thresholds_match": not threshold_changes,
                             "differing_threshold_fields": threshold_changes,
                             "experimental_defaults_match": not experimental_changes,
                             "differing_experimental_fields": experimental_changes},
            "settings": settings, "evidence_ref": "market_watch", "collected_at": market_watch["collected_at"],
            "evaluated_at": evaluated, "feed": watch["feed"], "requested_symbols": watch["requested_symbols"],
            "calendar_ref": "market_watch.session", "experimental_window": {
                "timezone": "America/New_York", "date": local.date().isoformat(), "state": "inside" if inside else "outside"},
            "overall": "incomplete", "rows": rows}
