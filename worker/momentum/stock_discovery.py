"""Isolated, read-only Alpaca stock discovery using free IEX snapshots.

PAPER / RESEARCH ONLY. NOT a trading scanner: produces discovery candidates,
not strategy-qualified setups, prices suitable for orders, or verified returns.

Uses GET /v2/stocks/snapshots?feed=iex over a bounded liquid universe. The
paid SIP screener endpoints (movers / most-actives) are intentionally NOT
called — those need a separate entitlement and Derek's paid-data yes.

This module is deliberately NOT wired to the live observer or its publisher.
No paper or live orders can be submitted through this module.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .activities import timestamp_parts

HOST = "https://data.alpaca.markets"
SNAPSHOT_PATH = "/v2/stocks/snapshots"
FEED = "iex"
MAX_RESPONSE_BYTES = 262_144
MAX_TOP = 25
MAX_SYMBOLS = 50
MAX_AGE_SECONDS = 120
SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")
ERROR = "market_discovery_unavailable"
# Bounded free-IEX liquid universe (matches paper watchlist + nearby large caps).
DEFAULT_UNIVERSE = (
    "AAPL", "MSFT", "SPY", "AMZN", "GOOGL", "META", "NVDA", "QQQ", "IWM", "TSLA",
)
INCOMPLETE_REQUIREMENTS = (
    "fresh_iex_quotes",
    "same_clock_rvol",
    "verified_share_float",
    "source_linked_catalyst",
    "pattern_and_risk_review",
    "paper_order_approval",
)


class StockDiscoveryError(RuntimeError):
    """Never include credentials, provider response text or raw account data."""

    def __init__(self):
        super().__init__(ERROR)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise StockDiscoveryError()


def _instant(value):
    if not isinstance(value, (str, datetime)) or isinstance(value, str) and len(value) > 64:
        raise ValueError("Invalid screener time")
    return timestamp_parts(value)


def _top(value):
    if type(value) is not int or not 1 <= value <= MAX_TOP:
        raise ValueError("Only one to 25 requested screener rows allowed")
    return value


def _universe(symbols):
    if symbols is None:
        symbols = DEFAULT_UNIVERSE
    if not isinstance(symbols, (list, tuple)) or not 1 <= len(symbols) <= MAX_SYMBOLS:
        raise ValueError("Universe must be 1 to 50 symbols")
    out, seen = [], set()
    for raw in symbols:
        symbol = _symbol(raw)
        if symbol in seen:
            raise ValueError("Duplicate universe symbol")
        seen.add(symbol)
        out.append(symbol)
    return tuple(out)


def _decimal(value, *, positive=False, minimum=None, maximum=Decimal("1e9")):
    if type(value) not in (int, str, Decimal):
        raise ValueError("Provider screener numeric must be exact")
    raw = str(value)
    if not 1 <= len(raw) <= 48:
        raise ValueError("Invalid numeric size")
    try:
        number = Decimal(raw)
        if (
            not number.is_finite()
            or number.as_tuple().exponent < -12
            or number.as_tuple().exponent > 12
            or abs(number) > maximum
            or (positive and number <= 0)
            or (minimum is not None and number < minimum)
        ):
            raise ValueError("Invalid screener numeric")
        fixed = format(number, "f")
        if "." in fixed:
            fixed = fixed.rstrip("0").rstrip(".")
        return fixed
    except (InvalidOperation, ValueError, OverflowError, ArithmeticError):
        raise ValueError("Invalid screener numeric") from None


def _count(value):
    if type(value) not in (str, int, Decimal):
        raise ValueError("Nonnegative whole count required")
    if type(value) is Decimal:
        if not value.is_finite() or value < 0 or value > 10**18 or value != value.to_integral_value():
            raise ValueError("Nonnegative whole count required")
        return str(int(value))
    s = str(value)
    if not s.isascii() or not s.isdecimal() or len(s) > 19:
        raise ValueError("Nonnegative whole count required")
    n = int(s)
    if n > 10**18:
        raise ValueError("Unbounded count")
    return str(n)


def _symbol(value):
    if not isinstance(value, str) or SYMBOL.fullmatch(value) is None:
        raise ValueError("Invalid screener symbol")
    return value


def _status(last_updated, received_at, now, max_age_seconds):
    source, source_ns = _instant(last_updated)
    receipt, receipt_ns = _instant(received_at)
    _, now_ns = _instant(now)
    if source_ns > receipt_ns or receipt_ns > now_ns:
        raise ValueError("Inconsistent screener evidence times")
    fresh = (
        now_ns - source_ns <= max_age_seconds * 1_000_000_000
        and now_ns - receipt_ns <= max_age_seconds * 1_000_000_000
    )
    return ("fresh" if fresh else "stale"), source, receipt


def _bar_close_volume_time(bar):
    if not isinstance(bar, dict):
        raise ValueError("Missing bar")
    close = _decimal(bar.get("c"), positive=True)
    volume = _count(bar.get("v"))
    stamp = bar.get("t")
    if not isinstance(stamp, str):
        raise ValueError("Missing bar time")
    return close, volume, stamp


def _rows_from_snapshots(raw, *, received_at, collected_at, top, max_age_seconds):
    """Project IEX snapshot map into ranked gainers/losers/most-actives rows."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError("Invalid snapshot map")
    if len(raw) > MAX_SYMBOLS:
        raise ValueError("Unbounded snapshot map")

    rows = []
    newest_source = None
    for symbol_key, payload in raw.items():
        symbol = _symbol(symbol_key)
        if not isinstance(payload, dict):
            raise ValueError("Invalid snapshot payload")
        daily = payload.get("dailyBar")
        prev = payload.get("prevDailyBar")
        close, volume, daily_t = _bar_close_volume_time(daily)
        prev_close, _, prev_t = _bar_close_volume_time(prev)
        prev_dec = Decimal(prev_close)
        close_dec = Decimal(close)
        change = close_dec - prev_dec
        pct = (change / prev_dec) * Decimal(100)
        # Quantize before exact-string validation (IEX research ranks, not order prices).
        pct_s = _decimal(
            format(pct.quantize(Decimal("0.0001")), "f"),
            minimum=Decimal("-100"),
            maximum=Decimal("100000"),
        )
        change_s = _decimal(
            format(change.quantize(Decimal("0.00000001")), "f"),
            maximum=Decimal("1e7"),
        )
        # Prefer the newer of the two bar clocks as source evidence.
        source_candidate = daily_t if daily_t >= prev_t else prev_t
        if newest_source is None or source_candidate > newest_source:
            newest_source = source_candidate
        rows.append({
            "symbol": symbol,
            "price": close,
            "change": change_s,
            "percent_change": pct_s,
            "volume": volume,
            "trade_count": "0",  # IEX snapshots do not expose trade_count here
            "_pct": Decimal(pct_s),
            "_vol": int(volume),
        })

    state, updated, receipt = _status(
        newest_source, received_at, collected_at, max_age_seconds
    )

    by_pct_desc = sorted(rows, key=lambda r: r["_pct"], reverse=True)
    by_pct_asc = sorted(rows, key=lambda r: r["_pct"])
    by_vol = sorted(rows, key=lambda r: r["_vol"], reverse=True)

    def gainer_row(item, rank):
        gain = item["_pct"]
        in_band = Decimal("2") <= Decimal(item["price"]) <= Decimal("20")
        above_gain = gain >= Decimal("10")
        screen_match = in_band and above_gain
        return {
            "symbol": item["symbol"],
            "rank": rank,
            "category": "gainer",
            "price": item["price"],
            "change": item["change"],
            "percent_change": item["percent_change"],
            "numeric_screen_match": screen_match,
            "strategy_eligible": False,
            "trade_eligible": False,
            "screen_state": (
                ("fresh_numeric_research_candidate" if state == "fresh"
                 else "dated_numeric_research_candidate")
                if screen_match else "outside_numeric_research_screen"
            ),
        }

    def loser_row(item, rank):
        return {
            "symbol": item["symbol"],
            "rank": rank,
            "category": "loser",
            "price": item["price"],
            "change": item["change"],
            "percent_change": item["percent_change"],
            "numeric_screen_match": False,
            "strategy_eligible": False,
            "trade_eligible": False,
            "screen_state": "outside_numeric_research_screen",
        }

    gainers = [
        gainer_row(item, i)
        for i, item in enumerate([r for r in by_pct_desc if r["_pct"] >= 0][:top], 1)
    ]
    losers = [
        loser_row(item, i)
        for i, item in enumerate([r for r in by_pct_asc if r["_pct"] < 0][:top], 1)
    ]
    actives = [
        {
            "symbol": item["symbol"],
            "rank": i,
            "volume": item["volume"],
            "trade_count": item["trade_count"],
            "strategy_eligible": False,
            "trade_eligible": False,
        }
        for i, item in enumerate(by_vol[:top], 1)
    ]
    movers = {
        "status": state,
        "last_updated": updated,
        "received_at": receipt,
        "gainers": gainers,
        "losers": losers,
        "reason": None,
    }
    most_actives = {
        "status": state,
        "last_updated": updated,
        "received_at": receipt,
        "rows": actives,
        "reason": None,
    }
    return movers, most_actives


