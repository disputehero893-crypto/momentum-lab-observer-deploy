"""Coverage-gap tests for paper_run_hooks and data.SnapshotBuilder.

Paper only: every fake here is in-memory. Nothing places orders or touches a network.
"""
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from momentum import paper_run_hooks
from momentum.alpaca import BrokerError
from momentum.data import SnapshotBuilder, NY
from momentum.paper_run_logging import DisabledPaperRunLogger, PaperRunLoggingError

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)  # 11:00 ET


class RecordingLogger:
    enabled = True

    def __init__(self, *, run_id="run-1", fail_on=None):
        self.run_id = run_id
        self.run_key = "paper-2026-10-28:i1"
        self.calls = []
        self.fail_on = fail_on

    def _rec(self, name, kwargs):
        if self.fail_on == name:
            raise RuntimeError("boom")
        self.calls.append((name, kwargs))
        return {"id": self.run_id}

    def start_run(self, **kw): return self._rec("start_run", kw)
    def log_heartbeat(self, **kw): return self._rec("log_heartbeat", kw)
    def log_blocker(self, reason, **kw): return self._rec("log_blocker", dict(kw, reason=reason))
    def log_signal_evaluated(self, **kw): return self._rec("log_signal_evaluated", kw)
    def log_paper_order(self, **kw): return self._rec("log_paper_order", kw)
    def close_run(self, **kw): return self._rec("close_run", kw)

    def names(self):
        return [c[0] for c in self.calls]


def make_worker(logger=None, *, preset="liquid_large_cap_iex_v2"):
    config = SimpleNamespace(watchlist=("AAPL", "MSFT", "SPY"), review_hash=lambda: "hash-1")
    worker = SimpleNamespace(
        config=config, clock=lambda: NOW, instance_id="i1",
        session=SimpleNamespace(strategy=SimpleNamespace(preset=preset, profile="p")),
    )
    if logger is not None:
        worker.paper_run_logger = logger
    return worker


CYCLE = {
    "cycle_id": "c1", "completed_at": NOW.isoformat(), "cloud_state_published": True,
    "broker": {"verified": True}, "blockers": ["paper_execution_not_enabled"],
    "session": {
        "decisions": [{"symbol": "AAPL", "strategy": {"accepted": False, "reasons": ["no_impulse"]},
                       "risk": {"accepted": None}}],
        "actions": [
            {"kind": "paper_entry_intent", "submitted": True,
             "intent": {"setup_id": "s1", "symbol": "AAPL", "qty": "1"}},
            {"kind": "paper_exit_intent", "submitted": False, "intent": {"setup_id": "s2"}},
            {"kind": "note", "submitted": True},
        ],
    },
}


class AttachAndSessionTests(unittest.TestCase):
    def test_attach_falls_back_to_disabled_logger_on_error(self):
        worker = make_worker()
        with patch.object(paper_run_hooks, "create_paper_run_logger",
                          side_effect=PaperRunLoggingError("live_mode_rejected")), \
                self.assertLogs("momentum.paper_run_hooks", logging.WARNING):
            paper_run_hooks.attach_logger(worker)
        self.assertIsInstance(worker.paper_run_logger, DisabledPaperRunLogger)
        self.assertFalse(worker.paper_run_logger.enabled)

    def test_attach_uses_factory_result(self):
        worker = make_worker()
        sentinel = RecordingLogger()
        with patch.object(paper_run_hooks, "create_paper_run_logger", return_value=sentinel):
            paper_run_hooks.attach_logger(worker)
        self.assertIs(worker.paper_run_logger, sentinel)

    def test_start_session_without_logger_is_noop(self):
        self.assertIsNone(paper_run_hooks.start_session(make_worker()))

    def test_start_session_builds_paper_run_key_and_preset(self):
        logger = RecordingLogger()
        paper_run_hooks.start_session(make_worker(logger))
        name, kw = logger.calls[0]
        self.assertEqual(name, "start_run")
        self.assertEqual(kw["run_key"], "paper-2026-10-28:i1")
        self.assertEqual(kw["strategy_version"], "liquid_large_cap_iex_v2")
        self.assertEqual(kw["watchlist"], ("AAPL", "MSFT", "SPY"))
        self.assertEqual(kw["config_hash"], "hash-1")
        self.assertEqual(kw["summary"]["runtime"], "observation_only")

    def test_start_session_swallows_logger_errors(self):
        logger = RecordingLogger(fail_on="start_run")
        with self.assertLogs("momentum.paper_run_hooks", logging.WARNING):
            self.assertIsNone(paper_run_hooks.start_session(make_worker(logger)))

    def test_close_session_only_when_run_open(self):
        open_logger = RecordingLogger()
        paper_run_hooks.close_session(make_worker(open_logger), status="halted")
        self.assertEqual(open_logger.calls[0][1]["status"], "halted")
        closed = RecordingLogger(run_id=None)
        paper_run_hooks.close_session(make_worker(closed))
        self.assertEqual(closed.calls, [])
        paper_run_hooks.close_session(make_worker())  # no logger: no error

    def test_close_session_swallows_errors(self):
        with self.assertLogs("momentum.paper_run_hooks", logging.WARNING):
            paper_run_hooks.close_session(make_worker(RecordingLogger(fail_on="close_run")))


