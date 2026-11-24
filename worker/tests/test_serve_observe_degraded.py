"""serve-observe stays up when the journal cannot be opened or written.

These tests use local files only. They do not submit, cancel, or replace orders.
"""
import io
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from momentum.config import Config
from momentum.journal import Journal
from momentum.server import Application, make_server


def _config(directory, **overrides):
    values = dict(
        mode="paper",
        paper_enabled=False,
        db_path=str(Path(directory) / "momentum.sqlite3"),
        export_dir=str(Path(directory) / "exports"),
        control_token="synthetic-control-token-not-a-credential",
        api_key="synthetic-alpaca-key-not-a-credential",
        api_secret="synthetic-alpaca-secret-not-a-credential",
        cors_origin="https://dashboard.example",
        watchlist=("AAPL",),
        journal_min_free_mb=0,
    )
    values.update(overrides)
    return Config(**values)


class _OfflineServer:
    def __init__(self, address, handler):
        self.handler = handler
        self.server_name, self.server_port = address


def _request(server, path):
    raw = f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode()

    class FakeSocket:
        output = b""

        def makefile(self, *args):
            return io.BytesIO(raw)

        def sendall(self, data):
            self.output += bytes(data)

    connection = FakeSocket()
    server.handler(connection, ("127.0.0.1", 0), server)
    head, content = connection.output.split(b"\r\n\r\n", 1)
    status = int(head.split(b" ", 2)[1])
    return status, json.loads(content)


class DegradedJournalTests(unittest.TestCase):
    def test_health_reports_execution_locked_when_the_journal_is_writable(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = _config(directory)
            app = Application(cfg)
            server = make_server(cfg, app, address=("127.0.0.1", 0), server_class=_OfflineServer)
            status, health = _request(server, "/health")
            self.assertEqual(status, 200)
            self.assertEqual(health["status"], "ok")
            self.assertEqual(health["journal_status"], "writable")
            self.assertTrue(health["execution_disabled"])
            self.assertTrue(health["execution_locked"])
            self.assertFalse(health["paper_trading_enabled"])
            self.assertFalse(health["paper_execution_running"])
            self.assertEqual(health["orders_submitted"], 0)
            self.assertNotIn(b"synthetic-alpaca-key-not-a-credential", json.dumps(health).encode())

    def test_full_volume_open_stays_degraded_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = _config(directory)
            journal = Journal(cfg.db_path, min_free_bytes=0)
            kept = journal.append("paper-observation", "preserved_event", {"marker": "keep"}, key="keep")
            before = Path(cfg.db_path).read_bytes()
            with mock.patch.object(Journal, "__init__", side_effect=sqlite3.OperationalError(
                    "journal_volume_full_grow_the_disk_or_free_space")):
                app = Application(cfg)
            self.assertEqual(app.journal_status, "read_only")
            self.assertEqual(app.startup_error, "journal_volume_full_grow_the_disk_or_free_space")
            self.assertEqual(Path(cfg.db_path).read_bytes(), before)
            health = app.health()
            self.assertEqual(health["status"], "degraded")
            self.assertTrue(health["execution_disabled"])
            self.assertTrue(health["execution_locked"])
            self.assertFalse(health["paper_execution_running"])
            self.assertEqual(health["orders_submitted"], 0)
            reopened = Journal(cfg.db_path, min_free_bytes=0)
            self.assertEqual(reopened.event_by_key("keep")["payload"], kept["payload"])
            self.assertGreaterEqual(reopened._row_count(), 1)

    def test_non_database_file_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momentum.sqlite3"
            path.write_bytes(b"not a sqlite database")
            before = path.read_bytes()
            cfg = _config(directory)
            app = Application(cfg)
            self.assertEqual(path.read_bytes(), before)
            self.assertIsNone(app.journal)
            self.assertEqual(app.journal_status, "unavailable")
            self.assertEqual(app.startup_error, "journal_unavailable")
            self.assertEqual(app.health()["status"], "degraded")
            self.assertTrue(app.health()["execution_disabled"])

    def test_flag_true_still_locks_observation_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = _config(directory, paper_enabled=True)
            health = Application(cfg).health()
            self.assertTrue(health["paper_trading_enabled"])
            self.assertTrue(health["execution_disabled"])
            self.assertTrue(health["execution_locked"])
            self.assertFalse(health["paper_execution_running"])
            self.assertEqual(health["orders_submitted"], 0)

    def _serve(self, directory, path, extra_env):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        env = {
            "PATH": __import__("os").environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "HOME": __import__("os").environ.get("HOME", "/tmp"),
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "JOURNAL_PATH": str(path),
            "EXPORT_DIR": str(Path(directory) / "exports"),
            "WATCHLIST": "AAPL",
            "ALPACA_DATA_FEED": "iex",
            "ALPACA_API_KEY": "dummy-key",
            "ALPACA_SECRET_KEY": "dummy-secret",
            "PAPER_TRADING_ENABLED": "false",
        }
        env.update(extra_env)
        root = Path(__file__).resolve().parents[1]
        proc = subprocess.Popen(
            [sys.executable, "-m", "momentum", "serve-observe"],
            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return proc, port

    def _health(self, proc, port):
        body = None
        deadline = time.time() + 8
        while time.time() < deadline:
            if proc.poll() is not None:
                err = proc.stderr.read().decode("utf-8", "replace")
                self.fail("serve-observe exited %s before /health: %s" % (proc.returncode, err[:800]))
            try:
                with urllib.request.urlopen("http://127.0.0.1:%s/health" % port, timeout=0.5) as resp:
                    body = json.loads(resp.read().decode())
                    if body.get("journal_status") != "unopened" or body.get("startup_error"):
                        return body
            except Exception:
                time.sleep(0.05)
        self.assertIsNotNone(body)
        return body

    def test_serve_observe_journal_open_failure_does_not_exit_1(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momentum.sqlite3"
            path.write_bytes(b"not a sqlite database")
            before = path.read_bytes()
            proc, port = self._serve(directory, path, {"MOMENTUM_MODE": "paper", "PAPER_TRADING_ENABLED": "true"})
            try:
                body = self._health(proc, port)
                self.assertIsNone(proc.poll())
                self.assertNotEqual(proc.poll(), 1)
                self.assertEqual(body["status"], "degraded")
                self.assertEqual(body["journal_status"], "unavailable")
                self.assertTrue(body["execution_disabled"])
                self.assertTrue(body["execution_locked"])
                self.assertTrue(body["paper_trading_enabled"])
                self.assertFalse(body["paper_execution_running"])
                self.assertEqual(body["orders_submitted"], 0)
                self.assertEqual(path.read_bytes(), before)
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                proc.stdout.close()
                proc.stderr.close()

    def test_serve_observe_invalid_config_does_not_exit_1(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momentum.sqlite3"
            proc, port = self._serve(directory, path, {"MOMENTUM_MODE": "live"})
            try:
                body = self._health(proc, port)
                self.assertIsNone(proc.poll())
                self.assertEqual(body["status"], "degraded")
                self.assertEqual(body["journal_status"], "unopened")
                self.assertEqual(body["startup_error"], "configuration_invalid")
                self.assertTrue(body["execution_disabled"])
                self.assertFalse(body["paper_trading_enabled"])
                self.assertEqual(body["orders_submitted"], 0)
                self.assertFalse(path.exists())
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                proc.stdout.close()
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
