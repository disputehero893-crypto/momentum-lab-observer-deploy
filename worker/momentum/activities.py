"""Bounded, read-only Alpaca Trading API activity import.

References checked 2026-10-18 (Trading API, not the Broker API SSE):
https://docs.alpaca.markets/us/reference/getaccountactivities-2
https://docs.alpaca.markets/us/docs/account-activities
https://docs.alpaca.markets/us/docs/paper-trading

``after``/``until`` filter activity *creation*, while a FILL supplies its actual
``transaction_time`` and an NTA ``date`` can be the activity/settlement date.
Opaque activity IDs are cursors, never clocks. A short page is not terminal:
the final ID is sent again until an empty page is observed. Even that exhaustion
is an observation of this REST scan, not a transactionally consistent snapshot
or proof that late fees/corrections cannot arrive. Re-scan overlapping/full
history and reconcile independently before accepting financial completeness.

Paper trading omits regulatory fees. This module therefore never certifies
actual fee completeness or turns an empty fee stream into a zero-cost claim.
Broker Activity SSE correction/bust semantics are deliberately not projected
onto Trading REST: correction markers and unknown events remain blockers.

The per-request HTTP timeout is 12 seconds in PaperClient. The scan checks an
additional elapsed-time budget before and after every request; that budget does
not interrupt an in-flight request. No partial result is returned after any
timeout, malformed page, repeated ID, conflict or cap. No API secret, free-text
description or raw provider payload is included in normalized observations.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
import time
from typing import Any

from .alpaca import BrokerError
from .config import SYMBOL

SOURCE = "alpaca_account_activities"
MAX_PAGE_BYTES = 1_000_000
MAX_ACTIVITY_BYTES = 16_384
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,255}\Z")
_TYPE = re.compile(r"[A-Z][A-Z0-9_.]{0,31}\Z")
_RFC3339 = re.compile(
    r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})\Z"
)
_DECIMAL = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)\Z")
_CORRECTION_FIELDS = ("previous_id", "previous_execution_id", "correction_id", "replaces_id")
_CORRECTION_TYPES = {"trade_correct", "trade_bust", "correction", "correct", "bust", "reversal"}


class ActivityImportError(BrokerError):
    """Fixed error codes are safe to record; broker payloads never appear here."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ActivityImportError("invalid_activity_json") from None


def _identifier(value, field="activity_id"):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ActivityImportError("invalid_" + field)
    return value


