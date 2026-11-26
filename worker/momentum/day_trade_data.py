"""Real data adapters for ``ross_cameron_small_cap_v1`` (paper only, OFF by default).

Three adapters fill the profile's provider interfaces:

* ``FmpFloatProvider``        FloatProvider    Financial Modeling Prep ``/stable/shares-float``
* ``AlpacaMoversScanner``     GainersScanner   Alpaca screener movers + same-feed daily-volume RVOL
* ``AlpacaNewsCatalystProvider`` CatalystProvider Alpaca news (``/v1beta1/news``)

Safety rules baked in:

* Nothing is built unless ``DAY_TRADE_DATA_PROVIDERS_ENABLED`` is explicitly true AND the
  needed credentials exist (``build_provider_set``). Default: an empty ``ProviderSet``, which
  keeps the profile blocked exactly as before.
* Every adapter fails closed: any error, timeout, 4xx/5xx, bad JSON or odd value means
  "unknown" (``None`` / ``[]``), which makes the five-factor gate reject. Adapters never raise
  into the worker.
* Fixed hosts only, no redirects, bounded response size, short timeouts, per-adapter request
  budget. Keys are only sent to their own host and never appear in errors, logs or reports
  (``status()`` carries counts only).
* Read-only market-data GETs. No order, account or broker endpoint is touched here.
* The profile still requires ``ALPACA_DATA_FEED=sip`` (``provider_blockers``), so these free
  sources alone do not let it trade.
"""
from __future__ import annotations

import json
import threading
import time as _time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import DATA_API_HOST
from .day_trade_core import local
from .ross_cameron_small_cap_v1 import Catalyst, ProviderSet, ScannerRow

FMP_HOST = "https://financialmodelingprep.com"
ENABLE_FLAG = "DAY_TRADE_DATA_PROVIDERS_ENABLED"
ALLOWED_HOSTS = frozenset({FMP_HOST, DATA_API_HOST})
MAX_BODY_BYTES = 2_000_000
ZERO = Decimal(0)

# transport(url, headers, timeout) -> (status, body bytes). Tests inject a fake.
Transport = Callable[[str, Mapping[str, str], float], "tuple[int, bytes]"]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def default_transport(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    parts = urlsplit(url)
    if f"{parts.scheme}://{parts.netloc}" not in ALLOWED_HOSTS:
        raise ValueError("host not allowed")
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_BODY_BYTES + 1)
    except HTTPError as exc:  # status only; the message could echo the URL
        return exc.code, b""


class RateLimiter:
    """Sliding-window limiter: ``allow()`` is False once ``limit`` calls happened in ``window`` seconds."""

    def __init__(self, limit: int, window: float = 60.0, clock: Callable[[], float] = _time.monotonic) -> None:
        self.limit, self.window, self.clock = limit, window, clock
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = self.clock()
            while self._calls and now - self._calls[0] >= self.window:
                self._calls.popleft()
            if len(self._calls) >= self.limit:
                return False
            self._calls.append(now)
            return True


class _Client:
    """Shared GET-JSON helper: timeout, budget, cooldown after 429/5xx, counters, key-free errors."""

    def __init__(self, transport: Transport | None, timeout: float, limiter: RateLimiter,
                 clock: Callable[[], float], cooldown: float) -> None:
        self.transport = transport or default_transport
        self.timeout, self.limiter, self.clock, self.cooldown = timeout, limiter, clock, cooldown
        self.counters = {"requests": 0, "ok": 0, "errors": 0, "throttled": 0, "cooldown_skips": 0}
        self.last_error: str | None = None
        self.skipped = False          # True when the last call was refused locally (budget/cooldown), not a real failure
        self._blocked_until = 0.0

    def get_json(self, url: str, headers: Mapping[str, str]) -> Any | None:
        now = self.clock()
        self.skipped = True
        if now < self._blocked_until:
            self.counters["cooldown_skips"] += 1
            return None
        if not self.limiter.allow():
            self.counters["throttled"] += 1
            return None
        self.skipped = False
        self.counters["requests"] += 1
        try:
            status, body = self.transport(url, headers, self.timeout)
        except Exception as exc:  # never raise into the worker; class name only (no URL, no key)
            return self._fail(type(exc).__name__)
        if status == 429 or status >= 500:
            self._blocked_until = self.clock() + self.cooldown
            return self._fail(f"http_{status}")
        if status != 200:
            return self._fail(f"http_{status}")
        if len(body) > MAX_BODY_BYTES:
            return self._fail("body_too_large")
        try:
            data = json.loads(body, parse_float=Decimal, parse_constant=_reject_constant)
        except (ValueError, TypeError, UnicodeError, RecursionError, ArithmeticError):
            return self._fail("bad_json")
        self.counters["ok"] += 1
        self.last_error = None
        return data

    def _fail(self, reason: str) -> None:
        self.counters["errors"] += 1
        self.last_error = reason
        return None


