"""Live paper feed: crypto signal/entry/exit/stop/skip rows for paper_events.

Offline only: fake quotes, fake order transport, fake Supabase logger.
"""
from types import SimpleNamespace
import tempfile
import unittest
import uuid

from momentum import paper_run_hooks
from momentum.crypto_paper_loop import CryptoPaperLoop
from momentum.journal import Journal
from momentum.paper_run_logging import (
    DisabledPaperRunLogger, PaperRunLogger, PaperRunLoggingError, TRADE_EVENT_KINDS)
from test_crypto_paper_autopilot import NOW, Transport, ready, rising_watches, watch


class FakeLogger:
    enabled = True

    def __init__(self, fail_on=None):
        self.run_id = "run-1"
        self.run_key = "paper-2026-11-26:test"
        self.rows = []
        self.fail_on = fail_on

    def _add(self, kind, payload, key):
        if self.fail_on == kind:
            raise PaperRunLoggingError("unavailable")
        self.rows.append((kind, payload, key))
        return True

    def log_trade_event(self, kind, *, payload, event_key, occurred_at=None):
        return self._add(kind, payload, event_key)

    def log_heartbeat(self, **kw):
        return self._add("heartbeat", kw.get("extra"), kw.get("event_key"))

    def log_blocker(self, reason, **kw):
        return self._add("blocker", reason, kw.get("event_key"))

    def log_signal_evaluated(self, **kw):
        return self._add("signal_evaluated", kw, kw.get("event_key"))

    def log_paper_order(self, **kw):
        return self._add("paper_order", kw, kw.get("event_key"))

    def kinds(self):
        return [row[0] for row in self.rows]


def cycle_result(crypto, cycle_id="c1", blockers=()):
    return {"cycle_id": cycle_id, "completed_at": NOW.isoformat(), "blockers": list(blockers),
            "broker": {"verified": True}, "crypto_paper": crypto}


class LoopDetailTests(unittest.TestCase):
    def test_buy_and_stop_decisions_carry_price_qty_and_window(self):
        transport = Transport()
        with tempfile.TemporaryDirectory() as directory:
            cfg = ready(directory)
            loop = CryptoPaperLoop(cfg, journal=Journal(cfg.db_path), clock=lambda: NOW, transport=transport)
            first = loop.cycle(rising_watches()[0])
            hold = first["decisions"][-1]
            self.assertEqual(hold["action"], "hold")
            self.assertEqual(hold["reason"], "momentum_history_short")
            self.assertIn("mid", hold)
            for item in rising_watches()[1:]:
                summary = loop.cycle(item)
            buy = summary["decisions"][-1]
            self.assertEqual(buy["action"], "buy")
            self.assertEqual(buy["side"], "buy")
            self.assertEqual(buy["reason"], "momentum_rise")
            self.assertTrue(buy["client_order_id"].startswith("mlab_"))
            self.assertEqual(buy["price"], "100.82")
            self.assertGreater(float(buy["qty"]), 0)
            self.assertIsNotNone(buy["stop_price"])
            self.assertGreaterEqual(len(buy["window"]), 4)
            stop = loop.cycle(watch(NOW, "99.00", "99.02", age_ms=150))
            sell = stop["decisions"][-1]
            self.assertEqual((sell["action"], sell["reason"], sell["side"]), ("sell", "stop_hit", "sell"))
            self.assertEqual(sell["entry_price"], "100.82")
            self.assertEqual(sell["price"], "99")
            self.assertTrue(sell["pnl"].startswith("-"))
        # Detail is display-only: still exactly one buy and one sell.
        self.assertEqual([call["payload"]["side"] for call in transport.calls], ["buy", "sell"])


