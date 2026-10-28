# Offline strategy decision replay

This command evaluates caller-supplied snapshots with the existing `experimental_v1` strategy and writes a deterministic local report. Each frame calls `momentum.strategy.evaluate_setup` with the recorded decision time and a validated `StrategyConfig`. It does not load application settings or modify the running worker.

The strategy combines selected instructional screening thresholds with our experimental pattern, timing, and execution assumptions. See [STRATEGY_NOTES.md](STRATEGY_NOTES.md) for those distinctions. This utility does not establish an exact reproduction of Ross Cameron's discretion or provide evidence of profitability.

## Run the invented example

From the `worker` directory, with Python 3.11 or later:

```bash
PYTHONPATH=. python -m momentum.decision_replay examples/decision-replay.synthetic.json --output decision-report.json
```

The output path must be new. A successful command prints only `decision_replay_complete`. To repeat the command, choose a different output filename; existing files are never overwritten.

The supplied example contains four independent, invented cases derived from the public `valid_snapshot()` fixture in `tests/test_strategy.py`. The symbol, prices, volumes, news confirmation, float evidence, and session history are artificial. All four cases deliberately share one decision time; they do not form a continuous market path.

| Frame | Actual engine result | Interpretation |
| --- | --- | --- |
| `valid` | `first_pullback_breakout`, accepted | The supplied synthetic evidence satisfies the current strategy checks. |
| `missing-float` | `float_unconfirmed`, rejected | Required float evidence is absent. |
| `wide-spread` | `spread_too_wide`, rejected | The supplied quote exceeds the configured spread limit. |
| `future-bar` | `lookahead_bar`, rejected | A bar's recorded availability is later than the decision. |

An accepted result is the existing engine's setup intention. The replay never evaluates account risk, determines a share quantity, sends an order, simulates a fill, or calculates a return. An accepted frame does not create a position for subsequent frames.

## Input contract

The input is one UTF-8 JSON file with exactly these top-level keys:

```json
{
  "schema_version": "1.0",
  "kind": "strategy_decision_replay_input",
  "provenance": "synthetic",
  "strategy_overrides": {},
  "frames": [
    {
      "id": "case-1",
      "snapshot": {
        "now": "2026-10-16T13:35:01+00:00"
      }
    }
  ]
}
```

The abbreviated snapshot above illustrates the wrapper only. It lacks the evidence required for a passing setup and will receive the engine's rejection. Use the full example or the snapshot contract in [STRATEGY_NOTES.md](STRATEGY_NOTES.md) for quote, trade, calendar, completed-bar, point-in-time float/catalyst, previous-close, and same-clock volume fields. Current Market Watch quotes alone do not supply these requirements. The replay does not fetch or invent missing evidence.

Every frame has exactly `id` and `snapshot`. IDs are unique, 1–64 characters, begin with an ASCII letter or digit, and otherwise contain only ASCII letters, digits, underscores, dots, or hyphens. Snapshots remain in supplied order. Their `now` timestamps must be timezone-aware and globally nondecreasing, including across different symbols. Different cases may have equal decision times. The host's current time is never substituted or used to move a frame into a different session.

`strategy_overrides` accepts only existing `StrategyConfig` field names and values that the current class validates. Quote decimal settings as strings, for example `"target_r": "3"`; integral settings such as `max_hold_seconds` use JSON integers. Monetary and other decimal snapshot values should likewise use strings. Floating JSON number tokens, including exponent-form numeric tokens, are rejected throughout the input. JSON booleans cannot stand in for integer configuration values. The supported mode and profile remain `paper` and `experimental_v1`; arbitrary profile names or live mode are rejected.

For a local comparison, copy the example to a new input file and change its `strategy_overrides`, for example:

```json
{"target_r": "3"}
```

Run that new file to a different report path. The validated settings, configuration hash, and actual decisions in each report describe that replay only. In the invented passing case, the higher target requires more room below known resistance and the engine rejects it. No saved application configuration is applied or changed by either replay.

### Provenance is a caller declaration

`provenance` must be `synthetic` or `provider_capture`. Every report states `source_verification: "not_performed"`. A caller's provider/feed name does not authenticate a capture, establish complete data, or prove that information was available at the recorded time.

The wrapper rejects explicit contradictions: a declared provider capture cannot use a top-level `demo` or `synthetic` feed; recognized snapshot provenance markers must agree with the document declaration; and supplied `synthetic` markers must be booleans and agree with it. It also rejects non-string `provenance.kind` values. Other copied metadata is not verified. Data consistency and causal checks that the strategy already implements remain the engine's responsibility.

### Timestamp precision is limited by the current engine

Full timestamps must use bounded RFC3339-style text with uppercase `T` and either `Z` or an explicit `±HH:MM` offset. Offset hours must be 00–23 and minutes 00–59. Naive timestamps, fractional offset seconds, compact dates, comma fractions, and invalid calendar/time values are rejected. Clock-only fields use `HH:MM` or `HH:MM:SS[.fraction]`, without an offset. Timestamp and clock strings are at most 64 characters.

The core strategy uses Python microsecond timestamps. Before calling it, the replay rejects any supplied causal timestamp or clock with nonzero digits beyond six fractional places, using `timestamp_precision_not_supported`. This check covers decision time, quote/trade source and receipt times, bar start and availability, fact effective/known/published times, RVOL cutoff and historical known-at/clock fields, and configured window times.

