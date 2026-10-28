# Observation worker: implementation and deployment handoff

This milestone adds a continuous **GET-only Alpaca Paper observer**, durable activity accounting, and worker status/halts for the existing dashboard. The original implementation passed offline tests with invented, injected provider responses. The dedicated Render service is live with its persistent disk. Initial hosted checks verified public liveness, private-request authentication enforcement, fresh Paper account observation and advancing owner-bound cloud status with execution disabled. The implemented [Market Watch](MARKET_WATCH.md) increment adds quote-only observation of the approved AAPL, MSFT and SPY connection-test watchlist on IEX. Closed-session delivery is now separately verified: the three IEX rows reached the correct owner with advancing receipt/heartbeat times and unchanged Friday source times correctly marked stale. Active-session data behavior, signed-in Market Watch rendering, financial completeness and the remaining operational acceptance are open. See [DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md) for actual hosted evidence and source/deploy IDs.

The saved app connection was independently verified earlier: ACTIVE paper account, no blocking flags, at 2026-10-18T18:29:09.706582Z. That result is recorded in `../docs/account-link-acceptance.json`. It does not provision this worker or establish market-data connectivity.

## What runs

The publisher also derives an optional [observed-symbol requirements review](STRATEGY_REVIEW.md)
from the same Market Watch report and current validated strategy configuration.
It checks the range of the fresh observed ask and last trade; gain, RVOL, float
and catalyst remain unavailable. The overall review is always incomplete and
does not invoke the strategy/risk engine or add a provider request. The separate
[offline decision replay](DECISION_REPLAY.md) runs only against explicitly
supplied local input files and is not started by the observation service.

`ObservationWorker` acquires the existing persistent-journal lease before restoring risk state or checking the journal's owner binding. Every cycle polls cloud halt commands, verifies the actual paper account identity, collects configured market observations, optionally imports activity history, and calls `PaperSession.cycle(..., execute=False)`. Market collection precedes accounting. It polls commands again before publishing current status and records the cycle locally.

The default market path first collects the independently cached, bounded price-history observations described below, then requests the current New York day's regular-session calendar and one bounded snapshot batch for the configured symbols. The quote collector itself is independent of historical bars, strategy metadata and an RVOL baseline. It explicitly supplies an empty strategy-snapshot list to `PaperSession`, so quote display does not imply a strategy evaluation or trigger the strategy-history builder. An injected builder remains available for deterministic component tests. The accounting-history anchor is still blank; missing financial evidence does not prevent quote collection and is never filled with invented values.

There are three separate execution boundaries: the runtime always requests `execute=False`; its provider facade exposes only reads; and its default `ReadOnlyPaperClient` refuses POST/DELETE and refuses `submit`/`cancel` before any broker request or order journal write. `PAPER_TRADING_ENABLED=true` cannot arm this runtime. There is no live host or live execution mode.

One synchronous cycle runs at a time. The scheduler waits for the remainder of `POLL_SECONDS`, or at least one second after a slow cycle. It does not launch overlapping cycles or issue a burst of missed cycles. SIGTERM/SIGINT request cooperative shutdown, subsequent broker reads are refused, the HTTP service is drained, and ownership is released after the worker stops.

The lease protects one filesystem. It is not a distributed account lock. The journal binds to one paper account UUID and one app owner; a different account or owner cannot silently reuse it. Do not launch a second journal against the same account or discard an existing journal to bypass its ownership or halt state.

## Commands

From `worker/`, after secure server configuration:

```sh
python -m momentum observe --once
python -m momentum serve-observe
```

The first command performs one cycle and exits. A cycle error produces exit code 2 and a fixed diagnostic code. The second runs the observer and authenticated HTTP API together. Public `/health` reports process liveness; private `/api/readiness` and `/api/state` report the actual in-process worker and latest local cycle. An old journal record cannot make a restarted service appear connected.

`python -m momentum serve` remains the demo/journal API, without observation. `paper-preflight` now uses the GET-only adapter and does not need `PAPER_TRADING_ENABLED=true`. The worker has no shipped order-execution command.

## Server configuration

The worker does not load `.env` automatically. Provision values in the host's encrypted environment/secret settings. No secret belongs in frontend JavaScript, this package, chat, logs, readiness responses, or SQLite payloads.

