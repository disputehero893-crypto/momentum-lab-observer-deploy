"""Offline coverage for the authenticated HTTP surface and journal disk helpers.

No sockets, no broker, no network: requests are fed through an in-memory fake
socket. Paper/observe only; nothing here can start order execution.
"""
from dataclasses import replace
import io
import json
import tempfile
import unittest
from unittest import mock

from momentum.config import Config
from momentum.journal import IdempotencyConflict, Journal
from momentum.server import Application, make_server

TOKEN = "test-only-long-bearer-token"
ORIGIN = "https://dashboard.example"


class _OfflineServer:
    def __init__(self, address, handler):
        self.handler = handler
        self.server_name, self.server_port = address


def _raw_request(server, method, path, body=b"", headers=None):
    if isinstance(body, str):
        body = body.encode()
    headers = {"Host": "localhost", "Content-Length": str(len(body)), **(headers or {})}
    raw = (f"{method} {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
           + "\r\n").encode() + body

    class FakeSocket:
        output = b""

        def makefile(self, *args, **kwargs):
            return io.BytesIO(raw)

        def sendall(self, data):
            self.output += bytes(data)

    sock = FakeSocket()
    server.handler(sock, ("127.0.0.1", 0), server)
    head, content = sock.output.split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ", 2)[1])
    hdrs = {}
    for line in lines[1:]:
        k, _, v = line.decode().partition(":")
        hdrs[k.strip().lower()] = v.strip()
    return status, hdrs, content


class ServerSurfaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = self._tmp.name
        self.cfg = Config(db_path=d + "/journal.sqlite3", export_dir=d + "/exports",
                          control_token=TOKEN, cors_origin=ORIGIN)
        self.server = make_server(self.cfg, address=("127.0.0.1", 0), server_class=_OfflineServer)
        self.auth = {"Authorization": "Bearer " + TOKEN}
        self.json_auth = {**self.auth, "Content-Type": "application/json"}

    def tearDown(self):
        self._tmp.cleanup()

    def req(self, method, path, body=b"", headers=None):
        return _raw_request(self.server, method, path, body, headers)

    def jreq(self, method, path, body=b"", headers=None):
        status, hdrs, content = self.req(method, path, body, headers)
        return status, (json.loads(content) if content else None)

    # --- public / auth gates -------------------------------------------------
    def test_health_has_safe_headers_and_no_cors_for_foreign_origin(self):
        status, hdrs, content = self.req("GET", "/health", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["status"], "ok")
        self.assertEqual(hdrs["cache-control"], "no-store")
        self.assertEqual(hdrs["x-content-type-options"], "nosniff")
        self.assertNotIn("access-control-allow-origin", hdrs)

    def test_allowed_origin_gets_cors_header(self):
        status, hdrs, _ = self.req("GET", "/api/readiness", headers={**self.auth, "Origin": ORIGIN})
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["access-control-allow-origin"], ORIGIN)
        self.assertEqual(hdrs["vary"], "Origin")

    def test_wrong_token_is_401(self):
        status, body = self.jreq("GET", "/api/state", headers={"Authorization": "Bearer nope"})
        self.assertEqual((status, body["error"]), (401, "unauthorized"))

    def test_private_api_is_503_when_token_not_configured(self):
        server = make_server(replace(self.cfg, control_token=""), address=("127.0.0.1", 0),
                             server_class=_OfflineServer)
        status, _, content = _raw_request(server, "GET", "/api/readiness", headers=self.auth)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(content)["error"], "private_api_auth_not_configured")

    # --- preflight -----------------------------------------------------------
    def test_preflight_allowed_only_for_api_and_configured_origin(self):
        status, hdrs, content = self.req("OPTIONS", "/api/halt", headers={"Origin": ORIGIN})
        self.assertEqual(status, 204)
        self.assertEqual(hdrs["access-control-allow-origin"], ORIGIN)
        self.assertIn("POST", hdrs["access-control-allow-methods"])
        self.assertEqual(content, b"")
        self.assertEqual(self.req("OPTIONS", "/api/halt", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.req("OPTIONS", "/health", headers={"Origin": ORIGIN})[0], 403)
        server = make_server(replace(self.cfg, cors_origin=""), address=("127.0.0.1", 0),
                             server_class=_OfflineServer)
        self.assertEqual(_raw_request(server, "OPTIONS", "/api/halt", headers={"Origin": ORIGIN})[0], 403)

    # --- read-only GET routes ------------------------------------------------
    def test_readiness_and_state_never_report_execution(self):
        status, ready = self.jreq("GET", "/api/readiness", headers=self.auth)
        self.assertEqual(status, 200)
        self.assertFalse(ready["paper_execution_running"])
        self.assertFalse(ready["live_execution_supported"])
        self.assertFalse(ready["observation_running"])
        self.assertIn("observation_worker_has_no_current_cycle", ready["launch_blockers"])
        status, state = self.jreq("GET", "/api/state", headers=self.auth)
        self.assertEqual(status, 200)
        self.assertIsNone(state["latest_demo"])
        self.assertIsNone(state["paper_state"])

    def test_readiness_uses_observation_worker_cycle_blockers(self):
        class Worker:
            def state(self):
                return {"paper_connected": True, "paper_reconciled": True, "running": True,
                        "latest_cycle": {"blockers": ["market_closed"]}}
        app = Application(self.cfg, observation_worker=Worker())
        ready = app.readiness()
        self.assertTrue(ready["paper_connected"] and ready["observation_running"])
        self.assertFalse(ready["paper_execution_running"])
        self.assertIn("market_closed", ready["launch_blockers"])
        self.assertNotIn("observation_worker_has_no_current_cycle", ready["launch_blockers"])
        self.assertEqual(app.state()["paper_state"]["latest_cycle"]["blockers"], ["market_closed"])

    def test_demo_run_then_runs_listing_detail_and_events(self):
        status, result = self.jreq("POST", "/api/demo/run", json.dumps({"run_id": "demo-cov-1"}), self.json_auth)
        self.assertEqual(status, 200)
        self.assertEqual(result["run_id"], "demo-cov-1")
        status, runs = self.jreq("GET", "/api/runs", headers=self.auth)
        self.assertEqual(status, 200)
        self.assertEqual(runs["runs"][0]["run_id"], "demo-cov-1")
        self.assertEqual(set(runs["runs"][0]), {"run_id", "mode", "provenance", "status", "metrics", "generated_at"})
        status, detail = self.jreq("GET", "/api/runs/demo-cov-1", headers=self.auth)
        self.assertEqual((status, detail["run_id"]), (200, "demo-cov-1"))
        status, hdrs, content = self.req("GET", "/api/runs/demo-cov-1/events", headers=self.auth)
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["content-type"], "application/x-ndjson")
        rows = [json.loads(line) for line in content.decode().splitlines()]
        self.assertTrue(rows)
        last = rows[-1]["seq"]
        status, _, tail = self.req("GET", f"/api/runs/demo-cov-1/events?after={last}", headers=self.auth)
        self.assertEqual((status, tail), (200, b""))
        status, _, again = self.req("GET", "/api/runs/demo-cov-1/events?after=-5", headers=self.auth)
        self.assertEqual(len(again.decode().splitlines()), len(rows))
        self.assertEqual(self.jreq("GET", "/api/runs/demo-cov-1/events?after=x", headers=self.auth)[0], 400)
        self.assertEqual(self.jreq("GET", "/api/runs/demo-cov-1/other", headers=self.auth)[0], 404)
        self.assertEqual(self.jreq("GET", "/api/state", headers=self.auth)[1]["latest_demo"]["run_id"], "demo-cov-1")

    def test_unknown_or_invalid_runs_and_paths_are_404(self):
        self.assertEqual(self.jreq("GET", "/api/runs/missing", headers=self.auth)[0], 404)
        self.assertEqual(self.jreq("GET", "/api/runs/bad%20id", headers=self.auth)[0], 404)
        self.assertEqual(self.jreq("GET", "/api/nope", headers=self.auth)[0], 404)

    # --- POST validation -----------------------------------------------------
    def test_post_requires_auth_and_json(self):
        self.assertEqual(self.jreq("POST", "/api/halt", "{}", {"Content-Type": "application/json"})[0], 401)
        self.assertEqual(self.jreq("POST", "/api/halt", "{}", {**self.auth, "Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.jreq("POST", "/api/halt", "", self.json_auth)[0], 413)
        self.assertEqual(self.jreq("POST", "/api/halt", "x" * 65537, self.json_auth)[0], 413)
        self.assertEqual(self.jreq("POST", "/api/halt", "[1]", self.json_auth)[0], 400)
        self.assertEqual(self.jreq("POST", "/api/halt", "{bad", self.json_auth)[0], 400)
        self.assertEqual(self.jreq("POST", "/api/halt", "{}",
                                   {**self.auth, "Content-Type": "application/json; charset=utf-8"})[0], 202)

    def test_halt_rejects_resume_flatten_and_bad_ids(self):
        cases = [({"enabled": False}, "remote_resume_is_not_supported"),
                 ({"flatten": True}, "invalid_halt_request"),
                 ({"flatten": "yes"}, "invalid_halt_request"),
                 ({"request_id": "bad id!"}, "invalid_request_id")]
        for payload, error in cases:
            status, body = self.jreq("POST", "/api/halt", json.dumps(payload), self.json_auth)
            self.assertEqual((status, body["error"]), (400, error), payload)

    def test_kill_switch_latches_locally_with_flatten_flag_only(self):
        status, body = self.jreq("POST", "/api/kill-switch",
                                 json.dumps({"flatten": True, "request_id": "ks-1", "reason": "test"}), self.json_auth)
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "locally_latched")
        self.assertFalse(body["broker_acknowledged"])
        self.assertFalse(body["positions_confirmed_flat"])
        self.assertFalse(body["paper_execution_running"])
        ready = self.jreq("GET", "/api/readiness", headers=self.auth)[1]
        self.assertTrue(ready["control"]["enabled"])
        self.assertTrue(ready["control"]["flatten_requested"])
        # A later ordinary halt cannot downgrade the requested emergency flatten.
        self.assertEqual(self.jreq("POST", "/api/halt", json.dumps({"request_id": "h-2"}), self.json_auth)[0], 202)
        self.assertTrue(self.jreq("GET", "/api/readiness", headers=self.auth)[1]["control"]["flatten_requested"])

    def test_demo_run_rejects_invalid_run_id_and_unknown_post_path(self):
        status, body = self.jreq("POST", "/api/demo/run", json.dumps({"run_id": "no spaces"}), self.json_auth)
        self.assertEqual((status, body["error"]), (400, "invalid_run_id"))
        self.assertEqual(self.jreq("POST", "/api/demo/run", json.dumps({"run_id": 7}), self.json_auth)[0], 400)
        self.assertEqual(self.jreq("POST", "/api/other", "{}", self.json_auth)[0], 404)

    def test_unexpected_error_is_generic_500(self):
        with mock.patch("momentum.server.run_demo", side_effect=RuntimeError("secret detail")):
            status, body = self.jreq("POST", "/api/demo/run", json.dumps({"run_id": "demo-x"}), self.json_auth)
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "operation_failed_check_local_worker")
        self.assertNotIn("secret", json.dumps(body))


class JournalDiskHelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = self._tmp.name + "/j.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    def test_low_disk_disabled_when_threshold_zero(self):
        self.assertFalse(Journal(self.path)._low_disk())

    def test_disk_usage_errors_fail_open_for_guard_and_zero_free(self):
        j = Journal(self.path, min_free_bytes=1)
        with mock.patch("momentum.journal.shutil.disk_usage", side_effect=OSError("io")):
            self.assertFalse(j._low_disk())
            self.assertEqual(j._free_bytes(), 0)

    def test_low_disk_true_below_threshold(self):
        j = Journal(self.path, min_free_bytes=10 ** 18)
        self.assertTrue(j._low_disk())

    def test_append_trade_is_idempotent_and_rejects_conflicts(self):
        j = Journal(self.path)
        first = j.append_trade("run-a", "t-1", {"qty": 1})
        again = j.append_trade("run-a", "t-1", {"qty": 1})
        self.assertEqual(first["payload"], again["payload"])
        with self.assertRaises(IdempotencyConflict):
            j.append_trade("run-a", "t-1", {"qty": 2})
        with self.assertRaises(IdempotencyConflict):
            j.append_trade("run-b", "t-1", {"qty": 1})

    def test_latest_filters_by_run(self):
        j = Journal(self.path)
        j.append("r1", "x", {"n": 1}, key="k1")
        j.append("r2", "x", {"n": 2}, key="k2")
        self.assertEqual(j.latest("x", run_id="r1")["payload"], {"n": 1})
        self.assertEqual(j.latest("x")["payload"], {"n": 2})
        self.assertIsNone(j.latest("missing"))


if __name__ == "__main__":
    unittest.main()
