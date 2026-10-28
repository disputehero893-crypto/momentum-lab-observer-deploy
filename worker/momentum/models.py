"""Small, dependency-free contracts for the experimental paper strategy.

All prices use Decimal; all event times are timezone-aware and normalized to UTC.
This module contains no execution or network capability.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any, Mapping
from zoneinfo import ZoneInfo

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
PROFILE = "experimental_v1"


class InputError(ValueError):
    """A stable audit reason, optionally accompanied by the invalid field name."""

    def __init__(self, code: str, field_name: str = "") -> None:
        self.code = code
        self.field_name = field_name
        super().__init__(f"{code}: {field_name}" if field_name else code)


def decimal_value(value: Any, name: str, *, minimum: Decimal | None = None) -> Decimal:
    if value is None or isinstance(value, bool):
        raise InputError("missing_or_invalid_number", name)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise InputError("missing_or_invalid_number", name) from None
    if not result.is_finite() or abs(result) > Decimal("1e18"):
        raise InputError("nonfinite_or_out_of_range_number", name)
    if minimum is not None and result < minimum:
        raise InputError("number_below_minimum", name)
    return result


def positive_decimal(value: Any, name: str) -> Decimal:
    result = decimal_value(value, name)
    if result <= 0:
        raise InputError("number_must_be_positive", name)
    return result


def integer_value(value: Any, name: str, *, minimum: int = 0) -> int:
    result = decimal_value(value, name)
    if result != result.to_integral_value() or result < minimum:
        raise InputError("invalid_integer", name)
    return int(result)


def timestamp(value: Any, name: str = "timestamp") -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise InputError("missing_or_invalid_timestamp", name) from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise InputError("naive_timestamp", name)
    return result.astimezone(UTC)


def session_date(value: Any, name: str = "session_date") -> date:
    try:
        if isinstance(value, datetime):
            raise ValueError
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise InputError("invalid_session_date", name) from None


def plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def config_fingerprint(config: Any) -> str:
    """Hash the complete validated config, for replay and rule-change audits."""
    encoded = json.dumps(plain(asdict(config)), sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Decision:
    accepted: bool
    reasons: tuple[str, ...] = ()
    intent: dict[str, Any] | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    profile: str = PROFILE

    def to_dict(self) -> dict[str, Any]:
        return plain({"accepted": self.accepted, "reasons": self.reasons,
                      "intent": self.intent, "metrics": self.metrics, "profile": self.profile})

    @classmethod
    def reject(cls, *reasons: str, metrics: dict[str, Any] | None = None) -> "Decision":
        return cls(False, tuple(reasons), metrics=metrics or {})

    @classmethod
    def invalid(cls, error: InputError) -> "Decision":
        return cls.reject(error.code, metrics={"field": error.field_name})


@dataclass(frozen=True)
class Bar:
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    feed: str
    available_at: datetime

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=60)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], feed: str) -> "Bar":
        if not isinstance(value, Mapping):
            raise InputError("invalid_bar")
        if value.get("complete") is not True:
            raise InputError("bar_not_complete")
        start = timestamp(value.get("start"), "bar.start")
        if start.second or start.microsecond:
            raise InputError("bar_not_minute_aligned")
        if value.get("feed") != feed:
            raise InputError("mixed_feed", "bar.feed")
        prices = [positive_decimal(value.get(key), f"bar.{key}") for key in ("open", "high", "low", "close")]
        op, hi, lo, cl = prices
        if not lo <= min(op, cl) <= max(op, cl) <= hi:
            raise InputError("invalid_ohlc")
        end = start + timedelta(seconds=60)
        # Omitted availability is only an adapter assertion, not measured latency.
        available = timestamp(value.get("available_at", end), "bar.available_at")
        if available < end:
            raise InputError("bar_available_before_close")
        return cls(start, op, hi, lo, cl, integer_value(value.get("volume"), "bar.volume"), feed, available)


@dataclass(frozen=True)
class Quote:
    bid: Decimal
    ask: Decimal
    timestamp: datetime
    received_at: datetime
    feed: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], feed: str | None = None) -> "Quote":
        if not isinstance(value, Mapping):
            raise InputError("invalid_quote")
        bid = positive_decimal(value.get("bid"), "quote.bid")
        ask = positive_decimal(value.get("ask"), "quote.ask")
        if bid > ask:
            raise InputError("crossed_quote")
        event_time = timestamp(value.get("timestamp"), "quote.timestamp")
        received = timestamp(value.get("received_at"), "quote.received_at")
        if received < event_time:
            raise InputError("quote_received_before_event")
        quote_feed = value.get("feed")
        if not isinstance(quote_feed, str) or not quote_feed:
            raise InputError("missing_feed", "quote.feed")
        if feed is not None and quote_feed != feed:
            raise InputError("mixed_feed", "quote.feed")
        return cls(bid, ask, event_time, received, quote_feed)

    def check_time(self, now: datetime, maximum_age_seconds: Decimal) -> None:
        if self.timestamp > now or self.received_at > now:
            raise InputError("future_quote")
        if Decimal(str((now - self.timestamp).total_seconds())) > maximum_age_seconds:
            raise InputError("stale_quote")
        if Decimal(str((now - self.received_at).total_seconds())) > maximum_age_seconds:
            raise InputError("stale_quote")