def _reject_constant(_value: str) -> Any:
    raise ValueError("non-finite number")


def _dec(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    return ts.astimezone(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    try:
        text = str(value).strip().replace("Z", "+00:00")
        if "." in text:  # RFC3339 nanoseconds -> microseconds
            head, rest = text.split(".", 1)
            digits = ""
            while rest and rest[0].isdigit():
                digits, rest = digits + rest[0], rest[1:]
            text = f"{head}.{digits[:6]}{rest}"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _clean_symbol(symbol: Any) -> str | None:
    text = str(symbol or "").strip().upper()
    if 1 <= len(text) <= 10 and all(c.isalnum() or c in ".-" for c in text):
        return text
    return None


# ---- float: FMP ------------------------------------------------------------------
class FmpFloatProvider:
    """Share float from FMP ``/stable/shares-float``. Cached per symbol per New York day.

    Unknown (None) on any failure, a missing/non-positive ``floatShares``, an unparseable
    record, or when the request budget/cooldown is hit. Unknowns are cached only briefly so
    a flaky minute does not hammer the API, and a real float is cached for the whole day.
    FMP returns the *current* float: use for live/paper scans, not historical replays.
    Starter plan allows 300 calls/min; the default budget is 200/min.
    """

    name = "fmp-shares-float"

    def __init__(self, api_key: str, *, transport: Transport | None = None, timeout: float = 5.0,
                 calls_per_minute: int = 200, unknown_ttl: float = 300.0, cooldown: float = 60.0,
                 clock: Callable[[], float] = _time.monotonic) -> None:
        if not api_key:
            raise ValueError("api_key required")
        self._key = api_key
        self._client = _Client(transport, timeout, RateLimiter(calls_per_minute, 60.0, clock), clock, cooldown)
        self._clock, self._unknown_ttl = clock, unknown_ttl
        self._cache: dict[tuple[str, date], int] = {}
        self._unknown: dict[tuple[str, date], float] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # never show the key
        return "FmpFloatProvider()"

    def float_shares(self, symbol: str, as_of: datetime) -> int | None:
        try:
            sym = _clean_symbol(symbol)
            if sym is None:
                return None
            key = (sym, local(_utc(as_of)).date())
            with self._lock:
                if key in self._cache:
                    return self._cache[key]
                if self._unknown.get(key, 0.0) > self._clock():
                    return None
            value = self._fetch(sym)
            with self._lock:
                self._cache = {k: v for k, v in self._cache.items() if k[1] == key[1]}
                if value is None:
                    if not self._client.skipped:   # budget/cooldown refusals are retried as soon as allowed
                        self._unknown[key] = self._clock() + self._unknown_ttl
                else:
                    self._cache[key] = value
            return value
        except Exception:  # defensive: never crash the worker
            self._client.last_error = "unexpected"
            return None

    def _fetch(self, symbol: str) -> int | None:
        url = f"{FMP_HOST}/stable/shares-float?" + urlencode({"symbol": symbol, "apikey": self._key})
        data = self._client.get_json(url, {"Accept": "application/json"})
        if not isinstance(data, list):
            return None
        for record in data:
            if not isinstance(record, dict) or str(record.get("symbol", "")).upper() != symbol:
                continue
            shares = _dec(record.get("floatShares"))
            if shares is None or shares <= 0 or shares != shares.to_integral_value():
                return None
            return int(shares)
        return None

    def status(self) -> dict:
        return {"provider": self.name, **self._client.counters, "last_error": self._client.last_error,
                "cached_symbols": len(self._cache)}


def _alpaca_headers(key_id: str, secret: str) -> dict[str, str]:
    return {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}


# ---- catalyst: Alpaca news -------------------------------------------------------
class AlpacaNewsCatalystProvider:
    """Newest news item tagged to the symbol within ``lookback_hours`` before ``as_of``.

    Items published after ``as_of`` are ignored (no look-ahead). Roundup articles tagged to
    more than ``max_symbols_per_item`` tickers are not treated as a symbol-specific catalyst.
    Results (including "no news") are cached per symbol for ``ttl`` seconds. Alpaca news is
    now sourced from fiscal.ai; ``Catalyst.source`` is "alpaca-news:<source field>".
    """

    name = "alpaca-news"

    def __init__(self, key_id: str, secret: str, *, transport: Transport | None = None, timeout: float = 5.0,
                 lookback_hours: int = 24, max_symbols_per_item: int = 3, ttl: float = 300.0,
                 calls_per_minute: int = 100, cooldown: float = 60.0,
                 clock: Callable[[], float] = _time.monotonic) -> None:
        if not key_id or not secret:
            raise ValueError("credentials required")
        self._headers = _alpaca_headers(key_id, secret)
        self._client = _Client(transport, timeout, RateLimiter(calls_per_minute, 60.0, clock), clock, cooldown)
        self._clock, self._ttl = clock, ttl
        self.lookback_hours, self.max_symbols_per_item = lookback_hours, max_symbols_per_item
        self._cache: dict[str, tuple[float, Catalyst | None]] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "AlpacaNewsCatalystProvider()"

    def latest_catalyst(self, symbol: str, as_of: datetime) -> Catalyst | None:
        try:
            sym = _clean_symbol(symbol)
            if sym is None:
                return None
            now_utc = _utc(as_of)
            with self._lock:
                hit = self._cache.get(sym)
                if hit and hit[0] > self._clock() and hit[1] is not None and hit[1].published_at <= now_utc:
                    return hit[1]
                if hit and hit[0] > self._clock() and hit[1] is None:
                    return None
            result, ok = self._fetch(sym, now_utc)
            if ok:
                with self._lock:
                    self._cache[sym] = (self._clock() + self._ttl, result)
            return result
        except Exception:
            self._client.last_error = "unexpected"
            return None

    def _fetch(self, symbol: str, as_of: datetime) -> tuple[Catalyst | None, bool]:
        start = as_of - timedelta(hours=self.lookback_hours)
        query = urlencode({"symbols": symbol, "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "end": as_of.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 20, "sort": "desc",
                           "include_content": "false"})
        data = self._client.get_json(f"{DATA_API_HOST}/v1beta1/news?{query}", self._headers)
        if not isinstance(data, dict) or not isinstance(data.get("news"), list):
            return None, False
        best: Catalyst | None = None
        for item in data["news"]:
            if not isinstance(item, dict):
                continue
            published = _parse_ts(item.get("created_at"))
            headline = str(item.get("headline") or "").strip()
            tags = item.get("symbols")
            tags = [str(t).upper() for t in tags] if isinstance(tags, list) else []
            if published is None or not headline or published > as_of or symbol not in tags:
                continue
            if len(tags) > self.max_symbols_per_item:
                continue
            if best is None or published > best.published_at:
                best = Catalyst(symbol, headline[:300], published, f"alpaca-news:{str(item.get('source') or '')[:40]}")
        return best, True

    def status(self) -> dict:
        return {"provider": self.name, **self._client.counters, "last_error": self._client.last_error,
                "cached_symbols": len(self._cache)}


# ---- scanner: Alpaca movers ------------------------------------------------------
class AlpacaMoversScanner:
    """Top gainers from Alpaca's screener, enriched with a same-feed relative volume.

    Limits (by design, reported rather than hidden):

    * The stocks movers list resets at 09:30 ET and shows the previous session until then, so
      premarket gappers are NOT visible. A list whose ``last_updated`` is not from today's
      regular session (or is older than ``max_age_seconds``) returns ``[]``.
    * Movers carry no volume, so RVOL = today's daily-bar volume / mean of the previous
      ``rvol_days`` daily-bar volumes, both from the same ``feed`` (snapshots + daily bars).
      Today's partial-day volume vs a full-day average makes this a conservative (low) RVOL
      early in the day. Missing or short history gives RVOL 0, so the gate rejects it.
    * ``premarket_high`` is not available (None), so Gap and Go uses its opening-range path.
    * Only symbols already inside the price window and above the gain threshold are enriched.
    """

    name = "alpaca-movers"

    def __init__(self, key_id: str, secret: str, *, transport: Transport | None = None, timeout: float = 5.0,
                 top: int = 20, feed: str = "iex", rvol_days: int = 20, min_history_days: int = 10,
                 min_price: Decimal = Decimal("2"), max_price: Decimal = Decimal("20"),
                 min_gain_pct: Decimal = Decimal("10"), max_age_seconds: int = 600, ttl: float = 30.0,
                 calls_per_minute: int = 30, cooldown: float = 60.0,
                 clock: Callable[[], float] = _time.monotonic) -> None:
        if not key_id or not secret:
            raise ValueError("credentials required")
        if feed not in {"iex", "sip"}:
            raise ValueError("feed must be iex or sip")
        self._headers = _alpaca_headers(key_id, secret)
        self._client = _Client(transport, timeout, RateLimiter(calls_per_minute, 60.0, clock), clock, cooldown)
        self._clock, self._ttl = clock, ttl
        self.top, self.feed, self.rvol_days, self.min_history_days = max(1, min(int(top), 50)), feed, rvol_days, min_history_days
        self.min_price, self.max_price, self.min_gain_pct = min_price, max_price, min_gain_pct
        self.max_age_seconds = max_age_seconds
        self._cache: tuple[float, list[ScannerRow]] | None = None
        self._lock = threading.Lock()
        self.last_skip: str | None = None

    def __repr__(self) -> str:
        return "AlpacaMoversScanner()"

    def scan(self, as_of: datetime) -> Sequence[ScannerRow]:
        try:
            now_utc = _utc(as_of)
            with self._lock:
                if self._cache and self._cache[0] > self._clock():
                    return list(self._cache[1])
            rows = self._scan(now_utc)
            with self._lock:
                self._cache = (self._clock() + self._ttl, rows)
            return list(rows)
        except Exception:
            self._client.last_error = "unexpected"
            return []

    def _scan(self, as_of: datetime) -> list[ScannerRow]:
        self.last_skip = None
        data = self._client.get_json(f"{DATA_API_HOST}/v1beta1/screener/stocks/movers?top={self.top}", self._headers)
        if not isinstance(data, dict) or not isinstance(data.get("gainers"), list):
            self.last_skip = "movers_unavailable"
            return []
        updated = _parse_ts(data.get("last_updated"))
        today = local(as_of).date()
        if updated is None or local(updated).date() != today or not (-5 <= (as_of - updated).total_seconds() <= self.max_age_seconds):
            self.last_skip = "movers_stale_or_premarket"   # stale list: previous session or lagging
            return []
        picked: list[tuple[int, str, Decimal, Decimal, Decimal]] = []
        for rank, item in enumerate(data["gainers"], start=1):
            if not isinstance(item, dict):
                continue
            sym, last, pct, change = (_clean_symbol(item.get("symbol")), _dec(item.get("price")),
                                      _dec(item.get("percent_change")), _dec(item.get("change")))
            if sym is None or last is None or pct is None or change is None:
                continue
            if not (self.min_price <= last <= self.max_price) or pct < self.min_gain_pct:
                continue
            prior = last - change
            if prior <= 0:
                continue
            picked.append((rank, sym, last, prior, pct))
        if not picked:
            return []
        rvol = self._rvols([p[1] for p in picked], as_of)
        return [ScannerRow(symbol=sym, last=last, prior_close=prior, pct_change=pct,
                           rvol=rvol.get(sym, ZERO), premarket_high=None, rank=rank)
                for rank, sym, last, prior, pct in picked]

    def _rvols(self, symbols: list[str], as_of: datetime) -> dict[str, Decimal]:
        joined = ",".join(symbols)
        today = local(as_of).date()
        snaps = self._client.get_json(
            f"{DATA_API_HOST}/v2/stocks/snapshots?" + urlencode({"symbols": joined, "feed": self.feed}), self._headers)
        start = (as_of - timedelta(days=self.rvol_days * 2 + 10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bars = self._client.get_json(
            f"{DATA_API_HOST}/v2/stocks/bars?" + urlencode({
                "symbols": joined, "timeframe": "1Day", "start": start, "limit": 10000,
                "adjustment": "split", "feed": self.feed, "sort": "asc"}), self._headers)
        if not isinstance(snaps, dict) or not isinstance(bars, dict) or not isinstance(bars.get("bars"), dict):
            return {}
        out: dict[str, Decimal] = {}
        for sym in symbols:
            snap = snaps.get(sym)
            daily = snap.get("dailyBar") if isinstance(snap, dict) else None
            if not isinstance(daily, dict):
                continue
            stamp = _parse_ts(daily.get("t"))
            today_vol = _dec(daily.get("v"))
            # The daily bar timestamp is the session day at 00:00 ET (04:00/05:00 UTC); accept only today's.
            if stamp is None or today_vol is None or local(stamp).date() != today:
                continue
            history = []
            for entry in bars["bars"].get(sym) or []:
                if not isinstance(entry, dict):
                    continue
                ts, vol = _parse_ts(entry.get("t")), _dec(entry.get("v"))
                if ts is not None and vol is not None and vol > 0 and local(ts).date() < today:
                    history.append(vol)
            history = history[-self.rvol_days:]
            if len(history) < self.min_history_days:
                continue
            out[sym] = today_vol / (sum(history) / len(history))
        return out

    def status(self) -> dict:
        return {"provider": self.name, **self._client.counters, "last_error": self._client.last_error,
                "last_skip": self.last_skip}


# ---- opt-in construction ---------------------------------------------------------
def _flag(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def build_provider_set(env: Mapping[str, str], *, transport: Transport | None = None,
                       clock: Callable[[], float] = _time.monotonic) -> ProviderSet:
    """Construct the adapters only when ``DAY_TRADE_DATA_PROVIDERS_ENABLED`` is true.

    Each adapter additionally needs its own credentials (``FMP_API_KEY`` for float;
    ``ALPACA_API_KEY`` + ``ALPACA_SECRET_KEY`` for scanner and news). A missing credential
    leaves that slot empty, so the profile stays blocked. Nothing here reads
    ``DAY_TRADE_PROFILE``, and with the flag off the result is an empty ProviderSet.
    """
    if not _flag(env.get(ENABLE_FLAG)):
        return ProviderSet()
    fmp_key = env.get("FMP_API_KEY", "").strip()
    key_id, secret = env.get("ALPACA_API_KEY", "").strip(), env.get("ALPACA_SECRET_KEY", "").strip()
    feed = env.get("ALPACA_DATA_FEED", "iex").strip().lower()
    feed = feed if feed in {"iex", "sip"} else "iex"
    return ProviderSet(
        float_provider=FmpFloatProvider(fmp_key, transport=transport, clock=clock) if fmp_key else None,
        catalyst_provider=AlpacaNewsCatalystProvider(key_id, secret, transport=transport, clock=clock) if key_id and secret else None,
        gainers_scanner=AlpacaMoversScanner(key_id, secret, transport=transport, feed=feed, clock=clock) if key_id and secret else None)
