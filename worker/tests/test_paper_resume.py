"""Authenticated paper-only resume of a latched kill switch (invented data only)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from momentum import resume
from momentum.config import Config
from momentum.journal import Journal
from momentum.publisher import Publisher, PublisherError, build_worker_state
from momentum.server import Application
from worker_fakes import armed_config

OWNER = "9d59c43f-1d43-494e-afd9-1f73edabb444"
NOW = datetime(2026, 10, 16, 13, 30, tzinfo=timezone.utc)
SECRET = "sb_secret_FAKE_TEST_ONLY"
CLOUD = Config(supabase_url="https://project.supabase.co", supabase_service_role_key=SECRET,
               owner_user_id=OWNER)
RESUME = {"id": "11111111-1111-4111-8111-111111111111", "owner_id": OWNER, "kind": "resume_entries",
          "nonce": "22222222-2222-4222-8222-222222222222", "status": "queued",
          "created_at": "2026-10-16T13:29:00Z", "acknowledged_at": None, "error": None}


class Cloud:
    def __init__(self, rows, *, fail_get=False, fail_patch=False):
        self.rows, self.fail_get, self.fail_patch = rows, fail_get, fail_patch
        self.calls = []

    def __call__(self, method, url, headers, payload):
        query = parse_qs(urlsplit(url).query)
        self.calls.append((method, query, payload))
        if method == "GET":
            if self.fail_get:
                raise PublisherError()
            return [dict(r) for r in self.rows if "eq." + r["kind"] in query["kind"]]
        if method == "PATCH":
            if self.fail_patch:
                raise PublisherError()
            row = next(r for r in self.rows if "eq." + r["id"] in query["id"])
            return [{**row, **payload}]
        raise AssertionError("unexpected request")


class ResumeChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name) / "j.sqlite3")
        self.journal.set_kill_switch(True, "Cloud command channel unavailable", request_id="poll-failed")

    def publisher(self, cloud):
        return Publisher(CLOUD, transport=cloud, clock=lambda: NOW)

    def test_allowed_resume_clears_latch_and_acknowledges(self):
        cloud = Cloud([RESUME])
        with patch("builtins.print"):
            out = self.publisher(cloud).process_resumes(self.journal, lambda created: None)
        self.assertEqual(out, [(RESUME["id"], "resumed")])
        self.assertFalse(self.journal.kill_switch()["enabled"])
        self.assertTrue(self.journal.kill_switch()["reason"].startswith("operator_resume_entries:"))
        method, query, payload = cloud.calls[-1]
        self.assertEqual(method, "PATCH")
        self.assertEqual(payload["status"], "acknowledged")
        self.assertEqual(query["kind"], ["eq.resume_entries"])
        self.assertEqual(query["owner_id"], ["eq." + OWNER])

    def test_refused_resume_keeps_latch_and_records_reason(self):
        cloud = Cloud([RESUME])
        with patch("builtins.print"):
            out = self.publisher(cloud).process_resumes(self.journal, lambda created: "config_hash_mismatch")
        self.assertEqual(out, [(RESUME["id"], "config_hash_mismatch")])
        self.assertTrue(self.journal.kill_switch()["enabled"])
        self.assertEqual(cloud.calls[-1][2], {"status": "failed", "acknowledged_at": "2026-10-16T13:30:00.000000Z",
                                               "error": "config_hash_mismatch"})

    def test_unknown_refusal_code_is_collapsed(self):
        with patch("builtins.print"):
            out = self.publisher(Cloud([RESUME])).process_resumes(self.journal, lambda c: "anything else")
        self.assertEqual(out[0][1], "resume_check_failed")
        self.assertTrue(self.journal.kill_switch()["enabled"])

    def test_channel_failure_or_bad_rows_never_resume(self):
        bad_rows = [{**RESUME, "owner_id": "baaaaedd-82bd-4d00-baa1-2d5bc6c0c111"},
                    {**RESUME, "status": "acknowledged"}, {**RESUME, "extra": 1}]
        for cloud in [Cloud([RESUME], fail_get=True)] + [Cloud([row]) for row in bad_rows]:
            out = self.publisher(cloud).process_resumes(self.journal, lambda c: None)
            self.assertEqual(out, [(None, "resume_channel_unavailable")])
            self.assertTrue(self.journal.kill_switch()["enabled"])

    def test_ack_failure_keeps_local_resume_and_retry_is_idempotent(self):
        cloud = Cloud([RESUME], fail_patch=True)
        with patch("builtins.print"):
            self.publisher(cloud).process_resumes(self.journal, lambda c: None)
            self.assertFalse(self.journal.kill_switch()["enabled"])
            cloud.fail_patch = False
            # Even if prechecks would now refuse, the committed resume is only re-acknowledged.
            out = self.publisher(cloud).process_resumes(self.journal, lambda c: "resume_request_expired")
        self.assertEqual(out, [(RESUME["id"], "resumed")])
        self.assertEqual(cloud.calls[-1][2]["status"], "acknowledged")

    def test_later_halt_wins_over_committed_resume(self):
        cloud = Cloud([RESUME], fail_patch=True)
        with patch("builtins.print"):
            self.publisher(cloud).process_resumes(self.journal, lambda c: None)
            self.journal.set_kill_switch(True, "operator", request_id="later-halt")
            cloud.fail_patch = False
            out = self.publisher(cloud).process_resumes(self.journal, lambda c: None)
        self.assertEqual(out[0][1], "resume_check_failed")
        self.assertTrue(self.journal.kill_switch()["enabled"])

    def test_publisher_only_polls_known_command_kinds(self):
        publisher = self.publisher(Cloud([]))
        with self.assertRaises(PublisherError):
            publisher.request("GET", "paper_commands", {"owner_id": "eq." + OWNER, "kind": "eq.place_order"})


class ResumePrecheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = armed_config(self.temp.name)
        self.journal = Journal(Path(self.temp.name) / "j.sqlite3")
        self.journal.set_kill_switch(True, "cloud_command_dispatch_failed", request_id="d")

    def check(self, config=None, **overrides):
        args = dict(broker_verified=True, halt_seen=False, created_at=NOW - timedelta(minutes=1), now=NOW)
        args.update(overrides)
        return resume.resume_refusal(config or self.config, self.journal, **args)

    def test_all_checks_pass(self):
        self.assertIsNone(self.check())

    def test_refusals(self):
        self.assertEqual(self.check(replace(self.config, paper_reviewed_config_hash="0" * 64)), "config_hash_mismatch")
        self.assertEqual(self.check(replace(self.config, paper_reviewed_config_hash="")), "config_hash_mismatch")
        self.assertEqual(self.check(replace(self.config, mode="demo")), "not_paper_mode")
        self.assertEqual(self.check(broker_verified=False), "broker_paper_check_failed")
        self.assertEqual(self.check(halt_seen=True), "superseded_by_halt")
        self.assertEqual(self.check(created_at=NOW - timedelta(hours=1)), "resume_request_expired")
        self.assertEqual(self.check(created_at=NOW + timedelta(minutes=1)), "resume_request_from_future")

    def test_paper_blockers_refuse(self):
        cfg = replace(self.config, api_key="")
        cfg = replace(cfg, paper_reviewed_config_hash=cfg.review_hash())
        self.assertEqual(self.check(cfg), "paper_configuration_blocked")

    def test_live_host_refused(self):
        with patch.object(resume, "PAPER_API_HOST", "https://api.alpaca.markets"):
            self.assertEqual(self.check(), "live_execution_host_refused")
        with patch("momentum.crypto_execution.PAPER_HOST", "api.alpaca.markets"):
            self.assertEqual(self.check(), "live_execution_host_refused")

    def test_emergency_flatten_requires_review(self):
        self.journal.set_kill_switch(True, "autopilot_emergency_stop", flatten=True, request_id="e")
        self.assertEqual(self.check(), "emergency_flatten_requires_review")

    def test_unexpected_error_fails_closed(self):
        class Broken:
            mode = "paper"
            def review_hash(self):
                raise RuntimeError("boom")
        self.assertEqual(self.check(Broken()), "config_hash_mismatch")


class ReportingTests(unittest.TestCase):
    def test_reason_is_bounded_and_printable(self):
        self.assertEqual(resume.safe_reason("a\nb<script>" + "x" * 300)[:12], "a_b_script_x")
        self.assertLessEqual(len(resume.safe_reason("x" * 300)), 120)
        self.assertIsNone(resume.safe_reason(None))

    def test_config_report_statuses(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = armed_config(d)
            self.assertEqual(resume.config_report(cfg)["hash_status"], "match")
            self.assertEqual(resume.config_report(replace(cfg, paper_reviewed_config_hash="ab"))["hash_status"], "mismatch")
            unset = resume.config_report(replace(cfg, paper_reviewed_config_hash=""))
            self.assertEqual(unset["hash_status"], "unset")
            self.assertEqual(unset["computed_hash"], cfg.review_hash())
            self.assertNotIn("FAKE-SECRET", json.dumps(unset))

    def test_control_report_fails_closed(self):
        self.assertEqual(resume.control_report(None)["enabled"], True)

    def test_worker_state_carries_latch_and_hash_status(self):
        row = build_worker_state(OWNER, "instance-1", now=NOW, halted=True,
                                 kill_switch={"enabled": True, "flatten_requested": False, "reason": "bad\nreason"},
                                 paper_config={"hash_status": "mismatch", "computed_hash": "a" * 64,
                                               "blockers": ["exact_paper_configuration_not_reviewed"]})
        self.assertEqual(row["payload"]["kill_switch"], {"enabled": True, "flatten_requested": False,
                                                         "reason": "bad_reason"})
        self.assertEqual(row["payload"]["paper_config"]["hash_status"], "mismatch")
        self.assertEqual(row["payload"]["paper_config"]["computed_hash"], "a" * 64)
        weird = build_worker_state(OWNER, "instance-1", now=NOW,
                                   paper_config={"hash_status": "??", "computed_hash": "not-hex"})
        self.assertEqual(weird["payload"]["paper_config"]["hash_status"], "unavailable")
        self.assertIsNone(weird["payload"]["paper_config"]["computed_hash"])

    def test_health_reports_latch_and_hash_status_without_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = armed_config(d)
            app = Application(cfg)
            app.journal.set_kill_switch(True, "operator", request_id="h")
            health = app.health()
            self.assertEqual(health["kill_switch"]["reason"], "operator")
            self.assertTrue(health["kill_switch"]["enabled"])
            self.assertEqual(health["paper_config_hash_status"], "match")
            self.assertNotIn("FAKE-SECRET", json.dumps(health))
            self.assertNotIn("FAKE-KEY", json.dumps(health))


class ObservationResumeFlowTests(unittest.TestCase):
    """Full observer cycle with the real Publisher and an invented cloud."""

    def run_cycle(self, directory, *, armed, rows_kind="resume_entries", reviewed=None):
        from copy import deepcopy
        from test_worker_observation import Fixture
        from observer_fixtures import offline_observation_worker
        fixture = Fixture(directory, armed=armed)
        owner = "00000000-0000-4000-8000-000000000003"
        fixture.config = replace(fixture.config, supabase_url="https://fixture.supabase.co",
                                 supabase_service_role_key=SECRET, owner_user_id=owner)
        if reviewed is not None:
            fixture.config = replace(fixture.config, paper_reviewed_config_hash=reviewed)
        Journal(fixture.config.db_path).set_kill_switch(True, "Cloud command channel unavailable",
                                                        request_id="stale-poll-failure")
        command = {"id": "00000000-0000-4000-8000-000000000014", "owner_id": owner, "kind": rows_kind,
                   "nonce": "00000000-0000-4000-8000-000000000015", "status": "queued",
                   "created_at": (fixture.venue.now - timedelta(seconds=30)).isoformat(),
                   "acknowledged_at": None, "error": None}
        published = []

        def cloud(method, url, headers, payload):
            path, query = urlsplit(url).path, parse_qs(urlsplit(url).query)
            if path.endswith("paper_commands") and method == "GET":
                ok = command["status"] == "queued" and query["kind"] == ["eq." + command["kind"]]
                return [deepcopy(command)] if ok else []
            if path.endswith("paper_commands") and method == "PATCH":
                command.update(payload)
                return [deepcopy(command)]
            if method == "POST" and path.endswith("worker_state"):
                published.append(deepcopy(payload))
                return [deepcopy(payload)]
            raise AssertionError("Unexpected invented cloud operation")
        publisher = Publisher(fixture.config, transport=cloud, clock=lambda: fixture.venue.now)
        with patch("builtins.print"), offline_observation_worker(
                fixture.config, provider=fixture.client, snapshot_builder=fixture,
                publisher=publisher, clock=lambda: fixture.venue.now) as worker:
            result = worker.cycle()
        return result, command, published, fixture

    def test_verified_paper_cycle_resumes_and_publishes_clear_state(self):
        with tempfile.TemporaryDirectory() as d:
            result, command, published, fixture = self.run_cycle(d, armed=True)
            self.assertEqual(result["resume_requests"], [{"id": command["id"], "outcome": "resumed"}])
            self.assertEqual(command["status"], "acknowledged")
            self.assertFalse(result["halted"])
            self.assertFalse(Journal(fixture.config.db_path).kill_switch()["enabled"])
            payload = published[-1]["payload"]
            self.assertFalse(payload["halt_new_entries"])
            self.assertNotEqual(published[-1]["status"], "halted")
            self.assertEqual(payload["paper_config"]["hash_status"], "match")
            self.assertFalse(payload["kill_switch"]["enabled"])
            self.assertNotIn("FAKE", json.dumps(published))
            # Observation still never submits stock orders.
            self.assertEqual([c for c in fixture.venue.calls if c["method"] != "GET"], [])

    def test_hash_mismatch_refuses_and_reports(self):
        with tempfile.TemporaryDirectory() as d:
            result, command, published, _ = self.run_cycle(d, armed=True, reviewed="0" * 64)
            self.assertEqual(result["resume_requests"][0]["outcome"], "config_hash_mismatch")
            self.assertEqual((command["status"], command["error"]), ("failed", "config_hash_mismatch"))
            self.assertTrue(result["halted"])
            payload = published[-1]["payload"]
            self.assertTrue(payload["halt_new_entries"])
            self.assertEqual(payload["paper_config"]["hash_status"], "mismatch")
            self.assertRegex(payload["paper_config"]["computed_hash"], "^[0-9a-f]{64}$")
            self.assertEqual(payload["kill_switch"]["reason"], "Cloud command channel unavailable")

    def test_paper_trading_disabled_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            result, command, _, _ = self.run_cycle(d, armed=False)
            self.assertEqual(command["error"], "paper_configuration_blocked")
            self.assertTrue(result["halted"])


if __name__ == "__main__":
    unittest.main()
