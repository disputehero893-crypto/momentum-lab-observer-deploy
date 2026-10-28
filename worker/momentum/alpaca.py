"""Alpaca paper-only HTTP adapter. No configurable execution host or redirects."""
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from .config import PAPER_API_HOST, DATA_API_HOST, SYMBOL
from .journal import utc_now

class PaperDisabled(RuntimeError):
    pass

class BrokerError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status

class AmbiguousSubmission(BrokerError):
    pass

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BrokerError("HTTP redirects are not allowed for credential-bearing requests", code)

def decimal_string(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Price must be finite")
    return format(number, "f")

def parse_time(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include its time zone")
    return parsed.astimezone(timezone.utc)


def _bars_json(body):
    """Preserve numeric literals only on the stock-bars observation route."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    def no_constant(_value):
        raise ValueError
    try:
        return json.loads(body, parse_float=Decimal, parse_constant=no_constant, object_pairs_hook=unique)
    except (ValueError, TypeError, UnicodeError, ArithmeticError, RecursionError):
        raise BrokerError("Alpaca returned invalid stock bars JSON") from None


class PaperClient:
    def __init__(self, config, *, transport=None, clock=None):
        self.config = config
        self._transport = transport or self._http
        self._opener = build_opener(NoRedirect())
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _enabled(self):
        if self.config.mode != "paper" or not self.config.paper_enabled:
            raise PaperDisabled("Paper API requires MOMENTUM_MODE=paper and PAPER_TRADING_ENABLED=true")
        if not self.config.api_key or not self.config.api_secret:
            raise PaperDisabled("Paper API credentials are missing")

    def _http(self, method, url, headers, payload):
        request = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                          headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=12) as response:
                # Read one byte past the limit so a valid-looking truncated
                # prefix is never accepted as a complete broker response.
                body = response.read(8_000_001)
                if len(body) > 8_000_000:
                    raise BrokerError("Alpaca response exceeded the payload limit")
                if body and method == "GET" and urlsplit(url).path == "/v2/stocks/bars":
                    return _bars_json(body)
                return json.loads(body) if body else None
        except HTTPError as exc:
            # Do not echo response bodies, credentials, query strings or headers into logs.
            raise BrokerError(f"Alpaca HTTP {exc.code}", exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BrokerError("Alpaca connection failed or timed out") from exc
        except json.JSONDecodeError as exc:
            raise BrokerError("Alpaca returned invalid JSON") from exc
        except UnicodeDecodeError as exc:
            raise BrokerError("Alpaca returned invalid text encoding") from exc

    def _request(self, method, path, *, params=None, payload=None, data=False):
        self._enabled()
        if not isinstance(path, str) or not path.startswith("/v2/") or "://" in path or "#" in path or "?" in path:
            raise BrokerError("Only fixed /v2/ API paths are allowed")
        if ".." in path or "\\" in path:
            raise BrokerError("Invalid API path")
        if data and method != "GET":
            raise BrokerError("Market-data adapter is read-only")
        if path.startswith("/v2/account/activities"):
            # This integration intentionally has one unfiltered, read-only
            # activities route. Do not accept caller-supplied paths, categories,
            # dates, types or order filters that could hide account activity.
            from .activities import activity_request_params
            if path != "/v2/account/activities" or method != "GET" or data or payload is not None:
                raise BrokerError("Only the fixed read-only account activities route is allowed")
            try:
                allowed = {"after", "until", "direction", "page_size", "page_token"}
                if not isinstance(params, dict) or set(params) - allowed or params.get("direction") != "asc":
                    raise ValueError
                checked = activity_request_params(after=params.get("after"), until=params.get("until"),
                                                  page_size=params.get("page_size"),
                                                  page_token=params.get("page_token"))
            except (TypeError, ValueError, OverflowError):
                raise BrokerError("Invalid account activities query") from None
            params = checked
        host = DATA_API_HOST if data else PAPER_API_HOST
        url = host + path + ("?" + urlencode(params) if params else "")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != urlsplit(host).netloc:
            raise BrokerError("API host lock rejected request")
        if method not in ({"GET"} if data else {"GET", "POST", "DELETE"}):
            raise BrokerError("Unsupported paper API method")
        headers = {"APCA-API-KEY-ID": self.config.api_key,
                   "APCA-API-SECRET-KEY": self.config.api_secret,
                   "Accept": "application/json", "Content-Type": "application/json",
                   "User-Agent": "momentum-lab-paper/0.1.0"}
        return self._transport(method, url, headers, payload)

    def account(self):
        return self._request("GET", "/v2/account")

    def positions(self):
        return self._request("GET", "/v2/positions")

    def activity_page(self, *, after, until, page_token=None, page_size=100):
        """One bounded GET page; callers must use the importer for coverage.

        Alpaca's bounds refer to creation time, not a fill's execution time or
        a fee's reported activity/settlement date. All activity types are
        requested so unsupported financial events cannot disappear in a filter.
        """
        from .activities import activity_request_params, validate_activity_page
        params = activity_request_params(after=after, until=until,
                                         page_token=page_token, page_size=page_size)
        page = self._request("GET", "/v2/account/activities", params=params)
        return validate_activity_page(page, page_size=page_size)

    def open_orders(self):
        orders = self._request("GET", "/v2/orders", params={"status": "open", "limit": 500, "direction": "asc"})
        if len(orders) >= 500:
            raise BrokerError("Open-order response at limit; complete reconciliation is required")
        return orders

    def order_by_client_id(self, client_id):
        try:
            return self._request("GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_id})
        except BrokerError as exc:
            if exc.status == 404:
                return None
            raise

    def calendar(self, start, end):
        return self._request("GET", "/v2/calendar", params={"start": start, "end": end})

    def snapshots(self, symbols):
        if not symbols or any(not SYMBOL.fullmatch(s) for s in symbols):
            raise ValueError("A validated equity watchlist is required")
        return self._request("GET", "/v2/stocks/snapshots",
                             params={"symbols": ",".join(symbols), "feed": self.config.data_feed}, data=True)

    def observation_bars(self, symbols, *, timeframe, start, end):
        """One bounded raw observation page; the collector rejects partial pages.

        Symbol mapping is disabled explicitly. This method never follows a page
        token or infers coverage from a successful request.
        """
        from .activities import timestamp_parts
        if (not isinstance(symbols, (list, tuple)) or not 1 <= len(symbols) <= 10
                or any(not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol) for symbol in symbols)
                or len(set(symbols)) != len(symbols) or not isinstance(timeframe, str)
                or timeframe not in {"1Min", "1Day"}):
            raise ValueError("Invalid bounded bar observation request")
        start_stamp, start_ns = timestamp_parts(start)
        end_stamp, end_ns = timestamp_parts(end)
        maximum = (600 if timeframe == "1Min" else 26 * 3600) * 1_000_000_000
        if not 0 <= end_ns - start_ns <= maximum:
            raise ValueError("Bar observation request exceeds its bounded window")
        return self._request("GET", "/v2/stocks/bars", params={
            "symbols": ",".join(symbols), "timeframe": timeframe, "start": start_stamp, "end": end_stamp,
            "limit": 1000, "adjustment": "raw", "feed": self.config.data_feed, "sort": "asc",
            "asof": "-", "currency": "USD"}, data=True)

    def bars(self, symbol, start, end):
        if not SYMBOL.fullmatch(symbol):
            raise ValueError("Invalid symbol")
        result = []
        token = None
        seen = set()
        for _ in range(50):
            params = {"symbols": symbol, "timeframe": "1Min", "start": start, "end": end,
                      "limit": 10000, "adjustment": "raw", "feed": self.config.data_feed, "sort": "asc"}
            if token:
                params["page_token"] = token
            page = self._request("GET", "/v2/stocks/bars", params=params, data=True)
            result.extend(page.get("bars", {}).get(symbol, []))
            token = page.get("next_page_token")
            if not token:
                return result
            if token in seen:
                raise BrokerError("Repeated historical-data pagination token")
            seen.add(token)
        raise BrokerError("Historical-data page cap reached; no incomplete baseline is accepted")

    @staticmethod
    def client_order_id(intent):
        # Stable across restarts/instances: never contains the local run ID.
        setup = str(intent.get("setup_id", ""))
        if not setup:
            raise ValueError("Every paper order needs a stable setup_id")
        identity = "|".join([intent["symbol"], intent["side"], setup, str(intent.get("exit_generation", "0"))])
        return "mlab_" + sha256(identity.encode()).hexdigest()[:36]

    def submit(self, intent, journal, run_id, *, quote, guard):
        """Persist before POST; recover by ID, never blindly repeat an ambiguous POST."""
        self._enabled()
        blockers = self.config.paper_blockers()
        if blockers:
            raise PaperDisabled("Paper execution is blocked: " + ", ".join(blockers))
        from .strategy import StrategyConfig
        from .risk import RiskConfig
        if intent.get("mode") != "paper" or intent.get("profile") != "experimental_v1":
            raise PaperDisabled("Unapproved intention environment or profile")
        if intent.get("config_id") != StrategyConfig(**self.config.strategy_overrides).config_id:
            raise PaperDisabled("Strategy configuration changed since the intention")
        if intent.get("side") == "buy" and intent.get("risk_config_id") != RiskConfig(**self.config.risk_overrides).config_id:
            raise PaperDisabled("Entry is missing a current risk approval")
        symbol = intent.get("symbol", "")
        if not SYMBOL.fullmatch(symbol) or intent.get("side") not in {"buy", "sell"}:
            raise ValueError("Only long-equity buy/sell intents are supported")
        if symbol not in self.config.watchlist:
            raise PaperDisabled("Symbol is outside the reviewed watchlist")
        if intent.get("feed") != self.config.data_feed or quote.get("feed") != self.config.data_feed:
            raise PaperDisabled("Synthetic or mismatched feeds cannot reach the paper execution adapter")
        if quote.get("symbol", symbol) != symbol:
            raise PaperDisabled("Quote symbol does not match the order")
        qty = Decimal(str(intent.get("qty", "0")))
        if not qty.is_finite() or qty <= 0 or qty != qty.to_integral_value():
            raise ValueError("Paper qty must be a positive whole-share amount")
        price = Decimal(str(intent.get("entry_limit", intent.get("limit_price", "0"))))
        if not price.is_finite() or price <= 0:
            raise ValueError("Paper limit price must be positive and finite")
        increment = Decimal("0.01") if price >= 1 else Decimal("0.0001")
        if price != price.quantize(increment):
            raise ValueError("Limit price violates Alpaca equity tick precision")
        client_id = self.client_order_id(intent)
        payload = {"symbol": symbol, "qty": str(int(qty)), "side": intent["side"], "type": "limit",
                   "time_in_force": "day", "limit_price": decimal_string(price),
                   "extended_hours": True, "client_order_id": client_id}
        key = "paper-request:" + client_id
        prior = journal.event_by_key(key)
        existing = self.order_by_client_id(client_id)
        if existing is not None:
            journal.append(run_id, "paper_order_recovered", {"client_order_id": client_id, "order": existing},
                           key="recovered:" + run_id + ":" + client_id + ":" + sha256(json.dumps(existing, sort_keys=True).encode()).hexdigest()[:16])
            return existing
        if prior:
            # A definitive rejection/not-sent event can be inspected, but this setup still does not retry automatically.
            raise AmbiguousSubmission("Previously attempted order not found; automatic resubmission blocked")
        guard()
        age = (self.clock() - parse_time(quote["timestamp"])).total_seconds()
        receipt_age = (self.clock() - parse_time(quote["received_at"])).total_seconds()
        if age < 0 or receipt_age < 0 or max(age, receipt_age) > self.config.quote_max_age_seconds:
            raise PaperDisabled("Quote stale before order submission")
        intent_age = (self.clock()-parse_time(intent["observed_at"])).total_seconds()
        if not 0 <= intent_age <= 2:
            raise PaperDisabled("Intention stale before order submission")
        if intent["side"] == "buy":
            risk_age = (self.clock()-parse_time(intent["risk_checked_at"])).total_seconds()
            if not 0 <= risk_age <= 2 or intent.get("paper_only") is not True or intent.get("live_enabled") is not False:
                raise PaperDisabled("A fresh paper risk approval is required")
        journal.append(run_id, "paper_order_requested", {"client_order_id": client_id, "intent": intent, "request": payload},
                       key=key)
        # Recheck the locally durable kill switch immediately before the external request.
        try:
            guard()
        except Exception:
            journal.append(run_id, "paper_order_not_sent", {"client_order_id": client_id, "reason": "execution_gate_changed"},
                           key="not-sent:" + client_id)
            raise
        try:
            order = self._request("POST", "/v2/orders", payload=payload)
        except BrokerError as exc:
            definite = exc.status is not None and 400 <= exc.status < 500 and exc.status not in {408, 409, 429}
            event = "paper_order_rejected" if definite else "paper_submission_uncertain"
            journal.append(run_id, event, {"client_order_id": client_id, "http_status": exc.status},
                           key=event + ":" + client_id)
            if definite:
                raise
            raise AmbiguousSubmission("Order submission uncertain; reconcile by client_order_id before proceeding") from exc
        journal.append(run_id, "paper_order_acknowledged", {"client_order_id": client_id, "order": order},
                       key="ack:" + client_id)
        return order

    def cancel(self, order, journal, run_id):
        """Reconcile before retrying a cancel, at most three attempts per order.

        Cancel acceptance is not confirmation or flatness. A pending cancel is
        observed, not repeated. An unresolved third attempt needs operator review.
        """
        self._enabled()
        order_id = str(order.get("id", ""))
        client_id = str(order.get("client_order_id", ""))
        if not client_id.startswith("mlab_") or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", order_id):
            raise PaperDisabled("Only this worker's known paper orders may be canceled")
        if not journal.event_by_key("paper-request:" + client_id):
            raise PaperDisabled("Cannot cancel an order absent from the durable request journal")
        fresh = self.order_by_client_id(client_id)
        if fresh is None or str(fresh.get("id")) != order_id:
            raise BrokerError("Order identity could not be reconciled before cancellation")
        status = fresh.get("status")
        if status in {"filled", "canceled", "expired", "rejected", "replaced"}:
            journal.append(run_id, "paper_cancel_resolved", {"order": fresh},
                           key=f"cancel-resolved:{run_id}:{order_id}:{status}")
            return fresh
        if status == "pending_cancel":
            return fresh
        previous = [e for e in journal.events(event_type="paper_cancel_requested")
                    if e["payload"]["order_id"] == order_id]
        if len(previous) >= 3:
            raise BrokerError("Cancellation retry limit reached; operator reconciliation required")
        if previous and (self.clock()-parse_time(previous[-1]["timestamp"])).total_seconds() < 2:
            return fresh
        attempt = len(previous)+1
        key = f"cancel-request:{order_id}:{attempt}"
        journal.append(run_id, "paper_cancel_requested",
                       {"order_id": order_id, "client_order_id": client_id, "attempt": attempt},
                       key=key, timestamp=self.clock().isoformat())
        try:
            self._request("DELETE", "/v2/orders/" + order_id)
            journal.append(run_id, "paper_cancel_accepted", {"order_id": order_id, "confirmed_canceled": False},
                           key=f"cancel-accepted:{order_id}:{attempt}")
            return {**fresh, "cancel_request_accepted": True, "confirmed_canceled": False}
        except BrokerError as exc:
            journal.append(run_id, "paper_cancel_unconfirmed", {"order_id": order_id, "http_status": exc.status},
                           key=f"cancel-unconfirmed:{order_id}:{attempt}")
            raise

