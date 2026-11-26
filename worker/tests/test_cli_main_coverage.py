"""Offline coverage for the worker CLI (momentum.__main__).

Paper only: every network-facing collaborator (observation worker, read-only paper client,
HTTP server, Supabase publisher) is replaced with an in-memory fake. Nothing submits orders.
"""
from dataclasses import replace
import contextlib
import io
import json
import os
import signal
import tempfile
import threading
import unittest
from unittest.mock import patch

from momentum import __main__ as cli
from momentum.config import Config


class _Env:
    """Isolated environment pointing the journal and exports at a temp directory."""

    def __init__(self, **extra):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"JOURNAL_PATH": os.path.join(self.tmp.name, "j.sqlite3"),
                    "EXPORT_DIR": os.path.join(self.tmp.name, "exports"), **extra}

    def __enter__(self):
        self._patch = patch.dict(os.environ, self.env, clear=True)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        self.tmp.cleanup()


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class FakeServer:
    server_port = 8787

    def __init__(self, interrupt=False):
        self.interrupt = interrupt
        self.calls = []
        self._stop = threading.Event()

    def serve_forever(self):
        self.calls.append("serve_forever")
        if self.interrupt:
            raise KeyboardInterrupt
        self._stop.wait(2)

    def shutdown(self):
        self.calls.append("shutdown")
        self._stop.set()

    def server_close(self):
        self.calls.append("server_close")


class FakeWorker:
    instances = []

    def __init__(self, config, error_code=None, **_kwargs):
        self.config = config
        self.stop_event = threading.Event()
        self.error_code = error_code
        self.max_cycles = "unset"
        FakeWorker.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, max_cycles=None):
        self.max_cycles = max_cycles
        return 1

    def state(self):
        return {"latest_cycle": {"error_code": self.error_code} if self.error_code else None}


