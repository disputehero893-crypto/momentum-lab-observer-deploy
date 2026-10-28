"""Isolated, GET-only Alpaca US spot-crypto quote observation.

Not wired to the stock observer or an order route. Caller must explicitly add
an independently reviewed, owner-scoped publication path before deployment.
Alpaca reference: https://docs.alpaca.markets/us/reference/cryptolatestquotes-1
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

CRYPTO_URL = "https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes"
PAIR = re.compile(r"[A-Z0-9]{2,10}/[A-Z]{3}\Z")
PRICE = re.compile(r"\d{1,19}(?:\.\d{1,18})?\Z")
RFC3339 = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})\Z")
MAX_RESPONSE_BYTES = 262_144


class CryptoDataError(RuntimeError):
    """Always a fixed safe code; never broker response bodies or credentials."""
    def __init__(self, code="market_data_unavailable"):
        self.code = code
        super().__init__(code)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise CryptoDataError()


def _pairs(pairs):
    if (not isinstance(pairs, (tuple, list)) or not 1 <= len(pairs) <= 10
            or any(not isinstance(p, str) or not PAIR.fullmatch(p) for p in pairs)
            or len(set(pairs)) != len(pairs)):
        raise ValueError("Expected one to ten unique slash-formatted spot crypto pairs")
    return tuple(pairs)


def _timestamp(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Timezone required")
        value = value.isoformat()
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid timestamp")
    m = RFC3339.fullmatch(value)
    if m is None or m[4] == "-00:00":
        raise ValueError("Invalid timestamp")
    base = datetime.fromisoformat(m[1] + "T" + m[2] + m[4].replace("Z", "+00:00")).astimezone(timezone.utc)
    fraction = (m[3] or "").rstrip("0")
    delta = base - datetime(1970, 1, 1, tzinfo=timezone.utc)
    nanos = (delta.days * 86400 + delta.seconds) * 1_000_000_000 + int(fraction.ljust(9, "0") or "0")
    stamp = base.strftime("%Y-%m-%dT%H:%M:%S") + ("." + fraction if fraction else "") + "Z"
    return stamp, nanos


def _price(value):
    if type(value) not in (str, int, Decimal):
        raise ValueError("Invalid price type")
    try:
        literal = str(value)
        if len(literal) > 64:
            raise ValueError("Invalid price")
        n = Decimal(literal)
        # Check magnitude and exponent before rendering Decimal as a fixed string.
        # Extreme exponents must never cause unbounded string expansion.
        if (not n.is_finite() or n <= 0 or n > Decimal("1e18")
                or not -18 <= n.as_tuple().exponent <= 18):
            raise ValueError("Invalid price")
        v = format(n, "f")
        if "." in v:
            v = v.rstrip("0").rstrip(".")
        if not PRICE.fullmatch(v):
            raise ValueError("Invalid price")
        return v
    except (InvalidOperation, OverflowError, ValueError, ArithmeticError):
        raise ValueError("Invalid price") from None


def _row(pair, status, *, bid=None, ask=None, source_at=None, received_at=None, error_code=None):
    return {"pair": pair, "status": status, "bid": bid, "ask": ask,
            "source_at": source_at, "received_at": received_at, "error_code": error_code}


def build_crypto_watch(raw, pairs, *, received_at, collected_at, max_age_seconds=60):
    """Project raw provider quotes into the frozen, bounded frontend contract.

    An error in one pair does not manufacture prices for it or obscure other
    valid pairs. A malformed envelope produces error rows for every pair.
    """
    pairs = _pairs(pairs)
    if type(max_age_seconds) not in (int, float) or not 0 < max_age_seconds <= 120:
        raise ValueError("Invalid maximum age")
    received, received_ns = _timestamp(received_at)
    collected, collected_ns = _timestamp(collected_at)
    if received_ns > collected_ns:
        raise ValueError("Receipt follows collection")
    quotes = raw.get("quotes") if isinstance(raw, dict) else None
    quotes = quotes if isinstance(quotes, dict) else None
    rows = []
    for pair in pairs:
        if quotes is None:
            rows.append(_row(pair, "error", received_at=received, error_code="market_data_unavailable"))
            continue
        if pair not in quotes:
            rows.append(_row(pair, "missing", received_at=received, error_code="pair_missing"))
            continue
        value = quotes[pair]
        if not isinstance(value, dict):
            rows.append(_row(pair, "error", received_at=received, error_code="quote_invalid"))
            continue
        try:
            bid, ask = _price(value.get("bp")), _price(value.get("ap"))
            if Decimal(bid) > Decimal(ask):
                raise ValueError("Crossed quote")
        except (ValueError, TypeError, ArithmeticError):
            rows.append(_row(pair, "error", received_at=received, error_code="quote_invalid"))
            continue
        try:
            source, source_ns = _timestamp(value.get("t"))
            if source_ns > received_ns:
                raise ValueError("Source after receipt")
        except (ValueError, TypeError, ArithmeticError):
            rows.append(_row(pair, "error", received_at=received, error_code="quote_timestamp_invalid"))
            continue
        stale = collected_ns - source_ns > max_age_seconds * 1_000_000_000
        rows.append(_row(pair, "stale" if stale else "fresh", bid=bid, ask=ask,
                         source_at=source, received_at=received,
                         error_code="quote_stale" if stale else None))
    return {"schema_version": "1.0", "asset_class": "spot_crypto", "source": "alpaca_crypto",
            "observation_only": True, "execution_enabled": False, "collected_at": collected,
            "max_age_seconds": max_age_seconds, "requested_pairs": list(pairs), "rows": rows}


def _unique_json(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("Duplicate JSON field")
        out[k] = v
    return out


def _constant(_):
    raise ValueError("Nonfinite JSON constant")



def sanitize_crypto_watch(report, *, allowed_pairs, now, max_age_seconds=60):
    """Revalidate a report at publication, never promoting stale observations.

    Output is an allowlisted projection only. Invalid reports are rejected,
    allowing the separately owned equity observation to continue unharmed.
    """
    pairs = _pairs(allowed_pairs)
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 120:
        raise ValueError("Invalid configured crypto freshness")
    _, now_ns = _timestamp(now)
    if (not isinstance(report, dict) or report.get("schema_version") != "1.0"
            or report.get("asset_class") != "spot_crypto"
            or report.get("source") != "alpaca_crypto"
            or report.get("observation_only") is not True
            or report.get("execution_enabled") is not False
            or report.get("max_age_seconds") != max_age_seconds
            or report.get("requested_pairs") != list(pairs)
            or not isinstance(report.get("rows"), list)
            or len(report["rows"]) != len(pairs)):
        raise ValueError("Invalid crypto observation envelope")
    collected, collected_ns = _timestamp(report.get("collected_at"))
    if collected_ns > now_ns:
        raise ValueError("Future crypto collection")
    max_ns = max_age_seconds * 1_000_000_000
    rows = []
    for expected, item in zip(pairs, report["rows"]):
        if (not isinstance(item, dict) or item.get("pair") != expected
                or item.get("status") not in {"fresh", "stale", "missing", "error"}
                or item.get("error_code") not in {
                    None, "pair_missing", "pair_invalid", "quote_missing",
                    "quote_invalid", "quote_timestamp_invalid", "quote_stale",
                    "market_data_unavailable"}):
            raise ValueError("Invalid crypto row identity or status")
        status, error = item["status"], item.get("error_code")
        received_at = item.get("received_at")
        received, receipt_ns = (None, None) if received_at is None else _timestamp(received_at)
        if receipt_ns is not None and receipt_ns > collected_ns:
            raise ValueError("Invalid crypto receipt order")
        if status in {"fresh", "stale"}:
            if (status == "fresh" and error is not None
                    or status == "stale" and error != "quote_stale"
                    or receipt_ns is None):
                raise ValueError("Invalid quote claim")
            bid, ask = _price(item.get("bid")), _price(item.get("ask"))
            if Decimal(bid) > Decimal(ask):
                raise ValueError("Crossed crypto quote")
            source_at, source_ns = _timestamp(item.get("source_at"))
            if source_ns > receipt_ns or source_ns > now_ns or receipt_ns > now_ns:
                raise ValueError("Invalid crypto quote chronology")
            expired = (status == "stale" or now_ns - collected_ns > max_ns
                       or now_ns - receipt_ns > max_ns or now_ns - source_ns > max_ns)
            rows.append(_row(expected, "stale" if expired else "fresh", bid=bid, ask=ask,
                             source_at=source_at, received_at=received,
                             error_code="quote_stale" if expired else None))
        else:
            if (error is None or item.get("bid") is not None
                    or item.get("ask") is not None or item.get("source_at") is not None):
                raise ValueError("Invalid missing/error quote")
            if receipt_ns is not None and receipt_ns > now_ns:
                raise ValueError("Future receipt")
            rows.append(_row(expected, status, received_at=received, error_code=error))
    return {"schema_version": "1.0", "asset_class": "spot_crypto",
            "source": "alpaca_crypto", "observation_only": True,
            "execution_enabled": False, "collected_at": collected,
            "max_age_seconds": max_age_seconds, "requested_pairs": list(pairs), "rows": rows}


def crypto_watch_summary(report):
    """Bounded journal summary; does not repeatedly store provider price data."""
    if not isinstance(report, dict):
        return None
    rows = report.get("rows")
    if not isinstance(rows, list) or len(rows) > 10:
        return None
    return {"collected_at": report.get("collected_at"),
            "requested_pairs": report.get("requested_pairs"),
            "statuses": [r.get("status") if isinstance(r, dict) else "error" for r in rows]}


class CryptoQuoteReader:
    """The only network route is authenticated GET /v1beta3/crypto/us/latest/quotes."""
    def __init__(self, config, *, transport=None):
        self.config = config
        self._transport = transport or self._http
        self._opener = build_opener(_NoRedirect())

    def _http(self, method, url, headers, payload):
        request = Request(url, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=12) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise CryptoDataError()
                data = json.loads(body, parse_float=Decimal,
                                  parse_constant=_constant, object_pairs_hook=_unique_json)
                if not isinstance(data, dict):
                    raise CryptoDataError()
                return data
        except (HTTPError, URLError, OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise CryptoDataError() from None

    def latest_quotes(self, pairs):
        pairs = _pairs(pairs)
        if (self.config.mode != "paper" or not self.config.api_key
                or not self.config.api_secret):
            raise CryptoDataError()
        url = CRYPTO_URL + "?" + urlencode({"symbols": ",".join(pairs)})
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.netloc != "data.alpaca.markets"
                or parts.path != "/v1beta3/crypto/us/latest/quotes"):
            raise CryptoDataError()
        headers = {"APCA-API-KEY-ID": self.config.api_key,
                   "APCA-API-SECRET-KEY": self.config.api_secret,
                   "Accept": "application/json", "User-Agent": "momentum-lab-crypto-observe/0.1"}
        try:
            return self._transport("GET", url, headers, None)
        except CryptoDataError:
            raise
        except Exception:
            raise CryptoDataError() from None


class CryptoWatchCollector:
    """One bounded request; independently collected receipt and publication instants."""
    def __init__(self, provider, pairs=("BTC/USD", "ETH/USD"), *, clock=None, max_age_seconds=60):
        self.provider = provider
        self.pairs = _pairs(pairs)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if type(max_age_seconds) not in (int, float) or not 0 < max_age_seconds <= 120:
            raise ValueError("Invalid maximum age")
        self.max_age_seconds = max_age_seconds

    def collect(self):
        try:
            raw = self.provider.latest_quotes(self.pairs)
        except CryptoDataError:
            raw = None
        received = self.clock()
        collected = self.clock()
        return build_crypto_watch(raw, self.pairs, received_at=received,
                                  collected_at=collected, max_age_seconds=self.max_age_seconds)
