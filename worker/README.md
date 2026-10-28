# Momentum Lab worker

This is a runnable synthetic replay, authenticated journal API, reusable **paper-only** session controller, and continuous **GET-only observation worker**. The default `serve` command runs the demo/local API; `serve-observe` adds actual provider observation and cloud status/halts after secure server provisioning. Neither starts broker order execution. There is no live trading mode or live execution host.

The Render observer service is live with its persistent disk. Initial hosted verification passed: public liveness, unauthenticated private-request rejection, fresh GET-only Alpaca Paper observations and advancing owner-bound cloud status. User-provided screenshots also verified signed-in connection badges with trading disabled and the separate Blueprint Auto Sync setting off. The subsequent Market Watch deployment has now published actual IEX observations for all three approved symbols to the correct owner during a closed regular session. Active-session behavior, the signed-in Market Watch rendering, full operational acceptance and financial completeness remain open. [DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md) records actual hosted evidence, source/deploy IDs and the approved $7.25/month base hosting cost. Read [OBSERVATION.md](OBSERVATION.md) for runtime behavior and [LEDGER.md](LEDGER.md) for accounting revisions and completeness.

The implemented [Market Watch](MARKET_WATCH.md) increment observes the approved connection-test symbols **AAPL, MSFT and SPY on IEX**. The default observer collects the current New York day's regular-session calendar and one bounded snapshot batch before optional accounting import. It does not require strategy metadata or historical-volume inputs to report quotes. `ACCOUNTING_HISTORY_START` remains blank, so accounting remains incomplete and authoritative fees/P&L remain unknown. The optional `worker_state.payload.market_watch` reports bounded provider observations; closed-session owner-bound delivery is separately verified in `DEPLOYMENT_STATUS.md`, while active-session data behavior and signed-in rendering remain to be checked.

The checked-in `examples/demo-v1.json` and `examples/demo-v1.ndjson` were produced by this Python engine. The dashboard can use them without a server, account, data subscription, or API key. Every stock, quote, candle, float, catalyst and fill in that example is invented. It is an integration test, not evidence of profitable trading or a behavioral copy of Ross Cameron.

The subsequent [observed-symbol requirements](STRATEGY_REVIEW.md) release is also
deployed. The correct owner received advancing reports for the same three
symbols, with each missing input family explicit and every overall review
incomplete. The actual runtime/deployment, configuration identity and frontend
revision are recorded in [DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md). This adds
neither order execution nor a complete market-wide strategy scanner.

The additive [price-history observer](PRICE_HISTORY.md) supplies calendar-matched
raw daily closes and up to ten elapsed regular-session minute intervals for the
same configured symbols. It collects history before current quotes, preserves
actual request/receipt times, reports missing intervals, and caches the bounded
report for 60 seconds. Raw feed closes remain unverified for corporate-action
comparability, and ended candles may be revised. No gain percentage, strategy
pass or order is derived from this increment. Hosted verification is recorded
separately in the deployment receipt.

## Run the verified demo

Use Python 3.12 with the system time-zone database available. The tested interpreter was Python 3.12.14. There are **no third-party Python dependencies**.

From this `worker` directory:

```sh
python -m unittest discover -s tests -v
python -m momentum demo --run-id demo-v1
python -m momentum readiness
```

The demo command writes `exports/demo-v1.json`, `exports/demo-v1.ndjson` and `data/momentum.sqlite3`. Repeating the same run ID with the same effective configuration returns the existing run and produces no duplicate journal events or trades. A changed configuration cannot overwrite an existing run.

The 16-scenario fixture currently requires the default strategy and risk settings. It rejects edited settings before recording a run. Saving a strategy draft in the dashboard does not change this fixture or activate a paper strategy. The separate [offline decision replay](DECISION_REPLAY.md) evaluates supplied snapshots with supported strategy overrides and produces decisions only; arbitrary draft/account replay still requires a broader dataset and simulation engine.

The default replay contains two invented round trips and five fills. One entry progresses from 101 shares pending to 40 filled/61 pending, then 101 filled. A stale stop quote leaves all 101 shares exposed until a fresh quote is received. Other cases reject low relative volume, unknown float, wide spreads, missing reconciliation and halted entries. An emergency latch survives risk-state restoration.

The fixture's gross arithmetic is $18.18 minus $12.12 = $6.06. An explicitly invented fee of $0.003 per share per side totals $1.212, producing $4.848 net. Its simulated ending equity is $10,004.848 from $10,000. These decimal strings should only be formatted for display; they must not be described as paper-account or live returns. No queue, venue depth, latency or market impact is modeled.

