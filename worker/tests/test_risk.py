"""Operational and monetary limits independent of strategy pattern recognition."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
import unittest

from momentum.models import InputError, timestamp
from momentum.risk import RiskConfig, RiskManager
from momentum.strategy import StrategyConfig


NOW = "2026-10-16T13:35:01Z"


def valid_account():
    return {"mode": "paper", "session_date": "2026-10-16", "as_of": NOW,
            "equity": "10000", "buying_power": "10000", "realized_pnl": "0",
            "unrealized_pnl": "0", "fees": "0", "position_qty": 0, "open_order_qty": 0}


def valid_intent():
    return {"symbol": "DEMO", "side": "buy", "mode": "paper", "profile": "experimental_v1",
            "config_id": StrategyConfig().config_id, "entry_limit": "6.31", "limit_price": "6.31",
            "stop_price": "6.22", "target_price": "6.49", "observed_at": NOW,
            "feed": "demo", "liquidity_volume": 12000, "setup_id": "synthetic-risk-unit-test"}


def valid_quote():
    return {"symbol": "DEMO", "bid": "6.30", "ask": "6.31", "feed": "demo",
            "timestamp": NOW, "received_at": NOW}


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.manager = RiskManager()
        self.account = valid_account()
        self.intent = valid_intent()
        self.quote = valid_quote()
        self.now = NOW

    def assess(self, **flags):
        flags.setdefault("connected", True)
        flags.setdefault("reconciled", True)
        return self.manager.assess(self.intent, self.account, self.quote, self.now, **flags)

    def assert_reject(self, reason, **flags):
        result = self.assess(**flags)
        self.assertFalse(result.accepted, result.to_dict())
        self.assertIn(reason, result.reasons, result.to_dict())
        self.assertIsNone(result.intent)
        self.assertEqual(len(result.metrics["risk_config_id"]), 64)
        return result

    def test_planned_risk_quantity_buying_power_and_liquidity(self):
        result = self.assess()
        self.assertTrue(result.accepted, result.to_dict())
        self.assertEqual(result.intent["qty"], 120)
        self.assertEqual(Decimal(result.intent["notional"]), Decimal("757.20"))
        self.assertEqual(Decimal(result.intent["planned_risk"]), Decimal("13.20"))
        self.assertEqual(result.metrics["quantity_caps"],
                         {"risk": 227, "buying_power": 1584, "notional": 158,
                          "liquidity": 120, "configured_shares": 500})
        self.assertFalse(result.intent["live_enabled"])
        self.assertTrue(result.intent["paper_only"])
        json.dumps(result.to_dict())

    def test_position_sizing_floors_and_reserves_remaining_daily_budget(self):
        self.intent["liquidity_volume"] = 100000
        self.account["buying_power"] = "63.09"
        result = self.assess()
        self.assertTrue(result.accepted)
        self.assertEqual(result.intent["qty"], 9)
        self.account["buying_power"] = "10000"
        self.account["realized_pnl"] = "-74"
        result = self.assess()
        self.assertEqual(result.intent["qty"], 9)
        self.assertEqual(Decimal(result.intent["planned_risk"]), Decimal("0.99"))
        self.account["realized_pnl"] = "-74.90"
        self.assert_reject("zero_quantity_under_limits")

    def test_configured_shares_and_notional_are_independent_caps(self):
        self.manager = RiskManager(replace(RiskConfig(), max_shares=3))
        self.assertEqual(self.assess().intent["qty"], 3)
        self.manager = RiskManager(replace(RiskConfig(), max_notional="12.61"))
        self.assertEqual(self.assess().intent["qty"], 1)

    def test_zero_liquidity_capacity_and_zero_buying_power_block(self):
        self.intent["liquidity_volume"] = 99
        self.assert_reject("zero_quantity_under_limits")
        self.intent["liquidity_volume"] = 12000
        self.account["buying_power"] = "0"
        self.assert_reject("zero_quantity_under_limits")

    def test_partial_fill_and_pending_entry_each_count_as_exposure(self):
        for field in ("position_qty", "open_order_qty"):
            with self.subTest(field=field):
                self.account = valid_account()
                self.account[field] = 1
                self.assert_reject("existing_exposure")

    def test_gross_realized_unrealized_and_fees_trigger_exact_daily_limit(self):
        self.account.update(realized_pnl="-40", unrealized_pnl="-30", fees="5",
                            position_qty=10, open_order_qty=2)
        result = self.assert_reject("daily_loss_limit")
        self.assertEqual(Decimal(result.metrics["net_pnl"]), Decimal(-75))
        self.assertTrue(result.metrics["flatten_required"])
        self.assertTrue(result.metrics["cancel_entries_required"])
        self.account.update(realized_pnl="100", unrealized_pnl="0", fees="5",
                            position_qty=0, open_order_qty=0)
        self.assert_reject("daily_loss_limit")
        self.assertTrue(self.manager.locked)

    def test_mark_only_event_checks_daily_limit_without_entry_intent(self):
        self.intent = None
        self.account.update(realized_pnl="-75", position_qty=1)
        result = self.assert_reject("daily_loss_limit")
        self.assertTrue(result.metrics["flatten_required"])

    def test_daily_lock_survives_json_restart_and_new_session_can_start_flat(self):
        self.account["realized_pnl"] = "-75"
        self.assert_reject("daily_loss_limit")
        state = json.loads(json.dumps(self.manager.to_state()))
        self.manager = RiskManager()
        self.manager.restore_state(state)
        self.account["realized_pnl"] = "0"
        self.assert_reject("daily_loss_limit")
        self.now = "2026-10-19T13:35:01Z"
        self.account.update(session_date="2026-10-19", as_of=self.now)
        self.quote.update(timestamp=self.now, received_at=self.now)
        self.intent["observed_at"] = self.now
        self.assertTrue(self.assess().accepted)
        self.assertIn("2026-10-16", self.manager.to_state()["daily_locks"])

    def test_manual_halt_does_not_request_liquidation(self):
        self.account.update(position_qty=10, open_order_qty=2)
        result = self.assert_reject("new_entries_halted", halt_new_entries=True)
        self.assertFalse(result.metrics["flatten_required"])
        self.assertTrue(result.metrics["cancel_entries_required"])
        self.assertFalse(self.manager.locked)

    def test_emergency_kill_latches_and_requires_confirmed_fill(self):
        self.account.update(position_qty=10, open_order_qty=2)
        result = self.assert_reject("kill_switch_latched", kill_switch=True)
        self.assertTrue(result.metrics["flatten_required"])
        self.assertTrue(result.metrics["cancel_entries_required"])
        self.account.update(position_qty=0, open_order_qty=0)
        self.assert_reject("kill_switch_latched", kill_switch=False)
        state = self.manager.to_state()
        self.manager = RiskManager()
        self.manager.restore_state(state)
        self.assert_reject("kill_switch_latched", kill_switch=False)

    def test_restart_and_disconnect_require_explicit_current_assertions(self):
        self.assertTrue(self.assess().accepted)
        state = self.manager.to_state()
        self.manager = RiskManager()
        self.manager.restore_state(state)
        self.assertTrue(self.manager.to_state()["reconciliation_required"])
        default_call = self.manager.assess(self.intent, self.account, self.quote, self.now)
        self.assertFalse(default_call.accepted)
        self.assert_reject("reconciliation_required", connected=True, reconciled=False)
        self.assert_reject("feed_disconnected", connected=False, reconciled=True)
        self.assertTrue(self.assess(connected=True, reconciled=True).accepted)

    def test_missing_stale_and_future_account_timestamp_block(self):
        del self.account["as_of"]
        result = self.assess()
        self.assertFalse(result.accepted)
        self.assertEqual(result.metrics["field"], "account.as_of")
        for value in ("2026-10-16T13:34:55Z", "2026-10-16T13:35:02Z"):
            self.account["as_of"] = value
            self.assert_reject("stale_or_future_account")
        self.account["as_of"] = "2026-10-16T13:34:56Z"
        self.assertTrue(self.assess().accepted)

    def test_stale_future_and_crossed_quotes_block(self):
        for value, reason in (("2026-10-16T13:34:58.999Z", "stale_quote"),
                              ("2026-10-16T13:35:02Z", "future_quote")):
            self.quote.update(timestamp=value, received_at=value)
            self.assert_reject(reason)
        self.quote.update(timestamp=NOW, received_at=NOW, bid="6.32")
        self.assert_reject("crossed_quote")

    def test_quote_at_exact_age_limit_and_stale_intent(self):
        self.quote.update(timestamp="2026-10-16T13:34:59Z", received_at="2026-10-16T13:34:59Z")
        self.assertTrue(self.assess().accepted)
        self.intent["observed_at"] = "2026-10-16T13:34:58.999Z"
        self.assert_reject("stale_or_future_intent")

    def test_out_of_order_account_cannot_restore_older_budget(self):
        self.assertTrue(self.assess().accepted)
        self.now = "2026-10-16T13:35:00Z"
        self.account["as_of"] = self.now
        self.assert_reject("out_of_order_account_event")

    def test_new_york_session_date_used_at_utc_midnight(self):
        self.now = "2026-10-17T00:01:00Z"
        self.account["as_of"] = self.now
        self.quote.update(timestamp=self.now, received_at=self.now)
        self.intent["observed_at"] = self.now
        # Risk time accounting is independent of the strategy's entry window.
        self.assertTrue(self.assess().accepted)
        self.account["session_date"] = "2026-10-17"
        self.assert_reject("account_session_mismatch")

    def test_invalid_stops_reward_and_changed_ask_block(self):
        for stop, target, reason in (("6.31", "6.49", "invalid_stop_or_target"),
                                     ("6.22", "6.31", "invalid_stop_or_target"),
                                     ("6.22", "6.48", "insufficient_reward_room")):
            self.intent.update(stop_price=stop, target_price=target)
            self.assert_reject(reason)
        self.intent = valid_intent()
        self.quote["ask"] = "6.32"
        self.assert_reject("entry_limit_not_marketable")

    def test_unknown_position_account_or_nonfinite_risk_fails_closed(self):
        for field, value in (("position_qty", None), ("open_order_qty", -1),
                             ("realized_pnl", "NaN"), ("unrealized_pnl", "Infinity"),
                             ("fees", -1), ("buying_power", True)):
            self.account = valid_account()
            self.account[field] = value
            self.assertFalse(self.assess().accepted)
        self.account = None
        self.assert_reject("invalid_account")
        self.account = valid_account()
        self.intent = []
        self.assert_reject("invalid_intent")

    def test_live_modes_invalid_flags_and_small_equity_block(self):
        self.account["mode"] = "live"
        self.assert_reject("live_mode_forbidden")
        self.account = valid_account()
        self.intent["mode"] = "live"
        self.assert_reject("unapproved_intent_mode_or_profile")
        self.intent = valid_intent()
        self.assert_reject("invalid_operational_flags", connected="true")
        self.account["equity"] = "50"
        self.assert_reject("risk_budget_exceeds_equity")

    def test_malformed_persisted_state_aborts_restore(self):
        state = self.manager.to_state()
        for field, value in (("version", 2), ("kill_latched", "false"), ("daily_locks", []),
                             ("last_observed_at", "2026-10-16T13:35:00Z")):
            altered = {**state, field: value}
            with self.subTest(field=field), self.assertRaises(InputError):
                self.manager.restore_state(altered)

    def test_config_rejects_nonfinite_unbounded_or_live_settings(self):
        for changes in ({"mode": "live"}, {"per_trade_risk": "NaN"},
                        {"max_daily_loss": "Infinity"}, {"per_trade_risk": 0},
                        {"max_shares": 1.5}, {"max_volume_participation": "1.01"},
                        {"per_trade_risk": 76}, {"min_reward_r": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                RiskConfig(**changes)


if __name__ == "__main__":
    unittest.main()