| Setting | Meaning |
| --- | --- |
| `MOMENTUM_MODE=paper` | Explicitly selects paper observations. |
| `PAPER_TRADING_ENABLED=false` | Retained in the deployment; observation is independently incapable of orders. |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Paper credentials in the worker's server environment. The current worker does not automatically retrieve the app's Vault link. |
| `SUPABASE_URL` | Dedicated project: `https://wusnuhzjslikzswkahle.supabase.co`. |
| `SUPABASE_SERVICE_ROLE_KEY` | A server-only `sb_secret_` key or legacy service-role JWT for the dedicated project. This is a privileged secret; the publishable browser key is insufficient. |
| `OWNER_USER_ID` | The confirmed app user's exact UUID, provisioned consistently before the journal is first bound. |
| `WORKER_CONTROL_TOKEN` | Independent private-HTTP bearer secret. The Blueprint requests platform generation; verify the hosted private-API configuration without exposing the value. |
| `JOURNAL_PATH` | `/var/data/momentum.sqlite3` on the persistent disk. |
| `ALPACA_DATA_FEED` | `iex` by default; an entitlement failure never causes a silent feed switch. |
| `WATCHLIST` | At most ten explicit symbols. The approved connection-test selection is `AAPL,MSFT,SPY` on IEX; it does not select daily momentum candidates. |
| `SYMBOL_METADATA_PATH` | Optional operator-maintained point-in-time float/catalyst facts for strategy evaluation. Not required by the default quote-only Market Watch path; missing facts remain missing. |
| `POLL_SECONDS` | Five seconds by default; not a guarantee of cycle or halt latency. Quotes may age beyond their independent two-second freshness limit between polls. |
| `ACCOUNTING_HISTORY_START` | Explicit, timezone-qualified history anchor. Still blank for this increment: import is skipped and accounting remains incomplete. |
| `ACCOUNTING_BASELINE_PATH` | Optional reviewed opening inventory at that exact anchor, bound to the account and paper environment. |
| `ACCOUNTING_COST_POLICY` | `actual` by default. The separately named `paper_zero_fee_simulation` is an explicit simulation assumption, never proof of actual zero costs. |

Cloud settings are all-or-none for this runtime. A partial URL/key/owner configuration is rejected. Provider credentials can be absent for a diagnostic start, but account observation then fails and reports disconnected. Neither configuration presence nor the prior saved link produces a connected state.

### Reusing the saved account link

The existing connection Edge Function safely uses Vault inside the server. Its internal `begin_recheck` operation is tied to a confirmed user's active app session and a short connection-check lease. It is not a headless worker credential endpoint. This worker does not call it, borrow a user session, or read Vault plaintext through assistant tools.

The user completed secure provisioning of host-held paper credentials in Render, and successful observation verifies that route works. Disconnecting the app's saved link does **not** revoke the separately provisioned host copy. Stop the worker before changing those credentials or its owner/account binding.

A tighter future bridge can reuse saved keys inside the existing trusted server, grant an explicitly registered worker access only to fixed paper GETs, and invalidate that access on relink/disconnect using a connection generation. That bridge is not implemented in this milestone. A Vault secret UUID is not a safe generation marker because relinking can update a secret in place.

## Accounting evidence

For the approved Market Watch increment, `ACCOUNTING_HISTORY_START` remains blank. The observer reports `accounting_history_start_missing`, `complete=false`, and unknown authoritative fees/realized P&L as null. The optional importer described below runs only when an explicit history anchor is supplied. Quote collection occurs first and has no dependency on that anchor, strategy metadata or historical-volume inputs.

`activities.py` reads every activity type within fixed creation-time bounds. It follows exclusive activity-ID cursors until an empty page is observed, including after a short page. Request bodies, redirects, hidden type filters, oversized responses, repeated IDs, conflicting content, clock regressions and scan caps fail closed. The scan has a 30-second elapsed budget checked between requests and the adapter's per-request timeout; it cannot interrupt an already running request.

Fills use their actual `transaction_time`, preserve nanoseconds, and use execution quantity rather than cumulative order quantity. Date-only fees keep the provider's activity/settlement date. Signed credits stay signed. Deposits and withdrawals do not become trading profit. Unknown activities and corrections lacking authoritative revision semantics remain blockers.

The durable ledger appends normalized revisions, immutable coverage receipts and conflicts. It computes a long-only FIFO projection with Decimal arithmetic, carried lots, partial exits, signed costs and explicit higher revisions/cancellations. Its output includes observed subtotals, unknown authoritative amounts as null, coverage blockers and a projection identity. See `LEDGER.md` for the exact schema and limitations.

Pagination exhaustion alone does not prove complete executions, opening inventory, or final costs. The production REST importer leaves those financial completeness flags false. A history anchor and opening-baseline file do not independently satisfy the missing execution or fee evidence. The checked-in runtime cannot turn those flags on through an environment variable.

Alpaca explicitly says paper trading does not simulate regulatory fees. Consequently an empty fee stream does not justify a cost-adjusted profit claim. The optional zero-fee policy is separately labeled and still requires complete execution/opening evidence. The user has not selected that policy.

