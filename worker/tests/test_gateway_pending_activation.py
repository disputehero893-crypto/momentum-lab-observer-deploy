"""Pending paper activation stays consistent across receipts and state reads.

Uses only the existing fake Auth/DB/broker fixture. Run with the offline guard.
"""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
import uuid

from momentum.customer_gateway import CustomerGateway
from test_customer_gateway import Fixture, ORIGIN, OTHER, VERSION


class PendingActivationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.start()

    def retry_activation(self, request_id):
        return self.request("activate", {"config_version": VERSION, "mode": "paper"},
                            request_id=request_id)

    def test_refresh_and_duplicate_match_receipt_without_advancing_runtime(self):
        request_id = str(uuid.uuid4())
        status, receipt = self.activate(request_id)
        self.assertEqual((status, receipt["status"]), (200, "activating"))
        original = deepcopy(self.worker.latest)
        for _ in range(2):
            self.assertEqual(self.request()[1]["status"], "activating")
        duplicate = self.retry_activation(request_id)[1]
        self.assertTrue(duplicate["command"]["duplicate"])
        self.assertEqual(duplicate["status"], "activating")
        self.assertEqual(duplicate["runtime_reported_at"], original["completed_at"])
        self.assertEqual(self.worker.latest, original)
        self.assertEqual(len(self.worker.journal.events("autopilot", event_type="autopilot_command")), 1)
        self.assertEqual(self.f.posts(), [])
        self.worker.cycle()
        self.assertEqual(self.request()[1]["status"], "active")
        self.assertEqual(self.retry_activation(request_id)[1]["status"], "active")
        self.assertEqual(len(self.f.posts()), 1)

    def test_new_gateway_uses_current_durable_command_without_a_new_cycle(self):
        request_id = str(uuid.uuid4())
        self.activate(request_id)
        original = deepcopy(self.worker.latest)
        self.gateway = CustomerGateway(self.worker, self.cloud, origin=ORIGIN)
        self.assertEqual(self.request()[1]["status"], "activating")
        self.assertEqual(self.retry_activation(request_id)[1]["status"], "activating")
        self.assertEqual(self.worker.latest, original)
        self.assertEqual(self.f.posts(), [])

    def test_pending_activation_rejects_bad_or_unbound_observations(self):
        request_id = str(uuid.uuid4())
        self.activate(request_id)
        original = deepcopy(self.worker.latest)
        changes = (
            {"completed_at": (self.f.venue.now - timedelta(seconds=31)).isoformat()},
            {"completed_at": (self.f.venue.now + timedelta(seconds=1)).isoformat()},
            {"completed_at": "invalid"}, {"owner_id": OTHER}, {"account_id": OTHER},
            {"mode": "live"}, {"configuration_version": OTHER}, {"worker_review_hash": "wrong"},
            {"reconciled": False}, {"reconciled": 1}, {"blockers": ["unresolved"]},
            {"state": "blocked"},
        )
        for change in changes:
            with self.subTest(change=change):
                self.worker.latest = {**original, **change}
                self.assertEqual(self.request()[1]["status"], "error")
                self.assertEqual(self.retry_activation(request_id)[1]["status"], "error")
        self.worker.latest = None
        self.assertEqual(self.request()[1]["status"], "error")
        self.worker.latest = original
        self.worker.settings_blockers = ["settings_unavailable"]
        self.assertEqual(self.request()[1]["status"], "error")
        self.worker.settings_blockers = []
        with patch.object(type(self.worker.config), "paper_blockers", return_value=["review_required"]):
            self.assertEqual(self.request()[1]["status"], "error")
        self.worker.stop_event.set()
        self.assertEqual(self.request()[1]["status"], "error")
        self.worker.stop_event.clear()
        self.assertEqual(self.request()[1]["status"], "activating")
        self.assertEqual(self.f.posts(), [])

    def test_newer_pause_wins_over_old_activation_retry_before_cycle(self):
        request_id = str(uuid.uuid4())
        self.activate(request_id)
        self.assertEqual(self.request("pause")[1]["status"], "paused")
        self.assertEqual(self.retry_activation(request_id)[1]["status"], "paused")
        self.assertEqual(self.worker._control()["action"], "pause")
        self.assertEqual(len(self.worker.journal.events("autopilot", event_type="autopilot_command")), 2)
        self.assertEqual(self.f.posts(), [])

    def test_latched_stop_wins_over_old_activation_retry_before_cycle(self):
        request_id = str(uuid.uuid4())
        self.activate(request_id)
        self.assertEqual(self.request("emergency-stop")[1]["status"], "stopped")
        self.assertEqual(self.retry_activation(request_id)[1]["status"], "stopped")
        self.assertEqual(self.request()[1]["status"], "stopped")
        self.assertTrue(self.worker.journal.kill_switch()["enabled"])
        self.assertEqual(self.f.posts(), [])

    def test_refresh_does_not_extend_thirty_second_runtime_freshness(self):
        request_id = str(uuid.uuid4())
        self.activate(request_id)
        stamp = self.worker.latest["completed_at"]
        self.f.venue.advance(30)
        self.assertEqual(self.request()[1]["status"], "activating")
        self.f.venue.advance(0.001)
        result = self.retry_activation(request_id)[1]
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["runtime_reported_at"], stamp)
        self.assertFalse(result["runtime_fresh"])
        self.assertEqual(self.f.posts(), [])
