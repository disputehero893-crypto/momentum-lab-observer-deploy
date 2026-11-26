"""Actual cycle provenance, using only invented Auth/DB/broker fixtures."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
import uuid

from momentum.customer_gateway import CustomerGateway
from test_customer_gateway import Fixture, ORIGIN, VERSION


class ActivationCycleBindingTests(Fixture):
    def setUp(self):
        super().setUp()
        self.start()

    def retry(self, request_id):
        return self.request("activate", {"config_version": VERSION, "mode": "paper"},
                            request_id=request_id)

    def make_active(self, request_id=None):
        self.assertEqual(self.activate(request_id)[0], 200)
        self.worker.cycle()
        self.assertEqual(self.request()[1]["status"], "active")

    def test_reactivation_requires_its_own_cycle_even_when_timestamps_equal(self):
        self.make_active()
        previous = deepcopy(self.worker.latest)
        cycles = len(self.worker.journal.events("autopilot", event_type="autopilot_cycle"))
        posts = len(self.f.posts())
        self.assertEqual(self.request("pause")[1]["status"], "paused")
        request_id = str(uuid.uuid4())
        result = self.activate(request_id)[1]
        command = self.worker.journal.latest("autopilot_command", "autopilot")
        self.assertEqual(command["timestamp"], previous["completed_at"])
        self.assertEqual(result["status"], "activating")
        self.assertEqual(self.request()[1]["status"], "activating")
        self.assertEqual(self.retry(request_id)[1]["status"], "activating")
        self.assertEqual(self.worker.state()["state"], "paused")
        self.assertFalse(self.worker.state()["entries_enabled"])
        self.assertEqual(self.worker.latest, previous)
        self.assertEqual(len(self.worker.journal.events("autopilot", event_type="autopilot_cycle")), cycles)
        self.assertEqual(len(self.f.posts()), posts)
        self.worker.cycle()
        self.assertEqual(self.worker.latest["command_event_id"], command["event_id"])
        self.assertEqual(self.request()[1]["status"], "active")

    def test_new_activation_without_pause_also_needs_new_cycle_but_duplicate_does_not(self):
        first = str(uuid.uuid4())
        self.make_active(first)
        self.assertEqual(self.retry(first)[1]["status"], "active")
        self.assertEqual(self.consent()[0], 200)
        self.assertEqual(self.request()[1]["status"], "active")
        second = str(uuid.uuid4())
        self.assertEqual(self.activate(second)[1]["status"], "activating")
        current = self.worker._control()
        retry = self.retry(first)[1]
        self.assertTrue(retry["command"]["duplicate"])
        self.assertEqual(retry["status"], "activating")
        self.assertEqual(self.worker._control(), current)
        self.worker.cycle()
        self.assertEqual(self.retry(first)[1]["status"], "active")

    def test_cycle_records_start_command_not_reentrant_end_command(self):
        self.make_active()
        before = self.worker.journal.latest("autopilot_command", "autopilot")
        build = self.worker.builder.build_watchlist

        def change_command_during_cycle():
            self.assertEqual(self.request("pause")[0], 200)
            self.assertEqual(self.activate()[0], 200)
            return build()

        self.worker.builder.build_watchlist = change_command_during_cycle
        try:
            self.worker.cycle()
        finally:
            self.worker.builder.build_watchlist = build
        after = self.worker.journal.latest("autopilot_command", "autopilot")
        self.assertNotEqual(before["event_id"], after["event_id"])
        self.assertEqual(self.worker.latest["command_event_id"], before["event_id"])
        self.assertEqual(self.request()[1]["status"], "activating")
        self.assertFalse(self.worker.state()["entries_enabled"])
        self.worker.cycle()
        self.assertEqual(self.worker.latest["command_event_id"], after["event_id"])
        self.assertEqual(self.request()[1]["status"], "active")

    def test_missing_foreign_or_prior_instance_provenance_cannot_project_active(self):
        self.make_active()
        original = deepcopy(self.worker.latest)
        for change in ({"command_event_id": None}, {"command_event_id": "foreign"},
                       {"instance_id": "prior-process"}):
            with self.subTest(change=change):
                self.worker.latest = {**original, **change}
                self.assertEqual(self.request()[1]["status"], "activating")
                self.assertFalse(self.worker.state()["entries_enabled"])
        self.worker.latest = dict(original)
        self.worker.latest.pop("command_event_id", None)
        self.assertEqual(self.request()[1]["status"], "activating")
        self.worker.latest = original
        self.assertEqual(self.request()[1]["status"], "active")

    def test_gateway_reconstruction_preserves_pending_state_without_a_new_cycle(self):
        self.make_active()
        self.request("pause")
        self.activate()
        original = deepcopy(self.worker.latest)
        self.gateway = CustomerGateway(self.worker, self.cloud, origin=ORIGIN)
        self.assertEqual(self.request()[1]["status"], "activating")
        self.assertEqual(self.worker.latest, original)

    def test_pause_and_latched_stop_win_over_pending_provenance(self):
        first = str(uuid.uuid4())
        self.make_active(first)
        self.activate()
        self.assertEqual(self.request("pause")[1]["status"], "paused")
        self.assertEqual(self.retry(first)[1]["status"], "paused")
        self.assertEqual(self.request("emergency-stop")[1]["status"], "stopped")
        self.assertEqual(self.retry(first)[1]["status"], "stopped")
        self.assertTrue(self.worker.journal.kill_switch()["enabled"])

    def test_provenance_downgrade_does_not_hide_bad_runtime_or_journal_failure(self):
        self.make_active()
        self.activate()
        original = deepcopy(self.worker.latest)
        for change in ({"reconciled": False}, {"blockers": ["unresolved"]},
                       {"completed_at": (self.f.venue.now - timedelta(seconds=31)).isoformat()}):
            with self.subTest(change=change):
                self.worker.latest = {**original, **change}
                self.assertEqual(self.request()[1]["status"], "error")
        self.worker.latest = original
        with patch.object(self.worker.journal, "latest", side_effect=RuntimeError("unavailable")):
            self.assertEqual(self.request(), (503, {"error": "operation_unconfirmed"}))

    def test_restarted_worker_has_no_active_projection_until_its_own_cycle(self):
        self.make_active()
        previous_instance = self.worker.instance_id
        self.worker.__exit__()
        restarted = self.f.runner()
        restarted.settings_loader = self.cloud.settings_for
        restarted.__enter__()
        self.addCleanup(restarted.__exit__)
        self.worker = restarted
        self.gateway = CustomerGateway(restarted, self.cloud, origin=ORIGIN)
        self.assertIsNone(restarted.latest)
        self.assertEqual(self.request()[1]["status"], "error")
        self.assertNotEqual(restarted.instance_id, previous_instance)
        restarted.cycle()
        current = restarted.journal.latest("autopilot_command", "autopilot")
        self.assertEqual(restarted.latest["command_event_id"], current["event_id"])
        self.assertEqual(restarted.latest["instance_id"], restarted.instance_id)
        self.assertEqual(self.request()[1]["status"], "active")