class HookTests(unittest.TestCase):
    def worker(self, logger):
        return SimpleNamespace(paper_run_logger=logger)

    def test_entry_then_stop_rows(self):
        logger = FakeLogger()
        worker = self.worker(logger)
        window = [{"t": "2026-11-26T12:00:00+00:00", "mid": "100"}, {"t": "2026-11-26T12:00:20+00:00", "mid": "101"}]
        buy = {"pair": "BTC/USD", "action": "buy", "reason": "momentum_rise", "rule": "crypto_spot_momentum_v1",
               "side": "buy", "qty": "0.2", "price": "101", "stop_price": "99.99",
               "client_order_id": "mlab_abc", "window": window, "bid": "100.9", "ask": "101", "mid": "100.95"}
        paper_run_hooks.record_cycle(worker, cycle_result({"enabled": True, "skipped": None, "decisions": [buy]}))
        self.assertEqual(logger.kinds()[:2], ["signal", "entry"])
        entry = logger.rows[1][1]
        self.assertEqual((entry["symbol"], entry["side"], entry["qty"], entry["price"]), ("BTC/USD", "buy", "0.2", "101"))
        self.assertEqual(entry["pattern"], "momentum_rise")
        self.assertEqual(entry["window_start"], window[0]["t"])
        self.assertEqual(entry["window_end"], window[-1]["t"])
        self.assertEqual(logger.rows[1][2], "entry:" + logger.run_key + ":mlab_abc")
        sell = dict(buy, action="sell", side="sell", reason="stop_hit", price="98", entry_price="101",
                    pnl="-0.6", client_order_id="mlab_def")
        paper_run_hooks.record_cycle(worker, cycle_result({"enabled": True, "skipped": None, "decisions": [sell]}, "c2"))
        trade = [row for row in logger.rows if row[0] in TRADE_EVENT_KINDS]
        self.assertEqual([row[0] for row in trade], ["signal", "entry", "signal", "stop"])
        self.assertEqual(trade[-1][1]["pnl"], "-0.6")

    def test_skips_and_blockers_are_written_only_on_change(self):
        logger = FakeLogger()
        worker = self.worker(logger)
        hold = {"pair": "ETH/USD", "action": "hold", "reason": "momentum_not_rising"}
        for index in range(5):
            paper_run_hooks.record_cycle(worker, cycle_result(
                {"enabled": True, "skipped": None, "decisions": [hold]}, "c%d" % index,
                blockers=("market_closed",)))
        self.assertEqual(logger.kinds().count("skip"), 1)
        self.assertEqual(logger.kinds().count("blocker"), 1)
        self.assertEqual(logger.kinds().count("heartbeat"), 5)
        paper_run_hooks.record_cycle(worker, cycle_result(
            {"enabled": True, "skipped": None, "decisions": [dict(hold, reason="spread_too_wide")]}, "c9",
            blockers=("market_closed", "other_block")))
        self.assertEqual(logger.kinds().count("skip"), 2)
        self.assertEqual(logger.kinds().count("blocker"), 3)
        for index in range(3):
            paper_run_hooks.record_cycle(worker, cycle_result(
                {"enabled": True, "skipped": "kill_switch", "decisions": []}, "k%d" % index))
        self.assertEqual(logger.kinds().count("skip"), 3)
        # A new run resets the dedupe memory.
        logger.run_key = "paper-2026-11-27:test"
        paper_run_hooks.record_cycle(worker, cycle_result(
            {"enabled": True, "skipped": "kill_switch", "decisions": []}, "k9"))
        self.assertEqual(logger.kinds().count("skip"), 4)

    def test_refused_order_writes_signal_and_skip(self):
        logger = FakeLogger()
        refused = {"pair": "SOL/USD", "action": "hold", "reason": "kill_switch_enabled",
                   "intent": "buy", "signal_reason": "momentum_rise"}
        paper_run_hooks.record_cycle(self.worker(logger), cycle_result(
            {"enabled": True, "skipped": None, "decisions": [refused]}))
        self.assertEqual(logger.kinds()[:2], ["signal", "skip"])
        self.assertTrue(logger.rows[1][1]["order_refused"])
        self.assertEqual(logger.rows[1][1]["pattern"], "momentum_rise")

    def test_write_failure_never_raises_and_stops_cycle_writes(self):
        logger = FakeLogger(fail_on="entry")
        buy = {"pair": "BTC/USD", "action": "buy", "reason": "momentum_rise", "client_order_id": "mlab_x"}
        with self.assertLogs("momentum.paper_run_hooks", "WARNING"):
            paper_run_hooks.record_cycle(self.worker(logger), cycle_result(
                {"enabled": True, "skipped": None, "decisions": [buy]}))
        self.assertEqual(logger.kinds(), ["signal"])
        broken = SimpleNamespace(paper_run_logger=SimpleNamespace(enabled=True, run_id="r", run_key="k"))
        with self.assertLogs("momentum.paper_run_hooks", "WARNING"):
            paper_run_hooks.record_cycle(broken, cycle_result(
                {"enabled": True, "skipped": None, "decisions": [buy]}))

    def test_disabled_logger_and_no_crypto_are_noops(self):
        paper_run_hooks.record_cycle(self.worker(DisabledPaperRunLogger()), cycle_result({"decisions": []}))
        self.assertFalse(DisabledPaperRunLogger().log_trade_event(kind="entry", payload={}, event_key="k"))
        logger = FakeLogger()
        paper_run_hooks.record_cycle(self.worker(logger), {"cycle_id": "c", "completed_at": NOW.isoformat()})
        self.assertEqual(logger.kinds(), ["heartbeat"])


class LoggerTests(unittest.TestCase):
    def test_trade_event_rows_are_paper_and_bounded(self):
        calls = []

        def transport(method, url, headers, payload):
            calls.append((method, url, payload))
            return [payload] if "paper_runs" in url else []

        owner = str(uuid.uuid4())
        cfg = SimpleNamespace(mode="paper", supabase_url="https://abcdefgh.supabase.co",
                              supabase_service_role_key="sb_secret_FAKE_NOT_REAL", owner_user_id=owner)
        logger = PaperRunLogger(cfg, transport=transport, clock=lambda: NOW)
        logger.start_run(run_key="paper-test", strategy_version="v")
        logger.log_trade_event("entry", payload={"symbol": "BTC/USD", "mode": "live"},
                               event_key="entry:" + "x" * 300, occurred_at=NOW)
        method, url, row = calls[-1]
        self.assertEqual((method, row["kind"]), ("POST", "entry"))
        self.assertEqual(row["payload"]["mode"], "paper")
        self.assertFalse(row["payload"]["placed_by_logger"])
        self.assertEqual(len(row["event_key"]), 200)
        with self.assertRaises(ValueError):
            logger.log_trade_event("buy_now", payload={}, event_key="k")


if __name__ == "__main__":
    unittest.main()
