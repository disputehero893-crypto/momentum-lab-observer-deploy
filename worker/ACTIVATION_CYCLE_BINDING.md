# Current activation must have its own worker cycle

Prepared 2026-11-05. The cumulative status-and-cycle patch targets
`disputehero893-crypto/momentum-lab-observer`, PR #9 commit
`732152e8c4c1ed1d52c0a5755717bc9a0f46991b`.
It includes the earlier pending-status refresh repair. Apply this cumulative
patch instead of the earlier refresh-only patch, not on top of it.

## Existing contract and defect

`CUSTOMER_GATEWAY.md` requires a fresh, matching, reconciled, entry-enabled
actual worker cycle for active status, not an activation receipt. Previously,
an active cycle followed by pause and a new activation could immediately look
active again even though no cycle had run for the new command. Comparing times
alone would be insufficient: multiple commands and cycles can share a timestamp.

## Correction

Each cycle captures the latest durable command's event ID at its start and
persists it as `command_event_id` in the cycle payload. Existing `_control()`
callers retain the same command payload contract. No journal table migration
or external API request change is required.

`PaperAutopilot.state()` projects an active cycle as paused with entries disabled
unless the cycle's nonempty command event ID matches the current durable command
and its instance ID matches this worker. The customer gateway consequently
reports activating for healthy pending evidence. Only an actual subsequent cycle
for the current activation can restore active status. Reads and retries do not
rewrite raw evidence, extend freshness, append commands, or execute a cycle.

Offline status, latest pause, emergency stop, and risk latches retain precedence.
Missing/legacy provenance is conservative until the next cycle. The normal
restart path starts with no runtime observation and must complete a new cycle.
Consent and execution authorization are unchanged. No frontend activation is
enabled, hazard cleared, provider contacted, or trading logic changed.

## Verification

Run from the repository root on Linux with libseccomp.so.2 installed:

```sh
PYTHONDONTWRITEBYTECODE=1 python worker/run_gateway_offline_tests.py
```

The supplied runner installs inherited kernel network denial and a Python
HTTP/DNS/socket audit hook before test imports. Guard failure aborts the run.
Results for this increment:

- 123 focused current-head tests passed, including eight new regression methods.
- Ten independently authored regression methods passed. Before the fix, that
  suite reproduced six failing assertions, including provenance subtests.
- The separately preserved November 4 integrated local baseline plus both
  repairs passed 677 Python tests. This is not a full GitHub-head suite result.
- Passing runs reported zero HTTP/DNS/socket attempts and used fake transports.

Cases cover equal timestamps, fresh activation without an intervening pause,
duplicate and older request IDs, command changes during a cycle, missing/foreign
command provenance, prior worker instances, settings-change pauses, journal
unavailability, unhealthy/stale evidence, gateway reconstruction, and restart.

## Limits

The guarantee concerns current state projections, including the customer gateway
and operator state endpoint. Raw append-only cycle records and direct `cycle()`
results remain historical observations of their captured command. A reentrant
command change during a cycle does not relabel that raw record as evidence for
the new command. Consumers must use `state()` for current status; this patch
does not redesign execution cancellation or mid-cycle authorization checks.

This increment originally left an unrelated network-capable observer fixture
unresolved. The later test-only correction in `OFFLINE_INTEGRATION.md` closes
that local verification blocker without changing production logging. Missing
overnight increments remain missing. These local tests do not establish hosted
integration, real paper-broker acceptance, crash durability, financial
correctness beyond their assertions, strategy profitability, or launch readiness.
