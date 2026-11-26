"""serve-paper keeps observe's hooks and gates stock orders. Offline only."""
from dataclasses import replace
from urllib.parse import urlsplit
import tempfile
import threading
import unittest
from unittest.mock import patch

from momentum.alpaca import PaperDisabled, assert_paper_broker_url
from momentum.crypto_paper_loop import CryptoPaperLoop
from momentum.journal import Journal
from momentum.observation import ObservationWorker, StockPaperClient
from momentum.paper_risk import check_paper_intent
from momentum.paper_runtime import evaluate_paper_gates
from test_paper_trading_release import LIVE, PaperFake, _mutations
from worker_fakes import armed_config


class _Builder:
    def __init__(self, venue):
        self.venue = venue

    def build_watchlist(self):
        return [self.venue.snapshot()]


class _CryptoWatch:
    def collect(self):
        return {"rows": []}


class _ForexWatch:
    def __init__(self):
        self.calls = 0

    def collect(self):
        self.calls += 1
        return {"prices": []}


class _Publisher:
    def __init__(self, created_at):
        self.created_at = created_at
        self.resumes = 0
        self.refusal = "not-called"

    def acknowledge_halts(self, journal):
        return []

    def process_resumes(self, journal, precheck):
        self.resumes += 1
        self.refusal = precheck(self.created_at)
        return [("resume-1", "checked")]

    def publish_worker_state(self, **kwargs):
        return None


def _armed(directory, **changes):
    cfg = armed_config(directory)
    if changes:
        cfg = replace(cfg, **changes)
        cfg = replace(cfg, paper_reviewed_config_hash=cfg.review_hash())
    return cfg


