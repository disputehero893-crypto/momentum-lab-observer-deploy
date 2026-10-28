from dataclasses import replace
from decimal import Decimal
import tempfile
import unittest
from unittest.mock import patch

from momentum.config import Config
from momentum.demo import run_demo
from momentum.journal import Journal


class DemoTests(unittest.TestCase):
    def test_replay_arithmetic_partial_fill_stale_exit_and_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(db_path=directory+"/journal.sqlite3", export_dir=directory+"/exports")
            with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Demo network forbidden")):
                payload = run_demo(cfg)
                again = run_demo(cfg)
            self.assertEqual(payload, again)
            self.assertEqual(payload["metrics"]["event_count"], len(Journal(cfg.db_path).events("demo-v1")))
            self.assertEqual(payload["metrics"]["actual_broker_orders"], 0)
            self.assertEqual(payload["metrics"]["completed_trades"], 2)
            self.assertEqual(Decimal(payload["metrics"]["net_pnl"]), Decimal("4.848"))
            self.assertEqual(Decimal(payload["account"]["equity"]), Decimal("10004.848"))
            scenarios = {row["scenario"]: row for row in payload["scenario_checks"]}
            self.assertEqual(scenarios["partial entry fill"]["position_qty_after"], 40)
            self.assertEqual(scenarios["partial entry fill"]["pending_qty_after"], 61)
            self.assertEqual(scenarios["stale quote blocks stop exit"]["position_qty_after"], 101)
            self.assertIn("stale_quote", scenarios["stale quote blocks stop exit"]["exit_reasons"])
            self.assertIn("kill_switch_latched", scenarios["restart preserves emergency lock"]["risk_reasons"])
            self.assertEqual(len(payload["display_data"]), 16)

    def test_changed_fixture_config_rejected_before_run_events(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(db_path=directory+"/journal.sqlite3", export_dir=directory+"/exports",
                         strategy_overrides={"target_r": "3"})
            with self.assertRaisesRegex(ValueError, "fixed demo fixture"):
                run_demo(cfg)
            self.assertEqual(Journal(cfg.db_path).events(), [])

