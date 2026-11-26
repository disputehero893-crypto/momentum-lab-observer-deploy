"""Offline branch coverage for decision_replay (tests only; synthetic data, no network)."""
import copy
import io
import json
import os
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timezone
import tempfile
import threading
import unittest
from unittest.mock import patch

from momentum import decision_replay as replay
from momentum import observation
from momentum.alpaca import PaperDisabled
from momentum.observation import ObservationError
from test_worker_observation import ACCOUNT, OTHER_ACCOUNT as OTHER, Fixture


WORKER = Path(__file__).resolve().parents[1]
EXAMPLE = WORKER / "examples" / "decision-replay.synthetic.json"


def document():
    value = json.loads(EXAMPLE.read_bytes())
    value["frames"] = value["frames"][:1]
    return value


def encode(value):
    return json.dumps(value).encode("utf-8")


class DecisionReplayBranchTests(unittest.TestCase):
    def code(self, func, *args, **kwargs):
        with self.assertRaises(replay.ReplayError) as caught:
            func(*args, **kwargs)
        return caught.exception.code

    def test_unknown_error_code_collapses_to_replay_failed(self):
        self.assertEqual(replay.ReplayError("not_a_code").code, "replay_failed")

    def test_canonical_rejects_unserialisable_values(self):
        self.assertEqual(self.code(replay._canonical, {"x": object()}), "invalid_json")
        self.assertEqual(self.code(replay._canonical, float("nan")), "invalid_json")

    def test_depth_check_rejects_unbalanced_closers_and_ignores_quoted_braces(self):
        self.assertEqual(self.code(replay._depth_check, "]"), "invalid_json")
        replay._depth_check('{"a": "}}}\\"]]"}')  # quoted/escaped braces are not structure

    def test_decode_requires_bytes_and_rejects_bad_utf8(self):
        self.assertEqual(self.code(replay._decode, "{}"), "invalid_json")
        self.assertEqual(self.code(replay._decode, b"\xff\xfe"), "invalid_json")

    def test_check_time_optional_none_and_unparseable_values(self):
        self.assertIsNone(replay._check_time(None))
        self.assertEqual(self.code(replay._check_time, None, required=True), "timestamp_invalid")
        self.assertEqual(self.code(replay._check_time, 5), "timestamp_invalid")
        self.assertEqual(self.code(replay._check_time, "25:99", clock=True), "timestamp_invalid")
        self.assertEqual(self.code(replay._check_time, "2026-13-45T00:00:00Z"), "timestamp_invalid")

    def test_configuration_type_rules(self):
        self.assertEqual(self.code(replay._configuration, []), "strategy_config_invalid")
        self.assertEqual(self.code(replay._configuration, {"nope": 1}), "strategy_config_invalid")
        from dataclasses import fields
        from decimal import Decimal
        from momentum.strategy import StrategyConfig
        defaults = StrategyConfig()
        int_field = next(f.name for f in fields(StrategyConfig) if type(getattr(defaults, f.name)) is int)
        dec_field = next(f.name for f in fields(StrategyConfig) if isinstance(getattr(defaults, f.name), Decimal))
        self.assertEqual(self.code(replay._configuration, {int_field: "1"}), "strategy_config_invalid")
        self.assertEqual(self.code(replay._configuration, {dec_field: 1}), "strategy_config_invalid")
        self.assertEqual(self.code(replay._configuration, {"trade_start_et": 930}), "strategy_config_invalid")
        self.assertEqual(self.code(replay._configuration, {"trade_start_et": "9h30"}), "timestamp_invalid")
        self.assertEqual(self.code(replay._configuration, {dec_field: "not-a-number"}), "strategy_config_invalid")

    def test_provenance_marker_and_snapshot_flag_branches(self):
        p = replay._provenance
        self.assertEqual(self.code(p, {"provenance": {"kind": 7}}, "synthetic"), "provenance_invalid")
        self.assertEqual(self.code(p, {"provenance": {"kind": "provider_capture"}}, "synthetic"),
                         "provenance_mismatch")
        self.assertEqual(self.code(p, {"provenance": {"synthetic": "yes"}}, "synthetic"), "provenance_invalid")
        self.assertEqual(self.code(p, {"provenance": "synthetic"}, "provider_capture"), "provenance_mismatch")
        self.assertEqual(self.code(p, {"feed": "demo"}, "provider_capture"), "provenance_mismatch")
        self.assertEqual(self.code(p, {"synthetic": 1}, "synthetic"), "provenance_invalid")
        self.assertEqual(self.code(p, {"synthetic": False}, "synthetic"), "provenance_mismatch")
        self.assertIsNone(p({"provenance": {"kind": "synthetic", "synthetic": True}, "synthetic": True},
                            "synthetic"))

    def test_frame_time_non_precision_errors_become_frame_time_invalid(self):
        value = document()
        value["frames"][0]["snapshot"]["now"] = "yesterday"
        self.assertEqual(self.code(replay.replay_bytes, encode(value)), "frame_time_invalid")

    def test_read_file_branches(self):
        self.assertEqual(self.code(replay._read_file, "/nonexistent/decision-replay.json"),
                         "input_read_failed")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.code(replay._read_file, tmp), "input_not_regular_file")
            path = Path(tmp) / "in.json"
            path.write_bytes(b"{}")
            with patch.object(replay, "MAX_INPUT_BYTES", 1):
                self.assertEqual(self.code(replay._read_file, path), "input_too_large")
            # File grows between fstat and read: the post-read bound still applies.
            real_fstat = os.fstat

            class Small:
                def __init__(self, info):
                    self.st_mode, self.st_size = info.st_mode, 0

            with patch.object(replay, "MAX_INPUT_BYTES", 1), \
                    patch.object(replay.os, "fstat", lambda fd: Small(real_fstat(fd))):
                self.assertEqual(self.code(replay._read_file, path), "input_too_large")
            with patch.object(replay.os, "fdopen", side_effect=OSError("boom")), \
                    patch.object(replay.os, "close", wraps=os.close) as closer:
                self.assertEqual(self.code(replay._read_file, path), "input_read_failed")
                closer.assert_called_once()

    def test_create_output_write_failure_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            with patch.object(replay.os, "link", side_effect=OSError("no link")):
                self.assertEqual(self.code(replay._create_output, out, b"{}"), "output_write_failed")
            self.assertEqual(os.listdir(tmp), [])
            with patch.object(replay.os, "link", side_effect=OSError("no link")), \
                    patch.object(replay.os, "unlink", side_effect=OSError("busy")):
                self.assertEqual(self.code(replay._create_output, out, b"{}"), "output_write_failed")

    def test_replay_file_and_main_in_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.json"
            src.write_bytes(encode(document()))
            out = Path(tmp) / "out.json"
            summary = replay.replay_file(src, out)
            self.assertEqual(summary["frame_count"], 1)
            self.assertEqual(set(summary), {"frame_count", "config_id", "input_sha256"})
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                self.assertEqual(replay.main([str(src), "--output", str(Path(tmp) / "o2.json")]), 0)
                self.assertEqual(replay.main([str(src), "--output", str(out)]), 2)
                self.assertEqual(replay.main([]), 2)
                with patch.object(replay, "replay_file", side_effect=RuntimeError("secret /path")):
                    self.assertEqual(replay.main([str(src), "--output", str(Path(tmp) / "o3.json")]), 2)
            self.assertIn("decision_replay_complete", stdout.getvalue())
            err = stderr.getvalue()
            self.assertIn("decision_replay_error: output_exists", err)
            self.assertIn("decision_replay_error: invalid_arguments", err)
            self.assertIn("decision_replay_error: replay_failed", err)
            self.assertNotIn("secret", err)


