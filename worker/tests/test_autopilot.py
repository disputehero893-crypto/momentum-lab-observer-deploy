"""Local fake-venue verification. No network or brokerage credentials."""
from dataclasses import replace
from decimal import Decimal
import tempfile
import unittest
from types import SimpleNamespace

from momentum.autopilot import AutopilotError, PaperAutopilot
from momentum.alpaca import PaperClient
from momentum.lease import AlreadyRunning
from worker_fakes import armed_config, FakeVenue

OWNER = "11111111-1111-4111-8111-111111111111"
ACCOUNT = "22222222-2222-4222-8222-222222222222"
FOREIGN = "33333333-3333-4333-8333-333333333333"


class AutopilotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cfg = replace(armed_config(self.directory.name), owner_user_id=OWNER)
        self.venue = FakeVenue(self.cfg)
        self.return_account = ACCOUNT
        self.fail = False
        self.ledger_missing = False
        self.bid, self.ask = "9.81", "9.82"
        def transport(method, url, headers, payload):
            if self.fail:
                raise RuntimeError("sensitive transport error must not leak")
            result = self.venue.request(method, url, headers, payload)
            if url.endswith("/v2/account"):
                result["id"] = self.return_account
            return result
        self.client = PaperClient(self.cfg, transport=transport, clock=lambda: self.venue.now)
        self.builder = SimpleNamespace(build_watchlist=lambda: [self.venue.snapshot(bid=self.bid, ask=self.ask)])
        self.accounting = SimpleNamespace(collect=lambda account_id:
            {**self.venue.ledger(), "complete": not self.ledger_missing,
             "account_id": account_id, "environment": "paper"})

    def runner(self, cfg=None, account=ACCOUNT):
        return PaperAutopilot(cfg or self.cfg, account_id=account, provider=self.client,
            builder=self.builder, accounting=self.accounting, clock=lambda: self.venue.now)

    def command(self, runner, action="activate", request="activate-1", **kwargs):
        values = dict(owner_id=OWNER, action=action, request_id=request,
            configuration_version=runner.config_version, consent=action == "activate")
        values.update(kwargs)
        return runner.command(**values)

    def activate(self, runner):
        self.assertTrue(runner.cycle()["reconciled"])
        self.command(runner)

    def posts(self):
        return [c for c in self.venue.calls if c["method"] == "POST"]

    def test_default_is_paused_and_reads_only(self):
        with self.runner() as runner:
            result = runner.cycle()
            self.assertEqual(result["state"], "paused")
            self.assertFalse(result["execution_enabled"])
            self.assertEqual(self.posts(), [])

    def test_activation_needs_current_reconciliation(self):
        with self.runner() as runner:
            with self.assertRaisesRegex(AutopilotError, "fresh_reconciliation"):
                self.command(runner)
            runner.cycle()
            self.venue.advance(31)
            with self.assertRaisesRegex(AutopilotError, "fresh_reconciliation"):
                self.command(runner)

    def test_owner_version_and_consent_are_enforced(self):
        with self.runner() as runner:
            runner.cycle()
            for changes in ({"owner_id": FOREIGN}, {"configuration_version": "old"}, {"consent": False}, {"consent": "true"}):
                with self.assertRaises(AutopilotError):
                    self.command(runner, **changes)
            self.assertEqual(self.posts(), [])

    def test_automatic_entry_then_exit_and_actual_fill_reconciliation(self):
        with self.runner() as runner:
            self.activate(runner)
            result = runner.cycle()
            self.assertEqual(result["state"], "active")
            self.assertTrue(result["session"]["actions"][0]["submitted"])
            self.assertEqual(len(self.posts()), 1)
            entry = next(iter(self.venue.orders))
            self.venue.fill(entry, int(self.venue.orders[entry]["qty"]), "9.82")
            self.venue.advance()
            self.bid, self.ask = "9.70", "9.72"
            result = runner.cycle()
            self.assertEqual(len(self.posts()), 2)
            self.assertFalse(result["positions_confirmed_flat"])
            exit_id = next(k for k, v in self.venue.orders.items() if v["side"] == "sell")
            self.venue.fill(exit_id, int(self.venue.orders[exit_id]["qty"]), "9.70")
            self.venue.advance()
            result = runner.cycle()
            self.assertTrue(result["positions_confirmed_flat"])

    def test_pause_cancels_opening_remainder_and_manages_existing_position(self):
        with self.runner() as runner:
            self.activate(runner)
            runner.cycle()
            entry = next(iter(self.venue.orders))
            self.venue.fill(entry, 40, "9.82")
            self.command(runner, "pause", "pause-1")
            self.assertEqual(runner.state()["state"], "paused")
            self.venue.advance()
            result = runner.cycle()
            self.assertIn("cancel_opening_remainder", [a["kind"] for a in result["session"]["actions"]])
            self.venue.orders[entry]["status"] = "canceled"
            self.venue.advance()
            self.bid, self.ask = "9.70", "9.72"
            result = runner.cycle()
            self.assertEqual(result["state"], "paused")
            self.assertEqual(len(self.posts()), 2)
            self.assertEqual(self.posts()[-1]["payload"]["side"], "sell")

    def test_retry_old_activation_does_not_undo_newer_pause(self):
        with self.runner() as runner:
            self.activate(runner)
            self.command(runner, "pause", "pause-1")
            self.assertTrue(self.command(runner)["duplicate"])
            result = runner.cycle()
            self.assertEqual(result["state"], "paused")
            self.assertFalse(result["entries_enabled"])
            self.assertEqual(self.posts(), [])

    def test_command_id_conflict(self):
        with self.runner() as runner:
            self.activate(runner)
            with self.assertRaisesRegex(AutopilotError, "request_id_conflict"):
                self.command(runner, "pause", "activate-1")

    def test_restart_recovers_activation_and_does_not_duplicate_entry(self):
        with self.runner() as runner:
            self.activate(runner)
            runner.cycle()
        self.venue.advance()
        with self.runner() as restarted:
            result = restarted.cycle()
            self.assertEqual(result["state"], "active")
            self.assertEqual(len(self.posts()), 1)

    def test_foreign_broker_account_prevents_mutation(self):
        with self.runner() as runner:
            self.activate(runner)
            self.return_account = FOREIGN
            result = runner.cycle()
            self.assertEqual(result["state"], "blocked")
            self.assertEqual(self.posts(), [])

    def test_identity_binding_survives_restart(self):
        with self.runner() as runner:
            runner.cycle()
        with self.assertRaisesRegex(AutopilotError, "owner_account_binding_mismatch"):
            with self.runner(account=FOREIGN):
                pass

    def test_two_processes_cannot_own_same_journal(self):
        with self.runner():
            with self.assertRaises(AlreadyRunning):
                with self.runner():
                    pass

    def test_loss_limit_stops_new_entries(self):
        with self.runner() as runner:
            self.activate(runner)
            self.venue.realized = Decimal("-100")
            result = runner.cycle()
            self.assertEqual(result["state"], "stopped")
            self.assertEqual(self.posts(), [])

    def test_emergency_is_durable_and_cannot_be_resumed(self):
        with self.runner() as runner:
            self.activate(runner)
            self.command(runner, "emergency_stop", "stop-1")
            result = runner.cycle()
            self.assertEqual(result["state"], "stopped")
            self.command(runner, "pause", "pause-after-stop")
            self.assertEqual(runner.state()["state"], "stopped")
        with self.runner() as restarted:
            restarted.cycle()
            with self.assertRaisesRegex(AutopilotError, "risk_stop"):
                self.command(restarted, request="activate-2")
        self.assertEqual(self.posts(), [])

    def test_missing_accounting_blocks_orders(self):
        with self.runner() as runner:
            self.activate(runner)
            self.ledger_missing = True
            result = runner.cycle()
            self.assertEqual(result["state"], "blocked")
            self.assertEqual(self.posts(), [])

    def test_transport_failure_does_not_leak_secrets_or_claim_active(self):
        with self.runner() as runner:
            self.activate(runner)
            self.fail = True
            result = runner.cycle()
            self.assertEqual(result["state"], "blocked")
            self.assertNotIn("sensitive", str(result))
            self.assertEqual(self.posts(), [])

    def test_stale_and_shutdown_states_are_offline(self):
        with self.runner() as runner:
            self.activate(runner)
            runner.cycle()
            self.venue.advance(31)
            self.assertEqual(runner.state()["state"], "offline")
            self.assertFalse(runner.state()["entries_enabled"])
        self.assertEqual(runner.state()["state"], "offline")

    def test_unreviewed_settings_cannot_activate(self):
        cfg = replace(self.cfg, paper_reviewed_config_hash="")
        with self.runner(cfg) as runner:
            runner.cycle()
            with self.assertRaisesRegex(AutopilotError, "configuration_incomplete"):
                self.command(runner)

    def test_scheduler_has_bounded_one_cycle_mode(self):
        with self.runner() as runner:
            self.assertEqual(runner.run(max_cycles=1), 1)
            with self.assertRaises(AutopilotError):
                runner.run(max_cycles=True)

    def test_shutdown_during_data_fetch_prevents_order_submission(self):
        with self.runner() as runner:
            self.activate(runner)
            original = runner.builder.build_watchlist
            def stop_during_fetch():
                result = original()
                runner.stop_event.set()
                return result
            runner.builder = SimpleNamespace(build_watchlist=stop_during_fetch)
            runner.cycle()
            self.assertEqual(self.posts(), [])
            self.assertEqual(runner.state()["state"], "offline")

    def test_pause_and_emergency_remain_available_from_stale_settings(self):
        with self.runner() as runner:
            self.activate(runner)
            self.command(runner, "pause", "stale-pause", configuration_version="previous-version")
            self.assertFalse(runner.cycle()["entries_enabled"])
            self.command(runner, "emergency_stop", "stale-stop", configuration_version="previous-version")
            self.assertEqual(runner.state()["state"], "stopped")
            self.assertEqual(self.posts(), [])


if __name__ == "__main__":
    unittest.main()
