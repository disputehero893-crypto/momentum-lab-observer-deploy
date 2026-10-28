"""Offline replay contract tests. Every price and setup in the fixture is invented."""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from momentum import decision_replay as replay
from momentum.models import plain
from momentum.strategy import StrategyConfig, evaluate_setup


WORKER = Path(__file__).resolve().parents[1]
EXAMPLE = WORKER / "examples" / "decision-replay.synthetic.json"


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def document():
    value = json.loads(EXAMPLE.read_bytes())
    value["frames"] = value["frames"][:1]
    return value


def at_path(value, path, replacement):
    current = value
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = replacement


class DecisionReplayTests(unittest.TestCase):
    def assert_error(self, code, raw):
        with self.assertRaises(replay.ReplayError) as caught:
            replay.replay_bytes(raw if isinstance(raw, bytes) else encode(raw))
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def cli(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, "-m", "momentum.decision_replay", *map(str, args)],
            cwd=cwd or WORKER, capture_output=True, text=True, timeout=10,
            env={"PYTHONPATH": str(WORKER), "PYTHONDONTWRITEBYTECODE": "1",
                 "PYTHONIOENCODING": "utf-8"})

    def test_example_is_exact_engine_output_with_explicit_nonexecution_flags(self):
        original = json.loads(EXAMPLE.read_bytes())
        report = json.loads(replay.replay_bytes(EXAMPLE.read_bytes()))
        self.assertEqual(report["kind"], "strategy_decision_replay_report")
        self.assertEqual(report["declared_provenance"], "synthetic")
        self.assertEqual(report["source_verification"], "not_performed")
        self.assertIs(report["execution_enabled"], False)
        self.assertIs(report["fills_simulated"], False)
        self.assertIs(report["performance_calculated"], False)
        self.assertEqual(report["risk_assessment"], "not_evaluated")
        self.assertEqual(report["frame_count"], 4)
        self.assertEqual(report["strategy_settings"], plain(asdict(StrategyConfig())))
        expected = ["first_pullback_breakout", "float_unconfirmed", "spread_too_wide", "lookahead_bar"]
        for before, after, reason in zip(original["frames"], report["frames"], expected):
            with self.subTest(identity=before["id"]):
                self.assertEqual(after["decision"], evaluate_setup(before["snapshot"]).to_dict())
                self.assertEqual(after["decision"]["reasons"], [reason])
                self.assertEqual(after["snapshot"], before["snapshot"])
                self.assertEqual(after["id"], before["id"])
                self.assertEqual(after["now"], before["snapshot"]["now"])
                self.assertNotIn("qty", after["decision"].get("intent") or {})

    def test_stable_bytes_and_canonical_hashes_ignore_only_json_layout(self):
        value = document()
        raw = encode(value)
        first = replay.replay_bytes(raw)
        self.assertEqual(first, replay.replay_bytes(raw))
        self.assertEqual(first, replay.replay_bytes(canonical(value)))
        self.assertEqual(first, replay.replay_bytes(json.dumps(value, sort_keys=True, indent=4).encode()))
        self.assertTrue(first.endswith(b"\n"))
        report = json.loads(first)
        self.assertEqual(first, canonical(report) + b"\n")
        self.assertEqual(report["input_sha256"], sha256(canonical(value)).hexdigest())
        self.assertEqual(report["config_id"], StrategyConfig().config_id)
        row = report["frames"][0]
        self.assertEqual(row["frame_sha256"], sha256(canonical(value["frames"][0])).hexdigest())
        self.assertEqual(row["snapshot_sha256"], sha256(canonical(value["frames"][0]["snapshot"])).hexdigest())
        value["frames"][0]["id"] = "different-case"
        changed = json.loads(replay.replay_bytes(encode(value)))
        self.assertNotEqual(changed["input_sha256"], report["input_sha256"])
        self.assertNotEqual(changed["frames"][0]["frame_sha256"], row["frame_sha256"])
        self.assertEqual(changed["frames"][0]["snapshot_sha256"], row["snapshot_sha256"])

    def test_supported_overrides_change_config_and_decision_without_mutation(self):
        value = document()
        before = deepcopy(value)
        default_cfg = plain(asdict(StrategyConfig()))
        original = json.loads(replay.replay_bytes(encode(value)))
        overridden = deepcopy(value)
        overridden["strategy_overrides"] = {"target_r": "3"}
        changed = json.loads(replay.replay_bytes(encode(overridden)))
        cfg = StrategyConfig(target_r="3")
        self.assertEqual(changed["config_id"], cfg.config_id)
        self.assertEqual(changed["strategy_settings"], plain(asdict(cfg)))
        self.assertNotEqual(original["config_id"], changed["config_id"])
        self.assertTrue(original["frames"][0]["decision"]["accepted"])
        self.assertFalse(changed["frames"][0]["decision"]["accepted"])
        self.assertEqual(changed["frames"][0]["decision"], evaluate_setup(value["frames"][0]["snapshot"], cfg).to_dict())
        self.assertEqual(changed["frames"][0]["snapshot_sha256"], original["frames"][0]["snapshot_sha256"])
        self.assertEqual(value, before)
        self.assertEqual(overridden["frames"], before["frames"])
        self.assertEqual(plain(asdict(StrategyConfig())), default_cfg)

    def test_stale_future_and_invalid_evidence_remain_real_engine_rejections(self):
        cases = [
            (("quote", "timestamp"), "2026-10-16T13:34:58Z", "stale_quote"),
            (("quote", "timestamp"), "2026-10-16T13:35:02Z", "future_quote"),
            (("bars", -1, "available_at"), "2026-10-16T13:35:01.000001Z", "lookahead_bar"),
            (("facts", "float_known_at"), "2026-10-16T13:36:00Z", "float_not_known_at_decision"),
            (("facts", "float_shares"), None, "float_unconfirmed"),
            (("quote", "ask"), "NaN", None),
        ]
        for path, replacement, reason in cases:
            with self.subTest(path=path, reason=reason):
                value = document()
                snapshot = value["frames"][0]["snapshot"]
                at_path(snapshot, path, replacement)
                if reason == "future_quote":
                    snapshot["quote"]["received_at"] = replacement
                actual = json.loads(replay.replay_bytes(encode(value)))["frames"][0]["decision"]
                self.assertEqual(actual, evaluate_setup(snapshot).to_dict())
                self.assertFalse(actual["accepted"])
                if reason:
                    self.assertIn(reason, actual["reasons"])

    def test_source_receipt_decision_strings_are_preserved_losslessly(self):
        value = document()
        snapshot = value["frames"][0]["snapshot"]
        snapshot["now"] = "2026-10-16T09:35:01.000000000-04:00"
        snapshot["quote"]["timestamp"] = "2026-10-16T13:35:01.000000000Z"
        snapshot["quote"]["received_at"] = "2026-10-16T13:35:01.000000000+00:00"
        snapshot["rvol"]["as_of"] = "2026-10-16T13:35:00.000000000Z"
        snapshot["rvol"]["history"][0]["clock"] = "09:35:00.000000000"
        value["strategy_overrides"] = {"trade_start_et": "07:00:00.000000000"}
        row = json.loads(replay.replay_bytes(encode(value)))["frames"][0]
        self.assertTrue(row["decision"]["accepted"])
        self.assertEqual(row["snapshot"], snapshot)
        self.assertEqual(row["now"], snapshot["now"])

    def test_all_engine_causal_timestamps_reject_unrepresentable_precision(self):
        timestamp_paths = [
            ("now",), ("quote", "timestamp"), ("quote", "received_at"),
            ("last_trade", "timestamp"), ("last_trade", "received_at"),
            ("bars", 0, "start"), ("bars", 0, "available_at"),
            ("facts", "float_effective_at"), ("facts", "float_known_at"),
            ("facts", "catalyst_published_at"), ("facts", "catalyst_known_at"),
            ("rvol", "as_of"), ("rvol", "history", 0, "known_at"),
        ]
        for path in timestamp_paths:
            with self.subTest(path=path):
                value = document()
                at_path(value["frames"][0]["snapshot"], path, "2026-10-16T13:35:01.0000001Z")
                self.assert_error("timestamp_precision_not_supported", value)
        for path in (("rvol", "history", 0, "clock"), ("rvol", "session_start_et")):
            with self.subTest(clock=path):
                value = document()
                at_path(value["frames"][0]["snapshot"], path, "09:35:00.123456700")
                self.assert_error("timestamp_precision_not_supported", value)
        for field in ("trade_start_et", "trade_end_et"):
            value = document()
            value["strategy_overrides"] = {field: "09:35:00.000000001"}
            self.assert_error("timestamp_precision_not_supported", value)

    def test_strict_timestamp_syntax_rejects_datetime_normalization_bypasses(self):
        invalid = ["2026-10-16T13:35:01", "2026-10-16 13:35:01Z", "2026-10-16T13:35:01+00:99",
                   "2026-10-16T13:35:01+24:00", "2026-10-16T13:35:01+00:00:00.1",
                   "20261016T133501Z", "2026-10-16T13:35:01,1234567Z", "2026-02-30T13:35:01Z",
                   "2026-10-16T13:35:01." + "0" * 64 + "Z", 123]
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                data = document()
                data["frames"][0]["snapshot"]["quote"]["timestamp"] = value
                self.assert_error("timestamp_invalid", data)
                data = document()
                data["frames"][0]["snapshot"]["now"] = value
                self.assert_error("frame_time_invalid", data)
        for field in ("trade_start_et", "trade_end_et"):
            value = document()
            value["strategy_overrides"] = {field: "07:00:00+00:00"}
            self.assert_error("timestamp_invalid", value)

    def test_chronology_is_global_nondecreasing_and_uses_offsets(self):
        value = document()
        second = deepcopy(value["frames"][0])
        second["id"] = "second"
        second["snapshot"]["symbol"] = "OTHER"
        second["snapshot"]["now"] = "2026-10-16T09:35:01-04:00"
        value["frames"].append(second)
        self.assertEqual(json.loads(replay.replay_bytes(encode(value)))["frame_count"], 2)
        second["snapshot"]["now"] = "2026-10-16T09:35:00.999999-04:00"
        self.assert_error("frame_time_out_of_order", value)
        second["snapshot"]["now"] = None
        self.assert_error("frame_time_invalid", value)

    def test_strict_wrappers_and_configuration_do_not_hide_unknown_fields(self):
        invalid = []
        for key, replacement in (("schema_version", "2.0"), ("kind", "backtest")):
            value = document()
            value[key] = replacement
            invalid.append(("input_schema_invalid", value))
        value = document(); value["unapproved"] = True
        invalid.append(("input_schema_invalid", value))
        value = document(); del value["provenance"]
        invalid.append(("input_schema_invalid", value))
        value = document(); value["frames"][0]["unapproved"] = True
        invalid.append(("frame_schema_invalid", value))
        value = document(); value["frames"][0]["snapshot"] = []
        invalid.append(("frame_schema_invalid", value))
        for overrides in ({"unapproved": "1"}, {"mode": "live"}, {"profile": "exact_ross"},
                          {"min_price": 2}, {"max_hold_seconds": "600"}, {"max_hold_seconds": True},
                          {"min_price": "NaN"}, {"target_r": "1.9"}, {"rvol_sessions": 20}):
            value = document(); value["strategy_overrides"] = overrides
            invalid.append(("strategy_config_invalid", value))
        for code, value in invalid:
            with self.subTest(code=code):
                self.assert_error(code, value)

    def test_frame_id_count_and_whole_document_validation_precede_engine(self):
        for identity in ("", "../raw", "unsafe space", "x" * 65, 9):
            value = document(); value["frames"][0]["id"] = identity
            self.assert_error("frame_id_invalid", value)
        value = document(); value["frames"].append(deepcopy(value["frames"][0]))
        self.assert_error("duplicate_frame_id", value)
        for count in (0, 501):
            value = document()
            value["frames"] = [{"id": "case-" + str(i), "snapshot": {"now": "2026-10-16T13:35:01Z"}}
                               for i in range(count)]
            self.assert_error("frame_count_invalid", value)
        value["frames"] = value["frames"][:500]
        self.assertEqual(json.loads(replay.replay_bytes(encode(value)))["frame_count"], 500)
        value["frames"][-1]["snapshot"]["now"] = "invalid-last-frame"
        with patch.object(replay, "evaluate_setup", side_effect=AssertionError("must not evaluate")) as engine:
            self.assert_error("frame_time_invalid", value)
            engine.assert_not_called()

    def test_duplicate_json_keys_float_tokens_and_constants_reject_at_every_depth(self):
        raw = canonical(document())
        duplicate = [raw.replace(b'"schema_version":"1.0"', b'"schema_version":"1.0","schema_version":"1.0"'),
                     raw.replace(b'"ask":"6.31"', b'"ask":"6.31","ask":"6.32"'),
                     raw.replace(b'"strategy_overrides":{}', b'"strategy_overrides":{"target_r":"2","target_r":"3"}')]
        for value in duplicate:
            self.assert_error("duplicate_json_key", value)
        for token in (b"6.31", b"6e0", b"1E-5"):
            self.assert_error("floating_json_number_forbidden", raw.replace(b'"ask":"6.31"', b'"ask":' + token))
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            self.assert_error("nonfinite_json_number", raw.replace(b'"ask":"6.31"', b'"ask":' + token))
        for token in (b"1000000000000000001", b"-1000000000000000001", b"1" * 5000):
            self.assert_error("integer_out_of_range", raw.replace(b'"ask":"6.31"', b'"ask":' + token))
        for invalid in (b"\xff", b"{}{}", b"{", b"\xef\xbb\xbf{}", b'"\\ud800"'):
            with self.subTest(invalid_length=len(invalid)):
                with self.assertRaises(replay.ReplayError):
                    replay.replay_bytes(invalid)

    def test_input_frame_and_nesting_exact_bounds(self):
        value = document()
        raw = canonical(value)
        self.assertEqual(json.loads(replay.replay_bytes(raw + b" " * (replay.MAX_INPUT_BYTES - len(raw))))["frame_count"], 1)
        self.assert_error("input_too_large", raw + b" " * (replay.MAX_INPUT_BYTES + 1 - len(raw)))
        frame = value["frames"][0]
        frame["snapshot"]["notes"] = ""
        frame["snapshot"]["notes"] = "x" * (replay.MAX_FRAME_BYTES - len(canonical(frame)))
        self.assertEqual(len(canonical(frame)), replay.MAX_FRAME_BYTES)
        replay.replay_bytes(encode(value))
        frame["snapshot"]["notes"] += "x"
        self.assert_error("frame_too_large", value)
        value = document()
        nested = "leaf"
        for _ in range(20):
            nested = [nested]
        value["frames"][0]["snapshot"]["notes"] = nested
        replay.replay_bytes(encode(value))
        value["frames"][0]["snapshot"]["notes"] = [nested]
        self.assert_error("input_too_deep", value)
        value["frames"][0]["snapshot"]["notes"] = '\\"' + "[{}]" * 100
        replay.replay_bytes(encode(value))

    def test_provenance_is_declared_and_explicit_contradictions_are_rejected(self):
        for declared in ("verified", None, True, {}, []):
            value = document(); value["provenance"] = declared
            self.assert_error("provenance_invalid", value)
        value = document(); value["provenance"] = "provider_capture"
        self.assert_error("provenance_mismatch", value)
        for metadata in ({"kind": []}, {"kind": {}}, {"synthetic": "true"}):
            value = document(); value["frames"][0]["snapshot"]["provenance"] = metadata
            self.assert_error("provenance_invalid", value)
        for metadata in ({"kind": "provider_capture"}, {"synthetic": False}, "provider_capture"):
            value = document(); value["frames"][0]["snapshot"]["provenance"] = metadata
            self.assert_error("provenance_mismatch", value)
        value = document()
        value["provenance"] = "provider_capture"
        # This invented test merely declares a provider feed; it does not verify one.
        snapshot = value["frames"][0]["snapshot"]
        snapshot["provenance"] = {"kind": "provider_capture", "synthetic": False}
        pending = [snapshot]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                if "feed" in item:
                    item["feed"] = "iex"
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        report = json.loads(replay.replay_bytes(encode(value)))
        self.assertEqual(report["declared_provenance"], "provider_capture")
        self.assertEqual(report["source_verification"], "not_performed")
        snapshot["synthetic"] = True
        self.assert_error("provenance_mismatch", value)

    def test_output_bound_and_engine_failure_create_no_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            with patch.object(replay, "MAX_OUTPUT_BYTES", 100):
                with self.assertRaises(replay.ReplayError) as caught:
                    replay.replay_file(EXAMPLE, output)
                self.assertEqual(caught.exception.code, "output_too_large")
            self.assertFalse(output.exists())
        with patch.object(replay, "evaluate_setup", side_effect=RuntimeError("private-sentinel")):
            self.assert_error("engine_evaluation_failed", document())

    def test_cli_report_integrity_success_and_existing_output_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            output = Path(directory) / "report.json"
            source.write_bytes(EXAMPLE.read_bytes())
            input_before = source.read_bytes()
            result = self.cli(source, "--output", output, cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "decision_replay_complete\n")
            self.assertEqual(result.stderr, "")
            self.assertEqual(output.read_bytes(), replay.replay_bytes(input_before))
            output_before = output.read_bytes()
            refused = self.cli(source, "--output", output, cwd=directory)
            self.assertEqual(refused.returncode, 2)
            self.assertEqual(refused.stderr, "decision_replay_error: output_exists\n")
            self.assertEqual(refused.stdout, "")
            self.assertEqual(output.read_bytes(), output_before)
            self.assertEqual(source.read_bytes(), input_before)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["input.json", "report.json"])

    def test_exclusive_output_refuses_input_symlinks_and_competing_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            source.write_bytes(EXAMPLE.read_bytes())
            before = source.read_bytes()
            with self.assertRaises(replay.ReplayError) as caught:
                replay.replay_file(source, source)
            self.assertEqual(caught.exception.code, "output_exists")
            self.assertEqual(source.read_bytes(), before)
            link = Path(directory) / "linked-report.json"
            link.symlink_to(source)
            with self.assertRaises(replay.ReplayError) as caught:
                replay.replay_file(source, link)
            self.assertEqual(caught.exception.code, "output_exists")
            self.assertTrue(link.is_symlink())
            self.assertEqual(source.read_bytes(), before)
            competing = Path(directory) / "race.json"
            real_link = os.link
            def race(temporary, destination, **kwargs):
                Path(destination).write_bytes(b"other writer")
                return real_link(temporary, destination, **kwargs)
            with patch.object(replay.os, "link", side_effect=race):
                with self.assertRaises(replay.ReplayError) as caught:
                    replay.replay_file(source, competing)
            self.assertEqual(caught.exception.code, "output_exists")
            self.assertEqual(competing.read_bytes(), b"other writer")
            self.assertFalse(list(Path(directory).glob(".decision-replay-*")))

    def test_cli_errors_do_not_echo_payload_arguments_or_os_paths(self):
        sentinel = "private-sentinel-must-not-appear"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / (sentinel + ".json")
            output = Path(directory) / "report.json"
            value = document(); value[sentinel] = sentinel
            source.write_bytes(encode(value))
            result = self.cli(source, "--output", output)
            self.assertEqual(result.stderr, "decision_replay_error: input_schema_invalid\n")
            self.assertNotIn(sentinel, result.stderr + result.stdout)
            self.assertFalse(output.exists())
            result = self.cli("--" + sentinel)
            self.assertEqual(result.stderr, "decision_replay_error: invalid_arguments\n")
            result = self.cli(Path(directory) / (sentinel + "-missing"), "--output", output)
            self.assertEqual(result.stderr, "decision_replay_error: input_read_failed\n")
            source.write_bytes(EXAMPLE.read_bytes())
            result = self.cli(source, "--output", Path(directory) / sentinel / "report.json")
            self.assertEqual(result.stderr, "decision_replay_error: output_write_failed\n")
            self.assertNotIn(sentinel, result.stderr + result.stdout)

    @unittest.skipUnless(hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX file contract")
    def test_nonregular_input_refuses_fifo_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "input.fifo"
            output = Path(directory) / "report.json"
            os.mkfifo(fifo)
            result = self.cli(fifo, "--output", output)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, "decision_replay_error: input_not_regular_file\n")
            self.assertFalse(output.exists())

    def test_fresh_subprocess_blocks_network_and_all_other_worker_imports(self):
        script = '''
import sys
allowed = {"momentum", "momentum.models", "momentum.strategy", "momentum.decision_replay"}
network = {"socket", "urllib", "http", "requests", "httpx", "urllib3", "ftplib", "smtplib"}
class Boundaries:
    def find_spec(self, fullname, path=None, target=None):
        if (fullname.startswith("momentum.") and fullname not in allowed) or fullname.split(".")[0] in network:
            raise AssertionError("forbidden_import")
def audit(event, args):
    if event.startswith("socket."):
        raise AssertionError("forbidden_network")
sys.meta_path.insert(0, Boundaries())
sys.addaudithook(audit)
from momentum.decision_replay import replay_bytes
with open(sys.argv[1], "rb") as source:
    first = replay_bytes(source.read())
assert b'"execution_enabled":false' in first
assert not any(name.startswith("momentum.") and name not in allowed for name in sys.modules)
assert not any(name.split(".")[0] in network for name in sys.modules)
print("offline_boundary_verified")
'''
        result = subprocess.run([sys.executable, "-c", script, str(EXAMPLE)], cwd=WORKER,
                                capture_output=True, text=True, timeout=10,
                                env={"PYTHONPATH": str(WORKER), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "offline_boundary_verified\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