def timestamp_parts(value):
    """Return canonical UTC RFC3339 and exact nanoseconds; reject naive times.

    Python datetime silently drops digits after microseconds. Preserve all nine
    RFC3339 fractional digits instead of reordering executions that share one
    microsecond. Calendar dates are not accepted as execution timestamps.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("A timezone-aware timestamp is required")
        value = value.isoformat()
    if not isinstance(value, str):
        raise ValueError("A timezone-aware RFC3339 timestamp is required")
    match = _RFC3339.fullmatch(value)
    if not match or match[4] == "-00:00":
        raise ValueError("A timezone-aware RFC3339 timestamp is required")
    base = datetime.fromisoformat(match[1] + "T" + match[2] + match[4].replace("Z", "+00:00"))
    base = base.astimezone(timezone.utc)
    fraction = (match[3] or "").rstrip("0")
    normalized = (f"{base.year:04d}-{base.month:02d}-{base.day:02d}T"
                  f"{base.hour:02d}:{base.minute:02d}:{base.second:02d}"
                  + ("." + fraction if fraction else "") + "Z")
    delta = base - datetime(1970, 1, 1, tzinfo=timezone.utc)
    nanoseconds = (delta.days * 86400 + delta.seconds) * 1_000_000_000 + int(fraction.ljust(9, "0") or "0")
    return normalized, nanoseconds


def activity_request_params(*, after, until, page_token=None, page_size=100):
    """Validate the only allowed activities query before any credential use."""
    start, start_ns = timestamp_parts(after)
    end, end_ns = timestamp_parts(until)
    if start_ns >= end_ns:
        raise ValueError("Activity history bounds must increase")
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise ValueError("Activity page_size must be an integer from 1 to 100")
    params = {"after": start, "until": end, "direction": "asc", "page_size": page_size}
    if page_token is not None:
        try:
            params["page_token"] = _identifier(page_token, "activity_cursor")
        except ActivityImportError:
            raise ValueError("Invalid activity cursor") from None
    return params


def validate_activity_page(page, *, page_size=100):
    if not isinstance(page, list):
        raise ActivityImportError("activities_response_not_array")
    if len(page) > page_size:
        raise ActivityImportError("activities_page_exceeds_requested_size")
    if len(_canonical(page)) > MAX_PAGE_BYTES:
        raise ActivityImportError("activities_page_payload_limit")
    for row in page:
        if not isinstance(row, dict):
            raise ActivityImportError("activity_not_object")
        if len(_canonical(row)) > MAX_ACTIVITY_BYTES:
            raise ActivityImportError("activity_payload_limit")
        _identifier(row.get("id"))
    return page


def _number(value, *, positive=False, nonnegative=False):
    # Broker decimals are strings. Integer fixtures are lossless; binary float
    # inputs are rejected so rounding cannot disappear at the ledger boundary.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ActivityImportError("invalid_activity_decimal")
    raw = str(value)
    if len(raw) > 80 or not _DECIMAL.fullmatch(raw):
        raise ActivityImportError("invalid_activity_decimal")
    try:
        result = Decimal(raw)
    except InvalidOperation:
        raise ActivityImportError("invalid_activity_decimal") from None
    if not result.is_finite() or result.copy_abs() > Decimal("1e18") or result.as_tuple().exponent < -18:
        raise ActivityImportError("activity_decimal_out_of_range")
    if (positive and result <= 0) or (nonnegative and result < 0):
        raise ActivityImportError("activity_decimal_out_of_range")
    # Decimal.normalize()/unary minus can round under the process context.
    # Formatting and trimming preserve every accepted decimal digit.
    fixed = format(result, "f")
    return "0" if not result else (fixed.rstrip("0").rstrip(".") if "." in fixed else fixed)


def _reported_date(value):
    if not isinstance(value, str):
        raise ActivityImportError("invalid_activity_date")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value).isoformat()
        # Date/time-shaped NTA dates still retain the provider's local calendar
        # date. Their timestamp is not promoted to a trade execution time.
        timestamp_parts(value)
        return date.fromisoformat(value[:10]).isoformat()
    except (ValueError, OverflowError):
        raise ActivityImportError("invalid_activity_date") from None


@dataclass(frozen=True)
class ActivityObservation:
    source_id: str
    activity_type: str
    kind: str
    source_fingerprint: str
    record: dict[str, Any] | None
    blockers: tuple[str, ...] = ()
    reported_date: str | None = None
    cash_change: str | None = None

    def to_dict(self):
        result = {"source_id": self.source_id, "activity_type": self.activity_type,
                  "kind": self.kind, "source_fingerprint": self.source_fingerprint,
                  "blockers": list(self.blockers)}
        if self.reported_date is not None:
            result["reported_date"] = self.reported_date
        if self.cash_change is not None:
            result["cash_change"] = self.cash_change
        return result


def normalize_activity(row):
    """Normalize one observation; unsupported/correcting rows remain visible.

    No order cumulative quantity is interpreted as a new fill, no cash deposit
    becomes profit, and no fee is attached to a guessed execution or trade day.
    Unknown provider fields affect source_fingerprint, making later mutations
    visible to the ledger's same-ID/revision conflict detection.
    """
    validate_activity_page([row], page_size=1)
    source_id = _identifier(row.get("id"))
    activity_type = row.get("activity_type")
    if not isinstance(activity_type, str) or not _TYPE.fullmatch(activity_type):
        raise ActivityImportError("invalid_activity_type")
    digest = sha256(_canonical(row)).hexdigest()
    base = {"source_id": source_id, "revision": 1, "status": "active", "source_fingerprint": digest}
    details = row.get("details")
    nested = details if isinstance(details, dict) else {}
    marker_values = [row.get("type"), row.get("execution_type"), row.get("event"), nested.get("execution_type")]
    correcting = any(isinstance(value, str) and value.lower() in _CORRECTION_TYPES for value in marker_values)
    correcting = correcting or any(row.get(key) not in (None, "") or nested.get(key) not in (None, "")
                                   for key in _CORRECTION_FIELDS)
    if correcting:
        return ActivityObservation(source_id, activity_type, "correction", digest, None,
                                   ("activity_correction_requires_review",))
    if row.get("currency", "USD") != "USD":
        return ActivityObservation(source_id, activity_type, "unsupported", digest, None,
                                   ("unsupported_activity_currency",))
    if activity_type == "FILL":
        if not isinstance(row.get("type"), str) or row["type"] not in {"fill", "partial_fill"}:
            return ActivityObservation(source_id, activity_type, "unsupported", digest, None,
                                       ("unsupported_fill_execution_type",))
        if (not isinstance(row.get("symbol"), str) or not SYMBOL.fullmatch(row["symbol"])
                or row.get("asset_class", "us_equity") != "us_equity"
                or not isinstance(row.get("side"), str) or row["side"] not in {"buy", "sell"}):
            return ActivityObservation(source_id, activity_type, "unsupported", digest, None,
                                       ("unsupported_fill_asset_or_side",))
        quantity = _number(row.get("qty"), positive=True)
        price = _number(row.get("price"), positive=True)
        if "cum_qty" in row and Decimal(_number(row["cum_qty"], nonnegative=True)) < Decimal(quantity):
            raise ActivityImportError("activity_cumulative_quantity_below_fill")
        if "leaves_qty" in row:
            _number(row["leaves_qty"], nonnegative=True)
        try:
            stamp, _ = timestamp_parts(row.get("transaction_time"))
        except (ValueError, OverflowError):
            raise ActivityImportError("invalid_fill_execution_time") from None
        record = {**base, "kind": "fill", "effective_at": stamp, "symbol": row["symbol"],
                  "side": row["side"], "quantity": quantity, "price": price,
                  "order_id": _identifier(row.get("order_id"), "activity_order_id")}
        return ActivityObservation(source_id, activity_type, "fill", digest, record)
    if activity_type == "FEE":
        day = _reported_date(row.get("date"))
        cash_change = _number(row.get("net_amount"))
        signed_cost = _number(format(Decimal(cash_change).copy_negate(), "f"))
        record = {**base, "kind": "fee", "amount": signed_cost, "effective_date": day,
                  "date_basis": "provider_activity_or_settlement_date"}
        return ActivityObservation(source_id, activity_type, "fee", digest, record,
                                   (), day, cash_change)
    if activity_type in {"CSD", "CSW"}:
        day = _reported_date(row.get("date"))
        cash_change = _number(row.get("net_amount"))
        if (activity_type == "CSD" and Decimal(cash_change) < 0
                or activity_type == "CSW" and Decimal(cash_change) > 0):
            raise ActivityImportError("cash_activity_sign_mismatch")
        return ActivityObservation(source_id, activity_type, "cash", digest, None, (), day, cash_change)
    return ActivityObservation(source_id, activity_type, "unsupported", digest, None,
                               ("unsupported_account_activity",))


@dataclass(frozen=True)
class ActivityImport:
    records: tuple[dict[str, Any], ...]
    observations: tuple[ActivityObservation, ...]
    coverage: dict[str, Any]

    def to_dict(self):
        return {"records": list(self.records), "observations": [row.to_dict() for row in self.observations],
                "coverage": dict(self.coverage)}

    @property
    def batch_id(self):
        return "alpaca-activities-" + sha256(_canonical(self.to_dict())).hexdigest()


def fetch_activities(client, *, account_id, after, until, max_pages=100,
                     max_records=10_000, max_elapsed_seconds=30, monotonic=None):
    """Fetch an entire bounded observation, or raise without a partial result.

    ``account_id`` is the verified account identity from the calling integration;
    the caller must pin/reconcile that identity using the same credentials. This
    function neither fetches credentials nor claims an opening lot baseline.
    Result records keep provider order. Regressing execution time is a blocker,
    never repaired by silently sorting an inconsistent scan.
    """
    _identifier(account_id, "account_id")
    params = activity_request_params(after=after, until=until)
    for value, maximum in ((max_pages, 1000), (max_records, 100_000)):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError("Activity scan caps must be positive bounded integers")
    if isinstance(max_elapsed_seconds, bool) or not isinstance(max_elapsed_seconds, (int, float)):
        raise ValueError("Activity scan timeout must be a finite number")
    if not 0 < max_elapsed_seconds <= 300:
        raise ValueError("Activity scan timeout must be greater than zero and at most 300 seconds")
    requested_at, request_ns = timestamp_parts(client.clock())
    if timestamp_parts(params["until"])[1] > request_ns:
        raise ValueError("Activity history cannot end in the future")
    elapsed_clock = monotonic or time.monotonic
    started = elapsed_clock()
    cursor, observations, seen = None, [], {}
    blockers = set()
    last_fill_ns = None
    pages = 0
    while pages < max_pages:
        if elapsed_clock() - started >= max_elapsed_seconds:
            raise ActivityImportError("activities_scan_deadline_exceeded")
        page = client.activity_page(after=params["after"], until=params["until"], page_token=cursor, page_size=100)
        pages += 1
        if elapsed_clock() - started >= max_elapsed_seconds:
            raise ActivityImportError("activities_scan_deadline_exceeded")
        # Injected providers are held to the same constraints as PaperClient.
        validate_activity_page(page)
        if not page:
            completed_at, completion_ns = timestamp_parts(client.clock())
            if completion_ns < request_ns:
                raise ActivityImportError("activities_observation_clock_regressed")
            rows = tuple(row.record for row in observations if row.record is not None)
            coverage = {
                "account_id": account_id, "environment": "paper", "source": SOURCE,
                "window_start": params["after"], "window_end": params["until"],
                # Use the frozen upper bound, never completion time, for freshness.
                "as_of": params["until"], "requested_at": requested_at, "completed_at": completed_at,
                "executions_complete": False, "fees_complete": False,
                "opening_lots_complete": False, "opening_lots": [],
                "activity_pages_exhausted": True, "page_count": pages,
                "activity_count": len(observations), "ledger_record_count": len(rows),
                "last_activity_id": cursor, "query_time_basis": "activity_creation_time",
                "terminal_observation": "empty_page_after_exclusive_id_cursor" if cursor else "empty_first_page",
                "evidence": [
                    "All-types Trading API REST scan used fixed creation-time bounds, asc and exclusive activity-ID cursors.",
                    "An empty page was observed; this is not a transactionally consistent or financially final snapshot.",
                    "https://docs.alpaca.markets/us/reference/getaccountactivities-2",
                    "Paper trading omits regulatory fees: https://docs.alpaca.markets/us/docs/paper-trading",
                ],
                # Missing baseline/execution/cost completeness is represented by
                # the flags above. A caller may establish those independently;
                # explicit normalization anomalies must remain hard blockers.
                "blockers": sorted(blockers),
            }
            return ActivityImport(rows, tuple(observations), coverage)
        if len(observations) + len(page) > max_records:
            raise ActivityImportError("activities_record_cap_reached")
        for row in page:
            if "account_id" in row and row["account_id"] != account_id:
                raise ActivityImportError("activity_account_identity_mismatch")
            observation = normalize_activity(row)
            if observation.source_id in seen:
                code = ("activity_identity_content_conflict" if seen[observation.source_id] != observation.source_fingerprint
                        else "repeated_activity_id_or_cursor")
                raise ActivityImportError(code)
            seen[observation.source_id] = observation.source_fingerprint
            blockers.update(observation.blockers)
            if observation.kind == "fill":
                current_fill_ns = timestamp_parts(observation.record["effective_at"])[1]
                if last_fill_ns is not None and current_fill_ns < last_fill_ns:
                    blockers.add("activity_execution_order_regressed")
                last_fill_ns = current_fill_ns
            observations.append(observation)
        cursor = observations[-1].source_id
    raise ActivityImportError("activities_page_cap_reached")
