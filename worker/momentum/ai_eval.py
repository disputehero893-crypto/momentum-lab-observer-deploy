"""Offline watch-only replay for the AI advisor.

Recorded or synthetic bars are fed to the same advisor that tags live cycles.
The model is a local stub: it returns a scripted chart read and never calls
an external model, opens a socket, or reads an API key. Every candidate is
logged with submitted false. This module has no broker client and cannot
submit, cancel, or replace an order.

Supplied outcome labels are copied through. A win that is not exactly true or
false, and an R multiple that is not a real number, stay unknown. They are not
rewritten to zero. The resulting rules-vs-AI table is a check of that
arithmetic. It is not evidence of profitability, and it does not change the
strategy's acceptance criteria or the paper-trading switch.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping

from .ai_advisor import (AIAdvisor, AdvisorConfig, GeminiError, SignalError, parse_signal)
from .ai_chart import ChartError, normalize_bars
from .ai_report import build_report, load_records


MAX_INPUT_BYTES = 1024 * 1024
MAX_FRAMES = 200
MAX_BARS = 60
KIND = "ai_advisor_watch_replay"
TOP_KEYS = frozenset({"schema_version", "kind", "provenance", "frames"})
FRAME_KEYS = frozenset({"id", "symbol", "rules_accepted", "bars", "stub", "outcome"})
STUB_KEYS = frozenset({"call", "pattern", "confidence", "reason"})
OUTCOME_KEYS = frozenset({"win", "r_multiple"})
PROVENANCE = frozenset({"synthetic", "recorded"})
ERROR_CODES = frozenset({
    "invalid_arguments", "input_read_failed", "input_too_large", "invalid_json",
    "duplicate_json_key", "nonfinite_json_number", "input_schema_invalid",
    "provenance_invalid", "frame_count_invalid", "frame_schema_invalid",
    "frame_id_invalid", "duplicate_frame_id", "symbol_invalid",
    "rules_decision_invalid", "bars_invalid", "duplicate_bar", "stub_invalid",
    "outcome_invalid", "outcome_without_decision", "stub_not_called",
    "stub_read_rejected", "output_exists", "output_write_failed", "replay_failed",
})


class EvalError(ValueError):
    """Fixed code only. Field values and paths are not part of the message."""

    def __init__(self, code: str):
        self.code = code if code in ERROR_CODES else "replay_failed"
        super().__init__(self.code)


class StubModel:
    """Scripted stand-in for a chart-model generate call. No key and no transport."""

    def __init__(self):
        self._pending: list[str] = []
        self.calls = 0

    def push(self, text: str) -> None:
        self._pending.append(text)

    def generate(self, parts, *, system: str = "", schema=None, max_output_tokens: int = 1024) -> str:
        self.calls += 1
        if not self._pending:
            raise GeminiError("stub_exhausted")
        return self._pending.pop(0)


class _ImmediateExecutor:
    """Runs the chart read on the calling thread so the replay stays ordered."""

    def submit(self, fn, *args):
        from concurrent.futures import Future
        future = Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:  # pragma: no cover - the advisor catches read errors
            future.set_exception(exc)
        return future

    def shutdown(self, **_kwargs):
        return None


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise EvalError("duplicate_json_key")
        value[key] = item
    return value


def _no_constant(_text):
    raise EvalError("nonfinite_json_number")


def _load(raw: bytes) -> Any:
    if len(raw) > MAX_INPUT_BYTES:
        raise EvalError("input_too_large")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_no_constant)
    except EvalError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise EvalError("invalid_json") from None


def _real_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def _prepare_frame(frame: Mapping[str, Any], seen_ids: set[str], seen_bars: set[tuple[str, str]]) -> dict[str, Any]:
    if not isinstance(frame, Mapping) or set(frame) - {"outcome"} != FRAME_KEYS - {"outcome"} or not FRAME_KEYS >= set(frame):
        raise EvalError("frame_schema_invalid")
    frame_id = frame.get("id")
    if not isinstance(frame_id, str) or not frame_id or len(frame_id) > 64 or not frame_id[0].isalnum():
        raise EvalError("frame_id_invalid")
    if any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-" for ch in frame_id):
        raise EvalError("frame_id_invalid")
    if frame_id in seen_ids:
        raise EvalError("duplicate_frame_id")
    seen_ids.add(frame_id)
    symbol = frame.get("symbol")
    if not isinstance(symbol, str) or not symbol or len(symbol) > 10 or not symbol[0].isalpha():
        raise EvalError("symbol_invalid")
    if any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for ch in symbol) or symbol != symbol.upper():
        raise EvalError("symbol_invalid")
    if type(frame.get("rules_accepted")) is not bool:
        raise EvalError("rules_decision_invalid")
    bars = frame.get("bars")
    if not isinstance(bars, list) or not 2 <= len(bars) <= MAX_BARS:
        raise EvalError("bars_invalid")
    try:
        normalize_bars(bars)
    except ChartError:
        raise EvalError("bars_invalid") from None
    ordered = sorted(bars, key=lambda row: str(row.get("start", "")))
    starts = [str(row.get("start", "")) for row in ordered]
    if any(not start for start in starts) or len(set(starts)) != len(starts):
        raise EvalError("duplicate_bar")
    bar_key = (symbol, starts[-1])
    if bar_key in seen_bars:
        raise EvalError("duplicate_bar")
    seen_bars.add(bar_key)
    stub = frame.get("stub")
    if not isinstance(stub, Mapping) or set(stub) != STUB_KEYS:
        raise EvalError("stub_invalid")
    payload = {"symbol": symbol, "call": stub.get("call"), "pattern": stub.get("pattern"),
               "confidence": stub.get("confidence"), "reason": stub.get("reason")}
    text = json.dumps(payload, sort_keys=True, allow_nan=False)
    try:
        parse_signal(text, symbol)
    except (SignalError, ValueError, TypeError):
        raise EvalError("stub_invalid") from None
    outcome = None
    if "outcome" in frame:
        label = frame.get("outcome")
        if not isinstance(label, Mapping) or set(label) != OUTCOME_KEYS:
            raise EvalError("outcome_invalid")
        win = label.get("win")
        if win is not None and win is not True and win is not False:
            raise EvalError("outcome_invalid")
        r_multiple = label.get("r_multiple")
        if r_multiple is not None and not _real_number(r_multiple):
            raise EvalError("outcome_invalid")
        outcome = {"win": win, "r_multiple": r_multiple}
    return {"id": frame_id, "symbol": symbol, "rules_accepted": frame["rules_accepted"],
            "bars": ordered, "stub_text": text, "outcome": outcome}


def _prepare(document: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(document, Mapping) or set(document) != TOP_KEYS:
        raise EvalError("input_schema_invalid")
    if document.get("schema_version") != "1.0" or document.get("kind") != KIND:
        raise EvalError("input_schema_invalid")
    provenance = document.get("provenance")
    if provenance not in PROVENANCE:
        raise EvalError("provenance_invalid")
    frames = document.get("frames")
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
        raise EvalError("frame_count_invalid")
    prepared = []
    seen_ids: set[str] = set()
    seen_bars: set[tuple[str, str]] = set()
    for frame in frames:
        prepared.append(_prepare_frame(frame, seen_ids, seen_bars))
    return str(provenance), prepared


def _advisor(log_dir: str, stub: StubModel, clock: _Clock) -> AIAdvisor:
    # Thresholds stay at the advisor defaults. Spacing is zero only because the
    # stub does not contact a provider; the live worker defaults are untouched.
    config = AdvisorConfig(enabled=True, api_key="", model="stub-local", mode="advise",
                           min_interval_seconds=0, analysis_interval_seconds=0,
                           max_calls_per_day=MAX_FRAMES, log_dir=log_dir)
    return AIAdvisor(config, client=stub, executor=_ImmediateExecutor(), monotonic=clock,
                     today=lambda: "2026-10-16")


def replay(document: Mapping[str, Any], *, log_dir: str) -> dict[str, Any]:
    """Replay one document. Returns decisions plus the rules-vs-AI report."""
    provenance, frames = _prepare(document)
    stub = StubModel()
    clock = _Clock()
    advisor = _advisor(log_dir, stub, clock)
    decisions = []
    try:
        for index, frame in enumerate(frames):
            clock.t += 1
            stub.push(frame["stub_text"])
            advisor.start_reads([(frame["symbol"], frame["bars"], {"as_of": frame["bars"][-1]["start"]})])
            if stub.calls != index + 1:
                raise EvalError("stub_not_called")
            signal = advisor.signal(frame["symbol"])
            if signal is None:
                raise EvalError("stub_read_rejected")
            source = advisor.classify(frame["rules_accepted"], signal)
            if frame["outcome"] is not None and source is None:
                raise EvalError("outcome_without_decision")
            if source is not None:
                advisor.note_candidate(source, {
                    "setup_id": frame["id"], "symbol": frame["symbol"], "side": "buy",
                    "entry_limit": None, "stop_price": None, "target_price": None, "qty": None,
                    "observed_at": frame["bars"][-1]["start"], "reason": "watch_only_replay",
                    "config_id": "watch_only",
                }, signal, submitted=False, outcome="watch_only_not_submitted")
                if frame["outcome"] is not None:
                    advisor.feedback.append({
                        "type": "outcome", "setup_id": frame["id"], "symbol": frame["symbol"],
                        "source": source, "pattern": signal.pattern, "win": frame["outcome"]["win"],
                        "r_multiple": frame["outcome"]["r_multiple"],
                        "basis": "supplied_label_not_a_fill",
                    })
            decisions.append({
                "id": frame["id"], "symbol": frame["symbol"], "rules_accepted": frame["rules_accepted"],
                "source": source, "call": signal.call, "pattern": signal.pattern,
                "confidence": signal.confidence, "submitted": False,
                "outcome_logged": frame["outcome"] is not None,
            })
        records = load_records(advisor.feedback.path)
    finally:
        advisor.close()
    for record in records:
        if record.get("type") == "candidate" and record.get("submitted") is not False:
            raise EvalError("replay_failed")
    report = build_report(records)
    return {
        "schema_version": "1.0",
        "kind": "ai_advisor_watch_replay_report",
        "evaluation": {
            "mode": "watch_only",
            "advisor_mode": "advise",
            "model": "stub",
            "execution_enabled": False,
            "orders_submitted": 0,
            "paper_trading_enabled": False,
            "profitability_claimed": False,
            "outcome_basis": "supplied_labels_not_fills",
            "source_verification": "not_performed",
            "provenance": provenance,
            "min_confidence": advisor.config.min_confidence,
            "confluence_min_confidence": advisor.config.confluence_min_confidence,
            "stub_calls": stub.calls,
        },
        "decisions": decisions,
        "report": report,
        "log_path": str(advisor.feedback.path),
    }


def replay_bytes(raw: bytes, *, log_dir: str) -> dict[str, Any]:
    document = _load(raw)
    if not isinstance(document, dict):
        raise EvalError("input_schema_invalid")
    return replay(document, log_dir=log_dir)


def _write_output(path: str, payload: Mapping[str, Any]) -> None:
    raw = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise EvalError("output_exists") from None
    except OSError:
        raise EvalError("output_write_failed") from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
    except OSError:
        raise EvalError("output_write_failed") from None


def replay_file(input_path: str, output_path: str) -> dict[str, Any]:
    try:
        raw = Path(input_path).read_bytes()
    except OSError:
        raise EvalError("input_read_failed") from None
    with tempfile.TemporaryDirectory() as directory:
        result = replay_bytes(raw, log_dir=directory)
    public = {key: value for key, value in result.items() if key != "log_path"}
    _write_output(output_path, public)
    return public


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Watch-only AI advisor replay with a local stub model")
    parser.add_argument("input")
    parser.add_argument("--output", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code not in (0, None):
            print("ai_eval_error: invalid_arguments", file=sys.stderr)
            return 2
        return 0
    try:
        replay_file(args.input, args.output)
    except EvalError as error:
        print("ai_eval_error: " + error.code, file=sys.stderr)
        return 2
    print("ai_eval_complete")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
