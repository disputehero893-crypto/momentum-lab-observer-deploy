"""Offline coverage for data.SnapshotBuilder, PaperSession guard rails, and the container entrypoint.

Paper only: every provider here is in-memory. Nothing places orders, drops privileges, or touches a network.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from momentum import container_entrypoint
from momentum.alpaca import BrokerError, PaperDisabled
from momentum.data import SnapshotBuilder, NY
from momentum.journal import Journal
from momentum.session import PaperSession, whole
from worker_fakes import armed_config, FakeVenue

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)  # 11:00 ET


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



class WholeShareTests(unittest.TestCase):
    def test_accepts_whole_nonnegative_quantities(self):
        self.assertEqual(whole("40"), 40)
        self.assertEqual(whole(0), 0)
        self.assertEqual(whole("10.0"), 10)

    def test_rejects_fractional_negative_and_non_finite(self):
        for bad in ("1.5", "-1", "NaN", "Infinity"):
            with self.assertRaises(ValueError, msg=bad):
                whole(bad)


class SessionGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = armed_config(self.tmp.name)
        self.venue = FakeVenue(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, cfg=None, **kwargs):
        return PaperSession(cfg or self.cfg, self.venue.client, clock=lambda: self.venue.now, **kwargs)

    def no_posts(self):
        self.assertFalse(any(c["method"] == "POST" for c in self.venue.calls))

    def test_cycle_requires_lease_paper_mode_and_boolean_execute(self):
        unowned = self.session()
        with self.assertRaises(PaperDisabled):
            unowned.cycle(snapshots=[], execute=False)
        with self.session(cfg=replace(self.cfg, mode="demo")) as demo:
            with self.assertRaises(PaperDisabled):
                demo.cycle(snapshots=[], execute=False)
        with self.session() as session:
            with self.assertRaises(ValueError):
                session.cycle(snapshots=[], execute="yes")
        self.no_posts()

    def test_enter_releases_lease_when_state_restore_fails(self):
        session = self.session()
        with patch.object(session.journal, "latest", side_effect=RuntimeError("corrupt")):
            with self.assertRaises(RuntimeError):
                session.__enter__()
        self.assertFalse(session._owner)
        with self.session() as again:  # Lease was released, so a fresh owner can acquire it.
            self.assertTrue(again._owner)
        self.assertFalse(again._owner)

    def test_account_identity_status_and_block_fail_closed(self):
        original = self.venue.client.account

        def bad_account():
            account = original()
            account.update(id="someone-else", status="ONBOARDING", trading_blocked=True, account_blocked=True)
            return account

        self.venue.client.account = bad_account
        with self.session(expected_account_id="paper-acct-1") as session:
            result = session.cycle(snapshots=[self.venue.snapshot()], session_ledger=self.venue.ledger(), execute=True)
        for error in ("paper_account_identity_mismatch", "paper_account_not_verified_active", "paper_account_blocked"):
            self.assertIn(error, result["errors"])
        self.assertFalse(result["reconciled"])
        self.assertFalse(result["positions_confirmed_flat"])
        self.assertFalse(result["live_execution_supported"])
        self.no_posts()

    def test_ledger_account_and_environment_mismatch_block_entry(self):
        ledger = dict(self.venue.ledger(), account_id="other-account", environment="live")
        with self.session() as session:
            result = session.cycle(snapshots=[self.venue.snapshot()], session_ledger=ledger, execute=True)
        self.assertIn("session_ledger_account_mismatch", result["errors"])
        self.assertIn("session_ledger_environment_mismatch", result["errors"])
        self.assertEqual(result["mode"], "paper")
        self.no_posts()

    def test_malformed_ledger_is_treated_as_missing(self):
        ledger = dict(self.venue.ledger(), fees="not-a-number")
        with self.session() as session:
            result = session.cycle(snapshots=[self.venue.snapshot()], session_ledger=ledger, execute=True)
        self.assertIn("complete_current_session_pnl_and_fee_ledger_missing", result["errors"])
        self.no_posts()

    def test_duplicate_and_off_watchlist_snapshots_block_entry(self):
        snaps = [self.venue.snapshot(), self.venue.snapshot(), self.venue.snapshot(symbol="AAPL")]
        with self.session() as session:
            result = session.cycle(snapshots=snaps, session_ledger=self.venue.ledger(), execute=True)
        self.assertIn("duplicate_snapshot_symbol", result["errors"])
        self.assertIn("snapshot_outside_reviewed_watchlist", result["errors"])
        self.no_posts()

    def test_short_or_unowned_order_evidence_fails_closed(self):
        self.venue.client.positions = lambda: [{"symbol": "DEMO-A", "side": "short", "qty": "5",
                                                "avg_entry_price": "9.80", "unrealized_pl": "0"}]
        self.venue.client.open_orders = lambda: [{"client_order_id": "not-ours", "symbol": "DEMO-B"}]
        with self.session() as session:
            result = session.cycle(snapshots=[self.venue.snapshot()], session_ledger=self.venue.ledger(), execute=True)
        self.assertIn("unowned_or_short_position", result["errors"])
        self.assertIn("unowned_open_order", result["errors"])
        self.no_posts()

    def test_recovery_cap_reports_error_without_trading(self):
        journal = Journal(self.cfg.db_path)
        with self.session(journal=journal) as session:
            with patch.object(journal, "events", return_value=[{}] * 1001):
                result = session.cycle(snapshots=[self.venue.snapshot()], session_ledger=self.venue.ledger(),
                                       execute=True)
        self.assertIn("BrokerError", result["errors"])
        self.assertFalse(result["connected"])
        self.assertFalse(result["positions_confirmed_flat"])
        self.no_posts()


class ContainerEntrypointTests(unittest.TestCase):
    def test_non_root_skips_privilege_drop_and_defaults_to_serve(self):
        with patch("os.geteuid", return_value=10001), patch("os.setuid") as setuid, \
                patch("momentum.__main__.main", return_value=0) as cli, patch("sys.argv", ["entry"]):
            self.assertEqual(container_entrypoint.main(), 0)
        cli.assert_called_once_with(["serve"])
        setuid.assert_not_called()

    def test_root_prepares_data_dirs_then_drops_privileges(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"JOURNAL_PATH": os.path.join(directory, "data", "j.sqlite3"),
                   "EXPORT_DIR": os.path.join(directory, "exports")}
            order = []
            with patch.dict(os.environ, env), patch("os.geteuid", return_value=0), \
                    patch("os.chown", side_effect=lambda p, u, g: order.append(("chown", str(p), u, g))), \
                    patch("os.setgroups", side_effect=lambda g: order.append(("setgroups", g))), \
                    patch("os.setgid", side_effect=lambda g: order.append(("setgid", g))), \
                    patch("os.setuid", side_effect=lambda u: order.append(("setuid", u))), \
                    patch("momentum.__main__.main", side_effect=lambda a: order.append(("cli", a)) or 0), \
                    patch("sys.argv", ["entry", "status"]):
                self.assertEqual(container_entrypoint.main(), 0)
            self.assertTrue(os.path.isdir(os.path.join(directory, "data")))
            self.assertTrue(os.path.isdir(os.path.join(directory, "exports")))
            chowns = sorted(o[1] for o in order if o[0] == "chown")
            self.assertEqual(chowns, sorted([os.path.join(directory, "data"), os.path.join(directory, "exports")]))
            self.assertTrue(all(o[2:] == (10001, 10001) for o in order if o[0] == "chown"))
            kinds = [o[0] for o in order]
            self.assertEqual(kinds[-4:], ["setgroups", "setgid", "setuid", "cli"])
            self.assertEqual(order[-1], ("cli", ["status"]))


if __name__ == "__main__":
    unittest.main()
