# Worker test coverage

Tests and documentation only. This file records how statement coverage of the
`momentum` package is measured and which remaining uncovered lines are
deliberately left uncovered because they cannot be reached through any input
the code accepts. Nothing here changes runtime behavior, paper-only mode, or
the GET-only observer.

## Measure it

Python 3.12, no third-party runtime dependencies (`coverage` is a local dev tool only):

```bash
cd worker
python -m coverage run --source=momentum -m unittest discover -s tests
python -m coverage report -m --skip-covered
```

The offline suite (568 tests at the time of writing) uses synthetic fixtures,
in-memory transports/brokers and temporary SQLite files; network openers are
blocked and broker-facing tests assert that no order POST is sent. On a host
without a `python` executable on `PATH`, the two `test_worker_launcher` tests
fail with `exec: python: not found`; CI links `python` and runs them green.

Current result: 4556 statements, 8 missed (99%); 18 of 23 modules at 100%. All 8 missed lines are listed below.

## Unreachable lines (documented, not tested)

| Module:line | Guard | Why it cannot fire |
|---|---|---|
| `activities.py:145-146` | `except InvalidOperation` around `Decimal(raw)` | `raw` already passed `_DECIMAL` (`[+-]?(\d+(\.\d*)?\|\.\d+)`), and every string that regex admits (including Unicode `\d` digits) is a valid `Decimal` literal. |
| `ledger.py:107-108` | `except InvalidOperation` around `Decimal(encoded)` | Same reasoning: `_NUMBER` only admits ASCII decimal literals that `Decimal` always parses. |
| `crypto_observation.py:79` | `PRICE.fullmatch` failure after formatting | Earlier checks bound the value to `0 < n <= 1e18` with exponent in `[-18, 18]`, so the fixed rendering has at most 19 integer and 18 fractional digits, which `PRICE` always matches. |
| `crypto_observation.py:213` | missing/error row `receipt_ns > now_ns` ("Future receipt") | Every row already enforces `receipt_ns <= collected_ns`, and the envelope enforces `collected_ns <= now_ns`. |
| `market_observation.py:83` | fixed price string longer than 64 chars | Same bounds as above cap the rendering at 38 characters. |
| `strategy_review.py:78` | `whole_digits > 10` in `_nanoseconds_floor` | The preceding guard requires `0 < seconds <= 5`, so the value has at most one integer digit and `len(digits) + exponent + 9 <= 10`. |

These guards are kept as defense in depth (e.g. against a future regex or bound
change) and must not be removed just to raise the percentage.

## Reachable only by tampering (covered by tamper tests)

- `alpaca.py` API host lock (`API host lock rejected request`) and
  `crypto_observation.py` crypto URL lock: requests are built from fixed
  `https://` module constants and paths that are pre-validated (`/v2/...`, no
  `://`, `?`, `#`, `..`), so the lock cannot fire in normal operation.
  `tests/test_coverage_wrapup.py` patches the constant to prove the lock still
  refuses before any transport call (no GET, no POST).
- `server.py` `send()` with no payload: no route uses it today; the helper is
  exercised directly to pin the empty-body response and silent access log.
