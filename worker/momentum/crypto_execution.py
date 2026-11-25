"""PAPER-ONLY Alpaca crypto orders.

The only trading host this module will call is https://paper-api.alpaca.markets.
https://api.alpaca.markets is refused before any request is built. Order placement
is off unless CRYPTO_PAPER_EXECUTION_ENABLED is true, and even then it uses the
same paper lock as equity orders: MOMENTUM_MODE=paper, PAPER_TRADING_ENABLED,
PAPER_REVIEWED_CONFIG_HASH, and an empty Config.paper_blockers() list.

Crypto-specific differences from the equity adapter: slash symbols (BTC/USD),
fractional qty, 24/7 (no equity session or extended_hours), and time_in_force
gtc or ioc. Risk still goes through check_paper_intent plus RiskConfig.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import PAPER_API_HOST
from .paper_risk import (
    CRYPTO_PAPER_FEED, CRYPTO_PAPER_PAIRS, PaperIntent, RiskLimits, RiskState,
    check_paper_intent,
)
from .risk import RiskConfig

PAPER_HOST = "paper-api.alpaca.markets"
LIVE_HOST = "api.alpaca.markets"
PAPER_ORIGIN = "https://paper-api.alpaca.markets"
ORDER_PATH = "/v2/orders"
CLIENT_ID = re.compile(r"^mlab_[A-Za-z0-9]{8,40}$")
_PUBLIC_ORDER_KEYS = (
    "id", "client_order_id", "symbol", "qty", "side", "type",
    "time_in_force", "limit_price", "status", "asset_class",
)


class CryptoPaperError(RuntimeError):
    """Fixed message only. Never a broker body, header, or credential."""

    def __init__(self, message="crypto_paper_order_refused"):
        super().__init__(message)


class CryptoPaperHostError(CryptoPaperError):
    def __init__(self, message="crypto_order_host_refused"):
        super().__init__(message)


class CryptoPaperDisabled(CryptoPaperError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CryptoPaperError("crypto_order_redirect_refused")


def _bare_host(value):
    if not isinstance(value, str) or not value:
        return ""
    text = value.strip()
    if "://" in text:
        host = urlsplit(text).hostname or ""
    else:
        host = text
        if "@" in host:
            host = host.rsplit("@", 1)[1]
        host = host.split(":", 1)[0]
    return host.lower().rstrip(".")


def _is_live_host(value):
    return _bare_host(value) == LIVE_HOST


def assert_paper_crypto_order_url(url):
    """Allow only the exact paper orders URL. The live trading host never passes."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise CryptoPaperHostError("crypto_order_host_refused")
    if any(ch in url for ch in ("\\", "\n", "\r", "\x00", " ", "\t")):
        raise CryptoPaperHostError("crypto_order_host_refused")
    if _is_live_host(url):
        raise CryptoPaperHostError("live_trading_host_refused")
    parts = urlsplit(url)
    if _is_live_host(parts.hostname) or _is_live_host(parts.netloc):
        raise CryptoPaperHostError("live_trading_host_refused")
    if parts.scheme != "https" or parts.fragment or parts.query or ";" in parts.path:
        raise CryptoPaperHostError("crypto_order_host_refused")
    if parts.netloc != PAPER_HOST or parts.hostname != PAPER_HOST:
        raise CryptoPaperHostError("crypto_order_host_refused")
    if parts.port is not None or parts.username is not None or parts.password is not None:
        raise CryptoPaperHostError("crypto_order_host_refused")
    if parts.path != ORDER_PATH or ".." in parts.path:
        raise CryptoPaperHostError("crypto_order_host_refused")
    return parts


def paper_order_url():
    """The only URL a crypto order may use. A drifted host constant fails closed."""
    configured = str(PAPER_API_HOST).strip().rstrip("/")
    if _is_live_host(configured):
        raise CryptoPaperHostError("live_trading_host_refused")
    if configured != PAPER_ORIGIN:
        raise CryptoPaperHostError("crypto_order_host_refused")
    url = PAPER_ORIGIN + ORDER_PATH
    assert_paper_crypto_order_url(url)
    return url


def _positive_decimal(value, code):
    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise CryptoPaperError(code)
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, ArithmeticError):
        raise CryptoPaperError(code) from None
    exponent = number.as_tuple().exponent
    if (not number.is_finite() or number <= 0 or not isinstance(exponent, int)
            or exponent < -9 or number > Decimal("1000000")):
        raise CryptoPaperError(code)
    return number


def _decimal_string(number):
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _public_order(order):
    if not isinstance(order, dict):
        return {"accepted": order is not None}
    return {key: order[key] for key in _PUBLIC_ORDER_KEYS if key in order}


def _journal_halted(journal):
    if journal is None:
        return False
    try:
        state = journal.kill_switch()
    except Exception:
        return True
    return not isinstance(state, dict) or state.get("enabled") is not False


