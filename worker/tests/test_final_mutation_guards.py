"""Final mutation boundary regressions using invented data/transports only."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import test_customer_settings as settings


class FinalMutationGuardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = settings.CustomerSettingsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def check_final_settings_interruption(self, interrupt):
        f = self.fixture
        with f.runner() as runner:
            f.activate(runner)
            reads = []
            def load(owner):
                reads.append(owner)
                # Cycle start, session guard, adapter guard, final adapter guard.
                if len(reads) == 4:
                    interrupt(f, runner)
                return deepcopy(f.row)
            runner.settings_loader = load
            result = runner.cycle()
            self.assertEqual(len(reads), 4)
            self.assertEqual(f.posts(), [])
            self.assertFalse(result["entries_enabled"])
            self.assertEqual(len(runner.journal.events(event_type="paper_order_not_sent")), 1)

    def test_quote_and_intent_expiring_during_final_settings_read_cannot_submit(self):
        self.check_final_settings_interruption(lambda f, r: f.venue.advance(2.5))

    def test_account_and_ledger_expiring_during_final_settings_read_cannot_submit(self):
        self.check_final_settings_interruption(lambda f, r: f.venue.advance(6))

    def test_clock_regression_during_final_settings_read_cannot_submit(self):
        self.check_final_settings_interruption(lambda f, r: f.venue.advance(-1))

    def test_shutdown_during_final_settings_read_cannot_submit(self):
        self.check_final_settings_interruption(lambda f, r: r.stop_event.set())

    def test_ownership_loss_during_final_settings_read_cannot_submit(self):
        self.check_final_settings_interruption(lambda f, r: setattr(r.session, "_owner", False))

    def check_cancel_lookup_interruption(self, interrupt):
        f = self.fixture
        with f.runner() as runner:
            f.activate(runner)
            runner.cycle()
            f.command(runner, "pause", "pause-before-cancel")
            lookups = []
            def before_request(method, url):
                if "/v2/orders:by_client_order_id" in url:
                    lookups.append(url)
                    if len(lookups) == 2:
                        interrupt(runner)
            f.before_request = before_request
            result = runner.cycle()
            self.assertEqual(len(lookups), 2)
            self.assertEqual([c for c in f.venue.calls if c["method"] == "DELETE"], [])
            self.assertFalse(result["entries_enabled"])
            self.assertEqual(runner.journal.events(event_type="paper_cancel_requested"), [])

    def test_shutdown_during_cancel_lookup_cannot_delete(self):
        self.check_cancel_lookup_interruption(lambda r: r.stop_event.set())

    def test_ownership_loss_during_cancel_lookup_cannot_delete(self):
        self.check_cancel_lookup_interruption(lambda r: setattr(r.session, "_owner", False))

    def test_shutdown_after_cancel_journal_write_records_not_sent(self):
        f = self.fixture
        with f.runner() as runner:
            f.activate(runner)
            runner.cycle()
            f.command(runner, "pause", "pause-before-cancel")
            original = runner.journal.append
            def append(run_id, event_type, payload, **kwargs):
                result = original(run_id, event_type, payload, **kwargs)
                if event_type == "paper_cancel_requested":
                    runner.stop_event.set()
                return result
            with patch.object(runner.journal, "append", side_effect=append):
                result = runner.cycle()
            self.assertEqual([c for c in f.venue.calls if c["method"] == "DELETE"], [])
            self.assertFalse(result["entries_enabled"])
            self.assertEqual(len(runner.journal.events(event_type="paper_cancel_not_sent")), 1)
            self.assertEqual(runner.journal.events(event_type="paper_cancel_accepted"), [])

    def test_emergency_halt_still_allows_protective_cancel(self):
        f = self.fixture
        with f.runner() as runner:
            f.activate(runner)
            runner.cycle()
            f.command(runner, "emergency_stop", "emergency-halt")
            result = runner.cycle()
            self.assertTrue([c for c in f.venue.calls if c["method"] == "DELETE"])
            self.assertFalse(result["entries_enabled"])
            self.assertFalse(runner.stop_event.is_set())