def build_stock_discovery(
    snapshots,
    *,
    snapshots_received_at,
    collected_at,
    top=10,
    max_age_seconds=MAX_AGE_SECONDS,
):
    """Rank free IEX snapshots into research-only movers and most-actives.

    'fresh' means a recent IEX snapshot ranking over the bounded universe, NOT
    a validated tradable candidate, guaranteed liquidity, or a signal to buy.
    """
    top = _top(top)
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= MAX_AGE_SECONDS:
        raise ValueError("Invalid maximum age")
    collected, _ = _instant(collected_at)

    empty_movers = {
        "last_updated": None,
        "received_at": None,
        "gainers": [],
        "losers": [],
        "status": "unavailable",
        "reason": ERROR,
    }
    empty_actives = {
        "last_updated": None,
        "received_at": None,
        "rows": [],
        "status": "unavailable",
        "reason": ERROR,
    }

    if snapshots is None:
        movers, actives = empty_movers, empty_actives
    else:
        try:
            movers, actives = _rows_from_snapshots(
                snapshots,
                received_at=snapshots_received_at,
                collected_at=collected,
                top=top,
                max_age_seconds=max_age_seconds,
            )
        except (ValueError, TypeError, ArithmeticError, OverflowError, RecursionError):
            movers = {**empty_movers, "reason": "screener_response_invalid"}
            actives = {**empty_actives, "reason": "screener_response_invalid"}

    return {
        "schema_version": "1.1",
        "kind": "stock_discovery",
        "source": "alpaca_iex_snapshots",
        "feed": FEED,
        "discovery_only": True,
        "observation_only": True,
        "execution_enabled": False,
        "trade_eligible": False,
        "price_comparison": "iex_daily_bar_vs_prev_daily_bar",
        "market_session": "may_be_previous_session_before_market_open",
        "collected_at": collected,
        "max_age_seconds": max_age_seconds,
        "requested_top": top,
        "universe": list(DEFAULT_UNIVERSE),
        "movers": movers,
        "most_actives": actives,
        "remaining_requirements": list(INCOMPLETE_REQUIREMENTS),
    }


