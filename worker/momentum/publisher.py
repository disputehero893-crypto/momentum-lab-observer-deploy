"""Server-only publication of synthetic demos and actual paper observations.

The cloud is a dashboard mirror. A halt is committed to the local journal before
its cloud acknowledgment. This module never places orders or reports readiness.
"""
import base64
from datetime import datetime, timezone
import json
import math
import re
import uuid
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from .alpaca import NoRedirect
from .journal import canonical_json
from .market_observation import market_data_evidence, sanitize_market_watch
from .strategy import StrategyConfig
from .strategy_review import build_strategy_review
from .price_history import sanitize_price_history
from .crypto_observation import sanitize_crypto_watch


UTC = timezone.utc
MAX_REQUEST_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 2_000_000
MAX_COMMANDS = 50
BROKER_MAX_AGE_SECONDS = 30
WORKER_COLUMNS = "owner_id,instance_id,mode,status,heartbeat_at,data_as_of,payload"
COMMAND_COLUMNS = "id,owner_id,kind,nonce,status,created_at,acknowledged_at,error"
PROJECT_HOST = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\.supabase\.co\Z")
INSTANCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
SECRET_KEY = re.compile(r"sb_secret_[A-Za-z0-9_-]{1,1024}\Z")
LEGACY_KEY = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z")
OBSERVATION_ERRORS = frozenset({
    "observation_failed", "broker_unavailable", "market_data_unavailable",
    "ledger_unavailable", "reconciliation_required", "command_poll_failed",
    "command_invalid", "command_ack_unconfirmed", "publisher_unavailable", "worker_stopping",
})
ERROR_MESSAGES = {
    "request_invalid": "Invalid publisher request",
    "request_too_large": "Publisher request exceeds the size limit",
    "response_invalid": "Publisher response could not be verified",
    "response_too_large": "Publisher response exceeds the size limit",
    "publication_unavailable": "Supabase publication unavailable; local journal remains authoritative",
    "command_poll_failed": "Command channel unavailable; new entries locally halted",
    "command_invalid": "Invalid command channel response; new entries locally halted",
    "command_ack_unconfirmed": "Local halt is durable; cloud acknowledgment could not be verified",
    "local_halt_unavailable": "Durable local halt could not be verified; stop the worker",
}


class PublisherError(RuntimeError):
    """A bounded code and fixed message, never a remote response or secret."""

    def __init__(self, code="publication_unavailable"):
        self.code = code if code in ERROR_MESSAGES else "publication_unavailable"
        super().__init__(ERROR_MESSAGES[self.code])


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
        raise ValueError("A timezone-aware observation timestamp is required") from None


