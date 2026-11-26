"""Offline fail-closed coverage for publisher, observation, strategy_review and lease.

Every owner, account, command, quote and timestamp here is invented. Cloud and
broker transports are in-memory fakes, journals live in temporary SQLite files,
and urllib's opener is blocked so nothing can leave the process. No order is
ever submitted. Paper only.
"""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from momentum import publisher as pub
from momentum import strategy_review as sr
from momentum.alpaca import BrokerError
from momentum.config import Config
from momentum.journal import Journal
from momentum.lease import AlreadyRunning, WorkerLease
from momentum.observation import ObservationError
from momentum.publisher import Publisher, PublisherError, build_worker_state
from momentum.strategy import StrategyConfig
from test_strategy_review import NOW as REVIEW_NOW, OWNER as REVIEW_OWNER, invented_watch
from test_worker_observation import ACCOUNT, OTHER_ACCOUNT, Fixture
from test_worker_publisher import COMMAND, CONFIG, DEMO, NOW, OTHER, OWNER, Response


def block_network(test):
    blocker = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError(
        "Network access is forbidden in offline coverage tests"))
    blocker.start()
    test.addCleanup(blocker.stop)


class FakeHaltJournal:
    """In-memory journal whose latch can be forced to look unset."""

    def __init__(self, *, marker_enabled=True, latch_enabled=False):
        self.marker_enabled, self.latch_enabled = marker_enabled, latch_enabled
        self.set_calls = []

    def event_by_key(self, key):
        return {"payload": {"enabled": self.marker_enabled}}

    def set_kill_switch(self, enabled, reason, **kwargs):
        self.set_calls.append((enabled, reason, kwargs))

    def kill_switch(self):
        return {"enabled": self.latch_enabled}


