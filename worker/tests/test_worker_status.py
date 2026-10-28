from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from momentum.config import Config
from momentum.publisher import Publisher, PublisherError, build_worker_state


OWNER = "9d59c43f-1d43-494e-afd9-1f73edabb444"
NOW = datetime(2026, 10, 16, 13, 30, tzinfo=timezone.utc)
CONFIG = Config(supabase_url="https://project.supabase.co", supabase_service_role_key="sb_secret_FAKE_TEST_ONLY",
                owner_user_id=OWNER)


class WorkerStatusTests(unittest.TestCase):
    def state(self, **kwargs):
        return build_worker_state(OWNER, "observer-test-1", now=NOW, **kwargs)

    def evidence(self):
        return {"broker_verified": True, "broker_checked_at": NOW - timedelta(seconds=1),
                "data_verified": True, "data_feed": "iex", "data_as_of": NOW - timedelta(seconds=1),
                "data_received_at": NOW - timedelta(milliseconds=500)}

    def assert_no_execution(self, row):
        self.assertEqual(row["mode"], "paper")
        self.assertNotEqual(row["status"], "ready")
        payload = row["payload"]
        self.assertIs(payload["observation_only"], True)
        self.assertEqual(payload["trading"], "disabled")
        for key in ("execution_enabled", "entry_gate_open", "live_execution_supported"):
            self.assertIs(payload[key], False)
        self.assertIs(payload["capabilities"]["orders"], False)

    def test_defaults_are_disconnected_with_no_invented_data_time(self):
        row = self.state()
        self.assertEqual(row["status"], "disconnected")
        self.assertIsNone(row["data_as_of"])
        self.assertEqual(row["heartbeat_at"], "2026-10-16T13:30:00.000000Z")
        self.assertEqual(row["payload"]["broker"]["status"], "disconnected")
        self.assertEqual(row["payload"]["data_feed"]["status"], "disconnected")
        self.assert_no_execution(row)

    def test_fresh_provider_observations_match_frontend_connection_contract(self):
        row = self.state(**self.evidence())
        self.assertEqual(set(row), {"owner_id", "instance_id", "mode", "status", "heartbeat_at", "data_as_of", "payload"})
        self.assertEqual(row["status"], "observing")
        self.assertEqual(row["payload"]["broker"]["status"], "connected")
        self.assertEqual(row["payload"]["data_feed"]["status"], "connected")
        self.assertEqual(row["payload"]["broker"]["access"], "read_only")
        self.assertEqual(row["payload"]["data_feed"]["feed"], "iex")
        self.assert_no_execution(row)

    def test_broker_and_market_evidence_are_independent_and_require_attestation(self):
        cases = ((True, False, "connected", "disconnected"), (False, True, "disconnected", "connected"),
                 (False, False, "disconnected", "disconnected"))
        for broker, data, broker_status, data_status in cases:
            with self.subTest(broker=broker, data=data):
                row = self.state(**{**self.evidence(), "broker_verified": broker, "data_verified": data})
                self.assertEqual(row["payload"]["broker"]["status"], broker_status)
                self.assertEqual(row["payload"]["data_feed"]["status"], data_status)
                self.assert_no_execution(row)

    def test_stale_source_or_receipt_future_or_inconsistent_times_never_connect_data(self):
        cases = ((-3, -0.5), (-1, -3), (0.1, 0.2), (-1, 0.1), (-0.5, -1))
        for source_delta, receipt_delta in cases:
            with self.subTest(source_delta=source_delta, receipt_delta=receipt_delta):
                row = self.state(data_verified=True, data_feed="sip",
                                 data_as_of=NOW + timedelta(seconds=source_delta),
                                 data_received_at=NOW + timedelta(seconds=receipt_delta))
                self.assertEqual(row["payload"]["data_feed"]["status"], "disconnected")
                self.assertEqual(row["status"], "disconnected")
                self.assertIsNotNone(row["data_as_of"])

    def test_freshness_boundaries_are_inclusive_but_future_broker_is_not_fresh(self):
        for age, expected in ((30, "connected"), (30.000001, "disconnected"), (-0.000001, "disconnected")):
            row = self.state(broker_verified=True, broker_checked_at=NOW - timedelta(seconds=age))
            self.assertEqual(row["payload"]["broker"]["status"], expected)
        for age, expected in ((2, "connected"), (2.000001, "disconnected")):
            row = self.state(data_verified=True, data_feed="iex", data_as_of=NOW - timedelta(seconds=age),
                             data_received_at=NOW)
            self.assertEqual(row["payload"]["data_feed"]["status"], expected)

    def test_heartbeat_refresh_does_not_refresh_old_provider_evidence(self):
        old = self.evidence()
        row = build_worker_state(OWNER, "observer-test-1", now=NOW + timedelta(minutes=1), **old)
        self.assertEqual(row["heartbeat_at"], "2026-10-16T13:31:00.000000Z")
        self.assertEqual(row["data_as_of"], "2026-10-16T13:29:59.000000Z")
        self.assertEqual(row["status"], "disconnected")
        self.assertEqual(row["payload"]["broker"]["status"], "disconnected")

    def test_halt_and_safe_error_override_observing_without_inventing_flatness(self):
        row = self.state(**self.evidence(), error_code="market_data_unavailable")
        self.assertEqual(row["status"], "error")
        row = self.state(**self.evidence(), error_code="market_data_unavailable", halted=True)
        self.assertEqual(row["status"], "halted")
        self.assertIs(row["payload"]["halt_new_entries"], True)
        self.assertNotIn("flat", row["payload"])
        self.assert_no_execution(row)
        for raw in ("RAW_FAKE_SECRET sb_secret_FAKE_TEST_ONLY", {"error": "RAW_FAKE_SECRET"}, "__proto__"):
            row = self.state(error_code=raw)
            self.assertEqual(row["payload"]["last_error_code"], "observation_failed")
            self.assertNotIn("RAW_FAKE_SECRET", json.dumps(row))

    def test_invalid_attestations_and_unbounded_settings_are_rejected(self):
        cases = ({"broker_verified": "true"}, {"data_verified": 1}, {"halted": None},
                 {"broker_verified": True}, {"data_verified": True, "data_feed": "iex"},
                 {"data_feed": "synthetic"}, {"broker_checked_at": "2026-10-16T13:30:00"},
                 {"data_as_of": "not-a-time"})
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.state(**args)
        for maximum in (0, -1, 5.0001, float("inf"), float("nan"), True, "2"):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                self.state(max_data_age_seconds=maximum)
        with self.assertRaises(ValueError):
            build_worker_state(OWNER, "x" * 129, now=NOW)

    def test_publication_uses_owner_upsert_and_verifies_normalized_timestamps(self):
        calls = []
        def transport(method, url, headers, body):
            calls.append((method, url, headers, deepcopy(body)))
            row = deepcopy(body)
            row["heartbeat_at"] = row["heartbeat_at"].replace("Z", "+00:00")
            row["data_as_of"] = row["data_as_of"].replace("Z", "+00:00")
            return [row]
        publisher = Publisher(CONFIG, transport=transport, clock=lambda: NOW)
        row = publisher.publish_worker_state(instance_id="observer-test-1", **self.evidence())
        method, url, headers, body = calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(urlsplit(url).path, "/rest/v1/worker_state")
        self.assertEqual(parse_qs(urlsplit(url).query)["on_conflict"], ["owner_id"])
        self.assertEqual(headers["Prefer"], "resolution=merge-duplicates,return=representation")
        self.assertEqual(body["owner_id"], OWNER)
        self.assertEqual(row, body)
        self.assert_no_execution(row)

    def test_publisher_honors_configured_quote_age(self):
        publisher = Publisher(replace(CONFIG, quote_max_age_seconds=0.5),
                              transport=lambda method, url, headers, body: [deepcopy(body)], clock=lambda: NOW)
        row = publisher.publish_worker_state(instance_id="observer-test-1", **self.evidence())
        self.assertEqual(row["payload"]["data_feed"]["max_age_seconds"], 0.5)
        self.assertEqual(row["payload"]["data_feed"]["status"], "disconnected")

    def test_malformed_wrong_owner_or_invented_ready_echo_cannot_be_confirmed(self):
        def result_for(kind, body):
            if kind == "missing":
                return None
            if kind == "empty":
                return []
            if kind == "duplicate":
                return [deepcopy(body), deepcopy(body)]
            row = deepcopy(body)
            if kind == "owner":
                row["owner_id"] = "baaaaedd-82bd-4d00-baa1-2d5bc6c0c111"
            elif kind == "ready":
                row["status"] = "ready"
            elif kind == "payload":
                row["payload"]["execution_enabled"] = True
            elif kind == "timestamp":
                row["heartbeat_at"] = "2026-10-16T13:30:01Z"
            elif kind == "extra":
                row["raw_secret"] = "RAW_FAKE_SECRET"
            return [row]
        for kind in ("missing", "empty", "duplicate", "owner", "ready", "payload", "timestamp", "extra"):
            publisher = Publisher(CONFIG, transport=lambda m, u, h, b: result_for(kind, b), clock=lambda: NOW)
            with self.subTest(kind=kind), self.assertRaises(PublisherError) as caught:
                publisher.publish_worker_state(instance_id="observer-test-1")
            self.assertEqual(caught.exception.code, "response_invalid")
            self.assertNotIn("RAW_FAKE_SECRET", str(caught.exception))

    def test_transport_mutation_cannot_rewrite_expected_row(self):
        def transport(method, url, headers, body):
            body["status"] = "ready"
            return [body]
        publisher = Publisher(CONFIG, transport=transport, clock=lambda: NOW)
        with self.assertRaises(PublisherError):
            publisher.publish_worker_state(instance_id="observer-test-1")
