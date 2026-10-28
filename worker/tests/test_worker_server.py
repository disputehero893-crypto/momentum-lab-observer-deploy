from dataclasses import replace
import io
import json
import tempfile
import unittest

from momentum.config import Config
from momentum.server import make_server


class ServerTests(unittest.TestCase):
    def test_health_only_is_public_and_halt_is_not_flat_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(db_path=directory+"/journal.sqlite3", export_dir=directory+"/exports",
                         control_token="test-only-long-bearer-token", cors_origin="https://dashboard.example")
            class OfflineServer:
                def __init__(self, address, handler):
                    self.handler = handler
                    self.server_name, self.server_port = address
            server = make_server(cfg, address=("127.0.0.1", 0), server_class=OfflineServer)
            def request(method, path, body=None, headers=None):
                body = (body or "").encode()
                headers = {"Host": "localhost", "Content-Length": str(len(body)), **(headers or {})}
                raw = (f"{method} {path} HTTP/1.0\r\n"+"".join(f"{k}: {v}\r\n" for k, v in headers.items())+"\r\n").encode()+body
                class FakeSocket:
                    output = b""
                    def makefile(self, *args):
                        return io.BytesIO(raw)
                    def sendall(self, data):
                        self.output += bytes(data)
                connection = FakeSocket()
                server.handler(connection, ("127.0.0.1", 0), server)
                response_headers, content = connection.output.split(b"\r\n\r\n", 1)
                status = int(response_headers.split(b" ", 2)[1])
                return status, json.loads(content) if content else None
            try:
                self.assertEqual(request("GET", "/health")[0], 200)
                for path in ("/api/readiness", "/api/state", "/api/runs", "/api/runs/demo-v1/events"):
                    self.assertEqual(request("GET", path)[0], 401)
                auth = {"Authorization": "Bearer "+cfg.control_token}
                status, ready = request("GET", "/api/readiness", headers=auth)
                self.assertEqual(status, 200)
                self.assertFalse(ready["paper_execution_running"])
                self.assertEqual(request("GET", "/api/state", headers={**auth, "Origin": "https://wrong.example"})[0], 403)
                status, halted = request("POST", "/api/halt", json.dumps({"request_id": "halt-1"}),
                                        {**auth, "Content-Type": "application/json"})
                self.assertEqual(status, 202)
                self.assertFalse(halted["broker_acknowledged"])
                self.assertFalse(halted["positions_confirmed_flat"])
                status, _ = request("POST", "/api/halt", json.dumps({"enabled": False}),
                                    {**auth, "Content-Type": "application/json"})
                self.assertEqual(status, 400)
            finally:
                pass

