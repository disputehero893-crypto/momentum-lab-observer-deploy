"""Bounded, read-only watchlist observations; no strategy or order capability.

One current-day calendar GET (cached at most 300 seconds) and one snapshot GET
are sufficient. Historical bars, operator facts and accounting are deliberately
absent from this path. A successful HTTP response proves receipt, not freshness.

The calendar describes the REGULAR session only. Its closure says nothing about
extended/overnight eligibility. Quote and trade source timestamps retain their
provider nanosecond precision; receipt timestamps come from the worker clock.
The source <= receipt <= collection <= publication ordering is checked exactly.

Official endpoint contracts checked 2026-10-18:
https://docs.alpaca.markets/us/reference/legacycalendar
https://docs.alpaca.markets/us/reference/stocksnapshots-1
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
import math
import re

from .activities import timestamp_parts
from .config import SYMBOL
from .models import NY


SCHEMA_VERSION = "1.0"
MAX_SYMBOLS = 10
CALENDAR_MAX_AGE_SECONDS = 300
SOURCE = "alpaca_market_data"
CALENDAR_SOURCE = "alpaca_calendar"
ROW_ERRORS = frozenset({"symbol_missing", "symbol_invalid", "quote_missing", "quote_invalid",
                        "quote_timestamp_invalid", "quote_stale", "market_data_unavailable"})
TRADE_ERRORS = frozenset({"trade_missing", "trade_invalid", "trade_timestamp_invalid", "trade_stale"})
SESSION_ERRORS = frozenset({"calendar_unavailable", "calendar_invalid", "calendar_stale"})
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
_NANOSECOND = 1_000_000_000


def _instant(value):
    if not isinstance(value, (str, datetime)) or isinstance(value, str) and len(value) > 64:
        raise ValueError("Invalid market observation timestamp")
    return timestamp_parts(value)


def _datetime(value):
    # Only calendar-date conversion uses datetime's microsecond resolution.
    # All source/receipt comparisons use the exact integer nanoseconds above.
    return datetime.fromisoformat(_instant(value)[0].replace("Z", "+00:00"))


def _maximum(value):
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 < value <= 5:
        raise ValueError("Quote age must be positive and at most five seconds")
    return float(value)


def _allowlist(value):
    if (not isinstance(value, (list, tuple)) or len(value) > MAX_SYMBOLS
            or any(not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol) for symbol in value)
            or len(set(value)) != len(value)):
        raise ValueError("A bounded, unique configured watchlist is required")
    return tuple(value)


def _price(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Invalid observed price")
    raw = str(value)
    if len(raw) > 64:
        raise ValueError("Invalid observed price")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise ValueError("Invalid observed price") from None
    if (not number.is_finite() or number <= 0 or number > Decimal("1e18")
            or not -18 <= number.as_tuple().exponent <= 18):
        raise ValueError("Invalid observed price")
    fixed = format(number, "f")
    fixed = fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    if len(fixed) > 64:
        raise ValueError("Invalid observed price")
    return fixed


def _empty_row(symbol, feed, code, *, received_at=None):
    return {"symbol": symbol, "feed": feed,
            "status": "missing" if code in {"symbol_missing", "quote_missing"} else "error",
            "bid": None, "ask": None, "source_at": None, "received_at": received_at,
            "error_code": code, "last_trade": None, "last_trade_error_code": "trade_missing"}


def _row(value, *, symbol, feed, now_ns, collected_ns, maximum):
    """Revalidate one projected row without copying arbitrary provider fields."""
    if not isinstance(value, dict) or value.get("symbol") != symbol or value.get("feed") != feed:
        return _empty_row(symbol, feed, "symbol_invalid")
    receipt, receipt_ns = None, None
    try:
        if value.get("received_at") is not None:
            receipt, receipt_ns = _instant(value["received_at"])
            if receipt_ns > collected_ns or receipt_ns > now_ns:
                raise ValueError
    except (ValueError, TypeError, ArithmeticError):
        return _empty_row(symbol, feed, "quote_timestamp_invalid")
    incoming = value.get("status")
    if incoming is not None and (not isinstance(incoming, str) or incoming not in {"fresh", "stale", "missing", "error"}):
        return _empty_row(symbol, feed, "quote_invalid", received_at=receipt)
    code = value.get("error_code")
    if incoming in {"missing", "error"}:
        code = code if isinstance(code, str) and code in ROW_ERRORS else "quote_invalid"
        result = _empty_row(symbol, feed, code, received_at=receipt)
    else:
        try:
            bid, ask = _price(value.get("bid")), _price(value.get("ask"))
            if Decimal(bid) > Decimal(ask):
                raise ValueError
        except (ValueError, TypeError, ArithmeticError):
            result = _empty_row(symbol, feed, "quote_invalid", received_at=receipt)
        else:
            try:
                source, source_ns = _instant(value.get("source_at"))
                if receipt_ns is None or source_ns > receipt_ns:
                    raise ValueError
            except (ValueError, TypeError, ArithmeticError):
                result = _empty_row(symbol, feed, "quote_timestamp_invalid", received_at=receipt)
            else:
                max_ns = int(Decimal(str(maximum)) * _NANOSECOND)
                fresh = (incoming != "stale" and now_ns - source_ns <= max_ns
                         and now_ns - receipt_ns <= max_ns)
                result = {"symbol": symbol, "feed": feed, "status": "fresh" if fresh else "stale",
                          "bid": bid, "ask": ask, "source_at": source, "received_at": receipt,
                          "error_code": None if fresh else "quote_stale", "last_trade": None,
                          "last_trade_error_code": "trade_missing"}
    trade = value.get("last_trade")
    if trade is None:
        error = value.get("last_trade_error_code")
        if isinstance(error, str) and error in TRADE_ERRORS:
            result["last_trade_error_code"] = error
    elif (not isinstance(trade, dict) or trade.get("status") is not None
          and (not isinstance(trade.get("status"), str) or trade["status"] not in {"fresh", "stale"})):
        result["last_trade_error_code"] = "trade_invalid"
    else:
        try:
            price = _price(trade.get("price"))
        except (ValueError, TypeError, ArithmeticError):
            result["last_trade_error_code"] = "trade_invalid"
        else:
            try:
                source, source_ns = _instant(trade.get("source_at"))
                received, received_ns = _instant(trade.get("received_at"))
                if not source_ns <= received_ns <= collected_ns <= now_ns:
                    raise ValueError
            except (ValueError, TypeError, ArithmeticError):
                result["last_trade_error_code"] = "trade_timestamp_invalid"
            else:
                max_ns = int(Decimal(str(maximum)) * _NANOSECOND)
                fresh = (trade.get("status") != "stale" and now_ns - source_ns <= max_ns
                         and now_ns - received_ns <= max_ns)
                result["last_trade"] = {"price": price, "source_at": source, "received_at": received,
                                        "status": "fresh" if fresh else "stale"}
                result["last_trade_error_code"] = None if fresh else "trade_stale"
    return result


def _unknown_session(now, code="calendar_unavailable", *, checked_at=None):
    return {"scope": "regular", "status": "unknown", "date": _datetime(now).astimezone(NY).date().isoformat(),
            "timezone": "America/New_York", "is_trading_day": None, "open_at": None, "close_at": None,
            "checked_at": checked_at, "max_age_seconds": CALENDAR_MAX_AGE_SECONDS,
            "source": CALENDAR_SOURCE, "error_code": code}


def _session(value, *, now, collected_ns):
    now_stamp, now_ns = _instant(now)
    result = _unknown_session(now_stamp, "calendar_invalid")
    if not isinstance(value, dict):
        return result
    if (value.get("scope") != "regular" or value.get("source") != CALENDAR_SOURCE
            or value.get("timezone") != "America/New_York"):
        return result
    if value.get("is_trading_day") is None:
        error = value.get("error_code")
        result["error_code"] = error if isinstance(error, str) and error in SESSION_ERRORS else "calendar_invalid"
        return result
    try:
        checked, checked_ns = _instant(value.get("checked_at"))
        if checked_ns > collected_ns or checked_ns > now_ns:
            raise ValueError
        result["checked_at"] = checked
        if (value.get("date") != result["date"]
                or now_ns - checked_ns > CALENDAR_MAX_AGE_SECONDS * _NANOSECOND):
            result["error_code"] = "calendar_stale"
            return result
        trading_day = value.get("is_trading_day")
        if type(trading_day) is not bool:
            raise ValueError
        if trading_day:
            opened, opened_ns = _instant(value.get("open_at"))
            closed, closed_ns = _instant(value.get("close_at"))
            if (opened_ns >= closed_ns or any(_datetime(stamp).astimezone(NY).date().isoformat() != result["date"]
                                              for stamp in (opened, closed))):
                raise ValueError
            status = "open" if opened_ns <= now_ns < closed_ns else "closed"
        else:
            if value.get("open_at") is not None or value.get("close_at") is not None:
                raise ValueError
            opened, closed, status = None, None, "closed"
    except (ValueError, TypeError, ArithmeticError):
        return _unknown_session(now_stamp, "calendar_invalid")
    return {**result, "status": status, "is_trading_day": trading_day, "open_at": opened,
            "close_at": closed, "checked_at": checked, "error_code": None}


def sanitize_market_watch(value, *, allowed_symbols, feed, now, max_age_seconds=2):
    """Project at most ten configured rows and expire evidence at publication.

    Unknown keys and unrequested symbols never cross this interface. Malformed
    rows remain isolated; invalid envelope identity/size is rejected outright.
    Neither an upstream status label nor a new heartbeat can refresh prices.
    """
    symbols, maximum = _allowlist(allowed_symbols), _maximum(max_age_seconds)
    now_stamp, now_ns = _instant(now)
    if (not isinstance(feed, str) or feed not in {"iex", "sip"} or not isinstance(value, dict)
            or value.get("schema_version") != SCHEMA_VERSION or value.get("source") != SOURCE
            or value.get("feed") != feed):
        raise ValueError("Invalid Market Watch envelope")
    collected, collected_ns = _instant(value.get("collected_at"))
    if collected_ns > now_ns:
        raise ValueError("Market Watch collection cannot be in the future")
    rows = value.get("rows")
    if not isinstance(rows, list) or len(rows) > MAX_SYMBOLS:
        raise ValueError("Market Watch rows exceed the bounded contract")
    indexed = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("symbol"), str) or row["symbol"] not in symbols:
            continue
        if row["symbol"] in indexed:
            raise ValueError("Market Watch cannot contain duplicate symbols")
        indexed[row["symbol"]] = row
    output = [_row(indexed[symbol], symbol=symbol, feed=feed, now_ns=now_ns,
                   collected_ns=collected_ns, maximum=maximum) if symbol in indexed
              else _empty_row(symbol, feed, "symbol_missing") for symbol in symbols]
    return {"schema_version": SCHEMA_VERSION, "source": SOURCE, "feed": feed, "collected_at": collected,
            "max_age_seconds": maximum, "requested_symbols": list(symbols),
            "session": _session(value.get("session"), now=now_stamp, collected_ns=collected_ns), "rows": output}


def market_data_evidence(watch):
    """Legacy all-requested-quotes status; per-symbol rows remain independent."""
    rows = watch["rows"]
    received = [row for row in rows if row["status"] in {"fresh", "stale"}]
    return {"verified": bool(rows) and all(row["status"] == "fresh" for row in rows),
            "as_of": min((row["source_at"] for row in received), key=lambda stamp: _instant(stamp)[1], default=None),
            "received_at": min((row["received_at"] for row in received), key=lambda stamp: _instant(stamp)[1], default=None),
            "feed": watch["feed"], "symbols_received": [row["symbol"] for row in received]}


class MarketWatchCollector:
    def __init__(self, provider, config, *, clock=None):
        self.provider = provider
        self.symbols = _allowlist(config.watchlist)
        self.feed = config.data_feed
        if self.feed not in {"iex", "sip"}:
            raise ValueError("A supported configured feed is required")
        self.maximum = _maximum(config.quote_max_age_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._calendar_cache = None

    def _calendar(self):
        now, now_ns = _instant(self.clock())
        today = _datetime(now).astimezone(NY).date()
        if self._calendar_cache is not None:
            cached = _session(self._calendar_cache, now=now, collected_ns=now_ns)
            if cached["status"] != "unknown":
                return cached
        try:
            raw = self.provider.calendar(today.isoformat(), today.isoformat())
            received, receipt_ns = _instant(self.clock())
        except (RuntimeError, ValueError, TypeError, KeyError, OSError, ArithmeticError):
            return _unknown_session(self.clock(), "calendar_unavailable")
        try:
            if not isinstance(raw, list) or len(raw) > 1:
                raise ValueError
            record = raw[0] if raw else None
            opened, closed = None, None
            if record is not None:
                if not isinstance(record, dict) or record.get("date") != today.isoformat():
                    raise ValueError
                stamps = []
                for key in ("open", "close"):
                    value = record.get(key)
                    if not isinstance(value, str) or not _CLOCK.fullmatch(value):
                        raise ValueError
                    stamps.append(_instant(datetime.combine(today, time.fromisoformat(value), tzinfo=NY))[0])
                opened, closed = stamps
            session = {**_unknown_session(received), "date": today.isoformat(), "is_trading_day": record is not None,
                       "open_at": opened, "close_at": closed, "checked_at": received, "error_code": None}
            checked = _session(session, now=received, collected_ns=receipt_ns)
            if checked["status"] == "unknown":
                return checked
            self._calendar_cache = checked
            return checked
        except (ValueError, TypeError, KeyError, ArithmeticError):
            return _unknown_session(received, "calendar_invalid")

    def collect(self):
        # Refresh calendar first so its bounded request never ages a quote that
        # has already been received. Empty configuration makes no provider call.
        session = self._calendar() if self.symbols else _unknown_session(self.clock())
        raw, receipt, failure = {}, None, False
        if self.symbols:
            try:
                raw = self.provider.snapshots(self.symbols)
                receipt = _instant(self.clock())[0]
                if not isinstance(raw, dict):
                    raise ValueError
            except (RuntimeError, ValueError, TypeError, KeyError, OSError, ArithmeticError):
                raw, failure = {}, True
        rows = []
        for symbol in self.symbols:
            if failure:
                row = _empty_row(symbol, self.feed, "market_data_unavailable", received_at=receipt)
            elif symbol not in raw:
                row = _empty_row(symbol, self.feed, "symbol_missing", received_at=receipt)
            elif not isinstance(raw[symbol], dict):
                row = _empty_row(symbol, self.feed, "symbol_invalid", received_at=receipt)
            else:
                record = raw[symbol]
                quote = record.get("latestQuote")
                if quote is None:
                    row = _empty_row(symbol, self.feed, "quote_missing", received_at=receipt)
                elif not isinstance(quote, dict):
                    row = _empty_row(symbol, self.feed, "quote_invalid", received_at=receipt)
                else:
                    row = {"symbol": symbol, "feed": self.feed, "bid": quote.get("bp"), "ask": quote.get("ap"),
                           "source_at": quote.get("t"), "received_at": receipt}
                trade = record.get("latestTrade")
                if isinstance(trade, dict):
                    row["last_trade"] = {"price": trade.get("p"), "source_at": trade.get("t"), "received_at": receipt}
                elif trade is not None:
                    row["last_trade_error_code"] = "trade_invalid"
            rows.append(row)
        collected = _instant(self.clock())[0]
        envelope = {"schema_version": SCHEMA_VERSION, "source": SOURCE, "feed": self.feed,
                    "collected_at": collected, "session": session, "rows": rows}
        return sanitize_market_watch(envelope, allowed_symbols=self.symbols, feed=self.feed,
                                     now=collected, max_age_seconds=self.maximum)


def market_watch_from_snapshots(snapshots, *, allowed_symbols, feed, now, max_age_seconds=2):
    """Compatibility for explicitly injected strategy builders, never the default.

    Their measured receipts are retained. Strategy session booleans are not
    upgraded to independently checked regular-session calendar evidence.
    """
    if not isinstance(snapshots, list) or len(snapshots) > MAX_SYMBOLS:
        raise ValueError("Invalid injected observation collection")
    rows = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        provenance = snapshot.get("provenance")
        if isinstance(provenance, dict) and provenance.get("synthetic") is True:
            raise ValueError("Synthetic data cannot verify a provider observation")
        quote = snapshot.get("quote")
        if not isinstance(quote, dict):
            continue
        row = {"symbol": snapshot.get("symbol"), "feed": quote.get("feed"), "bid": quote.get("bid"),
               "ask": quote.get("ask"), "source_at": quote.get("timestamp"), "received_at": quote.get("received_at")}
        trade = snapshot.get("last_trade")
        if isinstance(trade, dict) and trade.get("symbol") == snapshot.get("symbol") and trade.get("feed") == feed:
            row["last_trade"] = {"price": trade.get("price"), "source_at": trade.get("timestamp"),
                                 "received_at": trade.get("received_at")}
        rows.append(row)
    return sanitize_market_watch({"schema_version": SCHEMA_VERSION, "source": SOURCE, "feed": feed,
                                  "collected_at": _instant(now)[0], "session": _unknown_session(now), "rows": rows},
                                 allowed_symbols=allowed_symbols, feed=feed, now=now, max_age_seconds=max_age_seconds)
