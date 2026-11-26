"""Offline wrap-up coverage for the last reachable 1-3 line gaps in the worker package.

Tests only. Every price, timestamp and session is an invented synthetic fixture.
urllib's opener is blocked, broker/data transports are in-memory recorders, and
any test that touches the paper adapter asserts that no order POST was sent.
Lines that remain uncovered are genuinely unreachable; see worker/COVERAGE.md.
"""
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from decimal import Decimal
import io
import runpy
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings

from momentum import alpaca as alpaca_module
from momentum import crypto_observation as co
from momentum.alpaca import BrokerError, PaperClient
from momentum.config import Config
from momentum.crypto_observation import CryptoDataError, CryptoQuoteReader
from momentum.market_observation import MarketWatchCollector
from momentum.server import make_server
from momentum.strategy import StrategyConfig, evaluate_setup
from tests.test_strategy import experimental, valid_snapshot
from test_alpaca_adapter_coverage import Recorder
from test_crypto_market_observation_coverage import (
    MNOW, SYMBOLS, FakeProvider, envelope, sanitize, snap, z,
)
from worker_fakes import armed_config


def block_network(test):
    blocker = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError(
        "Network access is forbidden in offline coverage tests"))
    blocker.start()
    test.addCleanup(blocker.stop)


def run_as_main(module, argv):
    """Execute a module's ``if __name__ == "__main__"`` guard in-process."""
    out, err = io.StringIO(), io.StringIO()
    with patch.object(sys, "argv", argv), redirect_stdout(out), redirect_stderr(err), \
            warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # module already imported by the suite
        with unittest.TestCase().assertRaises(SystemExit) as ctx:
            runpy.run_module(module, run_name="__main__")
    return ctx.exception.code, out.getvalue(), err.getvalue()


class StrategyTimingTests(unittest.TestCase):
    """Reject branches that need a trade/clock just outside the trigger bar."""

    def test_trade_before_last_completed_bar_is_not_current_intrabar(self):
        s = valid_snapshot()
        # Last completed bar ends 13:35:00Z; the trade printed half a second earlier.
        s["last_trade"]["timestamp"] = s["last_trade"]["received_at"] = "2026-10-16T13:34:59.500000+00:00"
        result = evaluate_setup(s, experimental())
        self.assertFalse(result.accepted)
        self.assertEqual(result.reasons, ("trigger_not_current_intrabar",))
        self.assertIsNone(result.intent)

    def test_decision_a_full_minute_after_last_bar_is_stale(self):
        s = valid_snapshot()
        s["now"] = "2026-10-16T13:36:00.500000+00:00"
        for key in ("quote", "last_trade"):
            s[key]["timestamp"] = s[key]["received_at"] = "2026-10-16T13:35:59.500000+00:00"
        # Keep RVOL fresh (< 1 min old) with a matching same-clock history.
        s["rvol"]["as_of"] = "2026-10-16T13:35:30+00:00"
        for row in s["rvol"]["history"]:
            row["clock"] = "09:35:30"
            row["known_at"] = row["known_at"].replace("09:35:01", "09:35:31")
        result = evaluate_setup(s, experimental())
        self.assertFalse(result.accepted)
        self.assertEqual(result.reasons, ("stale_completed_bars",))
        self.assertIsNone(result.intent)

    def test_stop_at_or_below_zero_is_rejected(self):
        # A (valid, positive) tick larger than the pullback low pushes the stop below zero.
        s = valid_snapshot()
        s["last_trade"]["price"] = "13.30"
        s["quote"]["bid"], s["quote"]["ask"] = "13.29", "13.30"
        result = evaluate_setup(s, experimental(tick_size=Decimal("7")))
        self.assertFalse(result.accepted)
        self.assertEqual(result.reasons, ("invalid_stop_distance",))
        self.assertIsNone(result.intent)