class ServePaperSharedRuntimeTests(unittest.TestCase):
    def _worker(self, directory, cfg, venue, *, journal=None, **kwargs):
        journal = journal or Journal(cfg.db_path, sample_seconds=0, min_free_bytes=0)
        client = StockPaperClient(cfg, transport=venue.request, clock=lambda: venue.now)
        publisher = kwargs.pop("publisher", None)
        forex = kwargs.pop("forex_collector", None)
        crypto = kwargs.pop("crypto_collector", None)
        worker = ObservationWorker(
            cfg, provider=client, journal=journal, snapshot_builder=_Builder(venue),
            clock=lambda: venue.now, stock_execution=True, publisher=publisher,
            forex_collector=forex, crypto_collector=crypto,
            session_ledger_factory=kwargs.pop("session_ledger_factory", None), **kwargs)
        return worker, journal

    def test_serve_paper_runs_crypto_resume_and_forex_once(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = _armed(directory, crypto_watchlist=("BTC/USD",), crypto_paper_execution_enabled=True,
                         forex_watchlist=("EUR/USD",))
            venue = PaperFake(cfg)
            forex = _ForexWatch()
            publisher = _Publisher(venue.now)
            journal = Journal(cfg.db_path, sample_seconds=0, min_free_bytes=0)
            worker, journal = self._worker(
                directory, cfg, venue, journal=journal, publisher=publisher,
                forex_collector=forex, crypto_collector=_CryptoWatch(),
                session_ledger_factory=lambda: venue.ledger())
            with patch.object(CryptoPaperLoop, "run", side_effect=AssertionError("standalone timer")):
                with worker:
                    first = worker.cycle()
                    loop = worker._crypto_paper_loop
                    second = worker.cycle()
            self.assertIs(worker._crypto_paper_loop, loop)
            self.assertIs(loop.journal, journal)
            self.assertEqual(forex.calls, 2)
            self.assertEqual(publisher.resumes, 2)
            self.assertIn("forex_watch", first)
            self.assertEqual(first["resume_requests"], [{"id": "resume-1", "outcome": "checked"}])
            self.assertEqual(second["resume_requests"], [{"id": "resume-1", "outcome": "checked"}])
            self.assertIsNotNone(first.get("crypto_paper"))
            self.assertEqual(first["crypto_paper"]["host"], "paper-api.alpaca.markets")
            self.assertFalse(first["crypto_paper"]["live_execution_supported"])
            self.assertEqual(first["runtime"], "serve_paper")
            self.assertTrue(publisher.refusal is None or isinstance(publisher.refusal, str))

    def test_stock_orders_blocked_by_blocker_or_kill_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            cfg = replace(cfg, crypto_watchlist=("BTC/USD",), crypto_paper_execution_enabled=True,
                          forex_watchlist=("EUR/USD",), paper_reviewed_config_hash="not-the-reviewed-hash")
            venue = PaperFake(cfg)
            worker, _journal = self._worker(
                directory, cfg, venue, publisher=_Publisher(venue.now),
                forex_collector=_ForexWatch(), crypto_collector=_CryptoWatch(),
                session_ledger_factory=lambda: venue.ledger())
            with worker:
                result = worker.cycle()
            self.assertEqual(result["execution_reason"], "exact_paper_configuration_not_reviewed")
            self.assertFalse(result["paper_execution_active"])
            self.assertEqual(result["crypto_paper"]["skipped"], "paper_lock")
            self.assertTrue(result["crypto_paper"]["blockers"])
            self.assertEqual(_mutations(venue), [])

        with tempfile.TemporaryDirectory() as directory:
            cfg = _armed(directory, crypto_watchlist=("BTC/USD",), crypto_paper_execution_enabled=True)
            venue = PaperFake(cfg)
            worker, journal = self._worker(
                directory, cfg, venue, crypto_collector=_CryptoWatch(),
                session_ledger_factory=lambda: venue.ledger())
            with worker:
                journal.set_kill_switch(True, "test halt", request_id="halt-shared")
                result = worker.cycle()
            self.assertEqual(result["execution_reason"], "kill_switch_latched")
            self.assertFalse(result["paper_execution_active"])
            self.assertEqual(result["crypto_paper"]["skipped"], "kill_switch")
            self.assertEqual(_mutations(venue), [])

        with tempfile.TemporaryDirectory() as directory:
            cfg = replace(_armed(directory), paper_enabled=False)
            cfg = replace(cfg, paper_reviewed_config_hash=cfg.review_hash())
            venue = PaperFake(cfg)
            worker, _journal = self._worker(
                directory, cfg, venue, session_ledger_factory=lambda: venue.ledger())
            with worker:
                result = worker.cycle()
            self.assertEqual(result["execution_reason"], "paper_trading_not_enabled")
            self.assertEqual(_mutations(venue), [])

    def test_live_host_is_refused_before_any_account_read(self):
        class Boom:
            def account(self):
                raise AssertionError("live host must not be read")

        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
        reason, active = evaluate_paper_gates(
            cfg, Boom(), journal=None, broker_url=LIVE, broker_url_error=None)
        self.assertEqual(reason, "live_broker_url_refused")
        self.assertFalse(active)
        with self.assertRaises(PaperDisabled) as caught:
            assert_paper_broker_url(LIVE)
        self.assertEqual(str(caught.exception), "live_broker_url_refused")
        self.assertEqual(assert_paper_broker_url("https://paper-api.alpaca.markets"),
                         "https://paper-api.alpaca.markets")

    def test_check_paper_intent_runs_before_a_stock_submit(self):
        calls = []
        real = check_paper_intent

        def wrapped(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            cfg = _armed(directory)
            venue = PaperFake(cfg)
            worker, _journal = self._worker(
                directory, cfg, venue, session_ledger_factory=lambda: venue.ledger())
            with patch("momentum.paper_risk.check_paper_intent", wrapped), worker:
                result = worker.cycle()
            self.assertTrue(calls, result)
            self.assertEqual(calls[0][0].symbol, "DEMO-A")
            posts = [call for call in venue.calls if call["method"] == "POST"]
            self.assertEqual(len(posts), 1, result)
            self.assertEqual(urlsplit(posts[0]["url"]).hostname, "paper-api.alpaca.markets")
            self.assertTrue(posts[0]["payload"]["client_order_id"].startswith("mlab_"))
            self.assertTrue(result["paper_execution_active"], result)
            self.assertNotIn(LIVE, "".join(call["url"] for call in venue.calls))

        calls.clear()

        def reject(*args, **kwargs):
            calls.append(args)
            return False, "exposure_limit"

        with tempfile.TemporaryDirectory() as directory:
            cfg = _armed(directory)
            venue = PaperFake(cfg)
            worker, _journal = self._worker(
                directory, cfg, venue, session_ledger_factory=lambda: venue.ledger())
            with patch("momentum.paper_risk.check_paper_intent", reject), worker:
                result = worker.cycle()
            self.assertTrue(calls)
            self.assertEqual(_mutations(venue), [])
            reasons = [action.get("paper_intent_reason") for action in result["session"]["actions"]]
            self.assertIn("exposure_limit", reasons)


if __name__ == "__main__":
    unittest.main()
