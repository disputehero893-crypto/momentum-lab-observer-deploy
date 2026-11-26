"""Offline fail-closed coverage for risk, demo, activities and the container entrypoint.

Every account, intent, quote, activity and timestamp here is invented. The
broker transport is an in-memory fake that only answers GETs, journals live in
temporary SQLite files, and urllib's opener is blocked so nothing can leave the
process. No order is ever submitted. Paper only.

Known unreachable line (noted, not hacked): activities._number lines 145-146.
The _DECIMAL regex only admits plain decimal literals of at most 80 characters,
and every such literal is a valid Decimal, so InvalidOperation cannot be raised.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

from momentum.activities import (ActivityImportError, fetch_activities, normalize_activity,
                                 timestamp_parts)
from momentum.config import Config
from momentum.demo import SyntheticAccount, run_demo, synthetic_snapshot
from momentum.models import InputError
from momentum.risk import RiskManager
from test_risk import NOW, valid_account, valid_intent, valid_quote
from test_worker_activities import ACCOUNT, END, START, Pages, fill


def block_network(test):
    blocker = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError(
        "Network access is forbidden in offline coverage tests"))
    blocker.start()
    test.addCleanup(blocker.stop)


class RiskCoverageTests(unittest.TestCase):
    def setUp(self):
        block_network(self)
        self.manager = RiskManager()

    def assess(self, intent=None, account=None, quote=None, now=NOW):
        return self.manager.assess(valid_intent() if intent is None else intent,
                                   valid_account() if account is None else account,
                                   valid_quote() if quote is None else quote, now,
                                   connected=True, reconciled=True)

    def assert_reject(self, reason, **kwargs):
        result = self.assess(**kwargs)
        self.assertFalse(result.accepted, result.to_dict())
        self.assertEqual(result.reasons, (reason,))
        self.assertIsNone(result.intent)
        return result

    def test_restore_state_rejects_non_mapping_and_unknown_lock_reason(self):
        for bad in (None, [], "state"):
            with self.assertRaises(InputError) as caught:
                self.manager.restore_state(bad)
            self.assertEqual(caught.exception.code, "invalid_risk_state")
        state = self.manager.to_state()
        state["daily_locks"] = {"2026-10-16": "manual_override"}
        with self.assertRaises(InputError) as caught:
            self.manager.restore_state(state)
        self.assertEqual(caught.exception.code, "invalid_risk_state")
        # A rejected restore leaves the manager untouched.
        self.assertFalse(self.manager.locked)
        self.assertEqual(self.manager.to_state()["daily_locks"], {})

    def test_exposure_carried_into_new_session_requires_reconciliation(self):
        first = self.assess(intent=None, account=valid_account())
        self.assertTrue(first.accepted, first.to_dict())
        next_now = "2026-10-19T13:35:01Z"
        account = valid_account()
        account.update(session_date="2026-10-19", as_of=next_now, position_qty=5)
        intent = valid_intent()
        intent["observed_at"] = next_now
        quote = valid_quote()
        quote.update(timestamp=next_now, received_at=next_now)
        result = self.assert_reject("overnight_exposure_requires_reconciliation",
                                    intent=intent, account=account, quote=quote, now=next_now)
        self.assertEqual(result.metrics["position_qty"], 5)
        pending = dict(account, position_qty=0, open_order_qty=3)
        self.manager = RiskManager()
        self.assess()
        self.assert_reject("overnight_exposure_requires_reconciliation",
                           intent=intent, account=pending, quote=quote, now=next_now)

    def test_intent_side_symbol_quote_symbol_and_feed_rejects(self):
        cases = (("only_long_entries_supported", {"side": "sell"}),
                 ("only_long_entries_supported", {"side": None}),
                 ("missing_or_invalid_symbol", {"symbol": ""}),
                 ("missing_or_invalid_symbol", {"symbol": ["DEMO"]}),
                 ("quote_symbol_mismatch", {"symbol": "OTHER"}),
                 ("mixed_feed", {"feed": "sip"}))
        for reason, change in cases:
            with self.subTest(reason=reason, change=change):
                self.manager = RiskManager()
                intent = valid_intent()
                intent.update(change)
                self.assert_reject(reason, intent=intent)

    def test_quote_without_symbol_defaults_to_intent_symbol(self):
        quote = valid_quote()
        del quote["symbol"]
        result = self.assess(quote=quote)
        self.assertTrue(result.accepted, result.to_dict())
        self.assertTrue(result.intent["paper_only"])
        self.assertFalse(result.intent["live_enabled"])

    def test_misbehaving_mapping_collapses_to_invalid_risk_input(self):
        class Raising(dict):
            def __init__(self, data, field, error):
                super().__init__(data)
                self.field, self.error = field, error

            def get(self, key, default=None):
                if key == self.field:
                    raise self.error
                return super().get(key, default)

        for error in (KeyError("side"), TypeError("bad"), ValueError("bad"), ArithmeticError("bad")):
            with self.subTest(error=type(error).__name__):
                self.manager = RiskManager()
                result = self.assess(intent=Raising(valid_intent(), "side", error))
                self.assertFalse(result.accepted)
                self.assertEqual(result.reasons, ("invalid_risk_input",))
                self.assertEqual(result.metrics["error_type"], type(error).__name__)
                self.assertNotIn("bad", str(result.to_dict()))
                self.assertEqual(len(result.metrics["risk_config_id"]), 64)


class DemoAccountCoverageTests(unittest.TestCase):
    def setUp(self):
        block_network(self)
        self.snapshot = synthetic_snapshot("DEMO-A", "2026-10-16T13:35:05Z")
        self.account = SyntheticAccount("10000")

    def intent(self, **changes):
        intent = {"symbol": "DEMO-A", "entry_limit": "9.82", "stop_price": "9.70",
                  "target_price": "10.00", "setup_id": "synthetic-demo-cov"}
        intent.update(changes)
        return intent

    def open_position(self):
        self.account.pending = {"intent": self.intent(), "remaining": 10}
        self.assertIsNotNone(self.account.fill_entry(self.snapshot, "all"))
        self.assertIsNone(self.account.pending)

    def test_mark_refuses_quote_for_a_different_symbol(self):
        self.open_position()
        other = synthetic_snapshot("DEMO-B", "2026-10-16T13:35:06Z")
        with self.assertRaisesRegex(ValueError, "matching quote"):
            self.account.mark(other)

    def test_fill_entry_without_pending_or_mismatched_quote_is_noop(self):
        self.assertIsNone(self.account.fill_entry(self.snapshot, "all"))
        self.account.pending = {"intent": self.intent(), "remaining": 10}
        other = synthetic_snapshot("DEMO-B", "2026-10-16T13:35:05Z")
        self.assertIsNone(self.account.fill_entry(other, "all"))
        above = deepcopy(self.snapshot)
        above["quote"]["ask"] = "9.83"
        self.assertIsNone(self.account.fill_entry(above, 5))
        self.assertEqual(self.account.pending["remaining"], 10)
        self.assertIsNone(self.account.position)
        self.assertEqual(self.account.fills, [])
        self.assertEqual(self.account.cash, Decimal("10000"))

    def test_fill_exit_requires_flat_pending_position_and_marketable_bid(self):
        exit_intent = self.intent(entry_limit="9.75", reason="target")
        self.assertEqual(self.account.fill_exit(exit_intent, self.snapshot), (None, None))
        self.account.pending = {"intent": self.intent(), "remaining": 10}
        self.account.fill_entry(self.snapshot, 4)
        self.assertEqual(self.account.fill_exit(exit_intent, self.snapshot), (None, None))
        self.account.fill_entry(self.snapshot, "all")
        low = deepcopy(self.snapshot)
        low["quote"]["bid"] = "9.74"
        self.assertEqual(self.account.fill_exit(exit_intent, low), (None, None))
        self.assertEqual(self.account.position["qty"], 10)
        self.assertEqual(self.account.trades, [])
        self.assertTrue(all(row["broker_confirmed"] is False for row in self.account.fills))

    def test_run_demo_rejects_bad_run_id_before_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(db_path=directory + "/journal.sqlite3", export_dir=directory + "/exports")
            for run_id in ("", "bad id", "x" * 81, "../escape", "demo;drop"):
                with self.subTest(run_id=run_id), self.assertRaisesRegex(ValueError, "run_id"):
                    run_demo(cfg, run_id=run_id)
            self.assertFalse(Path(cfg.db_path).exists())
            self.assertFalse(Path(cfg.export_dir).exists())


class ActivitiesCoverageTests(unittest.TestCase):
    def setUp(self):
        block_network(self)

    def test_naive_datetime_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            timestamp_parts(datetime(2026, 10, 16, 14, 30))
        self.assertEqual(timestamp_parts(datetime.fromisoformat("2026-10-16T09:30:00-05:00"))[0],
                         "2026-10-16T14:30:00Z")

    def test_invalid_activity_type_rejected(self):
        for value in (None, 7, "", "fill", "FILL TYPE", "F" * 33):
            with self.subTest(value=value):
                with self.assertRaises(ActivityImportError) as caught:
                    normalize_activity(fill(activity_type=value))
                self.assertEqual(str(caught.exception), "invalid_activity_type")

    def test_completion_clock_regression_fails_closed_without_partial_result(self):
        venue = Pages([[]])
        original = venue.request

        def regress(method, url, headers, payload):
            response = original(method, url, headers, payload)
            venue.now = venue.now - timedelta(seconds=1)
            return response

        venue.client._transport = regress
        with self.assertRaises(ActivityImportError) as caught:
            fetch_activities(venue.client, account_id=ACCOUNT, after=START, until=END)
        self.assertEqual(str(caught.exception), "activities_observation_clock_regressed")
        self.assertEqual(len(venue.calls), 1)
        self.assertTrue(all(call["method"] == "GET" and call["payload"] is None for call in venue.calls))


class ContainerEntrypointMainGuardTests(unittest.TestCase):
    def setUp(self):
        block_network(self)

    def test_module_main_guard_exits_with_cli_status_without_privilege_changes(self):
        calls = []

        def fake_cli(argv):
            calls.append(list(argv))
            return 0

        with patch("os.geteuid", return_value=1000), patch("os.setuid") as setuid, \
                patch("os.setgid") as setgid, patch("os.chown") as chown, \
                patch("momentum.__main__.main", fake_cli), patch.object(sys, "argv", ["entrypoint"]), \
                patch.dict(sys.modules):
            sys.modules.pop("momentum.container_entrypoint", None)
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("momentum.container_entrypoint", run_name="__main__")
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(calls, [["serve"]])
        setuid.assert_not_called()
        setgid.assert_not_called()
        chown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