For example, `.1234567` cannot be replayed faithfully and is rejected. `.123456000` is losslessly representable at microsecond precision and may remain in the input. Its original text is preserved. The wrapper does not round or truncate a provider timestamp to make a capture pass. Captures with meaningful nanosecond precision require a separate core-engine precision change before they can be evaluated by this utility.

Missing/null strategy evidence is left missing for the engine to reject. A supplied malformed causal timestamp fails the complete input. A valid but stale or future timestamp reaches the engine unchanged and receives its actual causal decision.

### Resource and structural bounds

| Boundary | Limit or behavior |
| --- | --- |
| Input file | Regular local file, at most 4 MiB of raw UTF-8 JSON bytes |
| Frames | 1–500 |
| Each frame | At most 128 KiB of canonical JSON for the complete `id`/`snapshot` wrapper |
| Container nesting | At most 24 object/array levels, with the root object counted as level one |
| Integer tokens | Absolute value at most 10¹⁸ |
| Output | At most 8 MiB, including its final newline |
| JSON keys | Duplicate keys rejected at every nesting level |
| Wrapper/configuration fields | Unknown keys rejected |
| Floating/nonfinite JSON tokens | Rejected; a quoted invalid numeric field is handled by the existing strategy/config validator |

All structural validation, timestamp representation checks, and frame ordering checks finish before the first engine call. Any such error prevents report creation. Additional snapshot fields are preserved, subject to the bounds and recognized timestamp/provenance checks; the wrapper does not create extra strategy criteria from them.

## Report contract and reproducibility

Each report contains:

- The schema/kind, declared provenance, and `source_verification: "not_performed"`.
- The actual profile, current engine `config_id`, and full canonical validated `strategy_settings`, with Decimal settings encoded as strings.
- The original frame IDs, original `now` strings, and unchanged snapshot values.
- Exact `evaluate_setup(snapshot, config).to_dict()` results. Any normalization within an engine decision remains the engine's own output; original source/receipt/decision strings are also retained in the copied snapshot.
- SHA-256 hashes of the canonical input document, every original frame wrapper, and each snapshot.
- `execution_enabled: false`, `risk_assessment: "not_evaluated"`, `fills_simulated: false`, and `performance_calculated: false`.

Canonical JSON uses sorted object keys, compact separators, UTF-8, literal Unicode characters, and no nonfinite numbers. `input_sha256` is the hash of the canonical **parsed input document**, not its original file bytes. Changing whitespace or object-key order therefore leaves it unchanged. Array order, values, original timestamp strings, frame IDs, and overrides remain significant. Frame hashes include the original `id` and snapshot; snapshot hashes cover only the snapshot. The report's final newline is not part of these input/frame/snapshot hashes.

The same input, strategy implementation, and validated configuration produce identical report bytes. The report's configuration hash identifies settings; it is not a source-code hash, a signature, or proof of data provenance. Retain the project revision separately when comparing reports made with different engine versions.

## File handling and errors

Reports copy whatever snapshot data the caller supplies. Keep provider captures and their reports local; do not upload them to the application, a cloud service, or a public repository as part of this workflow. The utility does not inspect credentials or runtime journals, fetch account/market data, or upload files. It reads only the named input and writes the named local output through a temporary file in the same directory.

The complete bounded report is written to a private sibling temporary file and flushed before an exclusive hard link creates the final path. Existing files, symlinks, and an input path reused as the output are refused. A competing writer that creates the destination during processing also wins without being overwritten. The output directory must already exist and its filesystem must support same-directory hard links; there is no overwrite fallback. Named pipes and other nonregular input files are rejected.

On failure, the CLI exits with code 2 and prints one fixed message such as:

```text
decision_replay_error: timestamp_precision_not_supported
```

Errors never echo snapshot fields, supplied argument text, file paths, or underlying exception messages. Other useful fixed codes include `duplicate_json_key`, `floating_json_number_forbidden`, `input_too_large`, `frame_too_large`, `input_too_deep`, `frame_time_out_of_order`, `strategy_config_invalid`, `provenance_mismatch`, and `output_exists`. Exit code 0 means the local report was created, including when every engine decision in it is a rejection.

The module exposes `replay_bytes(raw: bytes) -> bytes`, `replay_file(input_path, output_path) -> dict`, and `ReplayError.code` for local tooling. `replay_file` returns only the frame count, configuration hash, and input hash as its summary. There is no server route, scheduler, UI trigger, account state, risk assessment, order submission, fill model, P&L calculation, or historical performance analysis in this increment.

## Focused verification

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_decision_replay.py' -v
```

The 19 tests cover exact engine equivalence, stable report bytes and canonical hashes, supported override effects and immutability, real stale/future/invalid-evidence decisions, lossless timestamp preservation, meaningful nanosecond rejection, strict offset parsing, chronological frame order, bounded JSON parsing, provenance contradictions, exclusive output and input integrity, sanitized errors, FIFO refusal, and a fresh subprocess that blocks network imports, socket operations, and every other worker module. No broker requests, account calls, or runtime journal reads are involved.
