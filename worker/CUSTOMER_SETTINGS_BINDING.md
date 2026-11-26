# Customer settings to paper execution — October 21, 2026

Status: implemented on the draft autopilot branch; not deployed. This is a
backend implementation increment, not hosted broker acceptance or a launch.

## Implemented

`momentum.customer_settings.compile_paper_settings` validates a complete,
server-loaded `autopilot_settings` row against the authenticated owner. It binds
the saved UUID and normalized row fingerprint to the exact worker review hash.
Money uses exact decimals; foreign owners, extra fields, invalid amounts,
inconsistent limits, unsupported symbols and unreviewed strategy identifiers
are rejected. The review map is server controlled and has no default approval.
A requested strategy name is not proof of a validated or profitable strategy.

The compiler applies the tighter of operator and customer risk limits and
ALWAYS returns a disarmed configuration. Configuration review, broker account
binding, current accounting, and explicit paper activation remain independent
requirements. The current execution engine is long-stock-only, whole-share,
at most one position including pending opening orders, and at most ten allowed
symbols. A requested position cap above one is conservatively limited to one
and disclosed in `customer_settings.effective_max_open_positions`. Crypto and
live settings are rejected, not silently converted into stock paper settings.
No crypto observation behavior has changed.

`PaperAutopilot` now accepts an optional `customer_binding` plus a required
`settings_loader(owner_id)` for this mode. It checks the current saved row on
activation, each cycle and at the entry-submission guards. Saved UUID, full row
fingerprint and worker hash are all part of durable authorization. Changing
any one cannot reuse the old activation. Observing a changed/invalid row
persists a pause, so later restoring an old row cannot silently resume entries.
A temporary loader exception blocks entries without revealing upstream errors.

The separate entry guard does not disable protective exits. Already-authorized
positions retain reconciliation, opening-remainder cancellation and protective
exit handling under the existing broker/ledger/quote gates. Pause and emergency
stop still work from stale settings screens. Command acknowledgment is not fill
or liquidation confirmation. Restart preserves idempotency and stop latches;
reconfiguration with open exposure is rejected.

## Verification

34 added tests use invented settings, a test-only reviewed-strategy map and the
existing fake venue. They cover sizing against customer limits; owner, version,
hash and fingerprint checks; setting changes during market-data/broker reads;
loader failures; partial fills, cancellation and protective exits; stop latches;
pending-order position caps; restart and duplicate command behavior.

Run the complete suites without suppressing or skipping failures:

```
cd worker && python -m unittest discover -s tests -v
cd .. && node --test control-plane/core.test.mjs
```

The branch's Paper Autopilot Regression workflow runs Python 3.12 and Node 22
and stores test logs plus the exact tracked source and SHA-256 checksums. The
hosted run is the verification source for that exact commit. Local Python 3.13
in this working environment preloads networking modules before subprocess code;
the unchanged replay-import-boundary test fails on both the original baseline
and candidate here. No test was weakened or removed to hide that limitation.

## Remaining before hosted activation

- Implement the authenticated customer gateway and deploy a bounded,
  owner-scoped settings loader; do not accept browser-supplied rows or treat a
  UUID as authentication. The current CLI is NOT automatically wired to this
  optional constructor path, and no execution URL/secret was configured.
- Provision the correct account-bound worker and a genuinely reviewed strategy
  registry. This patch does not promote the existing experimental strategy.
- Resolve real point-in-time strategy inputs and independently reconciled
  accounting/fee coverage, then complete actual Alpaca paper acceptance.
- Design command/settings concurrency across services. These rechecks cannot
  retract an already in-flight broker order or make database and broker writes
  atomic. A later revision must trigger reconciliation and cancellation.
- Verify authenticated desktop/mobile state, consent, pause/stop and failure
  recovery before customer publication. Build crypto's separate execution path.

The deployed GET-only observer, main branch, database schema, customer-facing
publication, broker credentials and real-money execution are unchanged. No real
paper or live broker order was placed by this work.
