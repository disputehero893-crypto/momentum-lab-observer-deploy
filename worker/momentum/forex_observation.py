"""Opt-in, GET-only OANDA practice forex quote observation.

Not wired into the equity strategy snapshot or an automatic order route.
Prices are practice-account observations, not fills. Live fxTrade hosts are
refused by the practice adapter allowlist.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re

from .activities import timestamp_parts
from .oanda import (
    APPROVED_FOREX_PAIRS, OandaPracticeClient, OandaPracticeDisabled, OandaPracticeError,
    display_pair, instrument_code, parse_forex_watchlist,
)

PRICE = re.compile(r"\d{1,19}(?:\.\d{1,18})?\Z")


def _pairs(value):
    if not isinstance(value, (tuple, list)) or not 1 <= len(value) <= 10:
        raise ValueError("Expected unique approved forex pairs")
    pairs = tuple(display_pair(p) for p in value)
    if any(p not in APPROVED_FOREX_PAIRS for p in pairs) or len(set(pairs)) != len(pairs):
        raise ValueError("Expected unique approved forex pairs")
    return pairs


def _price(value):
    if type(value) not in (str, Decimal):
        raise ValueError("Expected exact decimal price")
    raw = str(value)
    if len(raw) > 38 or PRICE.fullmatch(raw) is None:
        raise ValueError("Invalid forex quote")
    try:
        number = Decimal(raw)
        if not number.is_finite() or number <= 0 or number > Decimal("1e18"):
            raise ValueError("Invalid quote")
        fixed = format(number, "f")
        return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    except (InvalidOperation, ValueError, ArithmeticError, OverflowError):
        raise ValueError("Invalid forex quote") from None


def _row(pair, status, *, bid=None, ask=None, source_at=None, received_at=None, error_code=None):
    return {"pair": pair, "status": status, "bid": bid, "ask": ask,
            "source_at": source_at, "received_at": received_at, "error_code": error_code}


def _side_prices(value, name):
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ValueError("Invalid quote ladder")
    amounts = []
    for level in value:
        if not isinstance(level, dict):
            raise ValueError("Invalid price level")
        amounts.append(Decimal(_price(level.get("price"))))
    return format(max(amounts) if name == "bids" else min(amounts), "f")


def build_forex_watch(raw, pairs, *, received_at, collected_at, max_age_seconds=60):
    """Project a practice pricing response into a bounded dashboard envelope."""
    pairs = _pairs(pairs)
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 120:
        raise ValueError("Invalid forex freshness bound")
    receipt, receipt_ns = timestamp_parts(received_at)
    collected, collected_ns = timestamp_parts(collected_at)
    if receipt_ns > collected_ns:
        raise ValueError("Receipt is after collection")
    entries = raw.get("prices") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or len(entries) > 50:
        entries = None
    indexed, duplicates = {}, set()
    if entries is not None:
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("instrument"), str):
                continue
            instrument = entry["instrument"]
            if instrument in indexed:
                duplicates.add(instrument)
            indexed[instrument] = entry
    rows = []
    for pair in pairs:
        instrument = instrument_code(pair)
        if entries is None:
            rows.append(_row(pair, "error", received_at=receipt, error_code="market_data_unavailable"))
            continue
        if instrument in duplicates:
            rows.append(_row(pair, "error", received_at=receipt, error_code="quote_invalid"))
            continue
        value = indexed.get(instrument)
        if value is None:
            rows.append(_row(pair, "missing", received_at=receipt, error_code="pair_missing"))
            continue
        if value.get("tradeable") is not True:
            rows.append(_row(pair, "missing", received_at=receipt, error_code="quote_missing"))
            continue
        try:
            bid = _side_prices(value.get("bids"), "bids")
            ask = _side_prices(value.get("asks"), "asks")
            if Decimal(bid) > Decimal(ask):
                raise ValueError("Crossed price")
        except (ValueError, TypeError, ArithmeticError):
            rows.append(_row(pair, "error", received_at=receipt, error_code="quote_invalid"))
            continue
        try:
            source, source_ns = timestamp_parts(value.get("time"))
            if source_ns > receipt_ns:
                raise ValueError("Future price")
        except (ValueError, TypeError, ArithmeticError):
            rows.append(_row(pair, "error", received_at=receipt, error_code="quote_timestamp_invalid"))
            continue
        stale = collected_ns - source_ns > max_age_seconds * 1_000_000_000
        rows.append(_row(pair, "stale" if stale else "fresh",
                         bid=bid, ask=ask, source_at=source, received_at=receipt,
                         error_code="quote_stale" if stale else None))
    return {"schema_version": "1.0", "asset_class": "forex",
            "source": "oanda_practice", "observation_only": True,
            "execution_enabled": False, "collected_at": collected,
            "max_age_seconds": max_age_seconds,
            "requested_pairs": list(pairs), "rows": rows}


def sanitize_forex_watch(report, *, allowed_pairs, now, max_age_seconds=60):
    """Revalidate a report at publication. Never promote a stale quote to fresh."""
    pairs = _pairs(allowed_pairs)
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 120:
        raise ValueError("Invalid configured forex freshness")
    _, now_ns = timestamp_parts(now)
    if (not isinstance(report, dict) or report.get("schema_version") != "1.0"
            or report.get("asset_class") != "forex"
            or report.get("source") != "oanda_practice"
            or report.get("observation_only") is not True
            or report.get("execution_enabled") is not False
            or report.get("max_age_seconds") != max_age_seconds
            or report.get("requested_pairs") != list(pairs)
            or not isinstance(report.get("rows"), list)
            or len(report["rows"]) != len(pairs)):
        raise ValueError("Invalid forex observation envelope")
    collected, collected_ns = timestamp_parts(report.get("collected_at"))
    if collected_ns > now_ns:
        raise ValueError("Future forex collection")
    max_ns = max_age_seconds * 1_000_000_000
    rows = []
    for expected, item in zip(pairs, report["rows"]):
        if (not isinstance(item, dict) or item.get("pair") != expected
                or item.get("status") not in {"fresh", "stale", "missing", "error"}
                or item.get("error_code") not in {
                    None, "pair_missing", "pair_invalid", "quote_missing",
                    "quote_invalid", "quote_timestamp_invalid", "quote_stale",
                    "market_data_unavailable"}):
            raise ValueError("Invalid forex row identity or status")
        status, error = item["status"], item.get("error_code")
        received_at = item.get("received_at")
        received, receipt_ns = (None, None) if received_at is None else timestamp_parts(received_at)
        if receipt_ns is not None and receipt_ns > collected_ns:
            raise ValueError("Invalid forex receipt order")
        bid, ask, source_at = item.get("bid"), item.get("ask"), item.get("source_at")
        if status in {"fresh", "stale"}:
            if (status == "fresh" and error is not None
                    or status == "stale" and error != "quote_stale"):
                raise ValueError("Invalid forex freshness error code")
            bid, ask = _price(bid), _price(ask)
            if Decimal(bid) > Decimal(ask):
                raise ValueError("Crossed forex quote")
            source, source_ns = timestamp_parts(source_at)
            if receipt_ns is None or source_ns > receipt_ns:
                raise ValueError("Invalid forex source/receipt order")
            stale = now_ns - source_ns > max_ns
            if stale:
                status, error = "stale", "quote_stale"
            rows.append(_row(expected, status, bid=bid, ask=ask, source_at=source,
                             received_at=received, error_code=error))
        else:
            if status == "missing" and error not in {"pair_missing", "pair_invalid", "quote_missing"}:
                raise ValueError("Invalid forex missing code")
            if status == "error" and error not in {"quote_invalid", "quote_timestamp_invalid",
                                                   "market_data_unavailable"}:
                raise ValueError("Invalid forex error code")
            if any(v is not None for v in (bid, ask, source_at)):
                raise ValueError("Error forex rows cannot carry prices")
            rows.append(_row(expected, status, received_at=received, error_code=error))
    return {"schema_version": "1.0", "asset_class": "forex",
            "source": "oanda_practice", "observation_only": True,
            "execution_enabled": False, "collected_at": collected,
            "max_age_seconds": max_age_seconds,
            "requested_pairs": list(pairs), "rows": rows}


def forex_watch_summary(report):
    """Journal-safe summary: pair names and statuses only, never prices or tokens."""
    if not isinstance(report, dict):
        return None
    rows = report.get("rows") if isinstance(report.get("rows"), list) else []
    return {"source": "oanda_practice", "observation_only": True, "execution_enabled": False,
            "requested_pairs": list(report.get("requested_pairs") or []),
            "statuses": [row.get("status") if isinstance(row, dict) else "error" for row in rows]}


class ForexWatchCollector:
    """One bounded practice pricing GET; equity cycles continue if this fails."""

    def __init__(self, provider, pairs=("EUR/USD", "GBP/USD", "USD/JPY"), *,
                 clock=None, max_age_seconds=60):
        self.provider = provider
        self.pairs = _pairs(pairs)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 120:
            raise ValueError("Invalid max age")
        self.max_age_seconds = max_age_seconds

    def collect(self):
        try:
            raw = self.provider.pricing(self.pairs) if hasattr(self.provider, "pricing") else self.provider.latest_prices(self.pairs)
        except (OandaPracticeError, OandaPracticeDisabled):
            raw = None
        received = self.clock()
        collected = self.clock()
        return build_forex_watch(raw, self.pairs, received_at=received,
                                 collected_at=collected, max_age_seconds=self.max_age_seconds)