# --------------------------------------------------------------------------
# Publisher
# --------------------------------------------------------------------------
class PublisherCoverageTests(unittest.TestCase):
    def setUp(self):
        block_network(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name) / "publisher.sqlite3")

    def publisher(self, transport, config=CONFIG):
        return Publisher(config, transport=transport, clock=lambda: NOW)

    def test_helpers_reject_noncanonical_uuid_unserialisable_json_and_garbled_legacy_keys(self):
        # 36 characters that uuid.UUID accepts, but with dashes in the wrong places.
        misplaced = OWNER.replace("-", "")
        misplaced = misplaced[:9] + "-" + misplaced[9:12] + "-" + misplaced[12:16] + "-" + misplaced[16:20] + "-" + misplaced[20:]
        self.assertEqual(len(misplaced), 36)
        for bad in (misplaced, OWNER[:-1], None):
            with self.subTest(uuid=bad), self.assertRaises(ValueError):
                pub._uuid(bad)
        self.assertEqual(pub._uuid(OWNER.upper()), OWNER)
        for code, expected in (("request_too_large", "request_invalid"), ("response_too_large", "response_invalid")):
            with self.subTest(code=code), self.assertRaises(PublisherError) as caught:
                pub._json_bytes({"x": object()}, 1000, code)
            self.assertEqual(caught.exception.code, expected)
        # Matches the three-segment shape but the claims segment is not UTF-8 JSON.
        self.assertFalse(pub._legacy_service_key("aaa.bbbb.ccc"))
        self.assertFalse(pub._legacy_service_key("aaa.e30.ccc"))  # "{}" has no role
        self.assertEqual(PublisherError("not_a_code").code, "publication_unavailable")

    def test_unparseable_project_urls_are_rejected_before_io(self):
        for url in ("https://[not-ipv6", "https://project.supabase.co:99999"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Publisher(replace(CONFIG, supabase_url=url))

    def test_http_empty_body_is_none_and_transport_exceptions_collapse(self):
        with patch("momentum.publisher.build_opener") as opener:
            opener.return_value.open.return_value = Response(b"")
            self.assertIsNone(Publisher(CONFIG).publish_demo(DEMO))
        with patch("momentum.publisher.build_opener") as opener:
            opener.return_value.open.side_effect = OSError("FAKE-UPSTREAM-DETAIL")
            with self.assertRaises(PublisherError) as caught:
                Publisher(CONFIG).publish_demo(DEMO)
        self.assertEqual(caught.exception.code, "publication_unavailable")
        self.assertNotIn("FAKE-UPSTREAM-DETAIL", str(caught.exception))

    def test_request_rejects_bad_params_prefer_owner_scope_and_payload_owner(self):
        calls = []
        publisher = self.publisher(lambda *args: calls.append(args))
        scoped = {"owner_id": "eq." + OWNER, "kind": "eq.halt_new_entries"}
        cases = (
            ("GET", "paper_commands", {**scoped, **{"p%d" % i: "x" for i in range(9)}}, None, "return=minimal"),
            ("GET", "paper_commands", {**scoped, "": "x"}, None, "return=minimal"),
            ("GET", "paper_commands", {**scoped, "x": 1.5}, None, "return=minimal"),
            ("GET", "paper_commands", "owner_id=eq.x", None, "return=minimal"),
            ("GET", "paper_commands", scoped, None, "return=minimal\r\nX-Injected: 1"),
            ("GET", "paper_commands", {**scoped, "owner_id": "eq." + OTHER}, None, "return=minimal"),
            ("GET", "paper_commands", {"owner_id": "eq." + OWNER, "kind": "eq.other"}, None, "return=minimal"),
            ("POST", "worker_state", None, {"owner_id": OTHER}, "return=minimal"),
            ("POST", "demo_runs", None, None, "return=minimal"),
        )
        for method, table, params, payload, prefer in cases:
            with self.subTest(table=table, params=params, prefer=prefer), self.assertRaises(PublisherError) as caught:
                publisher.request(method, table, params, payload, prefer=prefer)
            self.assertEqual(caught.exception.code, "request_invalid")
        self.assertEqual(calls, [])

    def test_demo_requires_bounded_keys_and_an_empty_minimal_response(self):
        calls = []
        publisher = self.publisher(lambda *args: calls.append(args))
        for bad in ({**DEMO, "run_key": ""}, {**DEMO, "run_key": "r" * 161},
                    {**DEMO, "strategy_version": "s" * 129}, {**DEMO, "strategy_version": 1}):
            with self.subTest(keys=sorted(bad.items())[:1]), self.assertRaises(ValueError):
                publisher.publish_demo(bad)
        self.assertEqual(calls, [])
        with self.assertRaises(PublisherError) as caught:
            self.publisher(lambda *args: []).publish_demo(DEMO)
        self.assertEqual(caught.exception.code, "response_invalid")

    def test_market_watch_feed_must_match_configuration_before_any_request(self):
        calls = []
        publisher = self.publisher(lambda *args: calls.append(args))
        self.assertEqual(publisher.data_feed, "iex")
        with self.assertRaises(ValueError):
            publisher.publish_worker_state(instance_id="feed-check", market_watch={}, data_feed="sip")
        self.assertEqual(calls, [])

    def test_history_collected_after_quotes_is_dropped_without_hiding_quotes(self):
        watch = invented_watch()
        kwargs = dict(now=REVIEW_NOW, data_feed="iex", market_watch=watch, allowed_symbols=("DEMO-A",),
                      price_history={"fixture": True})
        later = {"collected_at": "2026-10-16T12:00:00.654322Z"}
        with patch("momentum.publisher.sanitize_price_history", return_value=later):
            row = build_worker_state(REVIEW_OWNER, "history-order", **kwargs)
        self.assertNotIn("price_history", row["payload"])
        self.assertIn("market_watch", row["payload"])
        earlier = {"collected_at": watch["collected_at"]}
        with patch("momentum.publisher.sanitize_price_history", return_value=earlier):
            row = build_worker_state(REVIEW_OWNER, "history-order", **kwargs)
        self.assertEqual(row["payload"]["price_history"], earlier)
        self.assertFalse(row["payload"]["execution_enabled"])
        self.assertFalse(row["payload"]["capabilities"]["orders"])

    def test_halt_requires_enabled_marker_and_a_latch_that_reads_back_enabled(self):
        publisher = self.publisher(lambda *args: None)
        disabled_marker = FakeHaltJournal(marker_enabled=False, latch_enabled=True)
        with self.assertRaises(PublisherError) as caught:
            publisher._halt(disabled_marker, "fixture", "fixture halt")
        self.assertEqual(caught.exception.code, "local_halt_unavailable")
        never_latches = FakeHaltJournal(marker_enabled=True, latch_enabled=False)
        with self.assertRaises(PublisherError) as caught:
            publisher._halt(never_latches, "fixture", "fixture halt")
        self.assertEqual(caught.exception.code, "local_halt_unavailable")
        # Reassert was attempted (no flatten) before failing closed.
        self.assertEqual(len(never_latches.set_calls), 1)
        self.assertIs(never_latches.set_calls[0][2]["flatten"], False)

    def test_command_shape_and_acknowledgment_chronology(self):
        publisher = self.publisher(lambda *args: None)
        for bad in (None, [], {**COMMAND, "extra": 1}):
            with self.subTest(command=bad), self.assertRaises(ValueError):
                publisher._command(bad, NOW)
        acked = {**COMMAND, "status": "acknowledged", "acknowledged_at": "2026-10-16T13:29:58Z"}
        with self.assertRaises(ValueError):
            publisher._command(acked, NOW, acknowledged=True)  # ack precedes creation
        with self.assertRaises(ValueError):
            publisher._command({**acked, "acknowledged_at": "2026-10-16T13:30:06Z"}, NOW, acknowledged=True)
        with self.assertRaises(ValueError):
            publisher._command({**COMMAND, "acknowledged_at": "2026-10-16T13:30:00Z"}, NOW)
        ok = publisher._command({**acked, "acknowledged_at": "2026-10-16T13:30:00Z"}, NOW, acknowledged=True)
        self.assertEqual(ok["id"], COMMAND["id"])

    def test_ack_with_different_timestamp_is_not_confirmed_but_halt_stays_durable(self):
        calls = []

        def transport(method, url, headers, payload):
            calls.append(method)
            if method == "GET":
                return [deepcopy(COMMAND)]
            # Plausible but not the acknowledged_at this worker sent.
            return [{**COMMAND, "status": "acknowledged", "acknowledged_at": "2026-10-16T13:29:59.500000Z"}]
        with self.assertRaises(PublisherError) as caught:
            self.publisher(transport).acknowledge_halts(self.journal)
        self.assertEqual(caught.exception.code, "command_ack_unconfirmed")
        self.assertTrue(self.journal.kill_switch()["enabled"])
        self.assertEqual(calls, ["GET", "PATCH"])
        self.assertNotIn("POST", calls)


# --------------------------------------------------------------------------
# Strategy review
# --------------------------------------------------------------------------
class StrategyReviewCoverageTests(unittest.TestCase):
    def setUp(self):
        block_network(self)

    def test_setting_contract_bounds(self):
        self.assertEqual(sr._setting(Decimal("2.5")), "2.5")
        self.assertEqual(sr._setting(7), 7)
        self.assertEqual(sr._setting("09:30"), "09:30")
        for bad in (0, -1, 10**18 + 1, 1.5, None, "", "x" * 65, True):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                sr._setting(bad)

    def test_nanosecond_floor_rejects_nonfinite_and_out_of_range_limits(self):
        for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("0"), Decimal("-1"), Decimal("5.000000001")):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                sr._nanoseconds_floor(bad)
        self.assertEqual(sr._nanoseconds_floor(Decimal("5")), 5_000_000_000)
        self.assertEqual(sr._nanoseconds_floor(Decimal("5.000")), 5_000_000_000)

    def test_freshness_invalid_order_unparseable_and_stale(self):
        source, receipt = "2026-10-16T12:00:00.000000001Z", "2026-10-16T12:00:00.000000002Z"
        base = sr.timestamp_parts(source)[1] - 1
        kwargs = dict(collected_ns=base + 3, now_ns=base + 4, maximum_ns=10)
        self.assertEqual(sr._freshness(source, receipt, **kwargs), "fresh")
        self.assertEqual(sr._freshness(receipt, source, **kwargs), "invalid")
        self.assertEqual(sr._freshness("not-a-time", receipt, **kwargs), "invalid")
        self.assertEqual(sr._freshness(None, receipt, **kwargs), "invalid")
        self.assertEqual(sr._freshness(source, receipt, collected_ns=base + 3, now_ns=base + 50, maximum_ns=10), "stale")

    def test_review_requires_a_recorded_market_watch_mapping(self):
        for bad in (None, [], "market_watch", {"collected_at": 1}):
            with self.subTest(watch=bad), self.assertRaises(ValueError):
                sr.build_strategy_review(bad, StrategyConfig(), now=REVIEW_NOW)