def _unique_json(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("Duplicate response key")
        output[key] = value
    return output


def _no_constant(_):
    raise ValueError("Nonfinite constant")


class StockDiscoveryReader:
    """GET-only IEX snapshot reader, paper credentials, fixed host and path."""

    def __init__(self, config, *, transport=None):
        if getattr(config, "mode", None) != "paper":
            raise StockDiscoveryError()
        if not getattr(config, "api_key", None) or not getattr(config, "api_secret", None):
            raise StockDiscoveryError()
        feed = getattr(config, "data_feed", FEED)
        if feed != FEED:
            raise StockDiscoveryError()
        self._api_key = config.api_key
        self._api_secret = config.api_secret
        self._opener = build_opener(_NoRedirect())
        self._transport = transport or self._http

    def _http(self, method, url, headers, payload):
        request = Request(url, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=12) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise StockDiscoveryError()
                data = json.loads(
                    body,
                    parse_float=Decimal,
                    parse_constant=_no_constant,
                    object_pairs_hook=_unique_json,
                )
                if not isinstance(data, dict):
                    raise StockDiscoveryError()
                return data
        except (HTTPError, URLError, OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise StockDiscoveryError() from None

    def _get(self, path, params):
        if path != SNAPSHOT_PATH:
            raise StockDiscoveryError()
        if params.get("feed") != FEED:
            raise StockDiscoveryError()
        url = HOST + path + "?" + urlencode(params)
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.netloc != "data.alpaca.markets"
            or parts.path != path
            or parts.fragment
        ):
            raise StockDiscoveryError()
        headers = {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._api_secret,
            "Accept": "application/json",
            "User-Agent": "tactirise-discovery-readonly-iex/0.2",
        }
        try:
            return self._transport("GET", url, headers, None)
        except StockDiscoveryError:
            raise
        except Exception:
            raise StockDiscoveryError() from None

    def snapshots(self, symbols=None):
        universe = _universe(symbols)
        return self._get(
            SNAPSHOT_PATH,
            {"symbols": ",".join(universe), "feed": FEED},
        )


class StockDiscoveryCollector:
    """One bounded IEX snapshot GET, ranked locally; no mutation or orders."""

    def __init__(self, reader, *, top=10, clock=None, max_age_seconds=MAX_AGE_SECONDS, symbols=None):
        self.reader = reader
        self.top = _top(top)
        self.symbols = _universe(symbols)
        if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= MAX_AGE_SECONDS:
            raise ValueError("Invalid max age")
        self.max_age_seconds = max_age_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def collect(self):
        try:
            snapshots = self.reader.snapshots(self.symbols)
        except StockDiscoveryError:
            snapshots = None
        received = self.clock()
        result = build_stock_discovery(
            snapshots,
            snapshots_received_at=received,
            collected_at=self.clock(),
            top=self.top,
            max_age_seconds=self.max_age_seconds,
        )
        result["universe"] = list(self.symbols)
        return result