The baseline file has exactly `schema_version` (`1.0`), `account_id`, `environment` (`paper`), `window_start`, `evidence`, and `opening_lots`. Each lot follows `LEDGER.md`. The account and exact anchor must match; lots must predate the anchor. An empty lot list is accepted only with explicit reviewed flatness evidence, never inferred from today's account. The file is held fixed for the process lifetime.

The older cumulative-order trade journal remains an independent observation log; its unavailable per-trade fee/net fields are not rewritten. The revision-aware ledger carries corrections and newly evidenced fee allocation. Account equity changes are never substituted for realized trading P&L.

## Dashboard and halt behavior

The publisher matches `frontend/src/lib/paper.ts`: owner, instance, paper mode, heartbeat, actual data timestamp and status strings. It can publish `observing`, `halted`, `error`, or `disconnected`; it never publishes `ready`. Broker and data feed are derived independently, and every payload explicitly disables execution. The optional `worker_state.payload.market_watch` adds a bounded, allowlisted envelope of provider quotes/trades and regular-session evidence without a new database table. An absent envelope means this format has not been reported; it must not be replaced with synthetic prices. See [MARKET_WATCH.md](MARKET_WATCH.md) for the exact contract.

Broker connectivity requires completed account, positions and open-order reads, with a verified account and a recent account request timestamp. Quotes need the configured feed, valid prices, source time and actual HTTP receipt time for the selected watchlist. Both source and receipt must be within the default two-second freshness window, neither may be in the future, and source must precede or equal receipt. Last trades retain independent timestamps and freshness states. A fresh heartbeat cannot refresh old market evidence. The dashboard's thirty-second heartbeat cutoff still applies, and Market Watch must recompute quote/trade age against wall-clock time between five-second worker polls. Valid old prices can remain visible as stale. Rows are rechecked before publication, so later accounting or account reads cannot extend their freshness. Private readiness also expires reconciliation according to its account, ledger and quote freshness limits.

Session state describes only the regular session for the current New York calendar date. Its dated open/close evidence must be no more than five minutes old; missing, invalid or stale calendar evidence produces an unknown state. A closed regular session does not establish premarket, after-hours or overnight data availability. Missing or malformed observations are explicit per symbol, allowing other valid rows to remain visible.

The separate optional `price_history` sibling adds raw daily closes for the
calendar-verified prior session and a bounded window of up to ten elapsed
regular-session minute intervals. Its historical GETs run before the current
quote request. The whole report is cached for 60 seconds with original timestamps;
request/receipt evidence expires after 120 seconds and its own calendar evidence
after 300 seconds. The current trading day never borrows yesterday's candles
before its first regular interval ends. Nontrading dates visibly reference the
prior calendar session. Missing intervals remain gaps and ended bars can be
revised. The raw close is not an official close or verified comparable across
corporate actions, so the selection gain requirement remains incomplete. See
[PRICE_HISTORY.md](PRICE_HISTORY.md) for the precise contract.

Full history remains in bounded current worker state and its latest owner-scoped
publication. The repeated cycle journal records only a safe history summary and
canonical report hash to avoid writing every candle again every five seconds.
Those hashes cannot reconstruct past inputs and are not a provider-capture
archive or evidence of a historical replay result.

Cloud halt handling validates the full returned batch, persists and reads back the local halt, then requests an acknowledgment. It verifies the returned command identity and status. A failed poll, malformed command or uncertain acknowledgment leaves new entries halted. The acknowledgment does not claim orders were canceled or positions liquidated. Remote resume remains unavailable.

The existing app's broker/feed indicators and halt control consume these rows. User-provided signed-in Connections screenshots at `2026-10-18 21:23 UTC` and `21:27 UTC` visually confirmed Supabase `Verified`, worker `Connected · observing`, broker `Connected · worker-reported` and Trading `Disabled`. The screenshots still showed older static wording. The corrected Lovable source passed a fresh localhost preview test; the updated hosted wording remains to be visually confirmed. See `DEPLOYMENT_STATUS.md` for the frontend commit IDs. This milestone does not upload the local accounting/cycle journal to `paper_runs` or `paper_events`; those dashboard result tables remain unchanged.

Halt handling is synchronous. A slow provider/history read can delay command processing and allow the cloud heartbeat to expire. This runtime never submits an order while waiting. A separate fast supervisor, stream/cache and measured halt responsiveness remain prerequisites for a future execution release.

## Approved hosting and current deployment

`render-observe.yaml` records the intended Docker web service with the observer and private API in one process, one persistent disk, and service auto-deploys disabled. The existing Render service follows that observation configuration. The image installs an executable `serve-observe` launcher that uses `exec` to route through the privilege-dropping Python entrypoint. This supports direct executable launch while retaining the existing image ENTRYPOINT and default CMD; it does not depend on an assertion that Render always preserves ENTRYPOINT. The Blueprint file path is `worker/render-observe.yaml` relative to the repository root.

