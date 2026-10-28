# Durable paper-account accounting

`momentum.ledger` adds a local, persistent accounting component. It imports normalized evidence and projects deterministic FIFO results for a long-only paper account. It has no network client, credential reader, scheduler, or order operation. A successful projection does not enable execution.

The storage uses the existing journal SQLite path, with separate append-only accounting tables. It does not modify the existing `events` or `trades` records. A late fee or corrected execution produces a new projection identity while preserving prior source revisions and immutable journal entries.

## Public interface

```python
from momentum.ledger import AccountingLedger

ledger = AccountingLedger(
    config.db_path,
    account_id=verified_paper_account_id,
    environment="paper",
    source="alpaca_account_activities",
)
receipt = ledger.ingest_batch(
    normalized_records,
    batch_id=unique_import_receipt_id,
    coverage=account_bound_coverage,
)
snapshot = ledger.snapshot(
    session_date=current_new_york_date,
    now=evaluation_time,
    cost_policy="actual",
)
report = snapshot.to_dict()
```

`LedgerSnapshot` stores an immutable serialized payload. `to_dict()` and `payload` return copies. `complete`, `blockers`, and `projection_id` are read-only conveniences. `revisions()` returns the normalized source audit history, never a raw provider response.

The session-facing fields are `session_date`, `realized_pnl`, `fees`, `as_of`, `source`, and `complete`. **Unknown authoritative amounts are `null`.** An integrating runtime must inspect `complete` and the amount fields before using them in risk calculations. It must not replace `null` with zero. The separate `observed_realized_pnl` and `observed_actual_fees` fields are observed subtotals, which can remain incomplete.

Additional report fields include `blockers`, `projection_id`, exact `account_id`, `environment`, `cost_policy`, `cost_basis`, `actual_costs_complete`, `execution_accounting_complete`, `net_realized_pnl`, `open_lots`, `matches`, `observed_execution_fees`, `unallocated_fee_ids`, and the immutable coverage receipt. `execution_armed` is always false.

## Normalized records

Every record has these fields:

| Field | Meaning |
| --- | --- |
| `source_id` | Stable identity within the exact account and source namespace. |
| `revision` | Positive, authoritative source revision. An unversioned adapter uses 1. Receipt order is not a revision. |
| `kind` | `fill` or `fee`. Unknown activity types must remain import blockers. |
| `status` | `active` or `canceled`. A cancellation is a new revision; it never deletes evidence. |
| `source_fingerprint` | Optional lowercase SHA-256 of the source row, so changed metadata also conflicts. Raw rows are not persisted here. |

An active fill also requires `effective_at`, `symbol`, `side` (`buy` or `sell`), `quantity`, and `price`. `order_id` is optional. Quantity and price must be positive exact decimal values. No short-inventory repair is attempted. A cancellation contains its original execution timestamp, identity, revision, kind, status, and optional source fingerprint, without quantities or prices.

An active fee requires signed `amount`, expressed as a cost: charges are positive and credits are negative. It has either a timezone-qualified `effective_at`, or `effective_date` with `date_basis="provider_activity_or_settlement_date"`. A date-only source is not converted into an invented timestamp. Optional `execution_id` supports explicit fee allocation; its absence never creates a guessed relationship. A canceled fee retains its time/date basis and omits the amount and allocation.

Timestamps normalize to UTC while retaining up to nine fractional digits. More precise timestamps are rejected. Financial inputs accept plain decimal strings, integers, or plain-decimal `Decimal` values; floats, booleans, nonfinite numbers, exponents in strings, excessive scale, and absolute values above 10^18 are rejected. Internal arithmetic uses an independent 96-digit, half-even Decimal context. Proportional fee allocation preserves the residual on the remaining lot.

## Coverage and opening inventory

`ingest_batch` requires all these coverage keys:

| Field | Required evidence |
| --- | --- |
| `account_id`, `environment`, `source` | Must exactly match the ledger instance. Only `paper` is supported. |
| `window_start`, `window_end` | Accounting/import bounds with `window_start < window_end`. |
| `as_of` | Evidence watermark, at or after `window_end`; never the later projection time. |
| `executions_complete` | Explicit boolean indicating independently established execution completeness. Exhausting REST pages alone cannot establish this. |
| `fees_complete` | Explicit boolean for actual cost completeness. An absent fee row is not proof of zero fees. |
| `opening_lots_complete` | Explicit boolean for reviewed inventory and basis at the exact start boundary. |
| `opening_lots` | Complete opening lot list, or an explicitly incomplete list. |
| `evidence` | Nonempty evidence reference string or list of strings. Use bounded references, without secrets or raw provider bodies. |
| `blockers` | List of unresolved import/accounting codes. These always keep the projection incomplete. |

Optional `opening_evidence` is mandatory when `opening_lots_complete` is true. An empty opening-lot list only represents a known flat boundary when the explicit completeness flag and opening evidence are both present. The account being flat now does not prove that it was flat at the historical boundary.

