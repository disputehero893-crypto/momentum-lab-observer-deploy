import io
import json
import tempfile
import unittest
from types import SimpleNamespace

from momentum.config import Config
from momentum.server import Application, make_server


class AutopilotApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cfg = Config(db_path=self.directory.name+"/journal.sqlite3",
                          control_token="fake-server-only-token", cors_origin="https://example.test")
        self.commands = []
        self.runner = SimpleNamespace(
            state=lambda: {"state": "paused", "execution_enabled": False},
            command=lambda **args: self.commands.append(args) or {"acknowledged": True})
        class OfflineServer:
            def __init__(self, address, handler):
                self.handler = handler
                self.server_name, self.server_port = address
        self.server = make_server(self.cfg, Application(self.cfg, autopilot=self.runner),
                                  server_class=OfflineServer)

    def request(self, method, path, body=None, authenticated=True, origin=None):
        encoded = json.dumps(body).encode() if body is not None else b""
        headers = {"Host": "localhost", "Content-Length": str(len(encoded)), "Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer "+self.cfg.control_token
        if origin:
            headers["Origin"] = origin
        raw = (f"{method} {path} HTTP/1.0\r\n"+"".join(f"{k}: {v}\r\n" for k,v in headers.items())+"\r\n").encode()+encoded
        class FakeSocket:
            output = b""
            def makefile(self, *args):
                return io.BytesIO(raw)
            def sendall(self, data):
                self.output += data
        connection = FakeSocket()
        self.server.handler(connection, ("127.0.0.1", 0), self.server)
        response, data = connection.output.split(b"\r\n\r\n", 1)
        return int(response.split(b" ", 2)[1]), json.loads(data) if data else None

    def test_state_and_commands_require_server_authentication(self):
        self.assertEqual(self.request("GET", "/api/autopilot/state", authenticated=False)[0], 401)
        self.assertEqual(self.request("POST", "/api/autopilot/commands", {}, authenticated=False)[0], 401)
        self.assertEqual(self.commands, [])

    def test_origin_is_checked_before_control(self):
        self.assertEqual(self.request("POST", "/api/autopilot/commands", {}, origin="https://bad.test")[0], 403)
        self.assertEqual(self.commands, [])

    def test_exact_command_fields_and_acknowledgment(self):
        payload = {"owner_id": "test-owner", "action": "pause", "configuration_version": "test-version",
                   "request_id": "test-command", "consent": False}
        self.assertEqual(self.request("POST", "/api/autopilot/commands", {**payload, "broker_key": "unwanted"})[0], 400)
        self.assertEqual(self.commands, [])
        status, result = self.request("POST", "/api/autopilot/commands", payload)
        self.assertEqual(status, 202)
        self.assertTrue(result["acknowledged"])
        self.assertEqual(self.commands, [payload])

    def test_execution_service_never_runs_synthetic_demo(self):
        status, result = self.request("POST", "/api/demo/run", {"run_id": "demo-v1"})
        self.assertEqual(status, 409)
        self.assertEqual(result["error"], "demo_unavailable_on_execution_service")

    def test_readiness_reports_correct_runtime(self):
        status, result = self.request("GET", "/api/readiness")
        self.assertEqual(status, 200)
        self.assertEqual(result["runtime"], "autopilot")
        self.assertFalse(result["paper_execution_running"])