| Item | Researched base estimate |
| --- | ---: |
| `0.5c-512mb` web service | $7/month |
| 1 GB persistent disk | $0.25/month |
| Hobby workspace | $0/month |
| **Additional base total** | **$7.25/month** |

These published prices were checked on October 18, 2026; the compute source labels its prices September 2026. The user approved the $7.25/month base hosting estimate, and the service and initial 1 GB disk have been created. The estimate excludes tax, excess bandwidth/build usage, extra disk growth and any paid workspace plan the user independently chooses; it is not a statement of the final invoice.

Render is connected to Derek's confirmed workspace, and the private source repository is [disputehero893-crypto/momentum-lab-observer](https://github.com/disputehero893-crypto/momentum-lab-observer). The executable-launcher correction resolved the earlier exit-128 launch failure. After the user completed secure credential provisioning, deploy `dep-dbajaf5g1s2s73cb18f0` became live at `2026-10-18T21:08:34Z`. Fresh GET-only Paper observations and advancing worker status for the verified owner were confirmed, alongside public HTTP 200 and private unauthenticated HTTP 401 checks. Service auto-deploys are off. A user-provided screenshot at `2026-10-18T21:41:37Z` separately verified the `momentum-lab-observer` Blueprint Settings display as `Auto Sync = No`, with branch `main` and path `worker/render-observe.yaml`.

The 1 GB disk is an initial observation footprint, not an archival capacity guarantee. Activity receipts and audit events are append-only. Measure growth during acceptance, back up SQLite consistently, and size/archive deliberately before extended operation. Do not silently delete audit or risk state. Render's disk prevents multiple instances and introduces restart/deployment downtime.

## Acceptance still required on the real host

1. Review actual billed usage against the approved base estimate. The service is live; secure provisioning, repository access, one disk/instance, the initial liveness/private-authentication checks and separate Blueprint Auto Sync off verification are established.
2. Visually confirm the hosted frontend's corrected static wording. Backend publication for the correct app owner, fresh Paper broker observation with execution disabled and the signed-in functional connection badges have passed verification. The wording corrections have passed a fresh localhost preview test; updated hosted rendering remains unverified.
3. Verify the approved AAPL, MSFT and SPY Market Watch increment against actual IEX observations and the signed-in view, following [MARKET_WATCH.md](MARKET_WATCH.md). First confirm owner-bound publication, original timestamps and explicitly stale old prices during a closed regular session; then confirm advancing source times, consistency, request/publication latency and UI expiry during an active supported data session. Record the actual deploy and evidence in `DEPLOYMENT_STATUS.md`. Strategy metadata/history and complete accounting are separate future inputs, not prerequisites for quote display.
4. Queue an app halt, observe durable local state before cloud acknowledgment, restart the host, and confirm the halt remains. Validate broker/cloud failures and stale-heartbeat behavior.
5. Verify journal backup/restore and disk growth, accounting history/opening evidence, late events, provider lifecycle behavior and loss accounting. Complete the missing financial coverage and streaming/cache integration before designing any order-execution launch.

No real worker broker requests, paper orders, live orders or paid resource creation were performed by the original implementation tests. Docker and a listening host were unavailable in that local build environment. The later successful hosted startup, actual GET-only Paper observations and cloud publication are separate evidence, recorded in `DEPLOYMENT_STATUS.md`. Two scoped launcher tests passed with a child-only root-identity stub; they do not verify container disk permissions or privilege dropping. The software tests use actual Python components with fake transports; they are not evidence of strategy profitability or an exact copy of a discretionary trader.

The Market Watch implementation passed a separate set of **51 focused tests**: **23 new Market Watch**, **14 observer** and **14 publisher** tests. They cover the bounded quote/session contract and observer/publication behavior with injected responses. [README.md](README.md) records the corresponding focused commands. The tests remain distinct from the later closed-session hosted observations recorded in `DEPLOYMENT_STATUS.md`; active-session latency and signed-in rendering remain unverified.

## Primary references

- [Alpaca activity endpoint](https://docs.alpaca.markets/us/reference/getaccountactivities-2)
- [Alpaca account activity objects](https://docs.alpaca.markets/us/docs/account-activities)
- [Alpaca paper simulation limits](https://docs.alpaca.markets/us/docs/paper-trading)
- [Render compute price source](https://render.com/articles/production-rails-hosting-guide)
- [Render disk/workspace price source](https://render.com/articles/how-much-does-cloud-application-hosting-cost-for-small-businesses)
- [Render Blueprint fields](https://render.com/docs/blueprint-spec)
- [Render persistent-disk behavior](https://render.com/docs/disks)
