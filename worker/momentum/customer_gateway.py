"""Authenticated v1 customer controls for a single account-bound PAPER worker.

Not enabled by the observer/default server. No broker credentials, synthetic
positions, raw worker payloads or shared operator tokens reach customer output.
"""
from datetime import datetime, timezone
import re
from urllib.parse import urlsplit

from .autopilot import AutopilotError
from .customer_cloud import CustomerCloudError, canonical_uuid, strict_json
from .customer_settings import SettingsBindingError
from .journal import IdempotencyConflict

MAX_BODY = 8192
CONSENT_VERSION = "paper-execution-v1-2026-10-21"
CONSENT_MAX_AGE = 900
_PATHS = {"/v1/bot/state": "GET", "/v1/bot/consent": "POST",
          "/v1/bot/activate": "POST", "/v1/bot/pause": "POST",
          "/v1/bot/emergency-stop": "POST"}
_COMMAND_ERRORS = frozenset({"request_id_conflict", "worker_not_running",
    "configuration_version_mismatch", "execution_configuration_incomplete",
    "risk_stop_requires_operator_resolution", "fresh_reconciliation_required",
    "customer_settings_unavailable", "saved_settings_changed",
    "broker_account_identity_mismatch"})


class GatewayError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def header(headers, name):
    if hasattr(headers, "get_all"):
        values = headers.get_all(name, [])
    else:
        values = [value for key, value in headers.items() if key.lower() == name.lower()]
    if len(values) > 1:
        raise GatewayError("duplicate_header")
    return values[0] if values else None


def valid_origin(value):
    if not isinstance(value, str) or len(value) > 255:
        return False
    try:
        part = urlsplit(value)
        return (part.scheme == "https" and bool(part.hostname) and not part.username
                and not part.password and not part.path and not part.query and not part.fragment
                and part.port in {None, 443} and value == "https://" + part.netloc
                and re.fullmatch(r"[a-z0-9.-]+(?::443)?", part.netloc) is not None)
    except ValueError:
        return False


