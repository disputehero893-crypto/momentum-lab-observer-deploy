# Offline verification for the cumulative status repair

Prepared 2026-11-05 against PR #9 of
`disputehero893-crypto/momentum-lab-observer`, commit
`732152e8c4c1ed1d52c0a5755717bc9a0f46991b`.
The cumulative patch includes the pending-status and activation-cycle repairs.
Apply it directly to that commit instead of either earlier patch, not on top
of them. Prior saved artifacts remain unchanged.

## The blocked request

The exact leaking fixture was
`ObservationTests.test_real_publisher_contract_in_full_worker_flow_with_invented_cloud_transport`
in `tests/test_worker_observation.py`. A fake publisher did not replace the
separate paper-run logger. Worker entry called `start_session`, `start_run`,
`request`, and the logger's real `_http` implementation. Best-effort logging
swallowed the exception, allowing the test assertions to pass even though the
outer network audit guard correctly rejected the run.

A safe before/after probe replaced `_http` with a recording exception before
any request could occur. Before this repair, the original test passed but the
probe detected one real-transport invocation. Afterward it detected zero. No
network access was enabled to diagnose or fix the problem.

## Test-only correction

`tests/observer_fixtures.py` constructs the real worker normally, then replaces
its logger with the existing `DisabledPaperRunLogger` before lifecycle entry.
All six observer construction sites use this explicit offline helper. The
constructor performs configuration validation but no transport I/O. A regression
checks that raw production construction still selects the enabled logger.

Existing opener-blocking mocks now assert that they were never called, so a
caught transport exception cannot silently pass. Replay and launcher subprocess
fixtures install the Python audit guard before application imports. Synthetic
negative tests verify that caught audit violations still fail a child process;
they do not perform an actual HTTP, DNS, or socket operation.

No production file changes in this isolation increment. The cumulative patch
still contains the earlier two production reporting fixes. Production logging,
execution authorization, consent, and frontend activation are unchanged here.

## Run and results

From the repository root, on Linux with `libseccomp.so.2` available:

```sh
PYTHONDONTWRITEBYTECODE=1 python worker/run_kernel_offline.py
```

The wrapper fails closed if it cannot install kernel network denial. Denial is
inherited by subprocesses. A Python HTTP/DNS/socket audit guard is installed
before discovery and catches attempts even when application code swallows the
exception. The workflow's Python test command now uses this wrapper too; this
is a local workflow edit, not a hosted CI run.

Verified on the current-head-derived source plus the cumulative patch:

- 467 Python tests passed in both the implementation and independent full runs.
- Two additional independent observer-lifecycle/default-logger checks passed.
- 14 unchanged Node control-plane contract tests passed under network denial.
- The specific fixture probe changed from one intercepted transport invocation
  to zero. Passing runs recorded zero actual HTTP/DNS/socket attempts.

The 467 tests comprise the original 447 methods plus six pending-state, eight
activation-cycle, and six fixture/guard regressions. The broader retained
November 4 integrated candidate is a different source set; its extra accounting
and other increments are not silently merged into this patch.

## Remaining limits

Local verification is now unblocked. This does not establish hosted CI success,
deployment, signed-in frontend acceptance, consent approval, live provider or
paper-broker integration, real trades, profitability, or launch readiness. The
missing overnight increments have not been recovered. Current state projections
are distinct from raw historical cycle records, as described in
`ACTIVATION_CYCLE_BINDING.md`. No live operations or denied downloads were used.
