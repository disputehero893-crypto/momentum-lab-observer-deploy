"""OANDA v20 PRACTICE (paper) adapter. Live fxTrade hosts are not implemented.

Hosts are a fixed code allowlist, never configuration:

- REST:    https://api-fxpractice.oanda.com
- stream:  https://stream-fxpractice.oanda.com

Any other host — including api-fxtrade.oanda.com and stream-fxtrade.oanda.com,
case variants, trailing DNS dots, explicit ports, userinfo, and subdomain
tricks — is refused. GET observation is available in paper mode when practice
credentials are present. Order/cancel/close calls also require the existing
paper execution gate (MOMENTUM_MODE=paper, PAPER_TRADING_ENABLED, reviewed
config hash, and an empty paper_blockers() list). There is no OANDA-only bypass.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .forex_risk import (
    ForexOrderIntent, ForexRiskConfig, ForexRiskState,
    check_forex_leverage_intent, compute_usd_metrics,
)
from .paper_risk import FOREX_PRACTICE_FEED, PaperIntent, RiskLimits, RiskState, check_paper_intent

PRACTICE_REST_HOST = "api-fxpractice.oanda.com"
PRACTICE_STREAM_HOST = "stream-fxpractice.oanda.com"
PRACTICE_REST_ORIGIN = "https://api-fxpractice.oanda.com"
PRACTICE_STREAM_ORIGIN = "https://stream-fxpractice.oanda.com"
PRACTICE_ROLES = {"rest": PRACTICE_REST_HOST, "stream": PRACTICE_STREAM_HOST}

# Host/URL env names are never used to select a host. If any is set, config fails.
HOST_OVERRIDE_ENV = frozenset({
    "OANDA_API_URL", "OANDA_API_HOST", "OANDA_API_BASE_URL", "OANDA_BASE_URL",
    "OANDA_REST_URL", "OANDA_REST_HOST", "OANDA_HOST", "OANDA_URL",
    "OANDA_STREAM_URL", "OANDA_STREAM_HOST", "OANDA_STREAM_BASE_URL",
    "OANDA_ACCOUNT_HOST", "OANDA_FXTRADE_URL", "OANDA_FXTRADE_HOST",
    "FXTRADE_API_URL", "FXTRADE_API_HOST", "OANDA_PRACTICE_API_URL",
    "OANDA_PRACTICE_HOST", "OANDA_PRACTICE_API_HOST", "OANDA_PRACTICE_BASE_URL",
    "OANDA_PRACTICE_STREAM_URL", "OANDA_PRACTICE_STREAM_HOST",
    "OANDA_PRACTICE_REST_URL", "OANDA_PRACTICE_REST_HOST",
})
ENVIRONMENT_OVERRIDE_ENV = frozenset({"OANDA_ENVIRONMENT", "OANDA_ENV"})
PUBLIC_SECRET_PREFIXES = ("VITE_", "PUBLIC_", "NEXT_PUBLIC_")
PUBLIC_SECRET_SUFFIXES = (
    "OANDA_PRACTICE_API_TOKEN", "OANDA_PRACTICE_ACCOUNT_ID",
    "OANDA_API_TOKEN", "OANDA_API_KEY", "OANDA_ACCOUNT_ID", "OANDA_TOKEN",
)

ACCOUNT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{4,79}$")
INSTRUMENT = re.compile(r"^[A-Z]{3}_[A-Z]{3}$")
ORDER_REF = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
TOKEN_CHAR = re.compile(r"^[!-~]{8,512}$")  # printable ASCII, no space
MAX_RESPONSE_BYTES = 1_000_000

# Conservative majors/crosses only. Dashboard form is slash-separated.
APPROVED_FOREX_PAIRS = frozenset({
    "EUR/USD", "GBP/USD", "USD/JPY", "EUR/GBP", "AUD/USD",
    "USD/CHF", "USD/CAD", "NZD/USD", "EUR/JPY", "GBP/JPY",
})


class OandaPracticeError(RuntimeError):
    """Safe public message; never a provider body, header, or credential."""

    def __init__(self, message="oanda_practice_unavailable", status=None):
        super().__init__(message)
        self.status = status


class OandaHostError(OandaPracticeError):
    def __init__(self, message="non_practice_host_refused"):
        super().__init__(message)


class OandaPracticeDisabled(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OandaPracticeError("HTTP redirects are not allowed for credential-bearing requests", code)


def redact_secret(value):
    """Replace a practice token with a fixed marker. Never echo the value."""
    if not value:
        return ""
    return "***"


def validate_account_id(value):
    if not isinstance(value, str) or ACCOUNT_ID.fullmatch(value) is None:
        raise ValueError("Invalid OANDA practice account identifier")
    if ".." in value or "/" in value or "\\" in value:
        raise ValueError("Invalid OANDA practice account identifier")
    return value


def validate_token(value):
    if not isinstance(value, str) or TOKEN_CHAR.fullmatch(value) is None:
        raise ValueError("Invalid OANDA practice credential")
    return value


def instrument_code(value):
    """Normalize EUR/USD or EUR_USD to the OANDA underscore form."""
    if not isinstance(value, str):
        raise ValueError("Invalid forex instrument")
    code = value.strip().upper().replace("/", "_")
    if INSTRUMENT.fullmatch(code) is None:
        raise ValueError("Invalid forex instrument")
    return code


def display_pair(value):
    return instrument_code(value).replace("_", "/")


def parse_forex_watchlist(raw):
    """Empty or omitted OANDA_FOREX_WATCHLIST is opt-in (no forex observation)."""
    if raw is None or str(raw).strip() == "":
        return ()
    parts = []
    for item in str(raw).split(","):
        item = item.strip()
        if not item:
            continue
        pair = display_pair(item)
        if pair not in APPROVED_FOREX_PAIRS:
            raise ValueError("Forex pair is not on the approved practice allowlist")
        parts.append(pair)
    if not 1 <= len(parts) <= 10 or len(set(parts)) != len(parts):
        raise ValueError("OANDA_FOREX_WATCHLIST must contain 1-10 unique approved pairs")
    return tuple(parts)


def refuse_host_overrides(env):
    """Practice hosts are fixed in code. Any host/URL override env var raises."""
    env = {} if env is None else env
    for name in HOST_OVERRIDE_ENV:
        if env.get(name):
            raise ValueError(f"{name} cannot override the fixed OANDA practice hosts")
    for name in ENVIRONMENT_OVERRIDE_ENV:
        value = env.get(name)
        if value is None or value == "":
            continue
        if str(value).strip().lower() != "practice":
            raise ValueError(f"{name} must be practice; live fxTrade is not implemented")


def refuse_public_oanda_secrets(env):
    """Frontend-prefixed OANDA secrets are refused and never used as credentials."""
    env = {} if env is None else env
    for key in env:
        if not isinstance(key, str) or not env.get(key):
            continue
        upper = key.upper()
        if any(upper.startswith(prefix) for prefix in PUBLIC_SECRET_PREFIXES) and any(
                upper.endswith(suffix) or suffix in upper for suffix in PUBLIC_SECRET_SUFFIXES):
            raise ValueError("OANDA practice credentials cannot use a VITE_ or public prefix")


def assert_practice_url(url, *, role="rest"):
    """Allow only the exact practice host for the requested role. No denylist."""
    if role not in PRACTICE_ROLES:
        raise OandaHostError()
    allowed = PRACTICE_ROLES[role]
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise OandaHostError()
    if any(ch in url for ch in ("\\", "\n", "\r", "\x00", " ", "\t")):
        raise OandaHostError()
    parts = urlsplit(url, allow_fragments=True)
    if parts.scheme != "https" or parts.fragment or ";" in parts.path:
        raise OandaHostError()
    if parts.netloc != allowed:
        raise OandaHostError()
    if parts.hostname != allowed:
        raise OandaHostError()
    if parts.port is not None or parts.username is not None or parts.password is not None:
        raise OandaHostError()
    if not parts.path.startswith("/v3/") or ".." in parts.path or "//" in parts.path[1:]:
        raise OandaHostError()
    if role == "stream" and not parts.path.endswith("/pricing/stream"):
        raise OandaHostError()
    if role == "rest" and parts.path.endswith("/pricing/stream"):
        raise OandaHostError()
    return parts


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("Nonfinite JSON constant")


class OandaPracticeClient:
    """Practice REST client. Tokens are omitted from repr/str."""

    def __init__(self, config, *, transport=None, clock=None):
        self.config = config
        token = getattr(config, "oanda_practice_api_token", "") or ""
        account = getattr(config, "oanda_practice_account_id", "") or ""
        self._token = validate_token(token) if token else ""
        self._account_id = validate_account_id(account) if account else ""
        self._transport = transport or self._http
        self._opener = build_opener(_NoRedirect())
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return (f"OandaPracticeClient(account_configured={bool(self._account_id)}, "
                f"rest_host={PRACTICE_REST_HOST!r}, token={redact_secret(self._token)!r})")

    def __str__(self):
        return self.__repr__()

    def _observe_ready(self):
        if getattr(self.config, "mode", None) != "paper":
            raise OandaPracticeDisabled("OANDA practice observation requires MOMENTUM_MODE=paper")
        if not self._token or not self._account_id:
            raise OandaPracticeDisabled("OANDA practice credentials are missing")

    def _execution_ready(self):
        self._observe_ready()
        if getattr(self.config, "oanda_environment", "") != "practice":
            raise OandaPracticeDisabled("OANDA_ENVIRONMENT must be 'practice' for practice orders")
        if getattr(self.config, "oanda_practice_orders_enabled", False) is not True:
            raise OandaPracticeDisabled("OANDA_PRACTICE_ORDERS_ENABLED must be true; forex is watch-only")
        if not getattr(self.config, "paper_enabled", False):
            raise OandaPracticeDisabled("OANDA practice execution requires PAPER_TRADING_ENABLED=true")
        blockers = list(self.config.paper_blockers()) if hasattr(self.config, "paper_blockers") else ["paper_lock_unavailable"]
        if blockers:
            raise OandaPracticeDisabled("OANDA practice execution requires the existing paper lock: " + ",".join(blockers))

    def _headers(self):
        return {
            "Authorization": "Bearer " + self._token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Datetime-Format": "RFC3339",
            "User-Agent": "momentum-lab-oanda-practice/0.1",
        }

    def _http(self, method, url, headers, payload):
        assert_practice_url(url, role="rest")
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=12) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise OandaPracticeError("OANDA practice response exceeded the payload limit")
                if not raw:
                    return None
                data = json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant,
                                  object_pairs_hook=_unique_json)
                if not isinstance(data, dict):
                    raise OandaPracticeError("OANDA practice returned invalid JSON")
                return data
        except OandaPracticeError:
            raise
        except HTTPError as exc:
            raise OandaPracticeError(f"OANDA practice HTTP {exc.code}", exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise OandaPracticeError("OANDA practice connection failed or timed out") from exc
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError, RecursionError) as exc:
            raise OandaPracticeError("OANDA practice returned invalid JSON") from exc

    def _allowed_path(self, method, path):
        acct = self._account_id
        if not isinstance(path, str) or not path.startswith("/v3/") or any(ch in path for ch in ("://", "?", "#", "\\", "..")):
            return False
        reads = {
            f"/v3/accounts/{acct}",
            f"/v3/accounts/{acct}/summary",
            f"/v3/accounts/{acct}/instruments",
            f"/v3/accounts/{acct}/pricing",
            f"/v3/accounts/{acct}/positions",
            f"/v3/accounts/{acct}/openPositions",
            f"/v3/accounts/{acct}/trades",
            f"/v3/accounts/{acct}/openTrades",
            f"/v3/accounts/{acct}/orders",
            f"/v3/accounts/{acct}/pendingOrders",
        }
        if method == "GET" and path in reads:
            return True
        if method == "GET" and re.fullmatch(r"/v3/instruments/[A-Z]{3}_[A-Z]{3}/candles", path):
            return True
        if method == "GET" and re.fullmatch(
                rf"/v3/accounts/{re.escape(acct)}/instruments/[A-Z]{{3}}_[A-Z]{{3}}/candles", path):
            return True
        if method == "POST" and path == f"/v3/accounts/{acct}/orders":
            return True
        if method == "PUT" and re.fullmatch(
                rf"/v3/accounts/{re.escape(acct)}/orders/{ORDER_REF.pattern[1:-1]}/cancel", path):
            return True
        if method == "PUT" and re.fullmatch(
                rf"/v3/accounts/{re.escape(acct)}/trades/{ORDER_REF.pattern[1:-1]}/close", path):
            return True
        return False

    def _request(self, method, path, *, params=None, payload=None):
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            self._execution_ready()
        else:
            self._observe_ready()
        if method not in {"GET", "POST", "PUT"}:
            raise OandaPracticeError("Unsupported OANDA practice API method")
        if method == "GET" and payload is not None:
            raise OandaPracticeError("GET requests cannot carry a body")
        if not self._allowed_path(method, path):
            raise OandaPracticeError("Only fixed OANDA practice API paths are allowed")
        query = ""
        if params:
            if not isinstance(params, dict):
                raise OandaPracticeError("Invalid query")
            query = "?" + urlencode(params)
        url = PRACTICE_REST_ORIGIN + path + query
        assert_practice_url(url, role="rest")
        return self._transport(method, url, self._headers(), payload)

    def account(self):
        return self._request("GET", f"/v3/accounts/{self._account_id}")

    def summary(self):
        return self._request("GET", f"/v3/accounts/{self._account_id}/summary")

    def instruments(self, pairs=()):
        params = {}
        if pairs:
            params["instruments"] = ",".join(instrument_code(p) for p in pairs)
        return self._request("GET", f"/v3/accounts/{self._account_id}/instruments", params=params or None)

    def pricing(self, pairs):
        instruments = ",".join(instrument_code(p) for p in pairs)
        return self._request("GET", f"/v3/accounts/{self._account_id}/pricing",
                             params={"instruments": instruments, "includeHomeConversions": "false"})

    latest_prices = pricing

    def candles(self, pair, *, granularity="M1", count=10):
        instrument = instrument_code(pair)
        if not isinstance(granularity, str) or not re.fullmatch(r"[SMHD][0-9]{1,3}|W|M", granularity):
            raise ValueError("Invalid candle granularity")
        if type(count) is not int or not 1 <= count <= 500:
            raise ValueError("Invalid candle count")
        return self._request("GET", f"/v3/instruments/{instrument}/candles",
                             params={"price": "MBA", "granularity": granularity, "count": str(count)})

    def positions(self):
        return self._request("GET", f"/v3/accounts/{self._account_id}/positions")

    def open_positions(self):
        return self._request("GET", f"/v3/accounts/{self._account_id}/openPositions")

    def open_trades(self):
        return self._request("GET", f"/v3/accounts/{self._account_id}/openTrades")

    def trades(self, *, state="ALL", count=50):
        if state not in {"OPEN", "CLOSED", "CLOSE_WHEN_TRADEABLE", "ALL"}:
            raise ValueError("Invalid OANDA trade state filter")
        if type(count) is not int or not 1 <= count <= 500:
            raise ValueError("Invalid OANDA trade count")
        return self._request("GET", f"/v3/accounts/{self._account_id}/trades",
                             params={"state": state, "count": str(count)})

    def pending_orders(self):
        return self._request("GET", f"/v3/accounts/{self._account_id}/pendingOrders")

    def submit_order(self, intent, *, risk_state, limits=None, forex_state=None, forex_config=None):
        """Place a practice order only when paper lock, #8 screen, and leverage gate pass."""
        self._execution_ready()
        if not isinstance(intent, dict):
            raise OandaPracticeError("Invalid practice order intent")
        instrument = instrument_code(intent.get("instrument") or intent.get("symbol") or "")
        pair = instrument.replace("_", "/")
        try:
            units = Decimal(str(intent.get("units") or intent.get("quantity") or ""))
            price = Decimal(str(intent.get("price") or ""))
        except (InvalidOperation, TypeError, ValueError, ArithmeticError) as exc:
            raise OandaPracticeError("Invalid practice order size or price") from exc
        if not units.is_finite() or units == 0 or not price.is_finite() or price <= 0:
            raise OandaPracticeError("Invalid practice order size or price")

        raw_stop = intent.get("stop_loss_price")
        if raw_stop is None and isinstance(intent.get("stopLossOnFill"), dict):
            raw_stop = intent["stopLossOnFill"].get("price")
        if raw_stop is None or str(raw_stop).strip() == "":
            raise OandaPracticeError("forex_leverage_rejected:stop_loss_required")
        try:
            stop_loss_price = Decimal(str(raw_stop))
        except (InvalidOperation, TypeError, ValueError, ArithmeticError) as exc:
            raise OandaPracticeError("forex_leverage_rejected:invalid_stop_loss") from exc
        if not stop_loss_price.is_finite() or stop_loss_price <= 0:
            raise OandaPracticeError("forex_leverage_rejected:invalid_stop_loss")

        client_id = intent.get("client_order_id") or (
            intent.get("clientExtensions", {}).get("id")
            if isinstance(intent.get("clientExtensions"), dict) else None
        )
        if not isinstance(client_id, str) or not ORDER_REF.fullmatch(client_id):
            raise OandaPracticeError("A bounded client order id is required")

        order_type = str(intent.get("type") or "LIMIT").upper()
        if order_type not in {"LIMIT", "MARKET_IF_TOUCHED", "MARKET"}:
            raise OandaPracticeError("Unsupported OANDA practice order type")

        if not isinstance(risk_state, RiskState):
            raise OandaPracticeError("A RiskState is required for practice orders")
        if not isinstance(forex_state, ForexRiskState):
            raise OandaPracticeError("A ForexRiskState is required for practice orders")

        limits = limits or RiskLimits(feed=FOREX_PRACTICE_FEED)
        # Equity IEX limits must not be silently reused without the practice feed label.
        if getattr(limits, "feed", None) != FOREX_PRACTICE_FEED:
            raise OandaPracticeError("Forex practice orders require the oanda_practice risk feed")

        if forex_config is None:
            overrides = getattr(self.config, "forex_risk_overrides", None) or {}
            forex_config = ForexRiskConfig(**overrides)

        def _opt_dec(val):
            if val is None or val == "":
                return None
            return val if isinstance(val, Decimal) else Decimal(str(val))

        try:
            base_to_usd = _opt_dec(intent.get("base_to_usd_rate"))
            quote_to_usd = _opt_dec(intent.get("quote_to_usd_rate"))
            pair_margin_rate = _opt_dec(intent.get("pair_margin_rate"))
        except (InvalidOperation, TypeError, ValueError, ArithmeticError) as exc:
            raise OandaPracticeError("Invalid forex conversion or margin rate") from exc

        fx_intent = ForexOrderIntent(
            pair=pair,
            units=units,
            entry_price=price,
            stop_loss_price=stop_loss_price,
            client_order_id=client_id,
            base_to_usd_rate=base_to_usd,
            quote_to_usd_rate=quote_to_usd,
            pair_margin_rate=pair_margin_rate,
        )
        fx_ok, fx_reason = check_forex_leverage_intent(fx_intent, forex_state, forex_config)
        if not fx_ok:
            raise OandaPracticeError(f"forex_leverage_rejected:{fx_reason}")

        usd_per_unit, _, _ = compute_usd_metrics(
            pair, abs(units), price, stop_loss_price,
            base_to_usd_rate=base_to_usd, quote_to_usd_rate=quote_to_usd,
        )
        screen = PaperIntent(pair, abs(units), usd_per_unit, client_id)
        approved, reason = check_paper_intent(screen, risk_state, limits)
        if not approved:
            raise OandaPracticeError(f"paper_risk_rejected:{reason}")

        tif = str(intent.get("time_in_force") or intent.get("timeInForce") or (
            "FOK" if order_type == "MARKET" else "GTC"
        )).upper()
        if order_type == "MARKET" and tif not in {"FOK", "IOC"}:
            raise OandaPracticeError("MARKET practice orders require FOK or IOC timeInForce")
        if order_type in {"LIMIT", "MARKET_IF_TOUCHED"} and tif not in {"GTC", "GTD", "GFD", "FOK", "IOC"}:
            raise OandaPracticeError("Unsupported timeInForce for practice order")

        order = {
            "instrument": instrument,
            "units": format(units, "f"),
            "type": order_type,
            "timeInForce": tif,
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": format(stop_loss_price, "f"), "timeInForce": "GTC"},
            "clientExtensions": {"id": client_id},
        }
        if order_type == "MARKET":
            raw_bound = intent.get("price_bound") or intent.get("priceBound")
            if raw_bound is not None and str(raw_bound).strip() != "":
                try:
                    bound = Decimal(str(raw_bound))
                except (InvalidOperation, TypeError, ValueError, ArithmeticError) as exc:
                    raise OandaPracticeError("Invalid priceBound for MARKET order") from exc
                if not bound.is_finite() or bound <= 0:
                    raise OandaPracticeError("Invalid priceBound for MARKET order")
                if units > 0 and bound < price:
                    raise OandaPracticeError("Buy priceBound cannot be below reference quote")
                if units < 0 and bound > price:
                    raise OandaPracticeError("Sell priceBound cannot be above reference quote")
                order["priceBound"] = format(bound, "f")
        else:
            order["price"] = format(price, "f")

        raw_tp = intent.get("take_profit_price")
        if raw_tp is None and isinstance(intent.get("takeProfitOnFill"), dict):
            raw_tp = intent["takeProfitOnFill"].get("price")
        if raw_tp is not None and str(raw_tp).strip() != "":
            try:
                tp_price = Decimal(str(raw_tp))
            except (InvalidOperation, TypeError, ValueError, ArithmeticError) as exc:
                raise OandaPracticeError("Invalid takeProfitOnFill price") from exc
            if not tp_price.is_finite() or tp_price <= 0:
                raise OandaPracticeError("Invalid takeProfitOnFill price")
            if (units > 0 and tp_price <= price) or (units < 0 and tp_price >= price):
                raise OandaPracticeError("Take profit price is on the wrong side of entry")
            order["takeProfitOnFill"] = {"price": format(tp_price, "f"), "timeInForce": "GTC"}

        return self._request("POST", f"/v3/accounts/{self._account_id}/orders", payload={"order": order})

    def cancel_order(self, order_id):
        self._execution_ready()
        if not isinstance(order_id, str) or not ORDER_REF.fullmatch(order_id):
            raise OandaPracticeError("Invalid practice order id")
        return self._request("PUT", f"/v3/accounts/{self._account_id}/orders/{order_id}/cancel")

    def close_trade(self, trade_id):
        """Protective flatten. This path does not run the entry-only risk screen."""
        self._execution_ready()
        if not isinstance(trade_id, str) or not ORDER_REF.fullmatch(trade_id):
            raise OandaPracticeError("Invalid practice trade id")
        return self._request("PUT", f"/v3/accounts/{self._account_id}/trades/{trade_id}/close")