## What is implemented

| Component | Current behavior |
| --- | --- |
| Strategy and risk engine | Pure deterministic first-pullback rules with independent sizing, point-in-time checks, fresh quotes and durable risk-state serialization. See `STRATEGY_NOTES.md`. |
| Synthetic replay | Calls that engine, simulates explicitly labeled fills and exports actual inputs and decisions. |
| Offline decision replay | Reproduces setup decisions for explicitly supplied recorded snapshots and supported strategy overrides, without broker access, journal access, fills or performance calculations. See `DECISION_REPLAY.md`. |
| SQLite journal | Append-only events/trades, duplicate-key conflict detection, durable stop flags and stable order-request identities. |
| Alpaca adapter | Fixed HTTPS paper execution host, separate read-only data host, no redirects, limit/day orders qualified for extended hours, whole shares, reviewed settings and fresh risk approval. |
| Strategy snapshot builder | Up to ten selected symbols, exact exchange calendar, raw one-minute history and a 30-session same-clock RVOL baseline, fresh snapshots and operator-supplied facts. The default quote observer does not invoke this strategy-history path. |
| Market Watch collector | Current-day regular-session calendar and one bounded snapshot batch for the selected symbols, independent of float/news metadata and historical volume. Preserves per-symbol quote/trade timestamps, missing values and freshness states. See `MARKET_WATCH.md`. |
| Price-history collector | Explicit previous-calendar-session raw daily closes and a maximum-ten-interval regular-session minute window, with bounded GETs, exact decimals, visible gaps, original request/receipt times and independent expiry. See `PRICE_HISTORY.md`. |
| Observed-symbol requirements | Checks fresh last-trade and ask prices against the actual configured band, while gain, RVOL, float and catalyst remain unavailable. Publishes a configuration hash and source/experiment comparison, always with an incomplete overall review. See `STRATEGY_REVIEW.md`. |
| `PaperSession` | A bounded, reusable account/order/position reconciliation → strategy → risk → paper intention → exit/cancel controller. Tested with an injected fake provider. |
| Execution accounting | Bounded activity import, append-only source revisions/coverage/conflicts, exact long-only FIFO, carried lots, late fees and explicit completeness. Unknown actual amounts remain null. |
| Observation scheduler | GET-only provider, market collection before optional accounting, permanent execution boundary, one journal/account/owner, serial cycles, cooperative shutdown and restart-safe risk/control state. |
| HTTP service | Authenticated demo, readiness, journal and local halt APIs. `serve-observe` also exposes the current observation worker; default `serve` remains local/demo. |
| Supabase publisher | Insert-only synthetic demo publication, actual observation heartbeat/status with optional validated `payload.market_watch`, and durable halt-before-acknowledgment processing. Observation commands schedule status and halt processing after configuration. |

## Export contract for the dashboard

The compact JSON includes:

- `schema_version`, `run_id`, `run_key`, `mode: "demo"`, `provenance: "synthetic"`, `status`, `strategy_version` and configuration hashes.
- `settings`, `account`, `positions`, `open_orders`, `trades`, `fills`, `equity_curve`, `risk_state`, `metrics` and `assumptions`.
- `decisions[]`: one entry per scenario, containing the actual strategy, risk and optional exit decisions, reasons and resulting quantities.
- `display_data[]`: corresponding actual fixture quotes, trades, five completed candles, facts and RVOL summary. Use these to draw charts and scanner rows. The full 30-session input remains in the NDJSON journal.
- `broker.connected: false`, `broker.reconciled: false`, `broker.orders_submitted: 0`, `broker.live_execution_supported: false`.

Each NDJSON line is an event with `seq`, `event_id`, `idempotency_key`, `run_id`, `event_type`, market `timestamp`, actual `recorded_at` and `payload`. Synthetic events use `demo_*` names and explicit provenance. A simulated order intention is never logged as a broker-confirmed fill.

## Serve the private API

Set a server-only bearer secret and run:

```sh
export WORKER_CONTROL_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m momentum serve
```

The default bind address is `127.0.0.1:8787`. `/health` returns only service liveness. Every data or control route requires `Authorization: Bearer <WORKER_CONTROL_TOKEN>`. With no token configured, private routes return 503. Tokens are not written to readiness output, journals or request logs.