class CryptoPaperClient:
    """Submit one Alpaca paper crypto order. Credentials stay off repr and results."""

    def __init__(self, config, *, transport=None, clock=None):
        self.config = config
        self._transport = transport or self._http
        self._opener = build_opener(_NoRedirect())
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __repr__(self):
        present = bool(getattr(self.config, "api_key", "") and getattr(self.config, "api_secret", ""))
        enabled = getattr(self.config, "crypto_paper_execution_enabled", False) is True
        return (f"CryptoPaperClient(credentials_configured={present}, "
                f"host={PAPER_HOST!r}, execution_enabled={enabled})")

    def __str__(self):
        return self.__repr__()

    def _execution_ready(self):
        if getattr(self.config, "crypto_paper_execution_enabled", False) is not True:
            raise CryptoPaperDisabled(
                "CRYPTO_PAPER_EXECUTION_ENABLED is false; crypto stays observation-only")
        if not hasattr(self.config, "paper_blockers"):
            raise CryptoPaperDisabled("crypto paper execution blocked: paper_lock_unavailable")
        blockers = list(self.config.paper_blockers())
        if blockers:
            raise CryptoPaperDisabled("crypto paper execution blocked: " + ",".join(blockers))

    def _headers(self):
        key = getattr(self.config, "api_key", "") or ""
        secret = getattr(self.config, "api_secret", "") or ""
        if not key or not secret:
            raise CryptoPaperDisabled("crypto paper execution blocked: paper_credentials_missing")
        return {
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _http(self, method, url, headers, payload):
        assert_paper_crypto_order_url(url)
        if method != "POST":
            raise CryptoPaperError("crypto_order_method_refused")
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=12) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise CryptoPaperError("crypto_order_response_too_large")
                return json.loads(raw) if raw else None
        except CryptoPaperError:
            raise
        except HTTPError:
            raise CryptoPaperError("crypto_order_submission_failed") from None
        except (URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeError):
            raise CryptoPaperError("crypto_order_submission_failed") from None

    def _observed_at(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise CryptoPaperError("crypto_clock_must_be_timezone_aware")
        return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _build(self, order):
        if not isinstance(order, dict):
            raise CryptoPaperError("invalid_crypto_order")
        if order.get("extended_hours") not in (None, False):
            raise CryptoPaperError("crypto_orders_do_not_use_extended_hours")
        symbol = str(order.get("symbol") or "").strip().upper()
        if symbol not in CRYPTO_PAPER_PAIRS:
            raise CryptoPaperError("crypto_symbol_not_approved")
        watchlist = tuple(getattr(self.config, "crypto_watchlist", ()) or ())
        if symbol not in watchlist:
            raise CryptoPaperError("crypto_symbol_not_in_watchlist")
        side = str(order.get("side") or "").strip().lower()
        if side not in {"buy", "sell"}:
            raise CryptoPaperError("invalid_crypto_side")
        qty = _positive_decimal(order.get("qty"), "invalid_crypto_qty")
        order_type = str(order.get("type") or "limit").strip().lower()
        tif = str(order.get("time_in_force") or "").strip().lower()
        if order_type == "market":
            if tif not in {"", "ioc"}:
                raise CryptoPaperError("crypto_market_orders_require_ioc")
            tif = "ioc"
            price = _positive_decimal(order.get("price") or order.get("limit_price"), "invalid_crypto_price")
            payload = {
                "symbol": symbol, "qty": _decimal_string(qty), "side": side,
                "type": "market", "time_in_force": tif,
            }
        elif order_type == "limit":
            if tif not in {"", "gtc", "ioc"}:
                raise CryptoPaperError("crypto_time_in_force_must_be_gtc_or_ioc")
            tif = tif or "gtc"
            price = _positive_decimal(order.get("limit_price") or order.get("price"), "invalid_crypto_price")
            payload = {
                "symbol": symbol, "qty": _decimal_string(qty), "side": side,
                "type": "limit", "time_in_force": tif, "limit_price": _decimal_string(price),
            }
        else:
            raise CryptoPaperError("unsupported_crypto_order_type")
        client_id = order.get("client_order_id")
        if not isinstance(client_id, str) or CLIENT_ID.fullmatch(client_id) is None:
            raise CryptoPaperError("invalid_client_order_id")
        payload["client_order_id"] = client_id
        stop = order.get("stop_price")
        stop_price = None if stop is None or stop == "" else _positive_decimal(stop, "invalid_crypto_stop")
        return payload, price, qty, stop_price

    def _risk_config(self):
        overrides = getattr(self.config, "risk_overrides", None) or {}
        if not isinstance(overrides, dict):
            raise CryptoPaperError("paper_risk_rejected:invalid_numeric_state")
        try:
            return RiskConfig(**overrides)
        except (TypeError, ValueError, ArithmeticError):
            raise CryptoPaperError("paper_risk_rejected:invalid_numeric_state") from None

    def _tighten(self, limits, risk_cfg):
        limits = limits or RiskLimits(feed=CRYPTO_PAPER_FEED)
        if getattr(limits, "feed", None) != CRYPTO_PAPER_FEED:
            raise CryptoPaperError("crypto_orders_require_paper_crypto_feed")
        try:
            return RiskLimits(
                max_order_notional=min(Decimal(limits.max_order_notional), risk_cfg.max_notional),
                max_total_exposure=Decimal(limits.max_total_exposure),
                max_daily_loss=min(Decimal(limits.max_daily_loss), risk_cfg.max_daily_loss),
                max_open_positions=limits.max_open_positions,
                max_feed_age_ms=limits.max_feed_age_ms,
                feed=CRYPTO_PAPER_FEED,
            )
        except (InvalidOperation, TypeError, ValueError, ArithmeticError):
            raise CryptoPaperError("paper_risk_rejected:invalid_numeric_state") from None

    def _pre_kill(self, risk_state, risk_manager, journal):
        if not isinstance(risk_state, RiskState) or risk_state.kill_switch is not False:
            raise CryptoPaperError("paper_risk_rejected:kill_switch_or_invalid_state")
        if risk_manager is not None and getattr(risk_manager, "locked", None) is not False:
            raise CryptoPaperError("paper_risk_rejected:kill_switch_or_invalid_state")
        if _journal_halted(journal):
            raise CryptoPaperError("paper_risk_rejected:kill_switch_or_invalid_state")

    def _enforce(self, payload, price, qty, stop_price, risk_state, limits, risk_manager, journal):
        self._pre_kill(risk_state, risk_manager, journal)
        risk_cfg = self._risk_config()
        if qty > Decimal(risk_cfg.max_shares):
            raise CryptoPaperError("paper_risk_rejected:max_shares")
        if stop_price is not None:
            per_unit = abs(price - stop_price)
            if per_unit <= 0 or qty * per_unit > risk_cfg.per_trade_risk:
                raise CryptoPaperError("paper_risk_rejected:per_trade_risk")
        screen = PaperIntent(payload["symbol"], qty, price, payload["client_order_id"])
        approved, reason = check_paper_intent(screen, risk_state, self._tighten(limits, risk_cfg))
        if not approved:
            raise CryptoPaperError("paper_risk_rejected:" + reason)

    def submit_order(self, order=None, *, risk_state, limits=None, journal=None,
                     run_id="crypto-paper", risk_manager=None, protective=False):
        """Place one paper crypto order. The URL is not caller-selectable.

        `protective=True` is a sell of a position this loop already holds. It
        still requires the paper lock, the paper host, and a clear kill switch.
        The entry notional screen is not applied to that sell.
        """
        return self.submit_to(
            paper_order_url(), order, risk_state=risk_state, limits=limits, journal=journal,
            run_id=run_id, risk_manager=risk_manager, protective=protective,
        )

    def submit_to(self, url, order=None, *, risk_state, limits=None, journal=None,
                  run_id="crypto-paper", risk_manager=None, protective=False):
        """Same as submit_order, but a non-paper URL is refused before any other work."""
        assert_paper_crypto_order_url(url)
        if url != paper_order_url():
            raise CryptoPaperHostError("crypto_order_host_refused")
        self._execution_ready()
        payload, price, qty, stop_price = self._build(order)
        if protective:
            if payload.get("side") != "sell":
                raise CryptoPaperError("protective_exit_must_be_a_sell")
            self._pre_kill(risk_state, risk_manager, journal)
        else:
            self._enforce(payload, price, qty, stop_price, risk_state, limits, risk_manager, journal)
        if journal is not None:
            key = "crypto-paper-request:" + payload["client_order_id"]
            if journal.event_by_key(key):
                raise CryptoPaperError("crypto_order_already_requested")
            journal.append(run_id, "crypto_paper_order_requested", {
                "request": payload, "observed_at": self._observed_at(),
            }, key=key)
        try:
            self._pre_kill(risk_state, risk_manager, journal)
        except CryptoPaperError:
            if journal is not None:
                journal.append(run_id, "crypto_paper_order_not_sent", {
                    "client_order_id": payload["client_order_id"], "reason": "kill_switch",
                }, key="crypto-paper-not-sent:" + payload["client_order_id"])
            raise
        try:
            response = self._transport("POST", url, self._headers(), payload)
        except CryptoPaperError:
            raise
        except Exception:
            raise CryptoPaperError("crypto_order_submission_failed") from None
        public = _public_order(response)
        if journal is not None:
            journal.append(run_id, "crypto_paper_order_acknowledged", {
                "client_order_id": payload["client_order_id"], "order": public,
            }, key="crypto-paper-ack:" + payload["client_order_id"])
        return {
            "environment": "paper",
            "host": PAPER_HOST,
            "live_execution_supported": False,
            "request": payload,
            "order": public,
        }