class RecordCycleTests(unittest.TestCase):
    def test_skips_when_disabled_or_no_run(self):
        disabled = DisabledPaperRunLogger(reason="missing_credentials")
        paper_run_hooks.record_cycle(make_worker(disabled), CYCLE)
        no_run = RecordingLogger(run_id=None)
        paper_run_hooks.record_cycle(make_worker(no_run), CYCLE)
        self.assertEqual(no_run.calls, [])
        paper_run_hooks.record_cycle(make_worker(), CYCLE)

    def test_full_cycle_logs_heartbeat_blocker_signal_and_submitted_paper_intent_only(self):
        logger = RecordingLogger()
        paper_run_hooks.record_cycle(make_worker(logger), CYCLE)
        self.assertEqual(logger.names(),
                         ["log_heartbeat", "log_blocker", "log_signal_evaluated", "log_paper_order"])
        hb = logger.calls[0][1]
        self.assertEqual(hb["status"], "observing")
        self.assertEqual(hb["event_key"], "heartbeat:paper-2026-10-28:i1:c1")
        self.assertTrue(hb["extra"]["broker_verified"])
        self.assertEqual(logger.calls[1][1]["reason"], "paper_trading_disabled")
        sig = logger.calls[2][1]
        self.assertFalse(sig["accepted"])
        self.assertEqual(sig["reasons"], ("no_impulse",))
        order = logger.calls[3][1]
        self.assertEqual(order["side"], "buy")
        self.assertEqual(order["client_order_id"], "s1")
        self.assertEqual(order["status"], "submitted")

    def test_status_error_and_halted(self):
        for result, expected in (({"error_code": "x"}, "error"), ({"halted": True}, "halted")):
            logger = RecordingLogger()
            paper_run_hooks.record_cycle(make_worker(logger), result)
            self.assertEqual(logger.calls[0][1]["status"], expected)
            self.assertIn(":unknown", logger.calls[0][1]["event_key"])

    def test_exit_intent_defaults_to_sell(self):
        logger = RecordingLogger()
        paper_run_hooks.record_cycle(make_worker(logger), {"cycle_id": "c2", "session": {"actions": [
            {"kind": "paper_exit_intent", "submitted": True, "intent": {"setup_id": "s9", "symbol": "SPY"}}]}})
        self.assertEqual(logger.calls[-1][1]["side"], "sell")

    def test_logger_failure_never_raises(self):
        logger = RecordingLogger(fail_on="log_blocker")
        with self.assertLogs("momentum.paper_run_hooks", logging.WARNING):
            paper_run_hooks.record_cycle(make_worker(logger), CYCLE)
        self.assertEqual(logger.names(), ["log_heartbeat"])

    def test_map_blocker(self):
        m = paper_run_hooks._map_blocker
        self.assertEqual(m("watchlist_outside_strategy_universe"), "watchlist_outside_strategy_universe")
        self.assertEqual(m("gated_preset_disabled"), "gated_preset_disabled")
        self.assertEqual(m("paper_trading_disabled_flag"), "paper_trading_disabled")
        self.assertEqual(m("live_mode_requested"), "live_mode_rejected")
        self.assertEqual(m("cloud_worker_not_configured"), "supabase_unavailable")
        self.assertEqual(m("cloud_state_publication_failed"), "supabase_unavailable")
        self.assertEqual(m("symbol_off_universe"), "off_universe_symbol")
        self.assertEqual(m(42), "other")


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


def _sessions(count, end):
    days, d = [], end
    while len(days) < count:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d.isoformat())
    return sorted(days)


