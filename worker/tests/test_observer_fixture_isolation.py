"""A fake publisher must not leave an independent real logger transport active."""
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from momentum.observation import ObservationWorker
from momentum.paper_run_logging import DisabledPaperRunLogger, PaperRunLogger
from offline_network import NetworkForbidden, NetworkGuard, offline_pythonpath
import test_worker_observation as observation_tests


class ObserverFixtureIsolationTests(unittest.TestCase):
    def configured_fixture(self, directory):
        fixture = observation_tests.Fixture(directory)
        fixture.config = replace(fixture.config, supabase_url="https://fixture.supabase.co",
            supabase_service_role_key="sb_secret_FAKE_TEST_ONLY", owner_user_id=observation_tests.ACCOUNT)
        return fixture

    def test_shared_fixture_disables_independent_logger_before_full_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.configured_fixture(directory)
            with patch.object(PaperRunLogger, "_http", side_effect=AssertionError("Real logger HTTP forbidden")) as http:
                with fixture.worker() as worker:
                    self.assertIsInstance(worker.paper_run_logger, DisabledPaperRunLogger)
                    self.assertFalse(worker.paper_run_logger.enabled)
                    self.assertIs(worker.publisher, fixture.cloud)
                    result = worker.cycle()
                    self.assertIsNone(result["error_code"])
                    self.assertTrue(result["cloud_state_published"])
                    self.assertFalse(result["execution_enabled"])
                    self.assertEqual(result["orders_submitted"], 0)
                self.assertEqual(http.call_count, 0)
            self.assertTrue(all(call["method"] == "GET" for call in fixture.venue.calls))

    def test_original_publisher_fixture_never_calls_real_logger_transport(self):
        case = observation_tests.ObservationTests("test_real_publisher_contract_in_full_worker_flow_with_invented_cloud_transport")
        with patch.object(PaperRunLogger, "_http", side_effect=AssertionError("Real logger HTTP forbidden")) as http:
            result = unittest.TestResult()
            case.run(result)
            self.assertTrue(result.wasSuccessful(), (result.errors, result.failures))
            self.assertEqual(http.call_count, 0)

    def test_production_constructor_still_selects_real_logger_without_constructor_io(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.configured_fixture(directory)
            with patch.object(PaperRunLogger, "_http", side_effect=AssertionError("Constructor I/O forbidden")) as http:
                worker = ObservationWorker(fixture.config, provider=fixture.client,
                    snapshot_builder=fixture, publisher=fixture.cloud, clock=lambda: fixture.venue.now)
                self.assertIsInstance(worker.paper_run_logger, PaperRunLogger)
                self.assertTrue(worker.paper_run_logger.enabled)
                self.assertIsNone(worker.paper_run_logger.run_id)
                self.assertEqual(http.call_count, 0)
                # Do not enter a production logger lifecycle with this fake URL.


class OfflineGuardTests(unittest.TestCase):
    def test_caught_synthetic_events_remain_failed_guard_evidence(self):
        for event in ("urllib.Request", "socket.getaddrinfo", "socket.connect", "socket.__new__"):
            with self.subTest(event=event):
                guard = NetworkGuard()
                try:
                    guard.audit(event, ())  # Synthetic event delivery only, no I/O.
                except NetworkForbidden:
                    pass
                self.assertEqual(guard.attempts, [event])
                with self.assertRaises(NetworkForbidden):
                    guard.assert_no_attempts()

    def test_child_guard_fails_even_when_synthetic_violation_is_caught(self):
        root = Path(__file__).resolve().parents[1]
        script = ("import sys\ntry:\n    sys.audit('socket.connect', None, None)\n"
                  "except Exception:\n    pass\n")
        result = subprocess.run([sys.executable, "-c", script], cwd=root,
            env={"PYTHONPATH": offline_pythonpath(root), "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("blocked 1 network attempt(s): socket.connect", result.stderr)

    def test_child_guard_is_present_before_application_imports(self):
        root = Path(__file__).resolve().parents[1]
        script = ("import sys, offline_network\n"
                  "assert offline_network._process_guard is not None\n"
                  "assert not any(x.startswith('momentum') for x in sys.modules)\n")
        result = subprocess.run([sys.executable, "-c", script], cwd=root,
            env={"PYTHONPATH": offline_pythonpath(root), "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