class OandaPracticeStreamClient:
    """GET-only pricing stream on the practice stream host."""

    def __init__(self, config, *, transport=None, clock=None):
        self.config = config
        token = getattr(config, "oanda_practice_api_token", "") or ""
        account = getattr(config, "oanda_practice_account_id", "") or ""
        self._token = validate_token(token) if token else ""
        self._account_id = validate_account_id(account) if account else ""
        self._transport = transport or self._http
        self._opener = build_opener(_NoRedirect())
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return (f"OandaPracticeStreamClient(account_configured={bool(self._account_id)}, "
                f"stream_host={PRACTICE_STREAM_HOST!r}, token={redact_secret(self._token)!r})")

    def _observe_ready(self):
        if getattr(self.config, "mode", None) != "paper":
            raise OandaPracticeDisabled("OANDA practice streaming requires MOMENTUM_MODE=paper")
        if not self._token or not self._account_id:
            raise OandaPracticeDisabled("OANDA practice credentials are missing")

    def _headers(self):
        return {
            "Authorization": "Bearer " + self._token,
            "Accept": "application/octet-stream",
            "Accept-Datetime-Format": "RFC3339",
            "User-Agent": "momentum-lab-oanda-practice/0.1",
        }

    def pricing_stream_url(self, pairs):
        instruments = ",".join(instrument_code(p) for p in pairs)
        path = f"/v3/accounts/{self._account_id}/pricing/stream"
        url = PRACTICE_STREAM_ORIGIN + path + "?" + urlencode({"instruments": instruments})
        assert_practice_url(url, role="stream")
        return url

    def _http(self, method, url, headers, payload):
        assert_practice_url(url, role="stream")
        if method != "GET" or payload is not None:
            raise OandaPracticeDisabled("OANDA practice streaming is GET-only")
        request = Request(url, headers=headers, method="GET")
        try:
            with self._opener.open(request, timeout=12) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise OandaPracticeError("OANDA practice stream exceeded the payload limit")
                lines = []
                for line in raw.split(b"\n"):
                    line = line.strip()
                    if not line:
                        continue
                    item = json.loads(line, parse_float=Decimal, parse_constant=_reject_constant,
                                      object_pairs_hook=_unique_json)
                    lines.append(item)
                return lines
        except OandaPracticeError:
            raise
        except HTTPError as exc:
            raise OandaPracticeError(f"OANDA practice HTTP {exc.code}", exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise OandaPracticeError("OANDA practice connection failed or timed out") from exc
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError, RecursionError) as exc:
            raise OandaPracticeError("OANDA practice returned invalid JSON") from exc

    def iter_pricing(self, pairs):
        self._observe_ready()
        url = self.pricing_stream_url(pairs)
        result = self._transport("GET", url, self._headers(), None)
        if result is None:
            return
        for item in result:
            yield item