class ObservationHelperBranchTests(unittest.TestCase):
    """observation.py helper guards: paper-only, read-only, fail-closed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = self._tmp.name
        self.fixture = Fixture(self.directory)
        self.config = self.fixture.config

    def tearDown(self):
        self._tmp.cleanup()

    def non_paper(self):
        # Config itself refuses "live"; force a non-paper mode past it to prove
        # the observation layer independently fails closed.
        cfg = copy.copy(self.config)
        object.__setattr__(cfg, "mode", "live")
        return cfg

    def test_read_only_client_requires_paper_mode_credentials_and_not_stopping(self):
        for cfg in (self.non_paper(), replace(self.config, api_key=""),
                    replace(self.config, api_secret="")):
            with self.assertRaises(PaperDisabled):
                observation.ReadOnlyPaperClient(cfg, transport=self.fixture.request)._enabled()
        stop = threading.Event()
        client = observation.ReadOnlyPaperClient(self.config, transport=self.fixture.request, stop_event=stop)
        client._enabled()
        stop.set()
        with self.assertRaises(PaperDisabled):
            client._enabled()
        with self.assertRaises(PaperDisabled):
            client._request("GET", "/v2/account", payload={"x": 1})

    def test_read_only_provider_denies_mutations_and_stops(self):
        stop = threading.Event()
        provider = observation._ReadOnlyProvider(self.fixture.client, stop)
        for name in ("submit", "cancel", "close_position", "replace"):
            with self.assertRaises(PaperDisabled):
                getattr(provider, name)
        self.assertEqual(provider.account()["id"], ACCOUNT)
        stop.set()
        with self.assertRaises(PaperDisabled):
            provider.account()

    def test_fresh_is_false_for_unparseable_future_or_stale_stamps(self):
        now = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)
        self.assertFalse(observation._fresh("not-a-time", now, 30))
        self.assertFalse(observation._fresh(None, now, 30))
        self.assertFalse(observation._fresh("2026-10-16T14:00:10Z", now, 30))
        self.assertFalse(observation._fresh("2026-10-16T13:00:00Z", now, 30))
        self.assertTrue(observation._fresh("2026-10-16T13:59:50Z", now, 30))

    def test_account_identity_rejects_malformed_and_blocked_accounts(self):
        good = {"id": ACCOUNT, "equity": "100", "buying_power": "50", "status": "ACTIVE",
                "trading_blocked": False, "account_blocked": False}
        self.assertEqual(observation._account_identity(good), ACCOUNT)
        invalid = [None, [], {**good, "id": "nope"}, {**good, "id": ACCOUNT.replace("-", "")},
                   {**good, "equity": "-1"}, {**good, "buying_power": "NaN"},
                   {k: v for k, v in good.items() if k != "equity"}, {**good, "equity": "x"}]
        for account in invalid:
            with self.assertRaises(ObservationError) as caught:
                observation._account_identity(account)
            self.assertEqual(str(caught.exception), "invalid_paper_account_response")
        blocked = [{**good, "status": "SUSPENDED"}, {**good, "trading_blocked": True},
                   {**good, "account_blocked": None}, {**good, "trade_suspended_by_user": True}]
        for account in blocked:
            with self.assertRaises(ObservationError) as caught:
                observation._account_identity(account)
            self.assertEqual(str(caught.exception), "paper_account_not_active_or_blocked")

    def test_baseline_requires_exact_matching_operator_evidence(self):
        start = "2026-10-16T00:00:00Z"
        self.assertIsNone(observation._baseline("", account_id=ACCOUNT, window_start=start))
        good = {"schema_version": "1.0", "account_id": ACCOUNT, "environment": "paper",
                "window_start": start, "evidence": "INVENTED TEST evidence", "opening_lots": []}
        path = Path(self.directory) / "baseline.json"
        path.write_text(json.dumps(good))
        self.assertEqual(observation._baseline(str(path), account_id=ACCOUNT, window_start=start), good)
        bad = [{**good, "extra": 1}, {**good, "schema_version": "2.0"}, {**good, "account_id": OTHER},
               {**good, "environment": "live"}, {**good, "window_start": "2026-10-17T00:00:00Z"},
               {**good, "evidence": "  "}, {**good, "evidence": "x" * 2001}, {**good, "opening_lots": {}}, []]
        for value in bad:
            path.write_text(json.dumps(value))
            with self.assertRaises(ObservationError) as caught:
                observation._baseline(str(path), account_id=ACCOUNT, window_start=start)
            self.assertEqual(str(caught.exception), "accounting_baseline_invalid_or_mismatched")
        path.write_bytes(b" " * 262_145)
        with self.assertRaises(ObservationError):
            observation._baseline(str(path), account_id=ACCOUNT, window_start=start)
        with self.assertRaises(ObservationError):
            observation._baseline(str(Path(self.directory) / "missing.json"), account_id=ACCOUNT,
                                  window_start=start)

    def test_worker_constructor_refuses_live_mode_and_partial_cloud_config(self):
        with self.assertRaises(ObservationError) as caught:
            observation.ObservationWorker(self.non_paper(), provider=self.fixture.client)
        self.assertEqual(str(caught.exception), "observation_requires_explicit_paper_mode")
        partial = replace(self.config, supabase_url="https://example.invalid",
                          supabase_service_role_key="", owner_user_id="")
        with self.assertRaises(ObservationError) as caught:
            observation.ObservationWorker(partial, provider=self.fixture.client, snapshot_builder=self.fixture)
        self.assertEqual(str(caught.exception), "incomplete_cloud_worker_configuration")

    def test_run_requires_active_lease_and_positive_cycle_count(self):
        worker = self.fixture.worker()
        with self.assertRaises(ObservationError):
            worker.run(max_cycles=1)
        with worker:
            for bad in (0, -1, 1.5, True):
                with self.assertRaises(ValueError):
                    worker.run(max_cycles=bad)
            waits = []
            worker.stop_event.wait = lambda delay: waits.append(delay)
            self.assertEqual(worker.run(max_cycles=2, elapsed_clock=lambda: 0.0), 2)
            self.assertEqual(waits, [max(1.0, float(self.config.poll_seconds))])


if __name__ == "__main__":
    unittest.main()
