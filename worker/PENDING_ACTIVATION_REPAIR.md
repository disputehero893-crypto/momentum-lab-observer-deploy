# Pending paper activation: status refresh repair

Prepared 2026-11-05 against GitHub PR #9, commit
`732152e8c4c1ed1d52c0a5755717bc9a0f46991b`, in
`disputehero893-crypto/momentum-lab-observer`.

## Change

Previously, a new activation POST alone returned `activating`. Refreshing state
or retrying the same request immediately returned `paused` until the next cycle.
The gateway now derives the pending state in one place, using the current
matching durable activation and a healthy, fresh, correctly bound paused cycle.
Initial POST, GET, duplicate POST, and a newly constructed gateway agree.

The pending branch requires exact successful reconciliation and no runtime,
settings, or paper-configuration blockers. It does not refresh the runtime
timestamp, append commands on reads/retries, or report this paused cycle as
active. A newer pause and a latched stop retain precedence. Stale, future,
unbound, unavailable, or unhealthy observations fail closed.

The initial refresh-only repair changes one production file. It does not enable frontend activation,
change consent wording or request contracts, clear hazards, change trading
logic, or authorize a provider connection, deployment, or real trade.

## Reproduce the focused verification

After applying this patch at the repository root, run:

```sh
PYTHONDONTWRITEBYTECODE=1 python worker/run_gateway_offline_tests.py
```

The included runner requires Linux and `libseccomp.so.2`. It installs inherited
kernel network denial and a Python HTTP/DNS/socket audit guard before importing
tests. If those guards cannot be installed, it aborts. The initial refresh repair
covered 50 existing gateway tests and six new regression methods. The follow-up
in `ACTIVATION_CYCLE_BINDING.md` extends this runner to worker lifecycle checks.
All use injected fake Auth/DB/broker data.

Observed results:

- Six new methods on unchanged production code reproduced seven assertion
  failures, including subtests. This is expected failing regression evidence.
- Patched current-head source: all 56 focused tests passed. Seven independently
  authored methods also passed. No HTTP/DNS/socket attempts occurred in these runs.
- Retained November 4 integrated local baseline plus this patch: 669 Python and
  27 Node tests passed under network denial, with zero observed attempts.
- Current PR #9 control-plane source: 14 Node tests passed under network denial.

The retained integrated baseline includes separate local November 4 work; its
669-test result is not an aggregate result for the unchanged GitHub branch.
An attempted full current-head run was rejected by the network audit guard after
one existing observer-test `urllib.Request` attempt. Kernel denial was active;
no live provider request succeeded. That run also exposed an executable-bit
loss during local source copying. Restoring retained file modes made the focused
gateway-plus-launcher run pass (58 tests). That increment did not claim the
broad suite green. The later test-only fixture correction and guarded full-suite
result in `OFFLINE_INTEGRATION.md` now supersede that verification limitation.

## Remaining gates and limits

- The initial refresh-only repair left a separate active -> pause -> activate
  provenance defect. The follow-up in `ACTIVATION_CYCLE_BINDING.md` closes that
  issue for current worker/gateway state projections. Its narrower scope and
  raw historical cycle-record limitations are documented there.
- Consent approval, frontend enablement, hosted integration, accounting evidence,
  operational readiness, and strategy acceptance remain independent launch gates.
- The missing overnight partial-scan, durable evidence, crash/restart, and
  frontend/control-contract increments have not been recovered by this patch.
- Recreating a gateway object was tested; a whole worker/process crash was not
  tested in this increment. Synthetic tests do not establish live paper-broker
  acceptance, financial correctness beyond their assertions, or profitability.

This is a local, independently reviewed status repair, not launch clearance.
