"""Paper-trading release. Fake broker only: no Alpaca and no network."""
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from momentum import __main__ as cli
from momentum.alpaca import PaperClient, PaperDisabled, assert_paper_broker_url, fill_report
from momentum.config import PAPER_API_HOST, Config, ConfigError
from momentum.journal import Journal, canonical_json
from momentum.observation import ObservationWorker, ReadOnlyPaperClient
from momentum.paper_runtime import PaperTradingWorker
from momentum.server import Application
from worker_fakes import FakeVenue, armed_config

ACCOUNT = "11111111-1111-4111-8111-111111111111"
LIVE = "https://api.alpaca.markets"
ROOT = Path(__file__).resolve().parents[1]


class PaperFake(FakeVenue):
    """In-process venue. Unknown GETs return an empty body. Mutations are recorded."""

    def __init__(self, config):
        super().__init__(config)
        self.account_extra = {"id": ACCOUNT, "paper": True}
        self.omit_fill = False

    def request(self, method, url, headers, payload):
        parsed = urlsplit(url)
        if method == "PATCH" and parsed.path.startswith("/v2/orders/"):
            self.calls.append({"method": method, "url": url, "payload": json.loads(json.dumps(payload))})
            order = next(row for row in self.orders.values() if row["id"] == parsed.path.rsplit("/", 1)[1])
            if payload and payload.get("limit_price"):
                order["limit_price"] = payload["limit_price"]
            order["status"] = "accepted"
            if self.omit_fill:
                return {"id": order["id"], "client_order_id": order["client_order_id"], "status": order["status"]}
            return json.loads(json.dumps(order))
        try:
            body = super().request(method, url, headers, payload)
        except AssertionError:
            if method != "GET":
                raise
            return {}
        if method == "GET" and parsed.path == "/v2/account" and isinstance(body, dict):
            body = {**body, **self.account_extra}
        return body


def _mutations(venue):
    return [call for call in venue.calls if call["method"] in {"POST", "DELETE", "PATCH"}]


class _Builder:
    def __init__(self, venue):
        self.venue = venue

    def build_watchlist(self):
        return [self.venue.snapshot()]