| Method and path | Behavior |
| --- | --- |
| `GET /health` | Minimal public liveness; no account or configuration. |
| `GET /api/readiness` | Sanitized settings, current release gates and durable control state. Configuration presence never means connected. |
| `GET /api/state` | Latest completed synthetic run and honest paper-service status. |
| `GET /api/runs` | Up to fifty completed demo summaries. |
| `GET /api/runs/{run_id}` | Completed JSON export. |
| `GET /api/runs/{run_id}/events?after=0` | Up to 5,000 NDJSON events after the global sequence cursor. |
| `POST /api/demo/run` | JSON `{"run_id":"demo-v1"}`; runs or retrieves the fixed synthetic fixture. |
| `POST /api/halt` | JSON `{"request_id":"unique-id","reason":"operator"}`; durably halts new entries locally. |
| `POST /api/kill-switch` | Same body; optional `"flatten":true` records an emergency flatten request. It never asserts positions are flat. |

Remote resume is intentionally absent. An ordinary halt cannot downgrade a previously recorded emergency. API acknowledgment means the request is in the local journal. The observation worker consumes that durable state but cannot cancel or submit orders; acknowledgment is not proof of liquidation.

For a browser integration, `CORS_ORIGIN` must equal one exact approved origin. Other origins are rejected. Browser OPTIONS preflight is allowed for that origin without a bearer token and returns no private data; every subsequent GET/POST still authenticates. Use HTTPS termination on the host. Do not compile the worker control secret, broker keys, or a Supabase service-role key into frontend JavaScript. The current dashboard uses owner-scoped Supabase access and the checked-in demo, so it does not need this control secret.

## Configuration and secret placement

`.env.example` documents environment variable names. The worker does not auto-load dotenv files. Keep local secret files out of version control; use server environment/secret settings on a host. `.gitignore` and `.dockerignore` exclude credentials, runtime journals and operator metadata.

The approved Market Watch configuration is `WATCHLIST=AAPL,MSFT,SPY`, `ALPACA_DATA_FEED=iex`, with the accounting-history anchor still blank. Quote observation does not require `SYMBOL_METADATA_PATH` or strategy history. The default quote freshness limit is two seconds for both the source event and actual HTTP receipt, independently of the thirty-second worker heartbeat. `POLL_SECONDS` defaults to five, so prices can legitimately age to stale between polls. Last-trade freshness is independent of bid/ask freshness; the dashboard must expire each against wall-clock time. Regular-session state uses current-day calendar evidence no more than five minutes old and becomes unknown when that evidence is unavailable. See [MARKET_WATCH.md](MARKET_WATCH.md) for the complete contract.

Required gates for any future submitted paper intention are all of:

