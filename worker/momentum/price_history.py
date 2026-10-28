"""Bounded raw daily close and elapsed regular-minute observations.

This is a current-state display projection, not a capture archive, strategy
snapshot, adjusted return series, or proof of a final/official closing price.
Provider bars may be revised. Missing IEX intervals are never filled in.
"""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re

from .activities import timestamp_parts
from .config import SYMBOL
from .models import NY


SCHEMA_VERSION = "1.0"
MAX_SYMBOLS = 10
REFRESH_SECONDS = 60
MAX_AGE_SECONDS = 120
CALENDAR_MAX_AGE_SECONDS = 300
NS = 1_000_000_000
MINUTE_NS = 60 * NS
ERROR_CODES = frozenset({
    "calendar_unavailable", "calendar_invalid", "calendar_stale", "history_unavailable",
    "history_response_invalid", "history_page_incomplete", "symbol_missing", "daily_bar_missing",
    "daily_bar_invalid", "daily_bar_session_mismatch", "minute_bar_invalid",
    "history_timestamp_invalid", "history_stale",
})
CALENDAR_ERRORS = frozenset({"calendar_unavailable", "calendar_invalid", "calendar_stale"})
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")


def _instant(value):
    if not isinstance(value, (str, datetime)) or isinstance(value, str) and len(value) > 64:
        raise ValueError("Invalid history timestamp")
    return timestamp_parts(value)


def _datetime(value):
    return datetime.fromisoformat(_instant(value)[0].replace("Z", "+00:00"))


def _stamp_ns(value):
    seconds, fraction = divmod(value, NS)
    instant = datetime.fromtimestamp(seconds, timezone.utc)
    return (f"{instant.year:04d}-{instant.month:02d}-{instant.day:02d}T"
            f"{instant.hour:02d}:{instant.minute:02d}:{instant.second:02d}.{fraction:09d}Z")


