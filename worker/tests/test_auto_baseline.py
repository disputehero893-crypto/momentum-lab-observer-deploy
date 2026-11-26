"""ACCOUNTING_BASELINE_AUTO and bound paper-account identity. Invented fixtures only."""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest

from momentum.activities import fetch_activities
from momentum.autopilot import ExecutionAccounting
from momentum.config import Config, ConfigError
from momentum.journal import Journal
from momentum.observation import AUTO_BASELINE_NAME, auto_opening_baseline
from momentum.server import Application

from test_activity_completeness import ACCOUNT, END, START, Provider, crypto_fill, eq, fill
from test_worker_observation import Fixture


def cash(number):
    return {"id": f"2026100100000000{number}::{number:08d}-0000-4000-8000-000000000003",
            "activity_type": "CSD", "date": "2026-10-01", "net_amount": "100000"}


class FlatProvider(Provider):
    def __init__(self, rows=(), positions=(), orders=(), **kwargs):
        super().__init__(rows, positions, **kwargs)
        self._orders = list(orders)

    def open_orders(self):
        return [dict(row) for row in self._orders]


class ConfigTests(unittest.TestCase):
    base = dict(mode="paper", accounting_history_start=START)

    def test_env_parse_and_hash_only_when_enabled(self):
        off = Config.from_env({"MOMENTUM_MODE": "paper", "ACCOUNTING_HISTORY_START": START})
        on = Config.from_env({"MOMENTUM_MODE": "paper", "ACCOUNTING_HISTORY_START": START,
                              "ACCOUNTING_BASELINE_AUTO": "true"})
        self.assertFalse(off.accounting_baseline_auto)
        self.assertTrue(on.accounting_baseline_auto)
        self.assertNotEqual(off.review_hash(), on.review_hash())
        self.assertEqual(off.review_hash(), Config.from_env({"MOMENTUM_MODE": "paper",
            "ACCOUNTING_HISTORY_START": START, "ACCOUNTING_BASELINE_AUTO": "false"}).review_hash())
        self.assertTrue(on.public()["accounting_baseline_auto"])

    def test_invalid_combinations_refused(self):
        with self.assertRaises(ConfigError):
            Config.from_env({"MOMENTUM_MODE": "paper", "ACCOUNTING_BASELINE_AUTO": "true"})
        with self.assertRaises(ConfigError):
            Config.from_env({"MOMENTUM_MODE": "paper", "ACCOUNTING_HISTORY_START": START,
                             "ACCOUNTING_BASELINE_AUTO": "maybe"})
        with self.assertRaises(ConfigError):
            Config.from_env({"MOMENTUM_MODE": "paper", "ACCOUNTING_HISTORY_START": START,
                             "ACCOUNTING_BASELINE_AUTO": "true", "ACCOUNTING_BASELINE_PATH": "/tmp/x.json"})


class AutoBaselineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.config = Config(mode="paper", db_path=os.path.join(self.dir, "j.sqlite3"),
                             accounting_history_start=START, accounting_baseline_auto=True)
        self.path = Path(self.dir) / AUTO_BASELINE_NAME

    def prove(self, provider, config=None):
        batch = fetch_activities(provider, account_id=ACCOUNT, after=START, until=END)
        return auto_opening_baseline(config or self.config, provider, account_id=ACCOUNT, batch=batch)

    def test_flat_account_with_only_cash_deposit_is_proven_and_persisted(self):
        baseline = self.prove(FlatProvider([cash(1)]))
        self.assertEqual(baseline["opening_lots"], [])
        self.assertIn("0 positions, 0 open orders and 0 fills", baseline["evidence"])
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored["account_id"], ACCOUNT)
        self.assertEqual(stored["environment"], "paper")
        # Later trading does not undo the persisted proof for the same start.
        again = self.prove(FlatProvider([cash(1), fill(1)], [eq("DEMO-A", "2")], [{"id": "o"}]))
        self.assertEqual(again, baseline)

    def test_refuses_unless_everything_is_flat(self):
        cases = {"position": FlatProvider([], [eq("DEMO-A", "1")]),
                 "crypto_position": FlatProvider([], [{"symbol": "BTCUSD", "qty": "0.1", "asset_class": "crypto"}]),
                 "open_order": FlatProvider([], [], [{"id": "order-1"}]),
                 "equity_fill": FlatProvider([fill(1), fill(2, side="sell")]),
                 "crypto_fill": FlatProvider([crypto_fill(1), crypto_fill(2, side="sell")]),
                 "unsupported_activity": FlatProvider([{"id": "x1", "activity_type": "JNLC",
                                                        "date": "2026-10-02", "net_amount": "5"}]),
                 "positions_error": FlatProvider([], positions_error=RuntimeError("boom"))}
        for name, provider in cases.items():
            with self.subTest(name):
                self.assertIsNone(self.prove(provider))
                self.assertFalse(self.path.exists())

    def test_disabled_or_explicit_path_never_auto_builds(self):
        self.assertIsNone(self.prove(FlatProvider(), replace(self.config, accounting_baseline_auto=False)))
        self.assertFalse(self.path.exists())

    def test_mismatched_start_on_disk_stays_unproven(self):
        self.assertIsNotNone(self.prove(FlatProvider()))
        moved = replace(self.config, accounting_history_start="2026-10-02T00:00:00Z")
        provider = FlatProvider()
        batch = fetch_activities(provider, account_id=ACCOUNT, after="2026-10-02T00:00:00Z", until=END)
        self.assertIsNone(auto_opening_baseline(moved, provider, account_id=ACCOUNT, batch=batch))

    def test_execution_accounting_opening_inventory(self):
        flat = ExecutionAccounting(self.config, FlatProvider([cash(1)]), FlatProvider().clock).collect(ACCOUNT)
        self.assertNotIn("opening_inventory_unproven", flat["blockers"])
        self.assertTrue(flat["complete"], flat["blockers"])
        other = Config(mode="paper", db_path=os.path.join(self.dir, "sub", "j.sqlite3"),
                       accounting_history_start=START, accounting_baseline_auto=True)
        os.makedirs(os.path.join(self.dir, "sub"))
        held = FlatProvider([], [eq("DEMO-A", "2")])
        result = ExecutionAccounting(other, held, held.clock).collect(ACCOUNT)
        self.assertIn("opening_inventory_unproven", result["blockers"])
        self.assertFalse(result["complete"])


class NumberedFixture(Fixture):
    def request(self, method, url, headers, payload):
        body = super().request(method, url, headers, payload)
        if url.split("?")[0].endswith("/v2/account"):
            body = {**body, "account_number": "PA0FIXTURE1"}
        return body


class AccountIdentityTests(unittest.TestCase):
    def test_bound_account_number_recorded_and_on_health(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = NumberedFixture(directory)
            with fixture.worker() as worker:
                result = worker.cycle()
                self.assertIsNone(result["error_code"], result)
                self.assertEqual(worker.paper_account, {"account_id": ACCOUNT, "account_number": "PA0FIXTURE1",
                                                        "environment": "paper"})
                health = Application(fixture.config, observation_worker=worker).health()
                self.assertEqual(health["paper_account"]["account_number"], "PA0FIXTURE1")
                # The fixture has fills, so no automatic baseline was claimed.
                self.assertIn("opening_inventory_unproven", result["accounting"]["blockers"])
            events = Journal(fixture.config.db_path).events(event_type="observation_account_identity")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["payload"]["account_number"], "PA0FIXTURE1")


class WorkerRuntimeTests(unittest.TestCase):
    def test_runtime_switch(self):
        from momentum.container_entrypoint import select_command
        self.assertEqual(select_command(["serve-observe"], {}), ["serve-observe"])
        self.assertEqual(select_command([], {}), ["serve"])
        self.assertEqual(select_command(["serve-observe"], {"WORKER_RUNTIME": "serve-observe"}), ["serve-observe"])
        self.assertEqual(select_command(["serve-observe"], {"WORKER_RUNTIME": "serve-paper"}), ["serve-paper"])
        self.assertEqual(select_command(["serve"], {"WORKER_RUNTIME": "serve-paper"}), ["serve"])
        with self.assertRaises(SystemExit):
            select_command(["serve-observe"], {"WORKER_RUNTIME": "live"})


if __name__ == "__main__":
    unittest.main()