class FakeClient:
    def __init__(self, sessions, today_listed=True):
        today = NOW.astimezone(NY).date().isoformat()
        self.records = [{"date": s} for s in sessions] + ([{"date": today}] if today_listed else [])
        self.bar_calls = 0
        self.calendar_calls = 0

    def calendar(self, start, end):
        self.calendar_calls += 1
        return self.records

    def bars(self, symbol, start, end):
        self.bar_calls += 1
        prior = self.records[-2]["date"]
        return [
            {"t": "2026-10-28T13:31:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 100},   # today 09:31 ET
            {"t": "2026-10-28T14:58:00Z", "o": 2, "h": 3, "l": 2, "c": 3, "v": 50},    # today 10:58 ET
            {"t": "2026-10-28T15:00:00Z", "o": 3, "h": 3, "l": 3, "c": 3, "v": 999},   # incomplete: skipped
            {"t": "2026-10-28T07:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 7},     # 03:00 ET: skipped
            {"t": prior + "T14:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 40},
            {"t": prior + "T19:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 400},     # after cutoff clock
        ]

    def snapshots(self, symbols):
        return {"AAPL": {"latestQuote": {"bp": 1.5, "ap": 1.6, "t": "q"},
                         "latestTrade": {"p": 1.55, "t": "t"}, "prevDailyBar": {"c": 1.4}}}


class SnapshotBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.meta = os.path.join(self.tmp.name, "meta.json")
        with open(self.meta, "w", encoding="utf-8") as fh:
            json.dump({"AAPL": {"float_shares": 15_000_000}}, fh)
        self.config = SimpleNamespace(watchlist=("AAPL", "MSFT"), data_feed="iex", metadata_path=self.meta)
        self.patch = patch("momentum.data.datetime", FrozenDatetime)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def sessions(self, n=30):
        return _sessions(n, NOW.astimezone(NY).date())

    def test_builds_paper_snapshot_without_inventing_missing_data(self):
        client = FakeClient(self.sessions())
        out = SnapshotBuilder(client, self.config).build_watchlist()
        aapl, msft = out
        self.assertEqual(aapl["provenance"]["mode"], "paper")
        self.assertFalse(aapl["provenance"]["synthetic"])
        self.assertEqual(aapl["feed"], "iex")
        self.assertTrue(aapl["session"]["is_trading_day"])
        self.assertEqual(len(aapl["bars"]), 2)
        self.assertEqual(aapl["rvol"]["current_volume"], "150")
        self.assertEqual(len(aapl["rvol"]["history"]), 1)
        self.assertEqual(aapl["rvol"]["history"][0]["volume"], "40")
        self.assertEqual(aapl["facts"]["previous_close"], "1.4")
        self.assertEqual(aapl["quote"]["bid"], "1.5")
        # MSFT has no snapshot and no metadata: stays missing, never invented.
        self.assertIsNone(msft["quote"]["bid"])
        self.assertIsNone(msft["last_trade"]["price"])
        self.assertEqual(msft["facts"], {})

    def test_history_and_calendar_are_cached_within_a_minute(self):
        client = FakeClient(self.sessions())
        builder = SnapshotBuilder(client, self.config)
        builder.build_watchlist()
        builder.build_watchlist()
        self.assertEqual(client.calendar_calls, 1)
        self.assertEqual(client.bar_calls, 2)

    def test_short_calendar_fails_closed(self):
        with self.assertRaises(BrokerError):
            SnapshotBuilder(FakeClient(self.sessions(29)), self.config).build_watchlist()

    def test_non_trading_day_flag(self):
        out = SnapshotBuilder(FakeClient(self.sessions(), today_listed=False), self.config).build_watchlist()
        self.assertFalse(out[0]["session"]["is_trading_day"])

    def test_unreadable_or_bad_metadata_fails_closed(self):
        for content in ("{not json", "[1, 2]"):
            with open(self.meta, "w", encoding="utf-8") as fh:
                fh.write(content)
            with self.assertRaises(BrokerError):
                SnapshotBuilder(FakeClient(self.sessions()), self.config).build_watchlist()
        self.config.metadata_path = os.path.join(self.tmp.name, "missing.json")
        with self.assertRaises(BrokerError):
            SnapshotBuilder(FakeClient(self.sessions()), self.config).build_watchlist()


if __name__ == "__main__":
    unittest.main()
