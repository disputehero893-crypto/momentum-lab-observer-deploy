# Final paper mutation guards

Prepared November 5, 2026 on exact PR18 head
`11d50c1fed78a0554df5711216d0a5a3ff19ec7c`. Draft source only; not deployed or
activated. All regression observations use invented accounts and fake transports.

## Reproduced gaps

The final customer-settings read can block after earlier checks have passed.
On unchanged PR18, advancing fake time during that read allowed a synthetic buy
after quote/intent freshness expired. Setting the shutdown flag during that read
also allowed a synthetic buy. A shutdown during cancellation lookup similarly
allowed a synthetic DELETE. No actual provider request occurred.

## Bounded correction

- The session checks current shutdown, ownership, reconciliation, ledger and
  session-date evidence after the potentially blocking settings read.
- The adapter repeats quote, receipt, intent and risk freshness validation after
  its final guard and before POST. A rejection after the durable request record
  uses the existing `paper_order_not_sent` evidence path.
- Every session cancellation path passes a process/ownership guard. The adapter
  requires exactly `True` before and after lookup and after the journal write,
  before DELETE. A final rejection records `paper_cancel_not_sent`, not an
  accepted cancellation. The existing three-request retry cap still counts
  persisted attempts, including not-sent attempts; it is not silently reset.
- Cancellation does not depend on new-entry permission or the kill switch being
  clear. Emergency halt can still cancel opening remainders. Settings outages
  continue to block buys without disabling otherwise-authorized protective exits.

The cancellation callback is optional for direct `PaperClient` callers to retain
the existing API. The session supplies it in all three production call sites.
Injected providers must honor the updated interface rather than drop the guard.

## Verification

Nine new regressions pass on the correction; seven fail on unchanged PR18.
The full corrected source passes 494 Python tests under inherited kernel
network denial and an HTTP/DNS/socket audit guard, with zero attempts. The 17
Node settings tests pass. Separate review reproduces the original three failing
cases against the correction. Hosted verification belongs to the exact draft
commit's CI run and is not inferred from these local results.

## Remaining release boundaries

These are final local rechecks, not an atomic transaction across the settings
database and broker. An already in-flight request cannot be retracted by this
change. Actual authentication/database/UI consent acceptance, runtime concurrency
and latency, crash durability, strategy/accounting evidence and real paper-provider
acceptance remain separate gates. The exact Render service and deployed revision
remain unverified; do not bypass the existing access denial.

No activation, deployment, real trade, provider call, credentials, or payment is
introduced. Main's existing image-publication side effects remain a separate
release decision.