def _day(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("Invalid history date")
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("Invalid history date")
    return result


def _symbols(value):
    if (not isinstance(value, (list, tuple)) or len(value) > MAX_SYMBOLS
            or any(not isinstance(x, str) or not SYMBOL.fullmatch(x) for x in value)
            or len(set(value)) != len(value)):
        raise ValueError("A bounded configured history watchlist is required")
    return tuple(value)


def _number(value, *, volume=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Invalid history number")
    raw = str(value)
    if not 1 <= len(raw) <= 64:
        raise ValueError("Invalid history number")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise ValueError("Invalid history number") from None
    if not number.is_finite() or number <= 0 or number > Decimal("1e18"):
        raise ValueError("Invalid history number")
    if volume:
        if number != number.to_integral_value():
            raise ValueError("Invalid history volume")
        return str(int(number))
    if not -18 <= number.as_tuple().exponent <= 18:
        raise ValueError("Invalid history price")
    fixed = format(number, "f")
    return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed


def _ohlcv(value, *, provider=False):
    if not isinstance(value, dict):
        raise ValueError("Invalid history bar")
    names = ("open", "high", "low", "close", "volume")
    keys = ("o", "h", "l", "c", "v") if provider else names
    result = {name: _number(value.get(key), volume=name == "volume") for name, key in zip(names, keys)}
    op, high, low, close = (Decimal(result[x]) for x in names[:4])
    if not low <= min(op, close) <= max(op, close) <= high:
        raise ValueError("Invalid history OHLC")
    return result


def _session(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid history session")
    day = _day(value.get("date"))
    opened, open_ns = _instant(value.get("open_at"))
    closed, close_ns = _instant(value.get("close_at"))
    if (open_ns >= close_ns or open_ns % MINUTE_NS or close_ns % MINUTE_NS
            or any(_datetime(x).astimezone(NY).date() != day for x in (opened, closed))):
        raise ValueError("Invalid history session")
    return {"date": day.isoformat(), "open_at": opened, "close_at": closed}


def _unknown_calendar(now, code="calendar_unavailable"):
    return {"source": "alpaca_calendar", "anchor_date": _datetime(now).astimezone(NY).date().isoformat(),
            "is_trading_day": None, "checked_at": None, "max_age_seconds": CALENDAR_MAX_AGE_SECONDS,
            "previous_session": None, "target_session": None, "error_code": code}


def _calendar_projection(value, *, now, collected_ns):
    fallback = _unknown_calendar(now, "calendar_invalid")
    if not isinstance(value, dict) or value.get("source") != "alpaca_calendar" or value.get("max_age_seconds") != 300:
        return fallback
    try:
        anchor = _day(value.get("anchor_date"))
        if value.get("is_trading_day") is None:
            code = value.get("error_code")
            return {**fallback, "anchor_date": anchor.isoformat(),
                    "error_code": code if isinstance(code, str) and code in CALENDAR_ERRORS else "calendar_invalid"}
        if type(value.get("is_trading_day")) is not bool:
            raise ValueError
        if value.get("error_code") not in (None, "calendar_stale"):
            code = value.get("error_code")
            return {**fallback, "anchor_date": anchor.isoformat(),
                    "error_code": code if isinstance(code, str) and code in CALENDAR_ERRORS else "calendar_invalid"}
        checked, checked_ns = _instant(value.get("checked_at"))
        _, now_ns = _instant(now)
        if checked_ns > collected_ns or collected_ns > now_ns:
            raise ValueError
        previous, target = _session(value.get("previous_session")), _session(value.get("target_session"))
        if _day(previous["date"]) >= anchor:
            raise ValueError
        if value["is_trading_day"]:
            if target["date"] != anchor.isoformat():
                raise ValueError
        elif target != previous:
            raise ValueError
        stale = (anchor != _datetime(now).astimezone(NY).date()
                 or anchor != _datetime(_stamp_ns(collected_ns)).astimezone(NY).date()
                 or now_ns - checked_ns > CALENDAR_MAX_AGE_SECONDS * NS
                 or value.get("error_code") == "calendar_stale")
        return {"source": "alpaca_calendar", "anchor_date": anchor.isoformat(),
                "is_trading_day": value["is_trading_day"], "checked_at": checked, "max_age_seconds": 300,
                "previous_session": previous, "target_session": target,
                "error_code": "calendar_stale" if stale else None}
    except (ValueError, TypeError, ArithmeticError):
        return fallback


def _close_empty(session, code, *, status="error", requested=None, received=None):
    return {"status": status, "session_date": session["date"] if session else None,
            "close": None, "source_at": None, "requested_at": requested, "received_at": received, "error_code": code}


def _minute_empty(session, code, *, status="error", requested=None, received=None, window=None):
    start_ns, cutoff_ns = window if window is not None else (None, None)
    expected = [] if window is None else [_stamp_ns(x) for x in range(start_ns, cutoff_ns, MINUTE_NS)]
    return {"status": status, "session_date": session["date"] if session else None,
            "interval_seconds": 60, "scope": "regular", "window_start_at": _stamp_ns(start_ns) if window else None,
            "cutoff_at": _stamp_ns(cutoff_ns) if window else None, "requested_at": requested, "received_at": received,
            "expected_intervals": len(expected), "missing_intervals": expected, "bars": [], "error_code": code}


def _attempt(value, *, calendar, collected_ns, now_ns, required=False):
    requested = received = None
    request_ns = receipt_ns = None
    if value.get("requested_at") is not None:
        requested, request_ns = _instant(value["requested_at"])
        if request_ns > collected_ns or calendar.get("checked_at") is not None and request_ns < _instant(calendar["checked_at"])[1]:
            raise ValueError
    if value.get("received_at") is not None:
        received, receipt_ns = _instant(value["received_at"])
        if request_ns is None or not request_ns <= receipt_ns <= collected_ns <= now_ns:
            raise ValueError
    if required and (request_ns is None or receipt_ns is None):
        raise ValueError
    return requested, received, request_ns, receipt_ns


def _stale(value, *, calendar, collected_ns, now_ns, request_ns=None, receipt_ns=None):
    return (value.get("status") == "stale" or calendar["error_code"] == "calendar_stale"
            or now_ns - collected_ns > MAX_AGE_SECONDS * NS
            or request_ns is not None and now_ns - request_ns > MAX_AGE_SECONDS * NS
            or receipt_ns is not None and now_ns - receipt_ns > MAX_AGE_SECONDS * NS)


def _close_projection(value, *, calendar, collected_ns, now_ns):
    session = calendar["previous_session"]
    if session is None:
        return _close_empty(None, calendar["error_code"] or "calendar_invalid")
    if not isinstance(value, dict):
        return _close_empty(session, "daily_bar_invalid")
    if value.get("status") == "observed" and value.get("error_code") is not None:
        return _close_empty(session, "daily_bar_invalid")
    try:
        request, receipt, request_ns, receipt_ns = _attempt(value, calendar=calendar,
            collected_ns=collected_ns, now_ns=now_ns, required=value.get("status") in {"observed", "stale"})
    except (ValueError, TypeError, ArithmeticError):
        return _close_empty(session, "history_timestamp_invalid")
    if value.get("status") in {"missing", "error"}:
        code = value.get("error_code")
        return _close_empty(session, code if isinstance(code, str) and code in ERROR_CODES else "daily_bar_invalid",
                            status=value["status"], requested=request, received=receipt)
    try:
        if value.get("status") not in {"observed", "stale"} or value.get("session_date") != session["date"]:
            raise ValueError
        source, source_ns = _instant(value.get("source_at"))
        midnight_ns = _instant(datetime.combine(_day(session["date"]), time(), NY))[1]
        if source_ns != midnight_ns:
            return _close_empty(session, "daily_bar_session_mismatch", requested=request, received=receipt)
        if not source_ns <= _instant(session["close_at"])[1] <= request_ns <= receipt_ns:
            raise ValueError
        close = _number(value.get("close"))
        stale = _stale(value, calendar=calendar, collected_ns=collected_ns, now_ns=now_ns,
                       request_ns=request_ns, receipt_ns=receipt_ns)
        return {"status": "stale" if stale else "observed", "session_date": session["date"], "close": close,
                "source_at": source, "requested_at": request, "received_at": receipt,
                "error_code": (calendar["error_code"] or "history_stale") if stale else None}
    except (ValueError, TypeError, ArithmeticError):
        return _close_empty(session, "daily_bar_invalid", requested=request, received=receipt)


def _window(session, requested_ns):
    opened, closed = (_instant(session[x])[1] for x in ("open_at", "close_at"))
    cutoff = min(requested_ns // MINUTE_NS * MINUTE_NS, closed)
    if cutoff <= opened:
        return None
    return max(opened, cutoff - 10 * MINUTE_NS), cutoff


def _minute_projection(value, *, calendar, collected_ns, now_ns):
    session = calendar["target_session"]
    if session is None:
        return _minute_empty(None, calendar["error_code"] or "calendar_invalid")
    if not isinstance(value, dict):
        return _minute_empty(session, "minute_bar_invalid")
    if value.get("status") in ("observed", "empty", "not_started") and value.get("error_code") is not None:
        return _minute_empty(session, "minute_bar_invalid")
    try:
        if (value.get("session_date") != session["date"] or value.get("scope") != "regular"
                or type(value.get("interval_seconds")) is not int or value["interval_seconds"] != 60
                or type(value.get("expected_intervals")) is not int or not 0 <= value["expected_intervals"] <= 10):
            raise ValueError
        if value.get("status") == "not_started" or value.get("status") == "stale" and value.get("expected_intervals") == 0:
            if (_window(session, collected_ns) is not None or value.get("bars") != []
                    or value.get("missing_intervals") != [] or value.get("expected_intervals") != 0
                    or any(value.get(k) is not None for k in ("window_start_at", "cutoff_at", "requested_at", "received_at"))):
                raise ValueError
            stale = _stale(value, calendar=calendar, collected_ns=collected_ns, now_ns=now_ns)
            return _minute_empty(session, (calendar["error_code"] or "history_stale") if stale else None,
                                 status="stale" if stale else "not_started")
        try:
            request, receipt, request_ns, receipt_ns = _attempt(value, calendar=calendar,
                collected_ns=collected_ns, now_ns=now_ns, required=value.get("status") in {"observed", "stale", "empty"})
        except (ValueError, TypeError, ArithmeticError):
            return _minute_empty(session, "history_timestamp_invalid")
        window = _window(session, request_ns) if request_ns is not None else None
        if value.get("status") in {"missing", "error"}:
            code = value.get("error_code")
            return _minute_empty(session, code if isinstance(code, str) and code in ERROR_CODES else "minute_bar_invalid",
                                 status=value["status"], requested=request, received=receipt, window=window)
        if value.get("status") not in {"observed", "stale", "empty"} or window is None:
            raise ValueError
        start_ns, cutoff_ns = window
        if (_instant(value.get("window_start_at"))[1] != start_ns or _instant(value.get("cutoff_at"))[1] != cutoff_ns
                or value.get("expected_intervals") != (cutoff_ns - start_ns) // MINUTE_NS):
            raise ValueError
        bars = value.get("bars")
        if not isinstance(bars, list) or len(bars) > 10:
            raise ValueError
        normalized, seen, previous = [], set(), None
        for bar in bars:
            if not isinstance(bar, dict):
                raise ValueError
            start, bar_ns = _instant(bar.get("start_at"))
            end, end_ns = _instant(bar.get("end_at"))
            if (bar_ns % MINUTE_NS or end_ns != bar_ns + MINUTE_NS or not start_ns <= bar_ns < end_ns <= cutoff_ns
                    or bar_ns in seen or previous is not None and bar_ns <= previous):
                raise ValueError
            seen.add(bar_ns)
            previous = bar_ns
            normalized.append({"start_at": start, "end_at": end, **_ohlcv(bar)})
        if value.get("status") == "empty" and normalized or value.get("status") == "observed" and not normalized:
            raise ValueError
        result = _minute_empty(session, None, requested=request, received=receipt, window=window)
        result["bars"] = normalized
        result["missing_intervals"] = [_stamp_ns(x) for x in range(start_ns, cutoff_ns, MINUTE_NS) if x not in seen]
        stale = _stale(value, calendar=calendar, collected_ns=collected_ns, now_ns=now_ns,
                       request_ns=request_ns, receipt_ns=receipt_ns)
        result["status"] = "stale" if stale else "observed" if normalized else "empty"
        result["error_code"] = (calendar["error_code"] or "history_stale") if stale else None
        return result
    except (ValueError, TypeError, ArithmeticError):
        return _minute_empty(session, "minute_bar_invalid")


def _envelope(symbols, feed, collected, calendar, rows):
    return {"schema_version": "1.0", "source": "alpaca_market_data", "observation_only": True,
            "feed": feed, "adjustment": "raw", "currency": "USD", "symbol_mapping": "disabled",
            "collected_at": collected, "refresh_interval_seconds": 60, "max_age_seconds": 120,
            "requested_symbols": list(symbols), "revisions_possible": True, "official_close": False,
            "corporate_action_comparability": "not_assessed", "full_session_coverage": False,
            "calendar": calendar, "rows": rows}


def sanitize_price_history(value, *, allowed_symbols, feed, now):
    """Project bounded history independently from current quote/account evidence."""
    symbols = _symbols(allowed_symbols)
    stamp, now_ns = _instant(now)
    if not isinstance(value, dict) or feed not in {"iex", "sip"}:
        raise ValueError("Invalid price history envelope")
    identity = {"schema_version": "1.0", "source": "alpaca_market_data", "observation_only": True,
                "feed": feed, "adjustment": "raw", "currency": "USD", "symbol_mapping": "disabled",
                "refresh_interval_seconds": 60, "max_age_seconds": 120, "revisions_possible": True,
                "official_close": False, "corporate_action_comparability": "not_assessed", "full_session_coverage": False}
    if any(type(value.get(k)) is not type(v) or value.get(k) != v for k, v in identity.items()):
        raise ValueError("Invalid price history identity")
    if value.get("requested_symbols") != list(symbols):
        raise ValueError("Price history watchlist mismatch")
    collected, collected_ns = _instant(value.get("collected_at"))
    if collected_ns > now_ns:
        raise ValueError("Future price history collection")
    raw_rows = value.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) > MAX_SYMBOLS:
        raise ValueError("Unbounded price history rows")
    calendar = _calendar_projection(value.get("calendar"), now=stamp, collected_ns=collected_ns)
    indexed = {}
    for row in raw_rows:
        if not isinstance(row, dict) or not isinstance(row.get("symbol"), str) or row["symbol"] not in symbols:
            continue
        if row["symbol"] in indexed:
            raise ValueError("Duplicate price history symbol")
        indexed[row["symbol"]] = row
    rows = []
    for symbol in symbols:
        row = indexed.get(symbol, {})
        rows.append({"symbol": symbol,
            "previous_close": _close_projection(row.get("previous_close"), calendar=calendar, collected_ns=collected_ns, now_ns=now_ns),
            "minute_bars": _minute_projection(row.get("minute_bars"), calendar=calendar, collected_ns=collected_ns, now_ns=now_ns)})
    return _envelope(symbols, feed, collected, calendar, rows)


def price_history_summary(value):
    """Journal only a digest/status summary; no provider-capture archive is made."""
    if value is None:
        return None
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return {"schema_version": "1.0", "collected_at": value["collected_at"], "feed": value["feed"],
            "requested_symbols": list(value["requested_symbols"]), "history_sha256": sha256(encoded).hexdigest(),
            "archive": "not_recorded", "rows": [{"symbol": row["symbol"],
                "previous_close_status": row["previous_close"]["status"],
                "minute_bars_status": row["minute_bars"]["status"], "bar_count": len(row["minute_bars"]["bars"])}
                for row in value["rows"]]}


class PriceHistoryCollector:
    def __init__(self, provider, config, *, clock=None):
        self.provider, self.symbols, self.feed = provider, _symbols(config.watchlist), config.data_feed
        if self.feed not in {"iex", "sip"}:
            raise ValueError("Unsupported history feed")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._calendar_cache = None
        self._cache = None
        self._last_attempt_ns = None

    def _calendar(self, now):
        stamp, now_ns = _instant(now)
        if self._calendar_cache is not None:
            cached = _calendar_projection(self._calendar_cache, now=stamp, collected_ns=now_ns)
            if cached["error_code"] is None:
                return cached
        today = _datetime(stamp).astimezone(NY).date()
        start = today - timedelta(days=14)
        try:
            raw = self.provider.calendar(start.isoformat(), today.isoformat())
            received, received_ns = _instant(self.clock())
        except (RuntimeError, ValueError, TypeError, KeyError, OSError, ArithmeticError, AttributeError):
            return _unknown_calendar(self.clock(), "calendar_unavailable")
        try:
            if received_ns < now_ns or not isinstance(raw, list) or len(raw) > 15:
                raise ValueError
            sessions, previous_day = {}, None
            for record in raw:
                if not isinstance(record, dict):
                    raise ValueError
                day = _day(record.get("date"))
                if not start <= day <= today or day in sessions or previous_day is not None and day <= previous_day:
                    raise ValueError
                stamps = []
                for key in ("open", "close"):
                    clock = record.get(key)
                    if not isinstance(clock, str) or not _CLOCK.fullmatch(clock):
                        raise ValueError
                    stamps.append(_instant(datetime.combine(day, time.fromisoformat(clock), NY))[0])
                sessions[day] = _session({"date": day.isoformat(), "open_at": stamps[0], "close_at": stamps[1]})
                previous_day = day
            prior = max((d for d in sessions if d < today), default=None)
            if prior is None:
                raise ValueError
            previous = sessions[prior]
            calendar = {"source": "alpaca_calendar", "anchor_date": today.isoformat(),
                "is_trading_day": today in sessions, "checked_at": received, "max_age_seconds": 300,
                "previous_session": previous, "target_session": sessions.get(today, previous), "error_code": None}
            checked = _calendar_projection(calendar, now=received, collected_ns=received_ns)
            if checked["error_code"] is not None:
                return checked
            self._calendar_cache = checked
            return checked
        except (ValueError, TypeError, ArithmeticError):
            return _unknown_calendar(received, "calendar_invalid")

    def _request(self, timeframe, start, end):
        request = _instant(self.clock())[0]
        receipt = None
        try:
            page = self.provider.observation_bars(self.symbols, timeframe=timeframe, start=start, end=end)
            receipt = _instant(self.clock())[0]
            if not isinstance(page, dict) or not isinstance(page.get("bars"), dict) or len(page["bars"]) > 1000:
                return None, request, receipt, "history_response_invalid"
            if page.get("next_page_token") not in (None, ""):
                return None, request, receipt, "history_page_incomplete"
            return page["bars"], request, receipt, None
        except (RuntimeError, ValueError, TypeError, KeyError, OSError, ArithmeticError, AttributeError):
            return None, request, receipt, "history_unavailable"

    def collect(self):
        now, now_ns = _instant(self.clock())
        if self._cache is not None and self._last_attempt_ns is not None and 0 <= now_ns - self._last_attempt_ns < REFRESH_SECONDS * NS:
            return sanitize_price_history(self._cache, allowed_symbols=self.symbols, feed=self.feed, now=now)
        self._last_attempt_ns = now_ns
        calendar = self._calendar(now) if self.symbols else _unknown_calendar(now)
        rows = []
        previous, target = calendar["previous_session"], calendar["target_session"]
        if calendar["error_code"] is not None:
            rows = [{"symbol": symbol, "previous_close": _close_empty(previous, calendar["error_code"]),
                     "minute_bars": _minute_empty(target, calendar["error_code"])} for symbol in self.symbols]
        else:
            midnight = datetime.combine(_day(previous["date"]), time(), NY)
            end = datetime.combine(_day(previous["date"]) + timedelta(days=1), time(), NY) - timedelta(microseconds=1)
            daily, daily_request, daily_receipt, daily_error = self._request("1Day", _instant(midnight)[0], _instant(end)[0])
            # Freeze the elapsed-minute window immediately before its GET,
            # after the independently bounded daily request has completed.
            minute_request, minute_ns = _instant(self.clock())
            window = _window(target, minute_ns)
            minute, minute_receipt, minute_error = None, None, None
            if window is not None:
                minute, actual_request, minute_receipt, minute_error = self._request("1Min", _stamp_ns(window[0]), _stamp_ns(window[1]))
                # _request captures its own actual start; recomputing a window
                # after I/O is forbidden. Both stamps must be the same interval.
                if _window(target, _instant(actual_request)[1]) != window:
                    minute, minute_error = None, "history_timestamp_invalid"
                minute_request = actual_request
            for symbol in self.symbols:
                close = _close_empty(previous, daily_error or "symbol_missing", status="error" if daily_error else "missing",
                                     requested=daily_request, received=daily_receipt)
                if daily is not None and symbol in daily:
                    records = daily[symbol]
                    try:
                        if not isinstance(records, list) or len(records) > 1:
                            raise ValueError
                        if not records:
                            close["error_code"] = "daily_bar_missing"
                        else:
                            values = _ohlcv(records[0], provider=True)
                            close = {"status": "observed", "session_date": previous["date"], "close": values["close"],
                                     "source_at": records[0].get("t"), "requested_at": daily_request,
                                     "received_at": daily_receipt, "error_code": None}
                    except (ValueError, TypeError, ArithmeticError):
                        close = _close_empty(previous, "daily_bar_invalid", requested=daily_request, received=daily_receipt)
                candles = _minute_empty(target, minute_error, status="not_started") if window is None else _minute_empty(
                    target, minute_error or "symbol_missing", status="error" if minute_error else "missing",
                    requested=minute_request, received=minute_receipt, window=window)
                if window is not None and minute is not None and symbol in minute:
                    try:
                        records = minute[symbol]
                        if not isinstance(records, list) or len(records) > 11:
                            raise ValueError
                        bars, seen = [], set()
                        for record in records:
                            values = _ohlcv(record, provider=True)
                            source, source_ns = _instant(record.get("t"))
                            if source_ns % MINUTE_NS or source_ns in seen:
                                raise ValueError
                            seen.add(source_ns)
                            # API end is inclusive and may include the current
                            # interval. Only fully elapsed requested slots survive.
                            if not window[0] <= source_ns < source_ns + MINUTE_NS <= window[1]:
                                continue
                            bars.append({"start_at": source, "end_at": _stamp_ns(source_ns + MINUTE_NS), **values})
                        bars.sort(key=lambda bar: _instant(bar["start_at"])[1])
                        candles.update(status="observed" if bars else "empty", bars=bars, error_code=None)
                    except (ValueError, TypeError, ArithmeticError):
                        candles = _minute_empty(target, "minute_bar_invalid", requested=minute_request,
                                                received=minute_receipt, window=window)
                rows.append({"symbol": symbol, "previous_close": close, "minute_bars": candles})
        collected = _instant(self.clock())[0]
        self._cache = sanitize_price_history(_envelope(self.symbols, self.feed, collected, calendar, rows),
                                             allowed_symbols=self.symbols, feed=self.feed, now=collected)
        return deepcopy(self._cache)