class CustomerGateway:
    def __init__(self, worker, cloud, *, origin):
        if (worker.customer_binding is None or worker.config.mode != "paper"
                or cloud.owner_id != worker.config.owner_user_id):
            raise ValueError("customer_worker_binding_required")
        canonical_uuid(worker.account_id)
        worker.customer_binding.verify_worker(worker.config)
        if not valid_origin(origin):
            raise ValueError("exact_https_customer_origin_required")
        self.worker, self.cloud, self.origin = worker, cloud, origin

    def preflight(self, path, headers):
        try:
            if path not in _PATHS or header(headers, "Origin") != self.origin:
                raise GatewayError("origin_not_allowed", 403)
            if header(headers, "Access-Control-Request-Method") != _PATHS[path]:
                raise GatewayError("method_not_allowed", 405)
            asked = header(headers, "Access-Control-Request-Headers") or ""
            allowed = {"authorization", "content-type", "x-tactirise-api-version", "x-tactirise-request-id"}
            if any(item.strip().lower() not in allowed for item in asked.split(",") if item.strip()):
                raise GatewayError("headers_not_allowed", 403)
            return 204, None
        except GatewayError as exc:
            return exc.status, {"error": exc.code}

    def handle(self, method, path, headers, body=b""):
        try:
            origin = header(headers, "Origin")
            if origin is not None and origin != self.origin:
                raise GatewayError("origin_not_allowed", 403)
            if path not in _PATHS:
                raise GatewayError("not_found", 404)
            if method != _PATHS[path]:
                raise GatewayError("method_not_allowed", 405)
            if header(headers, "x-tactirise-api-version") != "v1":
                raise GatewayError("api_version_required")
            if not isinstance(body, bytes) or len(body) > MAX_BODY:
                raise GatewayError("invalid_or_oversized_body", 413)
            owner = self.cloud.authenticate(header(headers, "Authorization"))
            if owner != self.worker.config.owner_user_id:
                raise GatewayError("owner_not_provisioned", 403)
            # Never accept identity, settings rows, broker keys, or destination URLs from the browser.
            if method == "GET":
                if body:
                    raise GatewayError("unexpected_body")
                with self.worker.lock:
                    return 200, self._state()
            try:
                request_id = canonical_uuid(header(headers, "x-tactirise-request-id"))
            except ValueError:
                raise GatewayError("request_id_required") from None
            action = path.rsplit("/", 1)[1].replace("-", "_")
            if action in {"consent", "activate"}:
                if (header(headers, "Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                    raise GatewayError("application_json_required", 415)
                try:
                    payload = strict_json(body)
                    expected = {"config_version", "mode"} | ({"acknowledged", "disclosure_version"} if action == "consent" else set())
                    if not isinstance(payload, dict) or set(payload) != expected:
                        raise ValueError
                    canonical_uuid(payload["config_version"])
                except Exception:
                    raise GatewayError("invalid_command_body") from None
                if payload["mode"] != "paper":
                    raise GatewayError("paper_only", 409)
                if action == "consent" and (payload["acknowledged"] is not True
                        or payload["disclosure_version"] != CONSENT_VERSION):
                    raise GatewayError("explicit_paper_consent_required", 409)
            else:
                if body:
                    raise GatewayError("unexpected_body")
                payload = {"config_version": self.worker.config_version, "mode": "paper"}
            with self.worker.lock:
                if not self.worker.active or self.worker.stop_event.is_set():
                    raise GatewayError("worker_not_running", 503)
                if action == "consent":
                    return self._consent(payload, request_id)
                # Share the worker's durable idempotency domain for all customer actions.
                key = "autopilot:command:customer-" + request_id
                prior = self.worker.journal.event_by_key(key)
                if action == "activate" and prior is None:
                    self._current(payload["config_version"])
                    self._require_consent()
                receipt = self.worker.command(owner_id=owner, action=action,
                    configuration_version=payload["config_version"],
                    request_id="customer-" + request_id, consent=(action == "activate"))
                state = self._state()
                state["command"] = {"request_id": request_id, "action": action,
                    "acknowledged": receipt["acknowledged"], "duplicate": receipt["duplicate"]}
                return 200, state
        except (GatewayError, CustomerCloudError) as exc:
            return exc.status, {"error": exc.code}
        except AutopilotError as exc:
            return 409, {"error": str(exc) if str(exc) in _COMMAND_ERRORS else "command_rejected"}
        except (IdempotencyConflict, SettingsBindingError):
            return 409, {"error": "configuration_or_request_conflict"}
        except Exception:
            # A mutation may already be durable. Clients must refetch, not blindly retry with a new ID.
            return 503, {"error": "operation_unconfirmed"}

    def _current(self, version):
        binding = self.worker.customer_binding
        binding.verify_worker(self.worker.config)
        if version != binding.settings_version:
            raise GatewayError("configuration_version_mismatch", 409)
        if not self.worker._settings_current():
            raise GatewayError("current_settings_unavailable", 409)

    def _consent_binding(self):
        worker = self.worker
        return {"owner_id": worker.config.owner_user_id, "account_id": worker.account_id,
                "mode": "paper", "config_version": worker.config_version,
                "settings_fingerprint": worker.customer_binding.settings_fingerprint,
                "worker_review_hash": worker.worker_config_hash, "disclosure_version": CONSENT_VERSION}

    def _consent(self, payload, request_id):
        self._current(payload["config_version"])
        key = "autopilot:command:customer-" + request_id
        expected = {**self._consent_binding(), "request_id": request_id, "acknowledged": True}
        prior = self.worker.journal.event_by_key(key)
        if prior:
            if prior["event_type"] != "customer_paper_consent" or prior["payload"] != expected:
                raise GatewayError("request_id_conflict", 409)
            row = prior
        else:
            row = self.worker.journal.append("autopilot", "customer_paper_consent", expected,
                key=key, timestamp=self.worker.clock().isoformat())
        return 200, {"owner_id": expected["owner_id"], "mode": "paper",
            "config_version": expected["config_version"], "consent_recorded": True,
            "recorded_at": row["timestamp"], "duplicate": prior is not None,
            "activation_performed": False, "request_id": request_id}

    def _require_consent(self):
        row = self.worker.journal.latest("customer_paper_consent", "autopilot")
        try:
            if not row or any(row["payload"].get(k) != v for k, v in self._consent_binding().items()):
                raise ValueError
            stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or not 0 <= (self.worker.clock() - stamp).total_seconds() <= CONSENT_MAX_AGE:
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise GatewayError("current_paper_consent_required", 409) from None

    def _state(self):
        worker = self.worker
        raw = worker.state()
        now = worker.clock()
        running = worker.active and not worker.stop_event.is_set()
        fresh = False
        try:
            stamp = datetime.fromisoformat(raw["completed_at"].replace("Z", "+00:00"))
            fresh = stamp.tzinfo is not None and 0 <= (now - stamp).total_seconds() <= 30
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        bound = (raw.get("owner_id") == worker.config.owner_user_id
                 and raw.get("account_id") == worker.account_id and raw.get("mode") == "paper"
                 and raw.get("configuration_version") == worker.config_version
                 and raw.get("worker_review_hash") == worker.worker_config_hash)
        control = worker._control()
        stopped = bool(worker.journal.kill_switch().get("enabled") or worker.session.risk.locked)
        status = "error"
        if running and stopped:
            status = "stopped"
        elif running and fresh and bound:
            if control["action"] != "activate" or not worker._matches(control):
                status = "paused"
            elif (raw.get("state") == "active" and raw.get("reconciled") is True
                    and raw.get("entries_enabled") is True and raw.get("execution_enabled") is True
                    and not raw.get("blockers") and not worker.settings_blockers and not worker.config.paper_blockers()):
                status = "active"
            elif (raw.get("state") == "paused" and raw.get("reconciled") is True
                    and not raw.get("blockers") and not worker.settings_blockers and not worker.config.paper_blockers()):
                # A healthy paused observation plus the current durable activation
                # is pending, consistently for POST, retry, and GET. It is not active.
                status = "activating"
        return {"owner_id": worker.config.owner_user_id, "mode": "paper", "status": status,
            "config_version": worker.config_version, "reported_at": now.isoformat(),
            "runtime_reported_at": raw.get("completed_at"), "runtime_fresh": bool(fresh and bound),
            "capabilities": {"pause": running, "emergencyStop": running},
            "positions_confirmed_flat": bool(running and fresh and bound and raw.get("positions_confirmed_flat") is True),
            "records_available": False, "disclosure_version": CONSENT_VERSION}