class MarketWatchWrapupTests(unittest.TestCase):
    def setUp(self):
        block_network(self)

    def test_future_collection_envelope_rejected(self):
        value = envelope()
        value["collected_at"] = z(MNOW + timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, "future"):
            sanitize(value)

    def test_inverted_calendar_hours_are_unknown_and_not_cached(self):
        provider = FakeProvider([{"date": "2026-10-16", "open": "16:00", "close": "09:30"}],
                                {"AAPL": snap(), "MSFT": snap()})
        config = SimpleNamespace(watchlist=list(SYMBOLS), data_feed="iex", quote_max_age_seconds=2)
        collector = MarketWatchCollector(provider, config, clock=lambda: MNOW)
        for _ in range(2):
            out = collector.collect()
            self.assertEqual((out["session"]["status"], out["session"]["error_code"]),
                             ("unknown", "calendar_invalid"))
        self.assertIsNone(collector._calendar_cache)
        self.assertEqual(provider.calendar_calls, 2)  # an invalid session is never reused


class TamperedHostLockTests(unittest.TestCase):
    """Defense-in-depth URL locks: unreachable with the shipped constants.

    They fire only if a module constant is tampered with; these tests pin that the
    lock still refuses before any transport call (no GET, no POST).
    """

    def setUp(self):
        block_network(self)

    def test_paper_adapter_refuses_non_https_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = Recorder({"status": "ACTIVE"})
            client = PaperClient(armed_config(tmp), transport=transport)
            with patch.object(alpaca_module, "PAPER_API_HOST", "http://paper-api.alpaca.markets"):
                with self.assertRaisesRegex(BrokerError, "host lock"):
                    client.account()
            self.assertEqual(transport.calls, [])
            self.assertFalse(any(method == "POST" for method, _, _ in transport.calls))

    def test_crypto_reader_refuses_unexpected_url(self):
        calls = []
        reader = CryptoQuoteReader(SimpleNamespace(mode="paper", api_key="k", api_secret="s"),
                                   transport=lambda *a: calls.append(a))
        for url in ("https://example.invalid/v1beta3/crypto/us/latest/quotes",
                    "https://data.alpaca.markets/v2/orders"):
            with self.subTest(url=url), patch.object(co, "CRYPTO_URL", url):
                with self.assertRaises(CryptoDataError):
                    reader.latest_quotes(["BTC/USD"])
        self.assertEqual(calls, [])


class ServerHandlerHelperTests(unittest.TestCase):
    """Handler helpers reachable by their default arguments but unused by routes."""

    def handler(self, d):
        class _Offline:
            def __init__(self, address, handler):
                self.handler = handler

        cfg = Config(db_path=d + "/journal.sqlite3", export_dir=d + "/exports",
                     control_token="t" * 32, cors_origin="https://example.invalid")
        handler_class = make_server(cfg, address=("127.0.0.1", 0), server_class=_Offline).handler
        handler = handler_class.__new__(handler_class)  # no socket; fields set by hand
        handler.request_version, handler.requestline, handler.command = "HTTP/1.0", "GET / HTTP/1.0", "GET"
        handler.client_address = ("127.0.0.1", 0)
        handler.headers = {"Origin": "https://example.invalid"}
        handler.wfile = io.BytesIO()
        return handler

    def test_send_without_payload_writes_empty_body_and_logs_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            handler = self.handler(d)
            err = io.StringIO()
            with redirect_stderr(err):
                handler.send(204)
                self.assertIsNone(handler.log_message("%s %s", "Authorization: Bearer x", "/v1"))
            raw = handler.wfile.getvalue().decode()
            head, _, body = raw.partition("\r\n\r\n")
            self.assertTrue(head.startswith("HTTP/1.0 204"))
            self.assertIn("Content-Length: 0", head)
            self.assertIn("Cache-Control: no-store", head)
            self.assertIn("Access-Control-Allow-Origin: https://example.invalid", head)
            self.assertEqual(body, "")
            self.assertEqual(err.getvalue(), "")  # access log never echoes headers or paths


class ModuleMainGuardTests(unittest.TestCase):
    def setUp(self):
        block_network(self)

    def test_worker_cli_main_guard_help_only(self):
        code, out, _ = run_as_main("momentum", ["momentum", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("paper-only", out)

    def test_decision_replay_main_guard_missing_input_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err = run_as_main("momentum.decision_replay",
                                         ["decision_replay", d + "/absent.json", "--output", d + "/out.json"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("decision_replay_error: "))
        self.assertNotIn(d, err)


if __name__ == "__main__":
    unittest.main()
