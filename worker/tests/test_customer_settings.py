"""Invented server rows and fake broker only. No hosted execution acceptance."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import tempfile
from types import SimpleNamespace
import unittest
from momentum.alpaca import PaperClient
from momentum.autopilot import PaperAutopilot, AutopilotError
from momentum.customer_settings import compile_paper_settings, SettingsBindingError
from momentum.strategy import StrategyConfig
from worker_fakes import armed_config, FakeVenue

OWNER = "11111111-1111-4111-8111-111111111111"
ACCOUNT = "22222222-2222-4222-8222-222222222222"
VERSION = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"
REQUEST = "55555555-5555-4555-8555-555555555555"

class CustomerSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = replace(armed_config(self.directory.name), owner_user_id=OWNER)
        self.venue = FakeVenue(self.base)
        self.row = {"owner_id": OWNER, "version": VERSION, "request_id": REQUEST,
                    "updated_at": self.venue.now.isoformat(), "configuration": {
            "mode": "paper", "market": "stocks", "capital_allocation": "100.00",
            "risk_per_trade": "5.00", "daily_loss_limit": "20.00",
            "max_position_exposure": "100.00", "max_open_positions": 3,
            "allowed_symbols": ["DEMO-A"], "strategy_version": "test-reviewed-v1"}}
        self.registry = {"test-reviewed-v1": StrategyConfig(**self.base.strategy_overrides).config_id}
        self.load_error = False
        self.loaded_owners = []
        self.bid, self.ask = "9.81", "9.82"
        self.before_request = lambda method, url: None

    def compile(self, row=None):
        return compile_paper_settings(self.base, self.row if row is None else row,
            verified_owner_id=OWNER, reviewed_strategy_ids=self.registry, now=self.venue.now)

    def load(self, owner):
        self.loaded_owners.append(owner)
        if self.load_error:
            raise RuntimeError("secret upstream exception")
        return deepcopy(self.row)

    def runner(self, config=None, binding=None):
        if config is None:
            config, binding = self.compile()
            # Test-only review, NOT a production strategy approval.
            config = replace(config, paper_enabled=True, paper_reviewed_config_hash=config.review_hash())
        def transport(method, url, headers, payload):
            self.before_request(method, url)
            result = self.venue.request(method, url, headers, payload)
            if url.endswith("/v2/account"):
                result["id"] = ACCOUNT
            return result
        return PaperAutopilot(config, account_id=ACCOUNT,
            provider=PaperClient(config, transport=transport, clock=lambda: self.venue.now),
            builder=SimpleNamespace(build_watchlist=lambda: [self.venue.snapshot(bid=self.bid, ask=self.ask)]),
            accounting=SimpleNamespace(collect=lambda account:
                {**self.venue.ledger(), "account_id": account, "environment": "paper"}),
            clock=lambda: self.venue.now, customer_binding=binding, settings_loader=self.load)

    def command(self, runner, action="activate", request="customer-activation", version=None):
        return runner.command(owner_id=OWNER, action=action,
            configuration_version=version or runner.config_version,
            request_id=request, consent=action == "activate")

    def activate(self, runner):
        self.assertTrue(runner.cycle()["reconciled"])
        return self.command(runner)

    def posts(self):
        return [c for c in self.venue.calls if c["method"] == "POST"]

    def test_compiler_disarms_and_binds_uuid_to_exact_hash(self):
        config, binding = self.compile()
        self.assertFalse(config.paper_enabled)
        self.assertEqual(config.paper_reviewed_config_hash, "")
        self.assertEqual(binding.settings_version, VERSION)
        self.assertEqual(binding.worker_review_hash, config.review_hash())
        self.assertEqual(config.watchlist, ("DEMO-A",))
        self.assertEqual(binding.effective_max_open_positions, 1)
        self.assertEqual(binding.requested_max_open_positions, 3)
        self.assertNotIn("FAKE-SECRET", str(binding.public()))
        self.assertEqual(self.venue.calls, [])

    def test_compiler_never_widens_operator_limits(self):
        self.row["configuration"].update(capital_allocation="10000", risk_per_trade="500",
            daily_loss_limit="1000", max_position_exposure="5000")
        config, binding = self.compile()
        self.assertEqual(Decimal(config.risk_overrides["per_trade_risk"]), Decimal("25"))
        self.assertEqual(Decimal(binding.effective_daily_loss), Decimal("75"))
        self.assertEqual(Decimal(binding.effective_position_notional), Decimal("1000"))

    def test_unknown_or_mismatched_reviewed_strategy_is_rejected(self):
        for registry in ({}, {"test-reviewed-v1": "wrong"}, None):
            with self.subTest(registry=registry), self.assertRaises(SettingsBindingError):
                compile_paper_settings(self.base, self.row, verified_owner_id=OWNER,
                    reviewed_strategy_ids=registry, now=self.venue.now)

    def test_live_and_crypto_are_not_silently_mapped_to_stocks(self):
        for key, value in (("mode", "live"), ("market", "crypto"), ("market", "forex")):
            row = deepcopy(self.row)
            row["configuration"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(SettingsBindingError):
                self.compile(row)

    def test_invalid_money_is_rejected(self):
        for value in ("NaN", "Infinity", "1e2", "1.001", "01", "0", "-1", "1000000000", 5, True, None):
            row = deepcopy(self.row)
            row["configuration"]["risk_per_trade"] = value
            with self.subTest(value=value), self.assertRaises(SettingsBindingError):
                self.compile(row)

    def test_inconsistent_limits_are_rejected(self):
        for key, value in (("risk_per_trade", "21"), ("daily_loss_limit", "101"),
                           ("max_position_exposure", "101"), ("max_position_exposure", "4")):
            row = deepcopy(self.row)
            row["configuration"][key] = value
            with self.subTest(key=key), self.assertRaises(SettingsBindingError):
                self.compile(row)

    def test_bad_position_count_is_rejected(self):
        for value in (0, 21, True, 1.0, "1"):
            row = deepcopy(self.row)
            row["configuration"]["max_open_positions"] = value
            with self.subTest(value=value), self.assertRaises(SettingsBindingError):
                self.compile(row)

    def test_symbols_must_fit_current_stock_executor(self):
        for value in ([], ["BTC/USD"], ["demo"], ["DEMO-A", "DEMO-A"],
                      [f"S{i}" for i in range(11)], [None], "AAPL"):
            row = deepcopy(self.row)
            row["configuration"]["allowed_symbols"] = value
            with self.subTest(value=value), self.assertRaises(SettingsBindingError):
                self.compile(row)

    def test_foreign_owner_extra_fields_and_bad_ids_are_rejected(self):
        for changes in ({"owner_id": OTHER}, {"version": "bad"}, {"request_id": None}, {"consent": True}):
            with self.subTest(changes=changes), self.assertRaises(SettingsBindingError):
                self.compile({**self.row, **changes})
        row = deepcopy(self.row)
        row["configuration"]["broker_key"] = "must-not-be-accepted"
        with self.assertRaises(SettingsBindingError):
            self.compile(row)

    def test_future_naive_and_invalid_timestamp_are_rejected(self):
        for stamp in ((self.venue.now+timedelta(seconds=1)).isoformat(), "2026-10-16T13:35:05", "not-a-time", None):
            with self.subTest(stamp=stamp), self.assertRaises(SettingsBindingError):
                self.compile({**self.row, "updated_at": stamp})

    def test_binding_detects_changes_even_when_uuid_is_reused(self):
        _, binding = self.compile()
        for changes in ({"capital_allocation": "101.00"}, {"risk_per_trade": "4.00"},
                        {"max_open_positions": 2}, {"strategy_version": "other"}):
            row = deepcopy(self.row)
            row["configuration"].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(SettingsBindingError, "saved_settings_changed"):
                binding.verify_current(row, now=self.venue.now)

    def test_equivalent_money_and_timezone_normalize_without_false_change(self):
        _, binding = self.compile()
        row = deepcopy(self.row)
        row["configuration"]["capital_allocation"] = "100"
        row["updated_at"] = "2026-10-16T08:35:05-05:00"
        binding.verify_current(row, now=self.venue.now)

    def test_input_mutation_does_not_mutate_compiled_config(self):
        config, binding = self.compile()
        self.row["configuration"]["allowed_symbols"].append("DEMO-B")
        self.base.risk_overrides["max_notional"] = "9999"
        self.assertEqual(config.watchlist, ("DEMO-A",))
        binding.verify_worker(config)

    def test_worker_config_tampering_is_detected(self):
        config, binding = self.compile()
        config.risk_overrides["max_notional"] = "9999"
        with self.assertRaisesRegex(SettingsBindingError, "worker_settings_binding_mismatch"):
            binding.verify_worker(config)

    def test_binding_and_loader_must_be_supplied_together(self):
        config, binding = self.compile()
        with self.assertRaises(AutopilotError):
            PaperAutopilot(config, account_id=ACCOUNT, customer_binding=binding)
        with self.assertRaises(AutopilotError):
            PaperAutopilot(config, account_id=ACCOUNT, settings_loader=self.load)

    def test_compiled_but_unreviewed_config_cannot_activate(self):
        config, binding = self.compile()
        with self.runner(config, binding) as runner:
            runner.cycle()
            with self.assertRaisesRegex(AutopilotError, "execution_configuration_incomplete"):
                self.command(runner)
            self.assertFalse(runner.state()["entries_enabled"])
        self.assertEqual(self.posts(), [])

    def test_end_to_end_saved_limits_cap_fake_paper_order(self):
        with self.runner() as runner:
            self.activate(runner)
            result = runner.cycle()
            self.assertEqual(result["configuration_version"], VERSION)
            self.assertEqual(result["state"], "active")
            self.assertEqual(len(self.posts()), 1)
            order = self.posts()[0]["payload"]
            self.assertLessEqual(Decimal(order["qty"])*Decimal(order["limit_price"]), Decimal("100"))
            intent = result["session"]["actions"][0]["intent"]
            self.assertLessEqual(Decimal(intent["planned_risk"]), Decimal("5"))
            self.assertEqual(set(self.loaded_owners), {OWNER})

    def test_different_saved_version_cannot_activate(self):
        with self.runner() as runner:
            runner.cycle()
            with self.assertRaisesRegex(AutopilotError, "configuration_version_mismatch"):
                self.command(runner, version=OTHER)
        self.assertEqual(self.posts(), [])

    def test_changed_row_blocks_activation_and_active_entries(self):
        with self.runner() as runner:
            self.activate(runner)
            self.row["version"] = OTHER
            result = runner.cycle()
            self.assertEqual(result["state"], "blocked")
            self.assertFalse(result["entries_enabled"])
            with self.assertRaisesRegex(AutopilotError, "customer_settings_changed"):
                self.command(runner, request="new-activate")
        self.assertEqual(self.posts(), [])

    def test_loader_failure_is_fail_closed_without_secret_leak(self):
        with self.runner() as runner:
            self.activate(runner)
            self.load_error = True
            result = runner.cycle()
            self.assertEqual(result["state"], "blocked")
            self.assertIn("customer_settings_unavailable", result["blockers"])
            self.assertNotIn("secret", str(result))
        self.assertEqual(self.posts(), [])

    def test_settings_change_during_broker_lookup_prevents_submission(self):
        with self.runner() as runner:
            self.activate(runner)
            def change(method, url):
                if "/v2/orders:by_client_order_id" in url:
                    self.row["version"] = OTHER
            self.before_request = change
            result = runner.cycle()
            self.assertFalse(result["entries_enabled"])
            self.assertEqual(result["state"], "blocked")
        self.assertEqual(self.posts(), [])

    def test_changed_settings_cancel_opening_remainder_and_keep_protective_exits(self):
        with self.runner() as runner:
            self.activate(runner)
            runner.cycle()
            entry = next(iter(self.venue.orders))
            self.venue.fill(entry, 4, "9.82")
            self.row["version"] = OTHER
            self.venue.advance()
            result = runner.cycle()
            self.assertIn("cancel_opening_remainder", [a["kind"] for a in result["session"]["actions"]])
            self.venue.orders[entry]["status"] = "canceled"
            self.venue.advance()
            self.bid, self.ask = "9.70", "9.72"
            result = runner.cycle()
            self.assertFalse(result["entries_enabled"])
            self.assertEqual(len(self.posts()), 2)
            self.assertEqual(self.posts()[-1]["payload"]["side"], "sell")
            self.assertFalse(result["positions_confirmed_flat"])

    def test_loader_outage_keeps_protective_exit_path(self):
        with self.runner() as runner:
            self.activate(runner)
            runner.cycle()
            entry = next(iter(self.venue.orders))
            self.venue.fill(entry, int(self.venue.orders[entry]["qty"]), "9.82")
            self.load_error = True
            self.bid, self.ask = "9.70", "9.72"
            self.venue.advance()
            result = runner.cycle()
            self.assertEqual(result["state"], "blocked")
            self.assertEqual(self.posts()[-1]["payload"]["side"], "sell")

    def test_stale_pause_and_stop_remain_usable_after_settings_change(self):
        with self.runner() as runner:
            self.activate(runner)
            self.row["version"] = OTHER
            self.command(runner, "pause", "pause", version="old-version")
            self.assertFalse(runner.state()["entries_enabled"])
            self.command(runner, "emergency_stop", "stop", version="old-version")
            self.assertEqual(runner.state()["state"], "stopped")
        self.assertEqual(self.posts(), [])

    def test_duplicate_activation_cannot_undo_pause(self):
        with self.runner() as runner:
            self.activate(runner)
            self.command(runner, "pause", "pause")
            self.assertTrue(self.command(runner)["duplicate"])
            self.assertEqual(runner.cycle()["state"], "paused")
        self.assertEqual(self.posts(), [])

    def test_restart_same_binding_recovers_without_duplicate_order(self):
        with self.runner() as runner:
            self.activate(runner)
            runner.cycle()
        self.venue.advance()
        with self.runner() as runner:
            self.assertEqual(runner.cycle()["state"], "active")
        self.assertEqual(len(self.posts()), 1)

    def test_new_version_on_flat_account_does_not_reuse_authorization(self):
        with self.runner() as runner:
            self.activate(runner)
        self.row["version"] = OTHER
        with self.runner() as runner:
            self.assertEqual(runner.cycle()["state"], "paused")
        self.assertEqual(self.posts(), [])

    def test_changed_worker_hash_with_same_uuid_requires_new_authorization(self):
        with self.runner() as runner:
            self.activate(runner)
        self.base = replace(self.base, order_ttl_seconds=20)
        with self.runner() as runner:
            self.assertEqual(runner.cycle()["state"], "paused")
        self.assertEqual(self.posts(), [])

    def test_changed_binding_cannot_reconfigure_open_exposure(self):
        with self.runner() as runner:
            self.activate(runner)
            runner.cycle()
        self.row["version"] = OTHER
        with self.assertRaisesRegex(AutopilotError, "configuration_change_requires_reconciled_flat_account"):
            with self.runner():
                pass

    def test_nonsemantic_uuid_reuse_still_requires_new_authorization(self):
        with self.runner() as runner:
            self.activate(runner)
        self.row["configuration"]["max_open_positions"] = 4
        with self.runner() as runner:
            self.assertEqual(runner.cycle()["state"], "paused")
        self.assertEqual(self.posts(), [])

    def test_restoring_old_row_does_not_silently_resume_entries(self):
        with self.runner() as runner:
            self.activate(runner)
            original = deepcopy(self.row)
            self.row["version"] = OTHER
            self.assertEqual(runner.cycle()["state"], "blocked")
            self.row = original
            self.assertEqual(runner.cycle()["state"], "paused")
            self.assertTrue(self.command(runner)["duplicate"])
            self.assertEqual(runner.cycle()["state"], "paused")
        self.assertEqual(self.posts(), [])
        with self.runner() as restarted:
            self.assertEqual(restarted.cycle()["state"], "paused")

    def test_changed_settings_during_snapshot_build_blocks_submission(self):
        with self.runner() as runner:
            self.activate(runner)
            def change():
                self.row["version"] = OTHER
                return [self.venue.snapshot()]
            runner.builder = SimpleNamespace(build_watchlist=change)
            self.assertEqual(runner.cycle()["state"], "blocked")
        self.assertEqual(self.posts(), [])

    def test_single_position_cap_includes_pending_orders(self):
        self.row["configuration"]["allowed_symbols"] = ["DEMO-A", "DEMO-B"]
        with self.runner() as runner:
            runner.builder = SimpleNamespace(build_watchlist=lambda:
                [self.venue.snapshot(symbol=s) for s in ("DEMO-A", "DEMO-B")])
            self.activate(runner)
            runner.cycle()
            self.assertEqual(len(self.posts()), 1)
            entry = next(iter(self.venue.orders))
            self.venue.fill(entry, int(self.venue.orders[entry]["qty"]), "9.82")
            self.venue.advance()
            runner.cycle()
            self.assertEqual(len(self.posts()), 1)

    def test_emergency_latch_survives_new_settings_version(self):
        with self.runner() as runner:
            self.activate(runner)
            self.command(runner, "emergency_stop", "stop")
        self.row["version"] = OTHER
        with self.assertRaisesRegex(AutopilotError, "configuration_change_cannot_clear_risk_latch"):
            with self.runner():
                pass

if __name__ == "__main__":
    unittest.main()