1. `MOMENTUM_MODE=paper` and `PAPER_TRADING_ENABLED=true`.
2. Paper-account `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in the worker environment.
3. A reviewed `WATCHLIST`, `ALPACA_DATA_FEED=iex` or `sip`, and an existing `SYMBOL_METADATA_PATH` file. Missing/unknown facts still reject at the strategy gate.
4. `PAPER_REVIEWED_CONFIG_HASH` equal to the exact current hash shown by `python -m momentum readiness`. That hash covers the complete effective strategy/risk settings, watchlist, feed, order timing, order type and fixed execution host. Changing those settings invalidates it. Review the effective rules and budgets before recording this acknowledgment.
5. The persistent journal lease, fresh complete account/order/position reconciliation, a complete current session P&L/fee ledger, current quote/intent, and no blocking operational or risk latch.
6. An integration explicitly calling `PaperSession.cycle(..., execute=True)`. No shipped CLI or HTTP command makes that call.

The example risk amounts—$25 per-trade risk budget, $75 daily net loss limit, $1,000 notional cap and 500-share cap—are demonstration defaults. A hash acknowledgment is an explicit configuration gate, not evidence that the strategy or budget is suitable.

`APCA_API_BASE_URL`, `ALPACA_API_BASE_URL`, or `ALPACA_BASE_URL` cannot point execution at another host. A live-mode setting is rejected. Synthetic or mismatched feed intentions are rejected by the paper adapter. There is no route to the live Alpaca execution hostname.

After paper credentials have been configured, `python -m momentum paper-preflight` makes explicitly **read-only** account, position, open-order and optional data/calendar requests. It reports only endpoint reachability and counts. It submits nothing and never labels a successful read as complete strategy reconciliation.

## Bounded paper controller contract

`PaperSession` accepts the provider interface implemented by `PaperClient` and an optional snapshot builder and clock. Use it as a context manager so the lease is acquired before risk state is restored. `cycle(snapshots=..., session_ledger=..., execute=False)` performs one reconciliation/evaluation cycle; `execute` defaults false. The provider tests in `tests/worker_fakes.py` demonstrate the contract using entirely invented data and no network.

The independent session ledger must supply `complete: true`, current New York `session_date`, aware `as_of`, gross `realized_pnl`, total `fees`, and a `source`. It must come from reconciled execution/accounting evidence. Account equity changes cannot substitute for realized trading P&L. The new activities importer feeds `AccountingLedger` in the observation runtime, but REST pagination alone does not establish full execution, opening-inventory or fee coverage. Unknown authoritative values remain null. Supplying keys cannot complete those prerequisites.

The controller checks all known request IDs, broker open orders, cumulative filled quantities, owned cost bases and actual positions. Partial fills count immediately. Unknown positions, unknown orders, disappearing attempted orders, regressing fills or conflicting quantities block entry. Cancellations are reconciled before retry, never taken as proof of a fill or flatness. An opening remainder must be canceled and subsequently reconciled before its position can be exited; a fill arriving during cancellation changes the required exit quantity. A submitted exit remains pending until the next authoritative fill/position reconciliation.

Risk checks run even without an entry signal or usable quote, and each changed risk state is checkpointed before broker I/O. Kill and daily-loss latches survive restart. The account freshness clock starts at the account request; it is never reset after slower downstream reads. The guard rechecks account and ledger age immediately before submission. Stale quotes cannot manufacture an exit price or flat position.

Order IDs are deterministic from strategy setup, symbol, side and confirmed exit generation. The request is committed before POST. An ambiguous response triggers lookup by that same ID, never blind resubmission. A missing previously attempted order remains blocked. Cancel retries require a fresh order lookup, are spaced at least two seconds apart and stop after three attempts. A pending cancel is observed rather than repeatedly resent.

Owned trade closures are reconstructed from confirmed cumulative order fills. Gross P&L is journaled. Per-trade `fees` and `net_pnl` remain null until a complete execution activity ledger allocates fees; no zero-cost result is invented. Historical trade recovery retains the original entry run ID. The prototype holding clock conservatively begins at order submission until a true first-fill activity timeline is integrated, so later partial completion cannot reset it.

## What remains before autonomous paper order execution

The continuous observer and quote-only Market Watch increment are deployed on Render. Fresh Paper account observations, advancing cloud heartbeats and newly received IEX snapshot responses for AAPL, MSFT and SPY have been verified. Friday source prices correctly remained stale during the Sunday verification; `DEPLOYMENT_STATUS.md` records that closed-session acceptance and its limits. The observer's transport cannot execute orders. These are the remaining integration gates:

1. **Financial coverage and provider reconciliation:** independently prove complete executions, opening inventory and costs, and reconcile late events/corrections with actual provider evidence. The durable ledger is implemented; its financial-completeness flags cannot be inferred from pagination exhaustion. Verify day changes and carried exposure against the real paper account.
2. **Fast supervision:** the observation scheduler and status/halt dispatcher are implemented, but serial REST history acquisition can delay commands and expire heartbeats. Measure latency, add appropriate streaming/cache and operational supervision before a separate execution release.
3. **Provider acceptance:** verify actual entitlement, premarket quotes, same-feed trades/bars, calendar, order lifecycle and timestamp precision. Offline tests made no real broker request; the later hosted check verified account observation, without establishing market-data or lifecycle completeness.
4. **Reliable facts and approved strategy settings:** source timestamped float/news data and obtain a concrete risk/strategy configuration and candidate-selection method. AAPL, MSFT and SPY are approved only as a read-only connection-test watchlist. Dashboard drafts remain drafts. There is no whole-market scanner, Level 2/tape model or exact Ross Cameron imitation.
5. **Operational hosted acceptance:** verify backups/disk growth, restart behavior, authenticated control operations, disconnections and durable halts while retaining the single persistent journal. Initial startup, broker observation, cloud publication and unauthenticated private-request rejection have passed; see `DEPLOYMENT_STATUS.md`. Signed-in connection badges and Blueprint Auto Sync off are visually verified. Static wording corrections passed a fresh localhost preview test; their updated hosted rendering remains to be visually confirmed. A saved Vault link remains independent of the separately provisioned host credentials.

## Optional Supabase publication

Set `SUPABASE_URL`, server-only `SUPABASE_SERVICE_ROLE_KEY`, and the signed-in owner's `OWNER_USER_ID`. The public project URL in `.env.example` is not a credential. Do not put the service-role key in the dashboard or in chat.

```sh
python -m momentum publish-demo --run-id demo-v1
```

This inserts the existing local synthetic export into `demo_runs` with `owner_id`, stable `id`, `run_key`, `strategy_version` and `payload`. It uses `ON CONFLICT DO NOTHING` semantics through PostgREST's `resolution=ignore-duplicates`; it does not require UPDATE privileges and never overwrites a prior run. A new revised fixture needs a new run key.

`Publisher.acknowledge_halts(journal)` validates owner-scoped queued commands, records and verifies the local halt, and then verifies the cloud acknowledgment. `Publisher.publish_worker_state(...)` matches the dashboard's existing status contract and derives connectivity from actual evidence timestamps. Both are scheduled by `observe`/`serve-observe` when cloud configuration is complete. Neither claims an order was canceled or a position closed. The worker does not upload its local accounting journal to `paper_runs`/`paper_events` in this milestone.

The optional `worker_state.payload.market_watch` is reconstructed from an allowlist before publication: at most ten configured symbols, normalized quote/trade decimal strings, original source/receipt timestamps and regular-session evidence. Missing or invalid symbols remain explicit per row; absent Market Watch data must not be replaced with synthetic prices. Slow accounting or account reads after quote collection cannot refresh those timestamps, and rows are checked again before publication. No table or browser-held broker credential is added by this contract.

The publisher derives optional `payload.strategy_review` from that same sanitized Market Watch and the current validated strategy configuration. Prices and source timestamps stay in Market Watch; the review references the matching report, expires evidence independently, exposes each missing input family, and never emits an order intention or eligibility claim. See [STRATEGY_REVIEW.md](STRATEGY_REVIEW.md) for the contract and source limitations.

The independent optional `payload.price_history` is revalidated against the same
configured symbols and feed. Calendar-matched raw closes and elapsed minute
intervals retain their source dates and original request/receipt times; a new
heartbeat does not renew them. Historical request/receipt freshness uses its
own 120-second bound, with separate 300-second calendar evidence. History errors
do not replace valid quote observations or change selection-review readiness.
Full bars are held in current state only; repeated cycle journal entries contain
a safe summary and report hash, not a durable provider-capture archive.

Modern `sb_secret_` keys use only `apikey`; legacy service-role JWTs also use Bearer authorization. Credentials and provider bodies are excluded from status and error payloads.

## Container and Render deployment

`Dockerfile` pins the Python base tag to `3.12.14-slim-bookworm` and installs the OS time-zone database. It contains no Python package installation. The executable `/usr/local/bin/serve-observe` launcher uses `exec python -m momentum.container_entrypoint serve-observe "$@"`, routing direct command execution through the existing Python entrypoint. When started as root, that entrypoint prepares the mounted data directories and drops to UID/GID 10001 before serving. The image's existing ENTRYPOINT and default demo CMD remain unchanged; the observation command is also a valid CLI argument when the image entrypoint receives it.

From the repository root containing `worker/`:

```sh
docker build -t momentum-lab-worker ./worker
docker run --rm -p 8787:10000 --env WORKER_CONTROL_TOKEN --mount type=volume,src=momentum-data,dst=/var/data momentum-lab-worker
```

`worker/render.yaml` remains an unused demo web-service template. Its Dockerfile/context paths are relative to the repository root. It specifies one `0.5c-512mb` instance, one persistent disk at `/var/data`, a generated control secret, explicit CORS origin, disabled service auto-deploys and demo mode. The existing hosted observer uses the observation configuration described below.

Use one process and one persistent disk. Render documents that a service with an attached persistent disk cannot be scaled. Do not deploy an independent copy using another journal against the same paper account. Keep `PAPER_TRADING_ENABLED=false` in the demo deployment.

`render-observe.yaml` records `dockerCommand: serve-observe`, paper mode with execution disabled, and placeholders for secure worker provisioning. The Render observer service now has one `0.5c-512mb` instance and a 1 GB disk at `/var/data`; service auto-deploys are off. The separate Blueprint Settings display showed `Auto Sync = No` in the user-provided screenshot at `2026-10-18T21:41:37Z`, with branch `main` and path `worker/render-observe.yaml`. The approved base estimate is $7/month compute plus $0.25/month disk on a $0 Hobby workspace, before taxes/usage. See `DEPLOYMENT_STATUS.md` for current hosted evidence and `OBSERVATION.md` for cost qualifications.

## Verification record

The price-history increment passed the complete **271-test worker suite**,
including six implementation/decoder tests and 28 independently written contract
tests. Exact-number/time handling, calendar/window rules, gaps, stale/error
states, bounded publication and the GET-only boundary are covered with invented
responses. See [PRICE_HISTORY.md](PRICE_HISTORY.md) for the contract and
[DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md) for separately recorded hosted
evidence and frontend results.

The observed-symbol requirements and offline decision-replay increment passed
the complete **237-test worker suite**. This includes 22 new selection-review
tests and 19 new offline replay tests. The standalone CLI also reproduced the
four bundled invented decisions without broker access or account simulation.
The frontend reported 216 passing focused/regression tests and a successful
build; independent source review resolved its settings-parser and exact
freshness-limit issues. See [STRATEGY_REVIEW.md](STRATEGY_REVIEW.md),
[DECISION_REPLAY.md](DECISION_REPLAY.md), and the deployment record for scope and
the distinction between offline checks and hosted evidence.

The original Python milestone passed **61 tests** on Python 3.12.14: 40 strategy/risk tests and 21 worker tests. The observation milestone passed **171 tests**, adding activity pagination/normalization, revision-aware accounting, exact timestamp/decimal arithmetic, publisher/status/halts, and injected runtime integration. `VERIFICATION.json` and `../docs/observation-python-test-results.txt` preserve those offline milestone results; their original deployment flags are historical. Two later scoped launcher tests passed separately, without rerunning the full suite. Current hosting status is recorded in `DEPLOYMENT_STATUS.md`.

Market Watch implementation verification passed **51 focused tests**: **23 new Market Watch tests**, **14 observer tests** and **14 publisher tests**. These checks use injected provider responses and do not establish hosted market-data delivery or latency. The corresponding focused commands from `worker/` are:

```sh
python -m unittest discover -s tests -p 'test_worker_market_watch.py' -v
python -m unittest discover -s tests -p 'test_worker_observation.py' -v
python -m unittest discover -s tests -p 'test_worker_publisher.py' -v
```

The actual Market Watch runtime commit, deployment, closed-session observations and remaining active-session/visual checks are recorded in [DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md).

HTTP tests use actual parsed HTTP requests with in-memory sockets because this environment prohibits opening listening sockets. Docker was unavailable locally, so no local container test was performed. After the launcher correction and secure credential provisioning, the later Render deployment became live and passed the separate hosted checks in `DEPLOYMENT_STATUS.md`. Provider tests use an injected fake transport. No broker credentials, accounts, subscriptions, external orders or cloud publication were used by those tests. The launcher tests stub only the child process's root-identity check; they do not establish container disk or privilege-drop acceptance.

## Primary API/deployment references checked for this implementation

- [Alpaca: Create an Order](https://docs.alpaca.markets/us/reference/postorder) — paper endpoint, client order ID, limit/day and extended-hours fields.
- [Alpaca: Get Order by Client Order ID](https://docs.alpaca.markets/us/reference/getorderbyclientorderid) — recovery route.
- [Alpaca: Cancel an Order](https://docs.alpaca.markets/us/reference/deleteorderbyorderid-1) — cancellation request and status semantics.
- [Alpaca: Get All Orders](https://docs.alpaca.markets/us/reference/getallorders-1) — open-order query and maximum response size.
- [Alpaca: Account](https://docs.alpaca.markets/us/reference/getaccount-1) and [Positions](https://docs.alpaca.markets/us/reference/getallopenpositions) — read/reconciliation primitives.
- [Alpaca: Stock Snapshots](https://docs.alpaca.markets/us/reference/stocksnapshots-1), [Historical Bars](https://docs.alpaca.markets/us/reference/stockbars) and [Market Calendar](https://docs.alpaca.markets/us/reference/legacycalendar) — snapshot fields, pagination and session dates.
- [Alpaca: Paper Trading](https://docs.alpaca.markets/us/docs/paper-trading) — simulation limits and separate paper environment.
- [Render Blueprint Reference](https://render.com/docs/blueprint-spec), [Monorepo Support](https://render.com/docs/monorepo-support) and [Compute Plan Naming Update](https://render.com/docs/compute-plans-update) — current deployment fields and plan ID.
- [Python official container images](https://hub.docker.com/_/python) — versioned base-image tag.