# --------------------------------------------------------------------------
# Observation worker
# --------------------------------------------------------------------------
class ObservationCoverageTests(unittest.TestCase):
    def setUp(self):
        block_network(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = temp.name
        self.fixture = Fixture(self.directory)
        self.posts = []
        original = self.fixture.venue.request

        def guarded(method, url, headers, payload):
            if method != "GET":
                self.posts.append((method, url))
            return original(method, url, headers, payload)
        self.fixture.venue.request = guarded

    def tearDown(self):
        self.assertEqual(self.posts, [])

    def test_stopped_or_active_instance_cannot_be_reentered(self):
        worker = self.fixture.worker()
        with worker:
            with self.assertRaises(ObservationError) as caught:
                worker.__enter__()
            self.assertEqual(str(caught.exception), "observation_instance_cannot_be_reentered")
        with self.assertRaises(ObservationError):
            worker.__enter__()

    def test_journal_write_error_on_start_stays_up_and_releases_on_exit(self):
        worker = self.fixture.worker()
        with patch.object(worker.journal, "append", side_effect=sqlite3.OperationalError("database is locked")):
            worker.__enter__()
        self.assertTrue(worker._active)
        self.assertEqual(worker.journal.last_write_error, "journal_write_failed")
        self.assertFalse(worker.state()["paper_execution_running"])
        worker.__exit__(None, None, None)
        with self.fixture.worker() as again:
            self.assertTrue(again._active)

    def test_exit_swallows_publication_and_journal_failures(self):
        worker = self.fixture.worker()
        worker.__enter__()
        self.fixture.cloud.publish_worker_state = MagicMock(side_effect=RuntimeError("FAKE-CLOUD"))
        with patch.object(worker.journal, "append", side_effect=sqlite3.OperationalError("disk I/O error")):
            worker.__exit__(None, None, None)
        self.assertFalse(worker._active)
        self.assertTrue(worker.stop_event.is_set())
        self.fixture.cloud.publish_worker_state.assert_called_once()

    def test_failing_injected_builder_falls_back_to_collector_and_blocks_strategy(self):
        def broken():
            raise BrokerError("fixture builder failure")
        self.fixture.build_watchlist = broken
        with self.fixture.worker() as worker:
            sentinel = {"collected_at": "2026-10-16T13:35:05Z"}
            with patch.object(worker.market_collector, "collect", return_value=sentinel) as collect, \
                    patch("momentum.observation.market_data_evidence", return_value={"verified": False}):
                snapshots, data, blockers, watch = worker._snapshots()
            collect.assert_called_once()
        self.assertEqual(snapshots, [])
        self.assertIs(watch, sentinel)
        self.assertIn("strategy_inputs_unavailable", blockers)
        self.assertIn("current_same_feed_quotes_unavailable", blockers)

    def test_missing_watchlist_and_absent_publisher_branches(self):
        with self.fixture.worker() as worker:
            worker.config = replace(worker.config, watchlist=())
            with patch.object(worker.market_collector, "collect", return_value={}), \
                    patch("momentum.observation.market_data_evidence", return_value={"verified": True}):
                _, _, blockers, _ = worker._snapshots()
            self.assertIn("watchlist_missing", blockers)
            worker.publisher = None
            self.assertEqual(worker._commands(), [])

    def test_history_and_crypto_state_fail_closed(self):
        with self.fixture.worker() as worker:
            self.assertIsNone(worker._history_state({"not": "history"}, now=self.fixture.venue.now))
            watch = {"collected_at": "2026-10-16T13:35:00Z"}
            with patch("momentum.observation.sanitize_price_history",
                       return_value={"collected_at": "2026-10-16T13:35:01Z"}):
                self.assertIsNone(worker._history_state({}, now=self.fixture.venue.now, watch=watch))
            with patch("momentum.observation.sanitize_price_history",
                       return_value={"collected_at": "2026-10-16T13:34:59Z"}):
                self.assertEqual(worker._history_state({}, now=self.fixture.venue.now, watch=watch),
                                 {"collected_at": "2026-10-16T13:34:59Z"})
            self.assertIsNone(worker._crypto_state({"x": 1}, now=self.fixture.venue.now))  # no pairs configured
            worker.config = replace(worker.config, crypto_watchlist=("BTC/USD",))
            self.assertIsNone(worker._crypto_state({"not": "crypto"}, now=self.fixture.venue.now))

    def test_session_reporting_other_account_fails_cycle_without_verification(self):
        with self.fixture.worker() as worker:
            with patch.object(worker.session, "cycle", return_value={"broker_account_id": OTHER_ACCOUNT, "errors": []}):
                result = worker.cycle()
        self.assertEqual(result["error_code"], "session_evaluation_failed")
        self.assertEqual(result["broker"], {"verified": False, "checked_at": None})
        self.assertEqual(result["orders_submitted"], 0)
        self.assertFalse(result["execution_enabled"])

    def test_session_unverified_account_clears_broker_evidence(self):
        with self.fixture.worker() as worker:
            with patch.object(worker.session, "cycle", return_value={
                    "broker_account_id": ACCOUNT, "broker_account_verified": False, "errors": []}):
                result = worker.cycle()
        self.assertIsNone(result["error_code"], result)
        self.assertEqual(result["broker"], {"verified": False, "checked_at": None})

    def test_publication_failure_and_unconfigured_cloud_are_blockers(self):
        with self.fixture.worker() as worker:
            self.fixture.cloud.publish_worker_state = MagicMock(side_effect=RuntimeError("FAKE-CLOUD"))
            failed = worker.cycle()
            self.assertFalse(failed["cloud_state_published"])
            self.assertIn("cloud_state_publication_failed", failed["blockers"])
            worker.publisher = None
            self.fixture.venue.advance(1)
            local = worker.cycle()
        self.assertFalse(local["cloud_state_published"])
        self.assertIn("cloud_worker_not_configured", local["blockers"])
        self.assertEqual(local["orders_submitted"], 0)


# --------------------------------------------------------------------------
# Lease
# --------------------------------------------------------------------------
class LeaseCoverageTests(unittest.TestCase):
    def test_missing_flock_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"fcntl": None}):
            lease = WorkerLease(Path(directory) / "journal.sqlite3")
            with self.assertRaises(AlreadyRunning):
                lease.__enter__()
            self.assertIsNone(lease.handle)
            self.assertFalse(Path(lease.path).exists())


if __name__ == "__main__":
    unittest.main()
