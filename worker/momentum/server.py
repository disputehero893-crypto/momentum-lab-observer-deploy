"""Small authenticated HTTP surface; it never starts broker execution."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import threading
from urllib.parse import urlsplit, unquote, parse_qs
import uuid

from .demo import run_demo, RUN_ID
from .journal import Journal, canonical_json


class Application:
    def __init__(self, config, *, observation_worker=None, autopilot=None, customer_gateway=None):
        self.config = config
        self.journal = Journal(config.db_path)
        self.mutation_lock = threading.Lock()
        self.observation_worker = observation_worker
        self.autopilot = autopilot
        if customer_gateway is not None and (customer_gateway.worker is not autopilot
                or customer_gateway.origin != config.cors_origin):
            raise ValueError("customer_gateway_application_mismatch")
        self.customer_gateway = customer_gateway

    def readiness(self):
        if self.autopilot is not None:
            state = self.autopilot.state()
            return {"mode": "paper", "runtime": "autopilot", "state": state["state"],
                    "configuration": self.config.public(),
                    "paper_execution_running": bool(state.get("execution_enabled")),
                    "entries_enabled": bool(state.get("entries_enabled")),
                    "live_execution_supported": False,
                    "launch_blockers": state.get("blockers", ["initial_reconciliation_pending"])}
        observed = self.observation_worker.state() if self.observation_worker else None
        latest = observed.get("latest_cycle") if observed else None
        return {"mode": self.config.mode, "demo_ready": True,
                "paper_connected": bool(observed and observed["paper_connected"]),
                "paper_reconciled": bool(observed and observed["paper_reconciled"]),
                "observation_running": bool(observed and observed["running"]),
                "paper_execution_running": False, "live_execution_supported": False,
                "configuration": self.config.public(), "control": self.journal.kill_switch(),
                "capabilities": {"synthetic_replay": True, "authenticated_journal_api": True,
                                 "paper_adapter": "implemented_and_offline_tested",
                                 "paper_session_controller": "reusable_offline_tested_component",
                                 "execution_activity_ledger": "revision_aware_local_accounting",
                                 "continuous_observation": "implemented_get_only",
                                 "cloud_status_and_halts": "implemented_requires_server_configuration",
                                 "continuous_paper_execution": "not_released"},
                "launch_blockers": [*self.config.paper_blockers(),
                                    "observation_runtime_cannot_execute_orders",
                                    "paper_order_execution_acceptance_not_completed",
                                    *(latest["blockers"] if latest else ["observation_worker_has_no_current_cycle"])]}

    def run(self, run_id):
        event = self.journal.event_by_key(f"demo:{run_id}:complete")
        return event["payload"] if event else None

    def state(self):
        if self.autopilot is not None:
            return {"readiness": self.readiness(), "autopilot": self.autopilot.state()}
        completed = self.journal.latest("demo_run_completed")
        return {"readiness": self.readiness(), "latest_demo": completed["payload"] if completed else None,
                "paper_state": self.observation_worker.state() if self.observation_worker else None,
                "paper_state_explanation": "Observation is read-only; no HTTP route starts broker order execution."}


def make_server(config, app=None, *, address=None, server_class=ThreadingHTTPServer):
    app = app or Application(config)

    class Handler(BaseHTTPRequestHandler):
        server_version = "MomentumLab/0.1"

        def log_message(self, format, *args):
            # Do not put Authorization headers, URLs, account data or payloads in logs.
            return

        def send(self, status, payload=None, *, content_type="application/json"):
            if isinstance(payload, bytes):
                body = payload
            elif payload is None:
                body = b""
            else:
                body = canonical_json(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            origin = self.headers.get("Origin")
            if origin and origin == config.cors_origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def authorized(self):
            origin = self.headers.get("Origin")
            if origin and origin != config.cors_origin:
                self.send(403, {"error": "origin_not_allowed"})
                return False
            if not config.control_token:
                self.send(503, {"error": "private_api_auth_not_configured"})
                return False
            expected = ("Bearer " + config.control_token).encode()
            supplied = self.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(supplied, expected):
                self.send(401, {"error": "unauthorized"})
                return False
            return True

        def customer_request(self, method):
            # Customer JWTs never enter the shared operator-token authentication path.
            from .customer_gateway import MAX_BODY, GatewayError, header
            gateway = app.customer_gateway
            if gateway is None:
                self.send(503, {"error": "customer_gateway_not_configured"})
                return
            try:
                if header(self.headers, "Transfer-Encoding") is not None:
                    raise GatewayError("transfer_encoding_not_supported")
                size = header(self.headers, "Content-Length")
                if size is not None and (not size.isascii() or not size.isdigit() or len(size) > 8):
                    raise GatewayError("invalid_content_length")
                length = int(size or "0")
                if length > MAX_BODY:
                    raise GatewayError("invalid_or_oversized_body", 413)
                if hasattr(self.connection, "settimeout"):
                    self.connection.settimeout(5)
                body = self.rfile.read(length) if length else b""
                if len(body) != length:
                    raise GatewayError("incomplete_body")
                status, result = gateway.handle(method, self.path, self.headers, body)
                self.send(status, result)
            except GatewayError as exc:
                self.send(exc.status, {"error": exc.code})
            except (OSError, ValueError):
                self.send(400, {"error": "invalid_customer_request"})

        def do_OPTIONS(self):
            if self.path.startswith("/v1/"):
                if app.customer_gateway is None:
                    self.send(503, {"error": "customer_gateway_not_configured"})
                    return
                status, result = app.customer_gateway.preflight(self.path, self.headers)
                if status != 204:
                    self.send(status, result)
                    return
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", config.cors_origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-TactiRise-API-Version, X-TactiRise-Request-ID")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # Browser preflight contains no bearer token and reveals no private data.
            if not self.path.startswith("/api/") or not config.cors_origin or self.headers.get("Origin") != config.cors_origin:
                self.send(403, {"error": "origin_not_allowed"})
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", config.cors_origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if self.path.startswith("/v1/"):
                self.customer_request("GET")
                return
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if path == "/health":
                self.send(200, {"status": "ok", "service": "momentum-lab"})
                return
            if not self.authorized():
                return
            if path == "/api/autopilot/state":
                if app.autopilot is None:
                    self.send(503, {"error": "autopilot_service_not_configured"})
                else:
                    self.send(200, app.autopilot.state())
            elif path == "/api/readiness":
                self.send(200, app.readiness())
            elif path == "/api/state":
                self.send(200, app.state())
            elif path == "/api/runs":
                rows = app.journal.events(event_type="demo_run_completed")[-50:]
                self.send(200, {"runs": [{k: row["payload"][k] for k in
                                        ("run_id", "mode", "provenance", "status", "metrics", "generated_at")}
                                       for row in reversed(rows)]})
            elif path.startswith("/api/runs/"):
                parts = path.split("/")
                run_id = parts[3]
                if not RUN_ID.fullmatch(run_id) or not app.run(run_id):
                    self.send(404, {"error": "run_not_found"})
                elif len(parts) == 4:
                    self.send(200, app.run(run_id))
                elif len(parts) == 5 and parts[4] == "events":
                    try:
                        after = max(0, int(parse_qs(parsed.query).get("after", ["0"])[0]))
                    except ValueError:
                        self.send(400, {"error": "invalid_cursor"})
                        return
                    rows = app.journal.events(run_id, after=after, limit=5000)
                    self.send(200, "".join(canonical_json(row)+"\n" for row in rows).encode(),
                              content_type="application/x-ndjson")
                else:
                    self.send(404, {"error": "not_found"})
            else:
                self.send(404, {"error": "not_found"})

        def do_POST(self):
            if self.path.startswith("/v1/"):
                self.customer_request("POST")
                return
            if not self.authorized():
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self.send(415, {"error": "application_json_required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    self.send(413, {"error": "invalid_or_oversized_body"})
                    return
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError
            except (ValueError, json.JSONDecodeError):
                self.send(400, {"error": "invalid_json_object"})
                return
            path = urlsplit(self.path).path
            try:
                with app.mutation_lock:
                    if path == "/api/autopilot/commands":
                        if app.autopilot is None:
                            self.send(503, {"error": "autopilot_service_not_configured"})
                        elif set(payload) != {"owner_id", "action", "configuration_version", "request_id", "consent"}:
                            self.send(400, {"error": "invalid_command_fields"})
                        else:
                            self.send(202, app.autopilot.command(**payload))
                    elif path == "/api/demo/run" and app.autopilot is not None:
                        self.send(409, {"error": "demo_unavailable_on_execution_service"})
                    elif path == "/api/demo/run":
                        run_id = payload.get("run_id", "demo-" + uuid.uuid4().hex[:16])
                        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
                            raise ValueError("invalid_run_id")
                        result = run_demo(config, app.journal, run_id=run_id)
                        self.send(200, result)
                    elif path in {"/api/halt", "/api/kill-switch"}:
                        if payload.get("enabled", True) is not True:
                            raise ValueError("remote_resume_is_not_supported")
                        flatten = payload.get("flatten", False)
                        if type(flatten) is not bool or (path == "/api/halt" and flatten):
                            raise ValueError("invalid_halt_request")
                        request_id = payload.get("request_id") or uuid.uuid4().hex
                        if not isinstance(request_id, str) or not RUN_ID.fullmatch(request_id):
                            raise ValueError("invalid_request_id")
                        event = app.journal.set_kill_switch(True, str(payload.get("reason", "operator")),
                                                          flatten=flatten, request_id=request_id)
                        self.send(202, {"status": "locally_latched", "event": event,
                                        "broker_acknowledged": False, "positions_confirmed_flat": False,
                                        "paper_execution_running": False})
                    else:
                        self.send(404, {"error": "not_found"})
            except ValueError as exc:
                self.send(400, {"error": str(exc)[:200]})
            except Exception:
                self.send(500, {"error": "operation_failed_check_local_worker"})

    server = server_class(address or (config.host, config.port), Handler)
    server.daemon_threads = True
    server.app = app
    return server
