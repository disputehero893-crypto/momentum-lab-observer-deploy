"""Actual observation/runtime boundaries with wholly invented injected providers."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from momentum.activities import fetch_activities
from momentum.alpaca import BrokerError, PaperDisabled
from momentum.config import Config, ConfigError
from momentum.journal import Journal
from momentum.lease import AlreadyRunning, WorkerLease
from momentum.ledger import AccountingLedger
from momentum.observation import ObservationError, ObservationWorker, ReadOnlyPaperClient
from momentum.publisher import Publisher
from momentum.server import Application
from worker_fakes import FakeVenue, armed_config

ACCOUNT = "00000000-0000-4000-8000-000000000001"
OTHER_ACCOUNT = "00000000-0000-4000-8000-000000000002"


class CloudFixture:
    def __init__(self):
        self.states, self.queued = [], []
        self.failed = False

    def acknowledge_halts(self, journal):
        if self.failed:
            raise RuntimeError("FAKE-SENSITIVE-UPSTREAM-TEXT")
        acknowledged = []
        for identity in self.queued:
            journal.set_kill_switch(True, "fixture_cloud_halt", request_id=identity)
            self.assert_durable = Journal(journal.path).kill_switch()["enabled"]
            acknowledged.append(identity)
        self.queued = []
        return acknowledged

    def publish_worker_state(self, **kwargs):
        self.states.append(deepcopy(kwargs))
        return kwargs


class Fixture:
    def __init__(self, directory, *, armed=False, history=True):
        cfg = replace(armed_config(directory), watchlist=("DEMO-A",), paper_enabled=armed,
                      accounting_history_start="2026-10-16T00:00:00Z" if history else "")
        self.config = replace(cfg, paper_reviewed_config_hash=cfg.review_hash())
        self.venue = FakeVenue(self.config)
        self.account_id = ACCOUNT
        self.account_reads = 0
        self.quote_delay = 0
        self.cloud = CloudFixture()
        self.activities = [
            {"id": "fill-1", "activity_type": "FILL", "type": "fill", "side": "buy",
             "symbol": "DEMO-A", "qty": "2", "price": "10", "order_id": "buy-fixture",
             "transaction_time": "2026-10-16T13:30:00.000000001Z"},
            {"id": "fill-2", "activity_type": "FILL", "type": "fill", "side": "sell",
             "symbol": "DEMO-A", "qty": "2", "price": "10.25", "order_id": "sell-fixture",
             "transaction_time": "2026-10-16T13:31:00.000000001Z"},
            {"id": "fee-1", "activity_type": "FEE", "date": "2026-10-16", "net_amount": "-0.0231"}]
        self.client = ReadOnlyPaperClient(self.config, transport=self.request, clock=lambda: self.venue.now)

    def request(self, method, url, headers, payload):
        parsed = urlsplit(url)
        if method != "GET":
            raise AssertionError("A mutation reached the invented provider")
        if parsed.path == "/v2/account/activities":
            self.venue.calls.append({"method": method, "url": url})
            return [] if parse_qs(parsed.query).get("page_token") else deepcopy(self.activities)
        if parsed.path == "/v2/account":
            self.account_reads += 1
            return {**self.venue.request(method, url, headers, payload), "id": self.account_id}
        return self.venue.request(method, url, headers, payload)

    def build_watchlist(self):
        snapshot = self.venue.snapshot()
        self.venue.advance(self.quote_delay)
        return [snapshot]

    def worker(self, **kwargs):
        return ObservationWorker(self.config, provider=self.client, snapshot_builder=self,
            publisher=self.cloud, clock=lambda: self.venue.now, **kwargs)


class ObservationTests(unittest.TestCase):
    def test_read_only_transport_works_without_execution_setting_and_rejects_all_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            self.assertFalse(fixture.config.paper_enabled)
            self.assertEqual(fixture.client.account()["id"], ACCOUNT)
            calls = len(fixture.venue.calls)
            for operation in (lambda: fixture.client.submit({}), lambda: fixture.client.cancel({}),
                              lambda: fixture.client._request("POST", "/v2/orders", payload={}),
                              lambda: fixture.client._request("DELETE", "/v2/orders/fixture")):
                with self.assertRaises(PaperDisabled):
                    operation()
            self.assertEqual(len(fixture.venue.calls), calls)
            self.assertTrue(all(row["method"] == "GET" for row in fixture.venue.calls))

    def test_real_importer_ledger_session_flow_preserves_unknown_fees_and_restarts_without_duplicate_fills(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            with fixture.worker() as worker:
                first = worker.cycle()
                self.assertIsNone(first["error_code"], first)
                self.assertFalse(first["accounting"]["complete"])
                self.assertIsNone(first["accounting"]["fees"])
                self.assertEqual(first["accounting"]["observed_actual_fees"], "0.0231")
                self.assertEqual(first["accounting"]["observed_realized_pnl"], "0.5")
                self.assertIsNone(first["session"]["account"]["fees"])
                self.assertFalse(first["session"]["execute_requested"])
                self.assertTrue(worker.state()["paper_connected"])
                fixture.venue.advance(1)
                second = worker.cycle()
                self.assertEqual(second["accounting_import"]["receipt"]["inserted"], 0)
                self.assertEqual(second["accounting_import"]["receipt"]["duplicates"], 3)
            self.assertFalse(worker.state()["paper_connected"])
            with fixture.worker() as restarted:
                self.assertFalse(restarted.state()["paper_connected"])
                third = restarted.cycle()
                self.assertIsNone(third["error_code"], third)
            ledger = AccountingLedger(fixture.config.db_path, account_id=ACCOUNT)
            self.assertEqual(len(ledger.revisions()), 3)
            self.assertEqual(len(Journal(fixture.config.db_path).events(event_type="paper_observation_cycle")), 3)
            self.assertTrue(all(row["method"] == "GET" for row in fixture.venue.calls))

    def test_even_fully_armed_fixture_and_accepted_intent_cannot_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, armed=True)
            baseline = Path(directory)/"baseline.json"
            baseline.write_text(json.dumps({"schema_version": "1.0", "account_id": ACCOUNT,
                "environment": "paper", "window_start": fixture.config.accounting_history_start,
                "evidence": "INVENTED TEST: independently reconciled flat opening inventory", "opening_lots": []}))
            fixture.config = replace(fixture.config, accounting_baseline_path=str(baseline))
            def complete_fixture(*args, **kwargs):
                imported = fetch_activities(*args, **kwargs)
                imported.coverage.update(executions_complete=True, fees_complete=True)
                imported.coverage["evidence"].append("INVENTED TEST ONLY: full independent execution and fee reconciliation")
                return imported
            with fixture.worker(activity_fetcher=complete_fixture) as worker:
                result = worker.cycle()
                self.assertIsNone(result["error_code"], result)
                self.assertTrue(result["accounting"]["complete"], result)
                entries = [row for row in result["session"]["actions"] if row["kind"] == "paper_entry_intent"]
                self.assertEqual(len(entries), 1, result)
                self.assertFalse(entries[0]["submitted"])
                self.assertFalse(result["session"]["execute_requested"])
                self.assertEqual(result["orders_submitted"], 0)
                self.assertFalse(Journal(fixture.config.db_path).events(event_type="paper_order_requested"))
                self.assertTrue(worker.state()["paper_reconciled"])
                fixture.venue.advance(3)
                self.assertFalse(worker.state()["paper_reconciled"])
                self.assertTrue(worker.state()["paper_connected"])

    def test_single_owner_and_no_overlapping_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            with fixture.worker() as worker:
                with self.assertRaises(AlreadyRunning):
                    with WorkerLease(fixture.config.db_path):
                        pass
                worker._cycle_lock.acquire()
                try:
                    with self.assertRaises(ObservationError):
                        worker.cycle()
                finally:
                    worker._cycle_lock.release()
            with self.assertRaises(ObservationError):
                worker.cycle()

    def test_cloud_halt_is_durable_before_publish_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.cloud.queued = ["fixture-halt-1"]
            with fixture.worker() as worker:
                result = worker.cycle()
                self.assertTrue(result["halted"])
                self.assertEqual(result["acknowledged_commands"], ["fixture-halt-1"])
                self.assertTrue(fixture.cloud.assert_durable)
                self.assertTrue(fixture.cloud.states[-1]["halted"])
            with fixture.worker() as restarted:
                self.assertTrue(restarted.cycle()["halted"])
            self.assertEqual(sum(row["event_type"] == "kill_switch_changed" for row in Journal(fixture.config.db_path).events()), 1)

    def test_dispatch_failure_latches_without_reading_broker_or_echoing_upstream_error(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.cloud.failed = True
            with fixture.worker() as worker:
                result = worker.cycle()
                self.assertEqual(result["error_code"], "command_dispatch_failed")
                self.assertTrue(result["halted"])
                self.assertEqual(fixture.account_reads, 0)
                self.assertNotIn("FAKE-SENSITIVE", json.dumps(result))

    def test_account_switch_cannot_rebind_persistent_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            with fixture.worker() as worker:
                self.assertIsNone(worker.cycle()["error_code"])
            fixture.account_id = OTHER_ACCOUNT
            with fixture.worker() as restarted:
                result = restarted.cycle()
                self.assertEqual(result["error_code"], "paper_account_check_failed")
                self.assertFalse(result["broker"]["verified"])
            binding = Journal(fixture.config.db_path).event_by_key("observation:account-binding")
            self.assertEqual(binding["payload"]["account_id"], ACCOUNT)

    def test_changed_owner_is_rejected_under_lease_before_cloud_command_or_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            with fixture.worker() as worker:
                worker.cycle()
            state_count = len(fixture.cloud.states)
            fixture.config = replace(fixture.config, supabase_url="https://fixture.supabase.co",
                supabase_service_role_key="sb_secret_FAKE_TEST_ONLY", owner_user_id=OTHER_ACCOUNT)
            fixture.cloud.queued = ["must-not-ack-new-owner"]
            with self.assertRaises(ObservationError):
                with fixture.worker():
                    pass
            self.assertEqual(fixture.cloud.queued, ["must-not-ack-new-owner"])
            self.assertEqual(len(fixture.cloud.states), state_count)
            self.assertFalse(Journal(fixture.config.db_path).kill_switch()["enabled"])
            with WorkerLease(fixture.config.db_path):
                pass

    def test_stale_quote_and_heartbeat_never_inherit_connected_status(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.quote_delay = 6
            with fixture.worker() as worker:
                result = worker.cycle()
                self.assertIsNone(result["error_code"], result)
                self.assertFalse(result["data_feed"]["verified"])
                self.assertIsNotNone(result["data_feed"]["as_of"])
                self.assertFalse(fixture.cloud.states[-1]["data_verified"])
                self.assertIn("complete_current_session_pnl_and_fee_ledger_missing", result["session"]["errors"])
                self.assertIsNone(result["session"]["account"]["fees"])
                fixture.venue.advance(31)
                self.assertFalse(worker.state()["paper_connected"])
                self.assertFalse(worker.state()["data_connected"])

    def test_scheduler_stops_cooperatively_without_catch_up_burst(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, history=False)
            waits = []
            with fixture.worker() as worker:
                def wait(seconds):
                    waits.append(seconds)
                    worker.stop_event.set()
                    return True
                worker.stop_event.wait = wait
                stamps = iter([0.0, 100.0])
                count = worker.run(elapsed_clock=lambda: next(stamps))
                self.assertEqual(count, 1)
                self.assertEqual(waits, [1.0])

    def test_readiness_requires_a_running_current_worker_not_persisted_history(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            with fixture.worker() as worker:
                app = Application(fixture.config, observation_worker=worker)
                self.assertFalse(app.readiness()["paper_connected"])
                worker.cycle()
                self.assertTrue(app.readiness()["paper_connected"])
                self.assertFalse(app.readiness()["paper_execution_running"])
            self.assertFalse(app.readiness()["paper_connected"])
            self.assertFalse(Application(fixture.config).readiness()["paper_connected"])

    def test_account_read_alone_does_not_verify_broker_when_positions_collection_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            def unavailable():
                raise BrokerError("INVENTED provider failure")
            fixture.client.positions = unavailable
            with fixture.worker() as worker:
                result = worker.cycle()
                self.assertGreater(fixture.account_reads, 0)
                self.assertFalse(result["broker"]["verified"])
                self.assertFalse(fixture.cloud.states[-1]["broker_verified"])
                self.assertFalse(worker.state()["paper_connected"])

    def test_real_publisher_contract_in_full_worker_flow_with_invented_cloud_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            owner = "00000000-0000-4000-8000-000000000003"
            fixture.config = replace(fixture.config, supabase_url="https://fixture.supabase.co",
                supabase_service_role_key="sb_secret_FAKE_TEST_ONLY", owner_user_id=owner)
            command = {"id": "00000000-0000-4000-8000-000000000004", "owner_id": owner,
                "kind": "halt_new_entries", "nonce": "00000000-0000-4000-8000-000000000005",
                "status": "queued", "created_at": fixture.venue.now.isoformat(), "acknowledged_at": None, "error": None}
            published = []
            def cloud(method, url, headers, payload):
                self.assertNotIn("Authorization", headers)
                self.assertEqual(headers["apikey"], "sb_secret_FAKE_TEST_ONLY")
                if method == "GET" and urlsplit(url).path.endswith("paper_commands"):
                    return [deepcopy(command)] if command["status"] == "queued" else []
                if method == "PATCH":
                    self.assertTrue(Journal(fixture.config.db_path).kill_switch()["enabled"])
                    command.update(payload)
                    return [deepcopy(command)]
                if method == "POST" and urlsplit(url).path.endswith("worker_state"):
                    published.append(deepcopy(payload))
                    return [deepcopy(payload)]
                raise AssertionError("Unexpected invented cloud operation")
            publisher = Publisher(fixture.config, transport=cloud, clock=lambda: fixture.venue.now)
            with ObservationWorker(fixture.config, provider=fixture.client, snapshot_builder=fixture,
                    publisher=publisher, clock=lambda: fixture.venue.now) as worker:
                result = worker.cycle()
                self.assertTrue(result["cloud_state_published"], result)
                self.assertEqual(command["status"], "acknowledged")
                self.assertEqual(published[-1]["status"], "halted")
                self.assertEqual(published[-1]["payload"]["broker"]["status"], "connected")
                self.assertEqual(published[-1]["payload"]["data_feed"]["status"], "connected")
                self.assertFalse(published[-1]["payload"]["execution_enabled"])
                self.assertNotIn("FAKE_TEST_ONLY", json.dumps(published))
            self.assertEqual(published[-1]["payload"]["broker"]["status"], "disconnected")
            self.assertNotIn("ready", [row["status"] for row in published])

    def test_accounting_configuration_does_not_invent_a_policy_or_naive_anchor(self):
        default = Config.from_env({})
        self.assertEqual(default.accounting_cost_policy, "actual")
        self.assertEqual(default.accounting_history_start, "")
        for values in ({"ACCOUNTING_COST_POLICY": "zero"},
                       {"ACCOUNTING_HISTORY_START": "2026-10-16"},
                       {"ACCOUNTING_BASELINE_PATH": "/tmp/unbound.json"}):
            with self.subTest(values=values), self.assertRaises(ConfigError):
                Config.from_env(values)


if __name__ == "__main__":
    unittest.main()