class CliCoverageTests(unittest.TestCase):
    def setUp(self):
        FakeWorker.instances = []

    def test_requires_a_command(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main([])

    def test_live_mode_is_rejected_with_exit_2(self):
        with _Env(MOMENTUM_MODE="live"):
            code, out, err = run(["readiness"])
        self.assertEqual(code, 2)
        self.assertIn("Worker stopped", err)
        self.assertEqual(out, "")

    def test_non_paper_host_override_is_rejected(self):
        with _Env(APCA_API_BASE_URL="https://api.alpaca.markets"):
            code, _, err = run(["readiness"])
        self.assertEqual(code, 2)
        self.assertIn("fixed paper API host", err)

    def test_readiness_prints_sanitized_json(self):
        with _Env(ALPACA_API_KEY="k-secret", ALPACA_SECRET_KEY="s-secret"):
            code, out, _ = run(["readiness"])
        self.assertEqual(code, 0)
        self.assertIsInstance(json.loads(out), dict)
        self.assertNotIn("k-secret", out)
        self.assertNotIn("s-secret", out)

    def test_demo_then_publish_demo_uses_journal_row(self):
        published = []

        class FakePublisher:
            def __init__(self, config):
                pass

            def publish_demo(self, payload):
                published.append(payload)

        with _Env() as env:
            code, out, _ = run(["demo", "--run-id", "cov-demo"])
            self.assertEqual(code, 0)
            summary = json.loads(out)
            self.assertEqual(summary["run_id"], "cov-demo")
            self.assertTrue(summary["export"].endswith("cov-demo.json"))
            with patch("momentum.publisher.Publisher", FakePublisher):
                code, out, _ = run(["publish-demo", "--run-id", "cov-demo"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out), {"published": True, "mode": "demo", "run_id": "cov-demo"})
            self.assertEqual(len(published), 1)
            self.assertTrue(os.path.isdir(env.env["EXPORT_DIR"]))

    def test_publish_demo_without_run_fails_closed(self):
        with _Env(), patch("momentum.publisher.Publisher") as pub:
            code, _, err = run(["publish-demo", "--run-id", "missing"])
        self.assertEqual(code, 2)
        self.assertIn("run the demo first", err)
        pub.assert_not_called()

    def test_serve_closes_server_on_interrupt(self):
        server = FakeServer(interrupt=True)
        with _Env(), patch.object(cli, "make_server", return_value=server):
            code, out, _ = run(["serve"])
        self.assertEqual(code, 0)
        self.assertIn("paper execution is not running", out)
        self.assertEqual(server.calls, ["serve_forever", "server_close"])

    def test_observe_once_reports_zero_orders(self):
        with _Env(), patch("momentum.observation.ObservationWorker", FakeWorker):
            code, out, _ = run(["observe", "--once"])
        self.assertEqual(code, 0)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(result, {"observation_cycles": 1, "execution_enabled": False,
                                  "orders_submitted": 0, "last_error_code": None})
        worker = FakeWorker.instances[0]
        self.assertEqual(worker.max_cycles, 1)
        self.assertTrue(worker.stop_event.is_set())

    def test_observe_once_with_error_exits_2(self):
        def factory(config):
            return FakeWorker(config, error_code="broker_unreachable")
        with _Env(), patch("momentum.observation.ObservationWorker", factory):
            code, out, _ = run(["observe", "--once"])
        self.assertEqual(code, 2)
        self.assertIn("broker_unreachable", out)

    def test_continuous_observe_runs_unbounded(self):
        with _Env(), patch("momentum.observation.ObservationWorker", FakeWorker):
            code, _, _ = run(["observe"])
        self.assertEqual(code, 0)
        self.assertIsNone(FakeWorker.instances[0].max_cycles)

    def test_serve_observe_starts_and_stops_api_thread(self):
        server = FakeServer()
        with _Env(), patch("momentum.observation.ObservationWorker", FakeWorker), \
                patch.object(cli, "make_server", return_value=server) as make:
            code, out, _ = run(["serve-observe"])
        self.assertEqual(code, 0)
        self.assertIn("execution disabled", out)
        make.assert_called_once()
        self.assertIn("shutdown", server.calls)
        self.assertIn("server_close", server.calls)
        self.assertTrue(FakeWorker.instances[0].stop_event.is_set())

    def test_paper_preflight_is_read_only(self):
        class FakeClient:
            def __init__(self, config):
                self.config = config

            def account(self):
                return {"status": "ACTIVE"}

            def positions(self):
                return [{"symbol": "AAPL"}]

            def open_orders(self):
                return []

        class FakeBuilder:
            def __init__(self, client, config):
                pass

            def build_watchlist(self):
                return [{"symbol": "AAPL"}, {"symbol": "MSFT"}]

        with _Env(WATCHLIST="AAPL,MSFT"), patch("momentum.observation.ReadOnlyPaperClient", FakeClient), \
                patch("momentum.data.SnapshotBuilder", FakeBuilder):
            code, out, _ = run(["paper-preflight"])
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["mode"], "paper")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["execution_armed"])
        self.assertEqual(result["orders_submitted"], 0)
        self.assertEqual(result["positions_count"], 1)
        self.assertEqual(result["open_orders_count"], 0)
        self.assertEqual(result["symbols_received"], ["AAPL", "MSFT"])

    def test_paper_preflight_without_watchlist_skips_snapshots(self):
        class FakeClient:
            def __init__(self, config):
                pass

            def account(self):
                return {"status": "ACTIVE"}

            def positions(self):
                return []

            def open_orders(self):
                return []

        # An unset WATCHLIST now defaults to the liquid IEX list AAPL,MSFT,SPY,
        # so the empty-watchlist branch is reached with an explicitly emptied Config.
        real_from_env = Config.from_env

        def empty_watchlist(env=None):
            return replace(real_from_env(env), watchlist=())

        with _Env(), patch("momentum.__main__.Config.from_env", side_effect=empty_watchlist), \
                patch("momentum.observation.ReadOnlyPaperClient", FakeClient), \
                patch("momentum.data.SnapshotBuilder") as builder:
            code, out, _ = run(["paper-preflight"])
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["symbols_received"], [])
        self.assertEqual(result["orders_submitted"], 0)
        self.assertFalse(result["execution_armed"])
        builder.assert_not_called()

    def test_paper_preflight_unset_watchlist_uses_liquid_default(self):
        class FakeClient:
            def __init__(self, config):
                pass

            def account(self):
                return {"status": "ACTIVE"}

            def positions(self):
                return []

            def open_orders(self):
                return []

        with _Env(), patch("momentum.observation.ReadOnlyPaperClient", FakeClient), \
                patch("momentum.data.SnapshotBuilder") as builder:
            builder.return_value.build_watchlist.return_value = [
                {"symbol": "AAPL"}, {"symbol": "MSFT"}, {"symbol": "SPY"}]
            code, out, _ = run(["paper-preflight"])
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(builder.call_args.args[1].watchlist, ("AAPL", "MSFT", "SPY"))
        self.assertEqual(result["symbols_received"], ["AAPL", "MSFT", "SPY"])
        self.assertEqual(result["orders_submitted"], 0)
        self.assertFalse(result["execution_armed"])

    def test_paper_preflight_broker_error_exits_2(self):
        class FailingClient:
            def __init__(self, config):
                pass

            def account(self):
                raise RuntimeError("paper account unreachable")

        with _Env(), patch("momentum.observation.ReadOnlyPaperClient", FailingClient):
            code, out, err = run(["paper-preflight"])
        self.assertEqual(code, 2)
        self.assertIn("paper account unreachable", err)
        self.assertEqual(out, "")


class ShutdownSignalsTests(unittest.TestCase):
    def test_main_thread_installs_and_restores_handlers(self):
        before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        stop = threading.Event()
        with cli.shutdown_signals(stop):
            handler = signal.getsignal(signal.SIGTERM)
            self.assertNotEqual(handler, before[signal.SIGTERM])
            handler(signal.SIGTERM, None)
            self.assertTrue(stop.is_set())
        self.assertEqual({s: signal.getsignal(s) for s in before}, before)

    def test_restores_handlers_on_error(self):
        before = signal.getsignal(signal.SIGINT)
        with self.assertRaises(ValueError):
            with cli.shutdown_signals(threading.Event()):
                raise ValueError("boom")
        self.assertEqual(signal.getsignal(signal.SIGINT), before)

    def test_non_main_thread_leaves_handlers_alone(self):
        before = signal.getsignal(signal.SIGTERM)
        seen = []

        def body():
            with cli.shutdown_signals(threading.Event()):
                seen.append(signal.getsignal(signal.SIGTERM))

        t = threading.Thread(target=body)
        t.start()
        t.join(5)
        self.assertEqual(seen, [before])


if __name__ == "__main__":
    unittest.main()