class PaperReleaseTests(unittest.TestCase):
    def _worker(self, directory, config, venue, **kwargs):
        journal = Journal(config.db_path, sample_seconds=0, min_free_bytes=0)
        return PaperTradingWorker(config, provider=venue.client, journal=journal,
                                  snapshot_builder=_Builder(venue), clock=lambda: venue.now, **kwargs), journal

    def _cycle(self, worker, venue):
        return worker.cycle(snapshots=[venue.snapshot()], session_ledger=venue.ledger())

    def test_live_urls_are_refused_and_cannot_be_configured(self):
        self.assertEqual(assert_paper_broker_url(PAPER_API_HOST), "https://paper-api.alpaca.markets")
        self.assertEqual(PAPER_API_HOST, "https://paper-api.alpaca.markets")
        for url in (LIVE, LIVE + "/v2/orders", "http://api.alpaca.markets",
                    "https://paper-api.alpaca.markets/v2", "https://paper-api.alpaca.markets.evil.test",
                    "http://paper-api.alpaca.markets", "https://broker.example", "", None):
            with self.subTest(url=url):
                with self.assertRaises(PaperDisabled) as caught:
                    assert_paper_broker_url(url)
                if url is not None and "api.alpaca.markets" in str(url) and "paper-api" not in str(url):
                    self.assertEqual(str(caught.exception), "live_broker_url_refused")
                else:
                    self.assertIn(str(caught.exception), {"live_broker_url_refused", "broker_url_not_paper_endpoint"})
        with self.assertRaises(ConfigError):
            Config.from_env({"APCA_API_BASE_URL": LIVE, "MOMENTUM_MODE": "paper"})
        with self.assertRaises(ConfigError):
            Config.from_env({"MOMENTUM_MODE": "live"})

    def test_paper_path_submits_cancel_and_replace_on_the_fake_only(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            venue = PaperFake(cfg)
            worker, journal = self._worker(directory, cfg, venue)
            with worker:
                first = self._cycle(worker, venue)
                self.assertTrue(first["paper_execution_active"], first)
                self.assertEqual(first["execution_reason"], "paper_gates_open")
                self.assertEqual(first["submitted_actions"], 1, first)
                self.assertFalse(first["live_execution_supported"])
                self.assertTrue(first["live_execution_locked"])
                posts = [call for call in venue.calls if call["method"] == "POST"]
                self.assertEqual(len(posts), 1)
                self.assertEqual(urlsplit(posts[0]["url"]).hostname, "paper-api.alpaca.markets")
                client_id = posts[0]["payload"]["client_order_id"]
                self.assertTrue(client_id.startswith("mlab_"))
                again = venue.client.submit(
                    journal.events(event_type="paper_order_requested")[-1]["payload"]["intent"],
                    journal, "paper-repeat", quote=venue.snapshot()["quote"], guard=lambda: True)
                self.assertEqual(again["client_order_id"], client_id)
                self.assertEqual(sum(call["method"] == "POST" for call in venue.calls), 1)
                venue.advance(20)
                second = self._cycle(worker, venue)
                self.assertTrue(any(call["method"] == "DELETE" for call in venue.calls), second)
                self.assertEqual(sum(call["method"] == "POST" for call in venue.calls), 1)
                order = next(iter(venue.orders.values()))
                venue.omit_fill = True
                venue.client.replace(order, journal, "paper-replace", limit_price="9.50")
                requested = journal.events(event_type="paper_replace_requested")
                acknowledged = journal.events(event_type="paper_replace_acknowledged")
                self.assertEqual(len(requested), 1)
                self.assertEqual(len(acknowledged), 1)
                self.assertLess(requested[0]["seq"], acknowledged[0]["seq"])
                self.assertIsNone(acknowledged[0]["payload"]["fill"]["filled_qty"])
                self.assertIsNone(acknowledged[0]["payload"]["fill"]["filled_avg_price"])
                self.assertNotEqual(acknowledged[0]["payload"]["fill"]["filled_qty"], 0)
                self.assertEqual(urlsplit(next(call["url"] for call in venue.calls if call["method"] == "PATCH")).hostname,
                                 "paper-api.alpaca.markets")
                kinds = {row["event_type"] for row in journal.events()}
                for kind in ("paper_order_requested", "paper_order_acknowledged",
                             "paper_cancel_requested", "paper_cancel_accepted",
                             "paper_replace_requested", "paper_replace_acknowledged"):
                    self.assertIn(kind, kinds)
                health = Application(cfg, paper_worker=worker).health()
                self.assertTrue(health["paper_trading_enabled"])
                self.assertTrue(health["paper_execution_active"])
                self.assertEqual(health["execution_reason"], "paper_gates_open")
                self.assertFalse(health["live_execution_supported"])
                self.assertTrue(health["live_execution_locked"])
                self.assertTrue(health["execution_locked"])
                self.assertGreaterEqual(health["orders_submitted"], 1)
                blob = canonical_json(health) + canonical_json([row["payload"] for row in journal.events()])
                self.assertNotIn("FAKE-KEY", blob)
                self.assertNotIn("FAKE-SECRET", blob)

    def test_flag_off_live_url_account_kill_switch_and_hash_do_not_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = replace(armed_config(directory), paper_enabled=False)
            venue = PaperFake(cfg)
            worker, _journal = self._worker(directory, cfg, venue)
            with worker:
                result = self._cycle(worker, venue)
                self.assertFalse(result["paper_execution_active"])
                self.assertEqual(result["execution_reason"], "paper_trading_not_enabled")
                self.assertEqual(_mutations(venue), [])
                self.assertEqual(result["orders_submitted"], 0)

        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            venue = PaperFake(cfg)
            worker, _journal = self._worker(directory, cfg, venue, broker_url=LIVE)
            with worker:
                result = self._cycle(worker, venue)
                self.assertEqual(result["execution_reason"], "live_broker_url_refused")
                self.assertFalse(result["paper_execution_active"])
                self.assertEqual(venue.calls, [])
                self.assertEqual(_mutations(venue), [])

        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            venue = PaperFake(cfg)
            venue.account_extra = {"id": ACCOUNT, "paper": True, "trading_blocked": True}
            worker, _journal = self._worker(directory, cfg, venue)
            with worker:
                result = self._cycle(worker, venue)
                self.assertEqual(result["execution_reason"], "paper_account_check_failed")
                self.assertFalse(result["paper_execution_active"])
                self.assertEqual(_mutations(venue), [])

        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            venue = PaperFake(cfg)
            worker, journal = self._worker(directory, cfg, venue)
            with worker:
                journal.set_kill_switch(True, "test halt", request_id="halt-paper")
                result = self._cycle(worker, venue)
                self.assertEqual(result["execution_reason"], "kill_switch_latched")
                self.assertFalse(result["paper_execution_active"])
                self.assertEqual(_mutations(venue), [])

        with tempfile.TemporaryDirectory() as directory:
            cfg = replace(armed_config(directory), paper_reviewed_config_hash="not-the-reviewed-hash")
            venue = PaperFake(cfg)
            worker, _journal = self._worker(directory, cfg, venue)
            with worker:
                result = self._cycle(worker, venue)
                self.assertEqual(result["execution_reason"], "exact_paper_configuration_not_reviewed")
                self.assertEqual(_mutations(venue), [])

    def test_risk_notional_cap_blocks_submit_while_gates_are_open(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = replace(armed_config(directory), risk_overrides={"max_notional": "1"})
            cfg = replace(cfg, paper_reviewed_config_hash=cfg.review_hash())
            venue = PaperFake(cfg)
            worker, _journal = self._worker(directory, cfg, venue)
            with worker:
                result = self._cycle(worker, venue)
                self.assertTrue(result["paper_execution_active"], result)
                self.assertEqual(_mutations(venue), [])
                self.assertEqual(result["submitted_actions"], 0)
                reasons = []
                for decision in result["session"]["decisions"]:
                    reasons.extend(decision["risk"]["reasons"])
                self.assertIn("zero_quantity_under_limits", reasons)
            self.assertIsNone(fill_report({"status": "new"})["filled_qty"])
            self.assertEqual(fill_report({"filled_qty": "0", "filled_avg_price": "1.00"})["filled_qty"], "0")
            self.assertIsNone(fill_report({"filled_qty": None, "filled_avg_price": ""})["filled_avg_price"])

    def test_serve_observe_still_cannot_submit_cancel_or_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            venue = PaperFake(cfg)
            readonly = ReadOnlyPaperClient(cfg, transport=venue.request, clock=lambda: venue.now)
            for call in (readonly.submit, readonly.cancel, readonly.replace):
                with self.assertRaises(PaperDisabled):
                    call()
            self.assertEqual(_mutations(venue), [])
            journal = Journal(cfg.db_path, sample_seconds=0, min_free_bytes=0)
            with ObservationWorker(cfg, provider=venue.client, journal=journal,
                                   snapshot_builder=_Builder(venue), clock=lambda: venue.now) as worker:
                result = worker.cycle()
                self.assertEqual(result["orders_submitted"], 0)
                self.assertFalse(result["execution_enabled"])
                for name in ("submit", "cancel", "replace"):
                    with self.assertRaises(PaperDisabled):
                        getattr(worker.provider, name)()
            self.assertEqual(_mutations(venue), [])
            health = Application(cfg).health()
            self.assertFalse(health["paper_execution_active"])
            self.assertTrue(health["execution_disabled"])
            self.assertTrue(health["execution_locked"])
            self.assertTrue(health["live_execution_locked"])
            self.assertFalse(health["live_execution_supported"])
            self.assertEqual(health["orders_submitted"], 0)
            self.assertEqual(health["execution_reason"], "observation_runtime_cannot_execute_orders")

    def test_blueprints_keep_the_flag_off_and_split_commands(self):
        observe = (ROOT / "render-observe.yaml").read_text()
        paper = (ROOT / "render-paper.yaml").read_text()
        self.assertIn("dockerCommand: serve-observe", observe)
        self.assertIn('value: "false"', observe)
        self.assertNotIn("dockerCommand: serve-paper", observe)
        self.assertIn("dockerCommand: serve-paper", paper)
        self.assertIn('value: "false"', paper)
        self.assertIn("https://paper-api.alpaca.markets", paper)
        self.assertNotIn("api.alpaca.markets", paper.replace("paper-api.alpaca.markets", ""))

    def test_serve_paper_command_uses_the_paper_worker(self):
        class FakeWorker:
            instances = []

            def __init__(self, config, **kwargs):
                self.stop_event = threading.Event()
                self.max_cycles = "unset"
                self.kwargs = kwargs
                FakeWorker.instances.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.stop_event.set()
                return False

            def run(self, max_cycles=None):
                self.max_cycles = max_cycles
                return 1

            def execution_status(self):
                return {"paper_execution_active": False, "execution_reason": "awaiting_first_cycle",
                        "orders_submitted": 0}

        class FakeServer:
            server_port = 9
            calls = []

            def serve_forever(self):
                self.calls.append("serve_forever")

            def shutdown(self):
                self.calls.append("shutdown")

            def server_close(self):
                self.calls.append("server_close")

        env = {"JOURNAL_PATH": os.path.join(tempfile.mkdtemp(), "j.sqlite3"),
               "EXPORT_DIR": os.path.join(tempfile.mkdtemp(), "exports"),
               "MOMENTUM_MODE": "paper", "PAPER_TRADING_ENABLED": "false"}
        with patch.dict(os.environ, env, clear=True), \
                patch("momentum.observation.ObservationWorker", FakeWorker), \
                patch("momentum.crypto_paper_loop.CryptoPaperLoop.run",
                      side_effect=AssertionError("standalone crypto timer must not start")), \
                patch.object(cli, "make_server", return_value=FakeServer()):
            code = cli.main(["serve-paper", "--once"])
        self.assertEqual(code, 0)
        self.assertEqual(len(FakeWorker.instances), 1)
        self.assertTrue(FakeWorker.instances[0].kwargs.get("stock_execution"))
        self.assertIsNotNone(FakeWorker.instances[0].kwargs.get("journal"))
        self.assertEqual(FakeWorker.instances[0].max_cycles, 1)
        self.assertTrue(FakeWorker.instances[0].stop_event.is_set())

    def test_serve_paper_live_mode_stays_up_without_orders(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "LANG": "C.UTF-8",
                "HOME": os.environ.get("HOME", "/tmp"),
                "HOST": "127.0.0.1",
                "PORT": str(port),
                "JOURNAL_PATH": str(Path(directory) / "momentum.sqlite3"),
                "EXPORT_DIR": str(Path(directory) / "exports"),
                "MOMENTUM_MODE": "live",
                "PAPER_TRADING_ENABLED": "true",
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "momentum", "serve-paper"],
                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                import time
                import urllib.request
                body = None
                deadline = time.time() + 8
                while time.time() < deadline:
                    if proc.poll() is not None:
                        err = proc.stderr.read().decode("utf-8", "replace")
                        self.fail("serve-paper exited %s: %s" % (proc.returncode, err[:800]))
                    try:
                        with urllib.request.urlopen("http://127.0.0.1:%s/health" % port, timeout=0.5) as resp:
                            body = json.loads(resp.read().decode())
                            break
                    except Exception:
                        time.sleep(0.05)
                self.assertIsNotNone(body)
                self.assertEqual(body["status"], "degraded")
                self.assertFalse(body["paper_execution_active"])
                self.assertEqual(body["execution_reason"], "mode_not_paper")
                self.assertFalse(body["live_execution_supported"])
                self.assertTrue(body["live_execution_locked"])
                self.assertEqual(body["orders_submitted"], 0)
                self.assertTrue(body["execution_disabled"])
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                proc.stdout.close()
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
