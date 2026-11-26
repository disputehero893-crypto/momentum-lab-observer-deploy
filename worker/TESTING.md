# Worker test suite (developer guide)

Documentation only. Nothing in this file changes runtime behavior. The worker
is **paper trading only**: there is no live execution mode, and the tests are
written to keep it that way.

See also: [COVERAGE.md](COVERAGE.md) (how coverage is measured, the 8
documented-unreachable lines and the tamper-only guards) and
[README.md](README.md) (module overview).

## Run the tests offline

Python 3.12, standard library only (no third-party runtime dependencies; no
network, no API keys, no broker or data subscription needed).

```bash
cd worker
python -m unittest discover -s tests -v          # full suite (CI command)
python -m unittest discover -s tests -p 'test_risk.py' -v   # one file
```

On this branch the suite is 568 tests. On a machine with no `python` on
`PATH` (only `python3`), the two `test_worker_launcher` tests fail with
`exec: python: not found` because `bin/serve-observe` execs `python`; CI links
`python` to `python3` first and they pass. Locally you can do the same, or
run with a venv whose `python` is on `PATH`.

Coverage (optional, `coverage` is a local dev tool only):

```bash
python -m coverage run --source=momentum -m unittest discover -s tests
python -m coverage report -m --skip-covered
```

Shared fakes live in `tests/worker_fakes.py` (in-memory provider/broker
contract, invented data only). Synthetic fixtures live in `examples/`
(`demo-v1.*`, `decision-replay.synthetic.json`, `price-history.synthetic.json`);
every symbol, quote, bar and fill in them is invented.

## Safety invariants the tests enforce

These are the properties a change must not break. If a test in this list
fails, treat it as a safety regression, not a flaky test.

1. **No network.** Network-sensitive test modules patch
   `urllib.request.OpenerDirector.open` (and, where relevant,
   `momentum.publisher.build_opener`) with a side effect that raises
   `AssertionError`, so any real HTTP attempt fails the test. Transports are
   in-memory recorders. Examples: `test_coverage_wrapup.py`,
   `test_publisher_review_coverage.py`, `test_price_history.py`,
   `test_price_history_contract.py`, `test_strategy_review.py`,
   `test_strategy_review_contract.py`.
2. **No order POSTs from guard paths.** Broker-facing tests use GET-only or
   recording broker fakes and assert that no `POST` (order submit) was sent
   when a guard refuses (e.g. `test_alpaca_adapter_coverage.py` checks
   `not any(c["method"] == "POST" ...)`; the publisher/risk/demo coverage
   files assert zero order POSTs). Data endpoints are refused for non-GET
   methods.
3. **Paper-only host locks.** `momentum/config.py` pins
   `PAPER_API_HOST = https://paper-api.alpaca.markets` and
   `DATA_API_HOST = https://data.alpaca.markets`. Tests cover that:
   - `APCA_API_BASE_URL` / `ALPACA_API_BASE_URL` / `ALPACA_BASE_URL` cannot
     override the paper host, and `ALPACA_PAPER_TRADE` cannot be turned off;
   - `MOMENTUM_MODE` accepts only `demo` or `paper` ("live execution is not
     implemented");
   - `PAPER_TRADING_ENABLED` defaults to false (`paper_enabled=False`);
   - the `alpaca.py` request host lock ("API host lock rejected request") and
     the `crypto_observation.py` crypto URL lock refuse before any transport
     call, proven by tamper tests that patch the constants
     (`test_coverage_wrapup.py`).
4. **Fail closed.** Malformed, stale, future-dated or out-of-bounds inputs
   (quotes, bars, calendars, ledger rows, commands) are rejected with a reason
   code and no write/side effect; journal/lease/halt failures leave the worker
   halted rather than trading.
5. **No secrets or paths echoed.** Error output is checked not to include
   error text or input paths where the code promises not to (e.g.
   `invalid_risk_input`, `decision_replay` missing input -> exit 2).

Do not weaken these tests (or the documented unreachable guards in
COVERAGE.md) to make a change pass.

## CI

Workflow files present in `.github/workflows/` on this branch:

| File | Workflow name | Triggers | What it runs |
|---|---|---|---|
| `worker-tests.yml` | **Worker tests** | `workflow_dispatch`; `push` to `fix/journal-growth-disk-guard-port` or `tactirise/**`; `pull_request` targeting `main`, `fix/journal-growth-disk-guard-port`, or `tactirise/**`, only when the PR changes `worker/**` or `.github/workflows/worker-tests.yml` | Job `python-worker-tests`: Python 3.12, links `python`, then `python -m unittest discover -s tests -v` in `worker/`. Checkout uses `persist-credentials: false`. |
| `crypto-readonly-tests.yml` | Crypto Read-Only Worker Tests | `workflow_dispatch`; `push` to `feature/crypto-readonly-observer` | Same unittest command (without the `python` link). |
| `publish-observe-ghcr.yml` | Publish observe image to GHCR | `push` to `main` touching `worker/**`; `workflow_dispatch` | **Not a test workflow.** Builds/pushes the observe image. Do not dispatch it to run tests. |
| `make-observe-ghcr-public.yml` | Make observe GHCR package public | `workflow_dispatch` | **Not a test workflow.** Changes package visibility. Do not dispatch it. |

Once this workflow file is present on a branch, pushes to `tactirise/**` run
**Worker tests**. Pull requests targeting `main`, the journal-guard branch, or
`tactirise/**` run when their base branch already contains this workflow update.
Pull request runs are also path-filtered: a PR that touches neither `worker/**`
nor `.github/workflows/worker-tests.yml` (for example a docs-only change outside
`worker/`) does not start **Worker tests**; use `workflow_dispatch` if a run is
needed. Push and `workflow_dispatch` runs are not path-filtered.
The workflow retains read-only `contents` permission and checkout credentials
are not persisted. For a one-off ref outside those filters, manual dispatch
remains available:

```bash
gh workflow run worker-tests.yml --ref <branch>
gh run list --workflow worker-tests.yml --branch <branch> -L 1
gh run watch <run-id> --exit-status
```

Note: merging worker changes into `main` triggers `publish-observe-ghcr.yml`
(image publish), so a merge to `main` is not docs-only.

`paper-autopilot-tests.yml` ("Paper Autopilot Regression", job
`offline-verification`, Python + Node control-plane tests) does **not** exist on
this branch or on `main`; it lives on the PR #4 / #9 review stack
(`codex/tactirise-autopilot-control` and the `review/*` branches), where it
runs on `pull_request` into those stack branches, on `push` to
`codex/tactirise-autopilot-control`, and on `workflow_dispatch`.