def _stamp(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _optional_time(value):
    return None if value is None else _time(value)


def _fresh(value, now, maximum):
    return value is not None and 0 <= (now - value).total_seconds() <= maximum


def _max_data_age(value):
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 < value <= 5:
        raise ValueError("Data freshness must be greater than zero and at most five seconds")
    return float(value)


def _json_bytes(value, maximum, code):
    try:
        data = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise PublisherError("request_invalid" if code == "request_too_large" else "response_invalid") from None
    if len(data) > maximum:
        raise PublisherError(code)
    return data


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
        # This recognizes the key type only. Supabase verifies the signature.
        return isinstance(claims, dict) and claims.get("role") == "service_role"
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return False


def build_worker_state(owner_id, instance_id, *, now, broker_verified=False,
                       broker_checked_at=None, data_verified=False, data_feed=None,
                       data_as_of=None, data_received_at=None, halted=False,
                       error_code=None, max_data_age_seconds=2, market_watch=None,
                       allowed_symbols=(), strategy_config=None, price_history=None,
                       crypto_watch=None, allowed_crypto_pairs=()):
    """Build the existing dashboard contract from validated provider evidence.

    The caller attests actual account/position/order reads, not a saved account
    link. Data timestamps must be the oldest applicable source and receipt times
    across the requested watchlist. Optional Market Watch is projected onto the
    configured symbol/feed allowlist and aged again at publication; its rows
    determine aggregate data status. No balances, credentials, returns, or raw
    errors enter this interface. Observing never means execution is ready.
    """
    owner = _uuid(owner_id)
    if not isinstance(instance_id, str) or not INSTANCE_ID.fullmatch(instance_id):
        raise ValueError("A bounded worker instance identifier is required")
    if any(type(value) is not bool for value in (broker_verified, data_verified, halted)):
        raise ValueError("Observation verification and halt flags must be booleans")
    now = _time(now)
    maximum = _max_data_age(max_data_age_seconds)
    watch, evidence = None, None
    if market_watch is not None:
        watch = sanitize_market_watch(market_watch, allowed_symbols=allowed_symbols,
            feed=data_feed, now=now, max_age_seconds=maximum)
        evidence = market_data_evidence(watch)
        data_verified, data_as_of, data_received_at = (evidence["verified"], evidence["as_of"], evidence["received_at"])
    checked = _optional_time(broker_checked_at)
    source = _optional_time(data_as_of)
    received = _optional_time(data_received_at)
    if data_feed not in {None, "iex", "sip"}:
        raise ValueError("Observation data feed must be iex or sip")
    if broker_verified and checked is None:
        raise ValueError("Verified broker evidence requires an observation timestamp")
    if data_verified and (data_feed is None or source is None or received is None):
        raise ValueError("Verified market evidence requires feed, source, and receipt timestamps")
    broker_fresh = broker_verified and _fresh(checked, now, BROKER_MAX_AGE_SECONDS)
    data_fresh = (data_verified and _fresh(source, now, maximum)
                  and _fresh(received, now, maximum) and source <= received)
    safe_code = None if error_code is None else (
        error_code if isinstance(error_code, str) and error_code in OBSERVATION_ERRORS else "observation_failed")
    status = ("halted" if halted else "error" if safe_code else
              "observing" if broker_fresh or data_fresh else "disconnected")
    row = {
        "owner_id": owner, "instance_id": instance_id, "mode": "paper", "status": status,
        "heartbeat_at": _stamp(now), "data_as_of": _stamp(source) if source is not None else None,
        "payload": {
            "provenance": "provider_observations", "observation_only": True,
            "broker": {"status": "connected" if broker_fresh else "disconnected", "mode": "paper",
                       "access": "read_only", "checked_at": _stamp(checked) if checked else None,
                       "max_age_seconds": BROKER_MAX_AGE_SECONDS,
                       "reason": "fresh_provider_read" if broker_fresh else
                       "broker_not_verified" if not broker_verified else "broker_observation_stale_or_future"},
            "data_feed": {"status": "connected" if data_fresh else "disconnected", "feed": data_feed,
                          "as_of": _stamp(source) if source else None,
                          "received_at": _stamp(received) if received else None,
                          "max_age_seconds": maximum,
                          "reason": "fresh_provider_data" if data_fresh else
                          "data_not_verified" if not data_verified else "data_observation_stale_or_inconsistent"},
            "trading": "disabled", "execution_enabled": False, "entry_gate_open": False,
            "live_execution_supported": False, "halt_new_entries": halted,
            "capabilities": {"provider_observation": True, "cloud_halt_requests": True, "orders": False},
            "last_error_code": safe_code,
        },
    }
    if watch is not None:
        row["payload"]["market_watch"] = watch
        row["payload"]["strategy_review"] = build_strategy_review(
            watch, strategy_config if strategy_config is not None else StrategyConfig(), now=now)
        # JSON timestamp strings retain provider nanoseconds. The existing SQL
        # data_as_of column remains a microsecond timestamptz summary only.
        row["payload"]["data_feed"]["as_of"] = evidence["as_of"]
        row["payload"]["data_feed"]["received_at"] = evidence["received_at"]
        if price_history is not None:
            try:
                history = sanitize_price_history(price_history, allowed_symbols=allowed_symbols, feed=data_feed, now=now)
                from .activities import timestamp_parts
                if timestamp_parts(history["collected_at"])[1] > timestamp_parts(watch["collected_at"])[1]:
                    raise ValueError("History cannot follow the associated quote collection")
                row["payload"]["price_history"] = history
            except (ValueError, TypeError, KeyError, ArithmeticError, UnicodeError, RecursionError):
                # Optional history never suppresses valid quote/account state.
                pass
    if crypto_watch is not None and allowed_crypto_pairs:
        try:
            # Strictly optional: bad crypto data cannot hide functioning equity data.
            row["payload"]["crypto_watch"] = sanitize_crypto_watch(
                crypto_watch, allowed_pairs=allowed_crypto_pairs, now=now)
        except (ValueError, TypeError, KeyError, ArithmeticError, UnicodeError, RecursionError):
            pass
    return row


class Publisher:
    def __init__(self, config, *, transport=None, clock=None):
        try:
            parsed = urlsplit(config.supabase_url)
            valid_url = (parsed.scheme == "https" and parsed.hostname is not None
                         and PROJECT_HOST.fullmatch(parsed.hostname) and parsed.port is None
                         and parsed.path in {"", "/"} and not parsed.username and not parsed.password
                         and not parsed.query and not parsed.fragment)
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
        self.max_data_age_seconds = _max_data_age(config.quote_max_age_seconds)
        self.watchlist, self.data_feed = tuple(config.watchlist), config.data_feed
        self.crypto_watchlist = tuple(config.crypto_watchlist)
        self.strategy_config = StrategyConfig(**config.strategy_overrides)
        self.transport = transport or self._http
        self.clock = clock or (lambda: datetime.now(UTC))

    def _http(self, method, url, headers, payload):
        try:
            encoded = _json_bytes(payload, MAX_REQUEST_BYTES, "request_too_large") if payload is not None else None
            request = Request(url, method=method, headers=headers, data=encoded)
            with build_opener(NoRedirect()).open(request, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise PublisherError()
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise PublisherError("response_too_large")
                if not body:
                    return None
                if response.headers.get_content_type() != "application/json":
                    raise PublisherError("response_invalid")
                try:
                    return json.loads(body, object_pairs_hook=_unique_object, parse_constant=_invalid_json_constant)
                except (ValueError, TypeError, UnicodeError, RecursionError):
                    raise PublisherError("response_invalid") from None
        except PublisherError:
            raise
        except Exception:
            raise PublisherError() from None

    def request(self, method, table, params=None, payload=None, *, prefer="return=minimal"):
        allowed = {("POST", "demo_runs"), ("POST", "worker_state"),
                   ("GET", "paper_commands"), ("PATCH", "paper_commands")}
        if (method, table) not in allowed:
            raise PublisherError("request_invalid")
        params = {} if params is None else params
        if (not isinstance(params, dict) or len(params) > 10
                or any(not isinstance(key, str) or not 1 <= len(key) <= 64
                       or type(value) not in {str, int} or len(str(value)) > 512
                       for key, value in params.items())
                or not isinstance(prefer, str) or len(prefer) > 128 or "\r" in prefer or "\n" in prefer):
            raise PublisherError("request_invalid")
        if table == "paper_commands":
            if params.get("owner_id") != "eq." + self.owner or params.get("kind") != "eq.halt_new_entries":
                raise PublisherError("request_invalid")
        elif not isinstance(payload, dict) or payload.get("owner_id") != self.owner:
            raise PublisherError("request_invalid")
        if payload is not None:
            _json_bytes(payload, MAX_REQUEST_BYTES, "request_too_large")
        url = self.base + table + ("?" + urlencode(params) if params else "")
        headers = {"apikey": self._key, "Content-Type": "application/json", "Accept": "application/json",
                   "Prefer": prefer, "User-Agent": "momentum-lab-worker/0.2"}
        # sb_secret_ keys are opaque API keys, not JWTs. Only legacy service-role
        # JWTs belong in Authorization; Supabase authenticates either key type.
        if not self._modern_key:
            headers["Authorization"] = "Bearer " + self._key
        try:
            result = self.transport(method, url, headers, payload)
            _json_bytes(result, MAX_RESPONSE_BYTES, "response_too_large")
            return result
        except PublisherError:
            raise
        except Exception:
            raise PublisherError() from None

    def publish_demo(self, payload):
        if not isinstance(payload, dict) or payload.get("mode") != "demo" or payload.get("provenance") != "synthetic":
            raise ValueError("Only explicitly synthetic demo exports can be published here")
        run_key, strategy = payload.get("run_key"), payload.get("strategy_version")
        if (not isinstance(run_key, str) or not 1 <= len(run_key) <= 160
                or not isinstance(strategy, str) or not 1 <= len(strategy) <= 128):
            raise ValueError("A bounded demo run key and strategy version are required")
        row = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, self.owner + ":demo:" + run_key)),
               "owner_id": self.owner, "run_key": run_key, "strategy_version": strategy, "payload": payload}
        # Existing rows are never overwritten. Service role has SELECT+INSERT.
        result = self.request("POST", "demo_runs", {"on_conflict": "owner_id,run_key"}, row,
                              prefer="resolution=ignore-duplicates,return=minimal")
        if result is not None:
            raise PublisherError("response_invalid")

    def publish_worker_state(self, *, instance_id, broker_verified=False, broker_checked_at=None,
                             data_verified=False, data_feed=None, data_as_of=None, data_received_at=None,
                             halted=False, error_code=None, max_data_age_seconds=None,
                             market_watch=None, price_history=None, crypto_watch=None):
        if market_watch is not None:
            if data_feed is not None and data_feed != self.data_feed:
                raise ValueError("Market Watch feed must match the worker configuration")
            data_feed = self.data_feed
        row = build_worker_state(self.owner, instance_id, now=self.clock(), broker_verified=broker_verified,
                                 broker_checked_at=broker_checked_at, data_verified=data_verified,
                                 data_feed=data_feed, data_as_of=data_as_of, data_received_at=data_received_at,
                                 halted=halted, error_code=error_code,
                                 max_data_age_seconds=self.max_data_age_seconds if max_data_age_seconds is None
                                 else max_data_age_seconds, market_watch=market_watch, allowed_symbols=self.watchlist,
                                 strategy_config=self.strategy_config, price_history=price_history,
                                 crypto_watch=crypto_watch, allowed_crypto_pairs=self.crypto_watchlist)
        # Preserve the expected row even if an injected transport mutates input.
        expected = json.loads(canonical_json(row))
        result = self.request("POST", "worker_state", {"on_conflict": "owner_id", "select": WORKER_COLUMNS}, row,
                              prefer="resolution=merge-duplicates,return=representation")
        try:
            if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
                raise ValueError
            actual = result[0]
            if set(actual) != set(WORKER_COLUMNS.split(",")):
                raise ValueError
            for key in ("owner_id", "instance_id", "mode", "status", "payload"):
                if actual[key] != expected[key]:
                    raise ValueError
            for key in ("heartbeat_at", "data_as_of"):
                if _optional_time(actual[key]) != _optional_time(expected[key]):
                    raise ValueError
        except (ValueError, TypeError, KeyError):
            raise PublisherError("response_invalid") from None
        return expected

    def _halt(self, journal, key, reason):
        """Commit and read back a stop latch before any remote acknowledgment."""
        try:
            event = journal.event_by_key("kill:" + key)
            if event is None:
                journal.set_kill_switch(True, reason, request_id=key, flatten=False)
                event = journal.event_by_key("kill:" + key)
            if not event or event.get("payload", {}).get("enabled") is not True:
                raise ValueError
            # A historical marker is insufficient if later local control cleared
            # the latch. Preserve the immutable marker and reassert durably.
            if journal.kill_switch().get("enabled") is not True:
                journal.set_kill_switch(True, reason, request_id=key + ":reassert:" + uuid.uuid4().hex, flatten=False)
            if journal.kill_switch().get("enabled") is not True:
                raise ValueError
        except Exception:
            raise PublisherError("local_halt_unavailable") from None

    def _command(self, value, now, *, acknowledged=False):
        if not isinstance(value, dict) or set(value) != set(COMMAND_COLUMNS.split(",")):
            raise ValueError("Invalid halt command")
        command = {**value, "id": _uuid(value["id"]), "owner_id": _uuid(value["owner_id"]),
                   "nonce": _uuid(value["nonce"])}
        created = _time(value["created_at"])
        if (command["owner_id"] != self.owner or command["kind"] != "halt_new_entries"
                or command["status"] != ("acknowledged" if acknowledged else "queued")
                or command["error"] is not None or (created - now).total_seconds() > 5):
            raise ValueError("Invalid halt command")
        if acknowledged:
            ack = _time(command["acknowledged_at"])
            if ack < created or (ack - now).total_seconds() > 5:
                raise ValueError("Invalid halt acknowledgment")
        elif command["acknowledged_at"] is not None:
            raise ValueError("Invalid queued halt command")
        return command

    def acknowledge_halts(self, journal):
        """Fail closed on an unreadable channel and persist before every ack.

        Acknowledgment means new paper entries are locally disabled; it does not
        assert provider order cancellation, closure of positions, or flatness.
        Caller holds the journal's single-worker lease for this whole operation.
        """
        try:
            raw = self.request("GET", "paper_commands", {
                "owner_id": "eq." + self.owner, "kind": "eq.halt_new_entries", "status": "eq.queued",
                "order": "created_at.asc", "limit": str(MAX_COMMANDS), "select": COMMAND_COLUMNS})
        except PublisherError:
            self._halt(journal, "publisher-poll-failed:" + self.owner, "Cloud command channel unavailable")
            raise PublisherError("command_poll_failed") from None
        try:
            if not isinstance(raw, list) or len(raw) > MAX_COMMANDS:
                raise ValueError
            now = _time(self.clock())
            commands = [self._command(value, now) for value in raw]
            if (len({c["id"] for c in commands}) != len(commands)
                    or len({c["nonce"] for c in commands}) != len(commands)):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            self._halt(journal, "publisher-invalid-commands:" + self.owner, "Cloud command validation failed")
            raise PublisherError("command_invalid") from None
        acknowledged = []
        for command in commands:
            self._halt(journal, "supabase-command-" + command["id"], "Supabase halt_new_entries command")
            ack_at = _stamp(_time(self.clock()))
            try:
                result = self.request("PATCH", "paper_commands", {
                    "owner_id": "eq." + self.owner, "id": "eq." + command["id"],
                    "nonce": "eq." + command["nonce"], "kind": "eq.halt_new_entries",
                    "status": "eq.queued", "select": COMMAND_COLUMNS},
                    {"status": "acknowledged", "acknowledged_at": ack_at, "error": None},
                    prefer="return=representation")
                if not isinstance(result, list) or len(result) != 1:
                    raise ValueError
                confirmed = self._command(result[0], _time(self.clock()), acknowledged=True)
                if (any(confirmed[key] != command[key] for key in ("id", "owner_id", "kind", "nonce"))
                        or _time(confirmed["created_at"]) != _time(command["created_at"])
                        or _time(confirmed["acknowledged_at"]) != _time(ack_at)):
                    raise ValueError
            except (PublisherError, ValueError, TypeError, KeyError):
                raise PublisherError("command_ack_unconfirmed") from None
            acknowledged.append(command["id"])
        return acknowledged

