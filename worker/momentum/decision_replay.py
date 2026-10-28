"""Bounded offline evaluation of supplied snapshots; no execution capability.

Only this module, the standard library, models and the existing strategy are
needed. Report snapshots are the caller's original data, copied without time
replacement. Source declarations are never verified by this local utility.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict, fields
from datetime import time
from decimal import Decimal
from hashlib import sha256
import json
import os
import re
import stat
import sys
import tempfile

from .models import plain, timestamp
from .strategy import StrategyConfig, evaluate_setup


MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_FRAME_BYTES = 128 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_FRAMES = 500
MAX_DEPTH = 24
TOP_KEYS = frozenset({"schema_version", "kind", "provenance", "strategy_overrides", "frames"})
FRAME_KEYS = frozenset({"id", "snapshot"})
FRAME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
DATETIME_TEXT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.([0-9]+))?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z")
CLOCK_TEXT = re.compile(r"[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.([0-9]+))?)?\Z")
TIMESTAMP_KEYS = frozenset({
    "now", "timestamp", "source_at", "received_at", "available_at", "start", "as_of",
    "known_at", "effective_at", "published_at", "captured_at", "observed_at", "bar_end",
    "observed_received_at", "float_effective_at", "float_known_at",
    "catalyst_published_at", "catalyst_known_at",
})
CLOCK_KEYS = frozenset({"clock", "trade_start_et", "trade_end_et", "session_start_et"})
ERROR_CODES = frozenset({
    "invalid_arguments", "input_read_failed", "input_not_regular_file", "input_too_large",
    "invalid_json", "duplicate_json_key", "floating_json_number_forbidden", "nonfinite_json_number",
    "integer_out_of_range", "input_too_deep", "input_schema_invalid", "strategy_config_invalid",
    "frame_count_invalid", "frame_schema_invalid", "frame_id_invalid", "duplicate_frame_id",
    "frame_too_large", "frame_time_invalid", "frame_time_out_of_order", "timestamp_invalid",
    "timestamp_precision_not_supported", "provenance_invalid", "provenance_mismatch",
    "engine_evaluation_failed", "output_too_large", "output_exists", "output_write_failed",
    "replay_failed",
})


class ReplayError(ValueError):
    """Only fixed codes leave the parser/CLI; raw fields and OS errors do not."""
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else "replay_failed"
        super().__init__(self.code)


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ReplayError("invalid_json") from None


def _digest(value):
    return sha256(_canonical(value)).hexdigest()


def _depth_check(text):
    # Count container nesting before json.loads can build a deeply recursive
    # object. Braces inside quoted/escaped strings are not structural nesting.
    depth, quoted, escaped = 0, False, False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ReplayError("input_too_deep")
        elif character in "]}":
            depth -= 1
            if depth < 0:
                raise ReplayError("invalid_json")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ReplayError("duplicate_json_key")
        value[key] = item
    return value


def _no_float(_text):
    raise ReplayError("floating_json_number_forbidden")


def _no_constant(_text):
    raise ReplayError("nonfinite_json_number")


def _integer(text):
    # Match the engine's finite input magnitude without parsing huge integers.
    if len(text.lstrip("-")) > 19:
        raise ReplayError("integer_out_of_range")
    value = int(text)
    if abs(value) > 10**18:
        raise ReplayError("integer_out_of_range")
    return value


def _decode(raw):
    if not isinstance(raw, bytes):
        raise ReplayError("invalid_json")
    if len(raw) > MAX_INPUT_BYTES:
        raise ReplayError("input_too_large")
    try:
        text = raw.decode("utf-8")
        _depth_check(text)
        return json.loads(text, object_pairs_hook=_unique_object, parse_float=_no_float,
                          parse_constant=_no_constant, parse_int=_integer)
    except ReplayError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ReplayError("invalid_json") from None


def _check_time(value, *, clock=False, required=False):
    # Null/absent strategy evidence is left to evaluate_setup. Supplied time
    # strings must have a representation the engine can interpret losslessly.
    if value is None and not required:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ReplayError("timestamp_invalid")
    match = (CLOCK_TEXT if clock else DATETIME_TEXT).fullmatch(value)
    if not match:
        raise ReplayError("timestamp_invalid")
    fraction = match.group(1) or ""
    if len(fraction.rstrip("0")) > 6:
        raise ReplayError("timestamp_precision_not_supported")
    try:
        return time.fromisoformat(value) if clock else timestamp(value)
    except (ValueError, TypeError, ArithmeticError):
        raise ReplayError("timestamp_invalid") from None


def _causal_times(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if key in TIMESTAMP_KEYS:
                    _check_time(child)
                elif key in CLOCK_KEYS:
                    _check_time(child, clock=True)
                if isinstance(child, (dict, list)):
                    pending.append(child)
        elif isinstance(item, list):
            pending.extend(child for child in item if isinstance(child, (dict, list)))


def _configuration(overrides):
    defaults = StrategyConfig()
    names = {field.name for field in fields(StrategyConfig)}
    if not isinstance(overrides, dict) or set(overrides) - names:
        raise ReplayError("strategy_config_invalid")
    for key, value in overrides.items():
        original = getattr(defaults, key)
        if isinstance(original, Decimal):
            if not isinstance(value, str) or not 1 <= len(value) <= 64:
                raise ReplayError("strategy_config_invalid")
        elif type(original) is int:
            if type(value) is not int:
                raise ReplayError("strategy_config_invalid")
        elif not isinstance(value, str) or not 1 <= len(value) <= 64:
            raise ReplayError("strategy_config_invalid")
        if key in {"trade_start_et", "trade_end_et"}:
            _check_time(value, clock=True, required=True)
    try:
        return StrategyConfig(**overrides)
    except (ValueError, TypeError, ArithmeticError):
        raise ReplayError("strategy_config_invalid") from None


def _provenance(snapshot, declared):
    # This detects explicit contradictions, not authenticity. A feed name or a
    # caller assertion never verifies a capture or supplies missing provenance.
    metadata = snapshot.get("provenance")
    if isinstance(metadata, dict):
        if "synthetic" in metadata:
            if type(metadata["synthetic"]) is not bool:
                raise ReplayError("provenance_invalid")
            if metadata["synthetic"] != (declared == "synthetic"):
                raise ReplayError("provenance_mismatch")
        marker = metadata.get("kind")
        if "kind" in metadata and not isinstance(marker, str):
            raise ReplayError("provenance_invalid")
        if marker in {"synthetic", "provider_capture"} and marker != declared:
            raise ReplayError("provenance_mismatch")
    elif metadata in ("synthetic", "provider_capture") and metadata != declared:
        raise ReplayError("provenance_mismatch")
    if declared == "provider_capture" and snapshot.get("feed") in ("demo", "synthetic"):
        raise ReplayError("provenance_mismatch")
    if "synthetic" in snapshot:
        if type(snapshot["synthetic"]) is not bool:
            raise ReplayError("provenance_invalid")
        if snapshot["synthetic"] != (declared == "synthetic"):
            raise ReplayError("provenance_mismatch")


def _validated(raw):
    value = _decode(raw)
    if (not isinstance(value, dict) or set(value) != TOP_KEYS
            or value.get("schema_version") != "1.0"
            or value.get("kind") != "strategy_decision_replay_input"):
        raise ReplayError("input_schema_invalid")
    if value["provenance"] not in ("synthetic", "provider_capture"):
        raise ReplayError("provenance_invalid")
    cfg = _configuration(value["strategy_overrides"])
    frames = value["frames"]
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
        raise ReplayError("frame_count_invalid")
    identities, prior = set(), None
    for frame in frames:
        if not isinstance(frame, dict) or set(frame) != FRAME_KEYS or not isinstance(frame["snapshot"], dict):
            raise ReplayError("frame_schema_invalid")
        identity = frame["id"]
        if not isinstance(identity, str) or not FRAME_ID.fullmatch(identity):
            raise ReplayError("frame_id_invalid")
        if identity in identities:
            raise ReplayError("duplicate_frame_id")
        identities.add(identity)
        if len(_canonical(frame)) > MAX_FRAME_BYTES:
            raise ReplayError("frame_too_large")
        snapshot = frame["snapshot"]
        try:
            current = _check_time(snapshot.get("now"), required=True)
        except ReplayError as error:
            if error.code == "timestamp_precision_not_supported":
                raise
            raise ReplayError("frame_time_invalid") from None
        if prior is not None and current < prior:
            raise ReplayError("frame_time_out_of_order")
        prior = current
        _causal_times(snapshot)
        _provenance(snapshot, value["provenance"])
    return value, cfg


def replay_bytes(raw: bytes) -> bytes:
    """Evaluate a complete bounded document and return deterministic JSON bytes.

    Structural errors abort the whole input before any engine call. Each valid
    frame is independent: no fills, positions, account, future paths or risk
    assessment are inferred from an earlier frame's accepted setup intention.
    """
    value, cfg = _validated(raw)
    rows = []
    for frame in value["frames"]:
        snapshot = frame["snapshot"]
        try:
            decision = evaluate_setup(deepcopy(snapshot), cfg).to_dict()
        except Exception:
            raise ReplayError("engine_evaluation_failed") from None
        rows.append({"id": frame["id"], "now": snapshot["now"],
                     "frame_sha256": _digest(frame), "snapshot_sha256": _digest(snapshot),
                     "snapshot": deepcopy(snapshot), "decision": decision})
    report = {"schema_version": "1.0", "kind": "strategy_decision_replay_report",
              "declared_provenance": value["provenance"], "source_verification": "not_performed",
              "profile": cfg.profile, "config_id": cfg.config_id, "strategy_settings": plain(asdict(cfg)),
              "hash_algorithm": "sha256", "canonicalization": "sorted_keys_compact_utf8_json_v1",
              "input_sha256": _digest(value), "frame_count": len(rows), "frames": rows,
              "execution_enabled": False, "risk_assessment": "not_evaluated",
              "fills_simulated": False, "performance_calculated": False}
    encoded = _canonical(report) + b"\n"
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ReplayError("output_too_large")
    return encoded


def _read_file(path):
    descriptor = None
    try:
        # Inspect the opened descriptor, and do not wait for a writer if a
        # caller accidentally supplies a FIFO instead of a regular JSON file.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ReplayError("input_not_regular_file")
        if info.st_size > MAX_INPUT_BYTES:
            raise ReplayError("input_too_large")
        handle = os.fdopen(descriptor, "rb")
        descriptor = None  # fdopen now owns and closes it.
        with handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
    except ReplayError:
        raise
    except (OSError, ValueError, TypeError):
        raise ReplayError("input_read_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > MAX_INPUT_BYTES:
        raise ReplayError("input_too_large")
    return raw


def _create_output(path, encoded):
    # A complete fsynced sibling temporary file is linked into place atomically.
    # link() exclusively creates its destination: existing files and symlinks
    # are never overwritten, including an input path reused as the output.
    temporary = None
    try:
        destination = os.fspath(path)
        if os.path.lexists(destination):
            raise ReplayError("output_exists")
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".decision-replay-",
                                         dir=os.path.dirname(destination) or ".", delete=False) as handle:
            temporary = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination, follow_symlinks=False)
    except ReplayError:
        raise
    except FileExistsError:
        raise ReplayError("output_exists") from None
    except (OSError, ValueError, TypeError):
        raise ReplayError("output_write_failed") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def replay_file(input_path, output_path) -> dict:
    """Read only the supplied input and exclusively create a bounded local report."""
    encoded = replay_bytes(_read_file(input_path))
    _create_output(output_path, encoded)
    report = json.loads(encoded)
    return {"frame_count": report["frame_count"], "config_id": report["config_id"],
            "input_sha256": report["input_sha256"]}


class _Arguments(argparse.ArgumentParser):
    def error(self, _message):
        raise ReplayError("invalid_arguments")


def main(argv=None) -> int:
    parser = _Arguments(prog="python -m momentum.decision_replay",
                        description="Evaluate supplied snapshots offline; no orders, fills or returns.")
    parser.add_argument("input", help="bounded UTF-8 JSON input file")
    parser.add_argument("--output", required=True, help="new local report path; must not already exist")
    try:
        args = parser.parse_args(argv)
        replay_file(args.input, args.output)
    except ReplayError as error:
        print("decision_replay_error: " + error.code, file=sys.stderr)
        return 2
    except Exception:
        print("decision_replay_error: replay_failed", file=sys.stderr)
        return 2
    print("decision_replay_complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
