"""Append-only paper_runs / paper_events logging to Supabase.

Opens a paper_runs row at session start, appends paper_events during the
session, and closes the run with completed_at + summary. Never places orders.
Rejects any non-paper mode. If SUPABASE_URL or the service-role key is missing,
degrades to a warning instead of crashing. The live Alpaca URL stays refused.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import logging
import re
import uuid
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener
from warnings import warn

from .alpaca import NoRedirect
from .journal import canonical_json

UTC = timezone.utc
LOG = logging.getLogger("momentum.paper_run_logging")
MAX_REQUEST_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 2_000_000
PROJECT_HOST = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\.supabase\.co\Z")
SECRET_KEY = re.compile(r"sb_secret_[A-Za-z0-9_-]{1,1024}\Z")
LEGACY_KEY = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z")
LIVE_ALPACA_HOSTS = frozenset({"api.alpaca.markets", "live.alpaca.markets"})
RUN_COLUMNS = "id,owner_id,run_key,mode,strategy_version,summary,started_at,completed_at,created_at"
EVENT_COLUMNS = "id,owner_id,run_id,event_key,occurred_at,received_at,kind,payload"

# Allowed event kinds written by this logger. paper_order records an order that
# another component already submitted; this module never submits or cancels.
EVENT_KINDS = frozenset({
    "session_started",
    "heartbeat",
    "signal_evaluated",
    "blocker",
    "paper_order",
    "session_ended",
})

BLOCKER_KINDS = frozenset({
    "off_universe_symbol",
    "watchlist_outside_strategy_universe",
    "gated_preset_disabled",
    "paper_trading_disabled",
    "paper_execution_not_enabled",
    "live_mode_rejected",
    "supabase_unavailable",
    "other",
})


class PaperRunLoggingError(RuntimeError):
    """Bounded code; never echoes remote bodies or secrets."""

    CODES = {
        "disabled": "Paper run logging is disabled (missing Supabase credentials)",
        "request_invalid": "Invalid paper-run logging request",
        "request_too_large": "Paper-run logging request exceeds the size limit",
        "response_invalid": "Paper-run logging response could not be verified",
        "response_too_large": "Paper-run logging response exceeds the size limit",
        "unavailable": "Supabase paper-run logging unavailable; local journal remains authoritative",
        "mode_rejected": "Paper run logging rejects non-paper mode",
        "live_host_rejected": "Live Alpaca hosts are refused",
        "no_active_run": "No active paper run to append to or close",
    }

    def __init__(self, code="unavailable"):
        self.code = code if code in self.CODES else "unavailable"
        super().__init__(self.CODES[self.code])


def _uuid(value):
    try:
        if not isinstance(value, str) or len(value) != 36:
            raise ValueError
        result = str(uuid.UUID(value))
        if result != value.lower():
            raise ValueError
        return result
    except (ValueError, TypeError, AttributeError):
        raise ValueError("A canonical UUID is required") from None


def _time(value):
    try:
        if isinstance(value, str) and len(value) <= 64:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("A timezone-aware timestamp is required") from None


def _stamp(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate response field")
        result[key] = value
    return result


def _invalid_json_constant(_value):
    raise ValueError("Invalid JSON constant")


def _legacy_service_key(value):
    if not isinstance(value, str) or len(value) > 4096 or not LEGACY_KEY.fullmatch(value):
        return False
    try:
        segment = value.split(".")[1]
        body = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        claims = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_invalid_json_constant)
        return isinstance(claims, dict) and claims.get("role") == "service_role"
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return False


def _json_bytes(value, maximum, code):
    try:
        data = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise PaperRunLoggingError("request_invalid" if code == "request_too_large" else "response_invalid") from None
    if len(data) > maximum:
        raise PaperRunLoggingError(code)
    return data


def _refuse_live_alpaca(env_or_url):
    """Hard-refuse live Alpaca hosts if they appear in related configuration."""
    if env_or_url is None:
        return
    text = str(env_or_url).strip().lower()
    if not text:
        return
    try:
        host = urlsplit(text if "://" in text else "https://" + text).hostname or ""
    except ValueError:
        host = text
    host = host.lower().rstrip(".")
    if host in LIVE_ALPACA_HOSTS or host.endswith(".alpaca.markets") and host != "paper-api.alpaca.markets" and host != "data.alpaca.markets":
        if "paper-api" not in host and host != "data.alpaca.markets":
            raise PaperRunLoggingError("live_host_rejected")


def create_paper_run_logger(config, *, transport=None, clock=None, warn_fn=None):
    """Factory that never raises for missing Supabase credentials.

    Returns an enabled PaperRunLogger when URL + service-role key + owner are
    present and mode is paper; otherwise returns a DisabledPaperRunLogger that
    warns once and no-ops. Non-paper mode still raises (hard reject).
    """
    mode = getattr(config, "mode", None)
    if mode != "paper":
        raise PaperRunLoggingError("mode_rejected")
    url = (getattr(config, "supabase_url", None) or "").strip()
    secret = getattr(config, "supabase_service_role_key", None) or ""
    owner = (getattr(config, "owner_user_id", None) or "").strip()
    if not url or not secret:
        message = (
            "Paper run logging disabled: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY "
            "is missing; paper_runs/paper_events will not be written"
        )
        (warn_fn or warn)(message, RuntimeWarning, stacklevel=2)
        LOG.warning(message)
        return DisabledPaperRunLogger(reason="missing_credentials")
    if not owner:
        message = (
            "Paper run logging disabled: OWNER_USER_ID is missing; "
            "paper_runs/paper_events will not be written"
        )
        (warn_fn or warn)(message, RuntimeWarning, stacklevel=2)
        LOG.warning(message)
        return DisabledPaperRunLogger(reason="missing_owner")
    return PaperRunLogger(config, transport=transport, clock=clock)


class DisabledPaperRunLogger:
    """No-op logger used when cloud credentials are absent."""

    enabled = False

    def __init__(self, reason="missing_credentials"):
        self.reason = reason
        self.run_id = None
        self.run_key = None

    def start_run(self, **kwargs):
        return None

    def append_event(self, **kwargs):
        return False

    def log_heartbeat(self, **kwargs):
        return False

    def log_signal_evaluated(self, **kwargs):
        return False

    def log_blocker(self, **kwargs):
        return False

    def log_paper_order(self, **kwargs):
        return False

    def close_run(self, **kwargs):
        return False


class PaperRunLogger:
    """Writes paper_runs / paper_events via the Supabase REST API (service role).

    service_role grants: INSERT+SELECT+UPDATE on paper_runs; INSERT+SELECT on
    paper_events (append-only). Unique (owner_id, run_key) and (owner_id, event_key).
    mode is constrained to 'paper' by a DB CHECK and by this class.
    """

    enabled = True

    def __init__(self, config, *, transport=None, clock=None):
        if getattr(config, "mode", None) != "paper":
            raise PaperRunLoggingError("mode_rejected")
        # Refuse any live Alpaca host that might have been smuggled via env.
        for attr in ("api_base_url", "alpaca_base_url", "supabase_url"):
            _refuse_live_alpaca(getattr(config, attr, None))
        try:
            parsed = urlsplit(config.supabase_url)
            valid_url = (
                parsed.scheme == "https"
                and parsed.hostname is not None
                and PROJECT_HOST.fullmatch(parsed.hostname)
                and parsed.port is None
                and parsed.path in {"", "/"}
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
            )
        except (ValueError, TypeError, AttributeError):
            valid_url = False
        if not valid_url:
            raise ValueError("SUPABASE_URL must be an HTTPS project URL")
        secret = config.supabase_service_role_key
        modern = isinstance(secret, str) and SECRET_KEY.fullmatch(secret) is not None
        if not modern and not _legacy_service_key(secret):
            raise ValueError("A server-only Supabase secret or service-role key is required")
        self.owner = _uuid(config.owner_user_id)
        self.base = "https://" + parsed.hostname + "/rest/v1/"
        self._key = secret
        self._modern_key = modern
        self.transport = transport or self._http
        self.clock = clock or (lambda: datetime.now(UTC))
        self.run_id = None
        self.run_key = None
        self._started_at = None
        self._strategy_version = None
        self._watchlist = ()
        self._config_hash = None

    def _http(self, method, url, headers, payload):
        try:
            encoded = _json_bytes(payload, MAX_REQUEST_BYTES, "request_too_large") if payload is not None else None
            request = Request(url, method=method, headers=headers, data=encoded)
            with build_opener(NoRedirect()).open(request, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise PaperRunLoggingError()
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise PaperRunLoggingError("response_too_large")
                if not body:
                    return None
                if response.headers.get_content_type() != "application/json":
                    raise PaperRunLoggingError("response_invalid")
                try:
                    return json.loads(body, object_pairs_hook=_unique_object, parse_constant=_invalid_json_constant)
                except (ValueError, TypeError, UnicodeError, RecursionError):
                    raise PaperRunLoggingError("response_invalid") from None
        except PaperRunLoggingError:
            raise
        except Exception:
            raise PaperRunLoggingError() from None

    def request(self, method, table, params=None, payload=None, *, prefer="return=minimal"):
        allowed = {
            ("POST", "paper_runs"),
            ("PATCH", "paper_runs"),
            ("POST", "paper_events"),
        }
        if (method, table) not in allowed:
            raise PaperRunLoggingError("request_invalid")
        params = {} if params is None else params
        if (
            not isinstance(params, dict)
            or len(params) > 10
            or any(
                not isinstance(key, str)
                or not 1 <= len(key) <= 64
                or type(value) not in {str, int}
                or len(str(value)) > 512
                for key, value in params.items()
            )
            or not isinstance(prefer, str)
            or len(prefer) > 128
            or "\r" in prefer
            or "\n" in prefer
        ):
            raise PaperRunLoggingError("request_invalid")
        if payload is not None:
            if not isinstance(payload, dict) or payload.get("owner_id") != self.owner:
                raise PaperRunLoggingError("request_invalid")
            if payload.get("mode") not in {None, "paper"}:
                raise PaperRunLoggingError("mode_rejected")
            _json_bytes(payload, MAX_REQUEST_BYTES, "request_too_large")
        url = self.base + table + ("?" + urlencode(params) if params else "")
        headers = {
            "apikey": self._key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": prefer,
            "User-Agent": "momentum-lab-worker/paper-run-logging/0.1",
        }
        if not self._modern_key:
            headers["Authorization"] = "Bearer " + self._key
        try:
            result = self.transport(method, url, headers, payload)
            _json_bytes(result, MAX_RESPONSE_BYTES, "response_too_large")
            return result
        except PaperRunLoggingError:
            raise
        except Exception:
            raise PaperRunLoggingError() from None

    def start_run(
        self,
        *,
        run_key,
        strategy_version,
        watchlist=(),
        config_hash=None,
        summary=None,
        started_at=None,
    ):
        """Open (or reuse) a paper_runs row. mode is always 'paper'."""
        if not isinstance(run_key, str) or not 1 <= len(run_key) <= 160:
            raise ValueError("A bounded run_key is required")
        if not isinstance(strategy_version, str) or not 1 <= len(strategy_version) <= 128:
            raise ValueError("A bounded strategy_version is required")
        if watchlist is None:
            watchlist = ()
        if not isinstance(watchlist, (list, tuple)) or len(watchlist) > 10:
            raise ValueError("watchlist must be a short sequence of symbols")
        symbols = tuple(str(s) for s in watchlist)
        if config_hash is not None and (not isinstance(config_hash, str) or len(config_hash) > 128):
            raise ValueError("config_hash must be a short string when provided")
        now = _time(started_at or self.clock())
        summary_obj = {
            "watchlist": list(symbols),
            "config_hash": config_hash,
            "status": "running",
            "observation_only": True,
            "execution_enabled": False,
            "live_execution_supported": False,
        }
        if isinstance(summary, dict):
            # Caller extras cannot flip mode or enable live execution.
            safe = {k: v for k, v in summary.items() if k not in {"mode", "live_execution_supported", "execution_enabled"}}
            summary_obj.update(safe)
            summary_obj["status"] = "running"
            summary_obj["observation_only"] = True
            summary_obj["execution_enabled"] = False
            summary_obj["live_execution_supported"] = False
        run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, self.owner + ":paper_run:" + run_key))
        row = {
            "id": run_id,
            "owner_id": self.owner,
            "run_key": run_key,
            "mode": "paper",
            "strategy_version": strategy_version,
            "summary": summary_obj,
            "started_at": _stamp(now),
            "completed_at": None,
        }
        try:
            result = self.request(
                "POST",
                "paper_runs",
                {"on_conflict": "owner_id,run_key", "select": RUN_COLUMNS},
                row,
                prefer="resolution=ignore-duplicates,return=representation",
            )
            if isinstance(result, list) and result:
                actual = result[0]
                if not isinstance(actual, dict) or actual.get("mode") != "paper":
                    raise PaperRunLoggingError("response_invalid")
                run_id = _uuid(actual["id"])
            elif result not in (None, []):
                raise PaperRunLoggingError("response_invalid")
        except PaperRunLoggingError:
            raise
        self.run_id = run_id
        self.run_key = run_key
        self._started_at = now
        self._strategy_version = strategy_version
        self._watchlist = symbols
        self._config_hash = config_hash
        self.append_event(
            event_key="session-started:" + run_key,
            kind="session_started",
            payload={
                "run_key": run_key,
                "strategy_version": strategy_version,
                "watchlist": list(symbols),
                "config_hash": config_hash,
                "mode": "paper",
            },
            occurred_at=now,
        )
        return run_id

    def append_event(self, *, event_key, kind, payload=None, occurred_at=None):
        if self.run_id is None:
            raise PaperRunLoggingError("no_active_run")
        if not isinstance(event_key, str) or not 1 <= len(event_key) <= 200:
            raise ValueError("A bounded event_key is required")
        if kind not in EVENT_KINDS:
            raise ValueError("Unsupported paper_events kind")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError("Event payload must be an object")
        # Never allow a payload to claim live mode or to request order placement.
        safe = dict(payload)
        safe.pop("place_order", None)
        safe.pop("submit_order", None)
        if safe.get("mode") not in {None, "paper"}:
            raise PaperRunLoggingError("mode_rejected")
        safe.setdefault("mode", "paper")
        now = _time(occurred_at or self.clock())
        received = _time(self.clock())
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, self.owner + ":paper_event:" + event_key))
        row = {
            "id": event_id,
            "owner_id": self.owner,
            "run_id": self.run_id,
            "event_key": event_key,
            "occurred_at": _stamp(now),
            "received_at": _stamp(received),
            "kind": kind,
            "payload": safe,
        }
        result = self.request(
            "POST",
            "paper_events",
            {"on_conflict": "owner_id,event_key", "select": EVENT_COLUMNS},
            row,
            prefer="resolution=ignore-duplicates,return=representation",
        )
        if result not in (None, []) and not (isinstance(result, list) and len(result) <= 1):
            raise PaperRunLoggingError("response_invalid")
        return True

    def _require_run(self):
        if self.run_id is None or not self.run_key:
            raise PaperRunLoggingError("no_active_run")

    def log_heartbeat(self, *, status="observing", extra=None, event_key=None, occurred_at=None):
        self._require_run()
        payload = {"status": status, "mode": "paper"}
        if isinstance(extra, dict):
            payload.update({k: v for k, v in extra.items() if k != "mode"})
            payload["mode"] = "paper"
        key = event_key or ("heartbeat:" + self.run_key + ":" + _stamp(_time(occurred_at or self.clock())))
        return self.append_event(event_key=key, kind="heartbeat", payload=payload, occurred_at=occurred_at)

    def log_signal_evaluated(self, *, symbol, accepted, reasons=(), metrics=None, event_key=None, occurred_at=None):
        self._require_run()
        payload = {
            "symbol": symbol,
            "accepted": bool(accepted),
            "reasons": list(reasons) if isinstance(reasons, (list, tuple)) else [str(reasons)],
            "metrics": metrics or {},
            "mode": "paper",
        }
        key = event_key or ("signal:" + self.run_key + ":" + str(symbol) + ":" + _stamp(_time(occurred_at or self.clock())))
        return self.append_event(event_key=key, kind="signal_evaluated", payload=payload, occurred_at=occurred_at)

    def log_blocker(self, reason, *, detail=None, event_key=None, occurred_at=None):
        self._require_run()
        code = reason if reason in BLOCKER_KINDS else "other"
        payload = {"reason": code, "detail": detail, "mode": "paper"}
        if reason == "paper_execution_not_enabled" or reason == "paper_trading_disabled":
            payload["paper_trading_enabled"] = False
        key = event_key or ("blocker:" + self.run_key + ":" + code + ":" + _stamp(_time(occurred_at or self.clock())))
        return self.append_event(event_key=key, kind="blocker", payload=payload, occurred_at=occurred_at)

    def log_paper_order(self, *, client_order_id, side, symbol, qty, status="recorded", event_key=None, occurred_at=None):
        """Record that another component already handled a paper order. Never submits."""
        self._require_run()
        payload = {
            "client_order_id": client_order_id,
            "side": side,
            "symbol": symbol,
            "qty": qty,
            "status": status,
            "mode": "paper",
            "placed_by_logger": False,
        }
        key = event_key or ("paper-order:" + self.run_key + ":" + str(client_order_id))
        return self.append_event(event_key=key, kind="paper_order", payload=payload, occurred_at=occurred_at)

    def close_run(self, *, status="completed", summary=None, completed_at=None):
        if self.run_id is None:
            raise PaperRunLoggingError("no_active_run")
        if not isinstance(status, str) or not 1 <= len(status) <= 64:
            raise ValueError("A bounded status is required")
        now = _time(completed_at or self.clock())
        if self._started_at is not None and now < self._started_at:
            raise ValueError("completed_at cannot precede started_at")
        summary_obj = {
            "watchlist": list(self._watchlist),
            "config_hash": self._config_hash,
            "status": status,
            "observation_only": True,
            "execution_enabled": False,
            "live_execution_supported": False,
            "strategy_version": self._strategy_version,
        }
        if isinstance(summary, dict):
            safe = {k: v for k, v in summary.items() if k not in {"mode", "live_execution_supported", "execution_enabled"}}
            summary_obj.update(safe)
            summary_obj["status"] = status
            summary_obj["observation_only"] = True
            summary_obj["execution_enabled"] = False
            summary_obj["live_execution_supported"] = False
        try:
            self.append_event(
                event_key="session-ended:" + self.run_key + ":" + status,
                kind="session_ended",
                payload={"status": status, "mode": "paper"},
                occurred_at=now,
            )
        except PaperRunLoggingError:
            # Still attempt to close the run row even if the end event fails.
            pass
        patch = {
            "owner_id": self.owner,
            "completed_at": _stamp(now),
            "summary": summary_obj,
            "mode": "paper",
        }
        result = self.request(
            "PATCH",
            "paper_runs",
            {
                "id": "eq." + self.run_id,
                "owner_id": "eq." + self.owner,
                "select": RUN_COLUMNS,
            },
            patch,
            prefer="return=representation",
        )
        if not isinstance(result, list) or len(result) != 1:
            raise PaperRunLoggingError("response_invalid")
        if result[0].get("mode") != "paper" or result[0].get("id") != self.run_id:
            raise PaperRunLoggingError("response_invalid")
        closed_id = self.run_id
        self.run_id = None
        return closed_id