Each opening lot contains `lot_id`, `symbol`, `quantity`, `price`, and `opened_at`, with optional originating `execution_id`. Lot identities must be unique and opening timestamps must precede the exact window start. The runtime must independently validate a baseline file's account, environment, and start boundary before merging its lots and evidence into a receipt.

A minimal incomplete receipt can contain no records, `opening_lots=[]`, `opening_lots_complete=false`, `fees_complete=false`, `executions_complete=false`, and honest import evidence. The ledger accepts and persists that observation, then reports the missing gates. Completeness is never inferred from an empty import.

### Creation time and economic time

An activities query can use creation-time bounds while a fill carries an earlier economic execution time. The importer must retain such backdated rows. A pre-window execution blocks the projection unless a reviewed opening lot explicitly identifies the execution and matches its symbol, timestamp, price, and bounded remaining quantity. An explicit correction before the boundary requires rebuilding history from an earlier trusted anchor. It cannot silently disappear behind the baseline.

## Projection and cost policies

Fills are ordered by their exact UTC nanosecond timestamp, then stable source identity for a reproducible observed report. An opaque source or lot identity does not prove economic order. Opposite-side executions at the same exact timestamp, multiple same-symbol buys at that timestamp, and same-symbol opening lots sharing an opening timestamp are explicitly ambiguous and block completeness. Equal-price buys remain ambiguous because their linked fees can differ. These projections leave authoritative realized P&L unknown and do not report complete per-match fees or net P&L.

FIFO matches consume only known long lots. A sale exceeding known inventory blocks the result; a later purchase cannot repair the missing earlier position.

Gross realized P&L includes exits on the requested New York session date. Earlier fills establish carried inventory. The projection also reports prior matches within its replay window, so consumers should use each match's closing date when displaying a session-only trade list.

Daily fees use the explicit fee timestamp's New York date or the provider's stated activity/settlement date. The latter remains labeled as such. Session costs charge all recognized fees for that date, including entry costs on lots that remain open. This accounting policy is separate from a claim about a broker's tax-lot basis or statement convention.

The default `actual` policy requires evidenced fee completeness. Unallocated account fees can contribute to a known session total while individual matched trades retain `fees=null` and `net_pnl=null`. Reviewed carried lots also retain unknown entry fees. Per-match costs are only allocated when the available fee evidence and execution references permit it. Late fees and explicit fee revisions restate the projection without rewriting earlier source or journal records.

`paper_zero_fee_simulation` is a separate, explicitly selected paper policy. It sets the scenario's session fee input to zero and clearly labels that simulation. Observed actual fees remain visible; actual cost completeness is not invented. The policy does not waive execution, opening-inventory, freshness, conflict, or unsupported-activity blockers. No setting in this component authorizes orders.

A net negative actual fee total is preserved as an observed credit but leaves the session-facing fee amount unknown and adds a blocker, because the existing risk/session contract requires nonnegative fees. It is not silently clamped to zero.

## Durability, limits, and readiness

Source revisions, import receipts, and conflict evidence have SQLite triggers preventing update or deletion. Each successful import and its coverage commit atomically. Identical revisions and receipts are idempotent, including concurrent retry. A changed source identity/revision produces durable conflict evidence and rejects the rest of that batch. Only an explicit higher provider revision resolves that source conflict. A changed immutable batch identity remains a blocking audit issue requiring review; changing the receipt ID cannot erase it.

Snapshots use a single SQLite read transaction and select the latest receipt, including an incomplete one. They never fall back to an older favorable receipt. Projection identities depend on the normalized economic state, coverage, policy, and unresolved conflicts; they do not depend on insertion order or later evaluation time. Historical revisions remain separately available in the audit tables.

Imports are limited to 10,000 records, opening inventory to 10,000 lots, and a projection/audit read to 100,000 source revisions or conflicts. Exceeding a projection bound fails closed. The current session must be covered from its New York midnight, and the earlier of the import watermark and covered window end must be no more than five seconds old. Recomputing the same snapshot never refreshes that timestamp.

Completeness in this module means the supplied accounting evidence satisfies these local gates. The integrating runtime must still establish provider/account identity, provenance, current positions and orders, import completeness, ownership, data freshness, and any unresolved account activity. It must preserve observation-only operation until subsequent acceptance work is complete.

## Validation scope

Run the focused, entirely offline suite from `worker/`:

```bash
python -m unittest discover -s tests -p test_worker_ledger.py -v
```

The fixture tests cover FIFO partial exits, inventory carry and session rollover, revisions and cancellations, backdated records, reordered and duplicate imports, durable conflicts, concurrent retry and restart, SQL append-only protection, account/source isolation, unknown/signed/date-only/late fees, explicit simulation, fee allocation residuals, nanosecond ordering and freshness, and independence from the caller's Decimal context.

These fixtures do not prove real Alpaca activity completeness, fee finality, source correction semantics, or broker accounting equivalence. No real credentials or broker calls are used by this module or its tests.
