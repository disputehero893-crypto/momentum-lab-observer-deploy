# Observer deployment status

Latest price-history verification: **2026-10-19T00:29:45.57024+00:00**. Runtime **`d87a14221e530d59ec6674e4e140645a78ce87a8`** deployed as **`dep-dbam70c9v7es7392hn3g`**, live at **`2026-10-19T00:26:15.262905Z`**. Two owner-bound checks verified raw daily closes and ten completed regular-session minute intervals for each of AAPL, MSFT and SPY on IEX. The dated session was October 16; the New York collection date was the Sunday nontrading date October 18. Original source dates were retained while history request/receipt times and worker heartbeats advanced. Broker access remained Paper and read-only, execution remained disabled, and every requirements review remained incomplete.

Earlier requirements-review verification: **2026-10-18T23:34:55.480933+00:00**. Runtime **`9b5e04b7f3451bc978e9236b3c9fde5c2b9e811f`** deployed successfully as **`dep-dbale8c9v7es738vs44g`**, live at **`2026-10-18T23:33:22.125324Z`**. The correct owner received the new selection requirements for AAPL, MSFT and SPY, with matching Market Watch collection times and advancing publication/receipt times. Broker access remained connected, Paper and read-only; execution remained disabled.

Earlier Market Watch verification: **2026-10-18T22:37:20.662819Z**. The approved AAPL, MSFT and SPY IEX observation increment published actual provider prices and timestamps to the correct owner's dashboard backend. Deploy **`dep-dbakiuk9v7es738sq110`** became **live** at **`2026-10-18T22:35:10.067335Z`**.

Successive worker heartbeats and HTTP receipt times advanced. The underlying quote/trade source timestamps remained on Friday, October 16, and all three rows remained explicitly **stale**. The Alpaca calendar returned a known nontrading date for Sunday, October 18, so the regular session was **closed**. This verifies closed-session data delivery; it does not establish active-session latency, strategy performance or order-execution readiness.

## Resources and source

| Item | Recorded value |
| --- | --- |
| Workspace | Derek's workspace, `tea-dbahn4c9v7es738ieu40` |
| Service | `srv-dbaipqm7bikc73epsj5g` |
| Service URL | https://momentum-lab-observer.onrender.com |
| Source repository | https://github.com/disputehero893-crypto/momentum-lab-observer |
| Branch | `main` |
| Deployed runtime commit | `d87a14221e530d59ec6674e4e140645a78ce87a8` |
| Latest deploy | `dep-dbam70c9v7es7392hn3g`, `live` |
| Runtime / command | Docker / `serve-observe` |
| Plan / instances | `0.5c-512mb` / one |
| Disk | `dsk-dbaipqm7bikc73epsn70`, 1 GB at `/var/data` |
| Service auto-deploy | Off; connector reverified after this deployment |
| Blueprint Auto Sync | **No (off)**, visually verified in the Blueprint Settings display at `2026-10-18T21:41:37Z`; separate from service auto-deploy |
| Observation configuration | `WATCHLIST=AAPL,MSFT,SPY`, `ALPACA_DATA_FEED=iex`, five-second poll, two-second source/receipt freshness |
| Accounting-history anchor | Still blank; accounting remains incomplete |
| Lovable source commit | `2cdb81eebfd9db1aa5bba0c749314e2e3780e16f` |
| Market Watch preview | https://id-preview--31bc21f0-6b6a-43e5-8d84-96ea1d806889.lovable.app/market-watch |
| Observed-symbol review | https://id-preview--31bc21f0-6b6a-43e5-8d84-96ea1d806889.lovable.app/scanner?view=observed |
| Strategy draft/reference comparison | https://id-preview--31bc21f0-6b6a-43e5-8d84-96ea1d806889.lovable.app/strategy |

The user approved **$7.25/month base Render hosting**: $7 compute plus $0.25 for the initial 1 GB disk on a $0 Hobby workspace, before taxes and usage extras. The post-deploy connector read confirms the same plan, one instance and the existing disk. This estimate is not a final invoice. The existing Supabase and Lovable subscriptions remain separate.

The Market Watch implementation used **8.3 Lovable credits**: 6.1 for the initial page and 2.2 for the reviewed corrections, as reported by the connector. This increment runs on the existing resources and IEX access.

The subsequent requirements-review/draft-comparison increment used **13.5 Lovable credits**: 9.5 for the initial build and 4.0 for the correction, from the connector's task-specific `cost_credits` fields. Agent prose describing intermediate billing lookups is not substituted for those fields. No paid resource, disk size, instance count, subscription or data-feed upgrade was added.

The price-history increment used **24.3 Lovable credits**: 15.0 for the initial build and 9.3 for its focused correctness correction, from the connector's task-specific fields. It used the existing Render service, disk, owner-scoped database and IEX access; no new infrastructure or subscription was added.

## Price-history deployment and current evidence

The reviewed eleven-file runtime change adds the optional `price_history`
sibling for the existing AAPL, MSFT and SPY IEX watchlist. It was committed with
an expected-head check. Service auto-deploy was reverified off and one explicit
deployment was triggered. No environment variable, database schema, subscription,
feed, disk size, instance count or Blueprint setting changed.

| Check | Actual evidence |
| --- | --- |
| Render deployment | `dep-dbam70c9v7es7392hn3g` started at `2026-10-19T00:25:37.252022Z`, became live at `2026-10-19T00:26:15.262905Z`, and reports runtime `d87a14221e530d59ec6674e4e140645a78ce87a8`. |
| New startup | `2026-10-19T00:26:08.209087964Z`: `Paper observation started; broker transport is GET-only; execution disabled`. |
| Owner/runtime | Owner `ae9f51a1-8de0-492d-8b69-98c9e692759f`, mode Paper, status observing, provenance provider observations, broker connected/read-only. |
| Execution/error state | `observation_only=true`, `execution_enabled=false`, `entry_gate_open=false`, `last_error_code=null`. |
| Successive observation checks | `2026-10-19T00:28:15.448053+00:00` and `2026-10-19T00:29:45.57024+00:00`. |
| Advancing heartbeat | `2026-10-19T00:28:14.532436+00:00` to `2026-10-19T00:29:44.637479+00:00`. |
| Advancing history collection | `2026-10-19T00:27:14.162116Z` to `2026-10-19T00:29:19.007828Z`, retaining each report's actual request/receipt times. |
| Calendar | New York anchor `2026-10-18`, `is_trading_day=false`, previous/target session `2026-10-16`, original calendar check `2026-10-19T00:26:08.924919Z`, no calendar error. |
| History contract | Raw USD IEX observations, symbol mapping disabled, 60-second refresh, 120-second retrieval-age limit, revisions possible, official close false, corporate-action comparability not assessed, full-session coverage false. |
| Quotes and requirements | All quote/trade sources remained stale during the closed-session check; requirements stayed incomplete with no strategy promotion. |
| Frontend | Authoritative Lovable commit `2cdb81eebfd9db1aa5bba0c749314e2e3780e16f`, agent finished, preview ready, production unpublished. |

### Actual dated history

These values were retrieved from the actual owner-bound worker publication.
They describe the dated session shown, not current executable prices. Each
minute window contains at most ten intervals, and missing intervals remain
explicit. The daily value is the selected feed's raw daily-bar close.

| Symbol | Session | Raw daily-bar close (USD) | Daily status | Minute window, UTC | Returned / expected | Missing intervals |
| --- | --- | --- | --- | --- | --- | --- |
| AAPL | 2026-10-16 | 343.39 | observed | 19:50–20:00 | 10 / 10 | None |
| MSFT | 2026-10-16 | 528.99 | observed | 19:50–20:00 | 10 / 10 | None |
| SPY | 2026-10-16 | 763.335 | observed | 19:50–20:00 | 10 / 10 | None |

Daily source timestamps identify New York midnight on the session date.
Historical request/receipt timestamps identify when the worker actually fetched
the data; they do not rewrite the bars' source dates or renew quote freshness.
The two observed history collections were more than sixty seconds apart and
used new request/receipt times, demonstrating cache turnover.

### Verification of this increment

The complete worker suite passed **271 tests in 3.080 seconds**. It includes
six implementation/decoder tests and 28 independently written history contract
tests, alongside all existing regression tests. They verify exact JSON decimal
decoding, nanosecond causality, calendar/DST and early-close dates, exact windows
derived from original requests, missing intervals, status/error consistency,
component isolation, bounded publication, summary-only cycle journals and the
GET-only transport boundary.

The initial frontend review reproduced concrete false-positive states and
invalid retained-value rendering. The focused correction validates stale
evidence as carefully as fresh evidence, enforces status-specific null/error
shapes, derives the exact minute window from the request, checks calendar
completeness and date binding, and compares equivalent timestamp representations
by instant. It preserves valid expired preopen reports and failed requests
without receipts. The corrected frontend reported **42 focused tests and 301 total tests across 13 files passing** with
successful typecheck and build. Independent review at the authoritative commit
returned GO for the reviewed contract and scope. Two nonblocking frontend
metadata branches remain permissive for malformed stale calendars or a single
optional timestamp on an error-only minute report. Those states cannot become
Observed, retain error bars or grant execution readiness, and the current backend
does not publish those malformed shapes.

A separate independent check passed the actual first hosted snapshot through
the pinned frontend's pure validators and status helpers at its recorded
observation time. Calendar, feed/symbol binding, exact timestamp ordering,
all three daily closes and all thirty minute bars passed, and each history
component returned Observed. The history collection was then 61.286 seconds old;
the advancing heartbeat did not renew that age. This was a pure-helper check,
not a full Zod/Vitest run or an authenticated rendered view.

The task-specific frontend suite/build results are Lovable reports. The Python
suite was run directly. The canonical DEMO-A/DEMO-B fixture is explicitly
invented; it is not an actual provider capture. The hosted rows above are
separately observed backend evidence.

Signed-in hosted rendering and active-session behavior have not been visually
or operationally established by this release. No new cloud-browser or direct
HTTP attempt was made after the documented automatic approval-review blocks.
Render deployment status/startup and advancing owner-bound observations verify
the deployed workload; they do not replace an authenticated UI screenshot.

The full bar report is kept in bounded current state and the latest owner
publication. The five-second cycle journal contains a summary/hash instead of
repeated full bars. There is no durable provider-capture archive or backtest
performance claim. Raw previous closes still do not satisfy the comparable-close
requirement; corporate actions, float/news, same-clock RVOL, discovery, accounting
completeness and operational acceptance remain open.

## Earlier requirements-review deployment and evidence

The reviewed 12-file runtime change was committed and the main branch advanced
with an expected-head check. Service auto-deploy was reverified off. One explicit
Render deployment was triggered for runtime `9b5e04b7f3451bc978e9236b3c9fde5c2b9e811f`;
no environment variables or Blueprint configuration changed. The existing disk,
plan, single instance and health path were reverified after launch. The offline
replay command is installed as code but is not started by `serve-observe`.

| Check | Actual evidence |
| --- | --- |
| Render deployment | `dep-dbale8c9v7es738vs44g` started at `2026-10-18T23:32:49.551878Z`, became live at `2026-10-18T23:33:22.125324Z`, and reports runtime `9b5e04b7f3451bc978e9236b3c9fde5c2b9e811f`. |
| Shutdown/startup | Prior observer logged 697 cycles, execution false, zero orders submitted and no last error at `2026-10-18T23:33:04.883313407Z`. New startup at `2026-10-18T23:33:15.282604793Z` stated `Paper observation started; broker transport is GET-only; execution disabled`. |
| Owner/runtime | Owner `ae9f51a1-8de0-492d-8b69-98c9e692759f`, new instance `b7544b7f-587f-412b-bbcc-31e93b8dcd7b`, mode Paper, status observing, provenance provider observations, broker connected/read-only. |
| Execution/error state | `observation_only=true`, `execution_enabled=false`, `entry_gate_open=false`, `last_error_code=null`. |
| First publication | Checked at `2026-10-18T23:33:56.018658+00:00`: heartbeat `2026-10-18T23:33:51.691878+00:00`, quote receipt `2026-10-18T23:33:50.848553Z`, shared Market Watch/review collection `2026-10-18T23:33:50.848606Z`. |
| Continuing publication | Checked at `2026-10-18T23:34:55.480933+00:00`: heartbeat `2026-10-18T23:34:51.669866+00:00`, quote receipt `2026-10-18T23:34:50.902258Z`, shared collection `2026-10-18T23:34:50.902313Z`. The review's evaluated-at instant matched each heartbeat. |
| Actual requirements | Exactly AAPL, MSFT and SPY on IEX. Each price check was unavailable with `quote_stale`; gain, RVOL, float and catalyst were unavailable with their specific missing-evidence reasons. Every overall state was incomplete. |
| Rule identity | Configuration `9c573dbfe6af98716621b4d497866b5ada4043810829c602c7b3eba23dc37229`; source threshold match true and experiment-default match true, with empty differences lists. |
| Calendar and clock | Sunday `2026-10-18` was a known nontrading date with regular session closed and no calendar error. Calendar checked at `2026-10-18T23:33:16.070863Z`. The separate experimental clock was outside its 07:00–10:00 New York window. |
| Frontend | Lovable head `22ef77b019ad3fdfd9f6f098cde0d2417dbf7dde`, agent finished, preview ready, production unpublished. The observed/synthetic Scanner, draft comparison and corrected Overview wording are present in source. |

The quote/trade values and Friday source timestamps remained the same as the
closed-session observations recorded below. Fresh HTTP receipts did not turn
those source events into fresh prices or passing selection evidence.

### Verification of this increment

The complete worker suite passed **237 tests in 2.407 seconds**, including
22 selection-review tests and 19 offline decision-replay tests. The final
freshness fix uses context-independent integer nanosecond flooring, including
high-precision and extremely small configured limits. The standalone CLI also
reproduced the four bundled invented decisions: valid setup, missing float,
wide spread and lookahead bar. It produced no broker request, risk assessment,
fill or performance result.

Lovable reported **216 passing regression tests across nine files**, including
66 selection-review tests, and a successful automatic build. Its correction
first reproduced nine failures. Independent source review verified the bounded
decimal parser, exact configuration-age checks, source-threshold consistency,
frozen contract values, owner/report linkage, shared evidence-status helpers
and accurate experimental-setting display. Task-specific frontend test/build
results are Lovable reports; the Python suite and CLI checks were run directly.

Signed-out desktop/mobile navigation and draft rendering were reported checked
by Lovable for the initial build. The signed-in hosted Scanner and Strategy
screens have not been visually inspected in this session. Database receipt
proves the backend contract is arriving, not a rendered screenshot. No new
direct HTTP or cloud-browser attempt was made after the previously documented
automatic approval-review blocks. Active-session observations, complete
accounting and operational acceptance remain open.

## Earlier Market Watch deployment and evidence

The Market Watch runtime, acceptance tests, documentation and nonsecret watchlist profile were committed first. The single environment update used a merge with `replace=false` and changed only `WATCHLIST`. That update automatically triggered `dep-dbakiuk9v7es738sq110`, even though service auto-deploy is off. No second deployment trigger was issued for that increment. Secret values were neither requested nor printed.

The checked-in `worker/render-observe.yaml` and the applied service watchlist both contain the approved diagnostic symbols. Service auto-deploy and Blueprint Auto Sync remain separate controls. Later documentation-only commits do not imply a new running image.

| Check | Evidence |
| --- | --- |
| Render launch | Runtime commit `7fe2554460d6041a69d434c0861032be1d617686` became live at `2026-10-18T22:35:10.067335Z`. |
| Execution boundary | New startup log at `2026-10-18T22:35:03.201073153Z`: `Paper observation started; broker transport is GET-only; execution disabled`. |
| Owner and broker | Owner `ae9f51a1-8de0-492d-8b69-98c9e692759f` received `mode=paper`, `status=observing`, `provenance=provider_observations`, broker `connected` / `read_only`. |
| Execution state | Published `observation_only=true` and `execution_enabled=false`. |
| Advancing heartbeat | From `2026-10-18T22:35:34.510825Z` to `2026-10-18T22:37:19.569444+00:00`. |
| Advancing quote receipt | From `2026-10-18T22:35:33.741354Z` to `2026-10-18T22:37:18.795119Z`, while provider source timestamps remained unchanged. |
| Typed row | The database JSON representation preserved the owner/paper envelope, RFC3339 heartbeat and nanosecond quote/trade strings expected by the frontend parser. |
| Calendar | `scope=regular`, `date=2026-10-18`, `timezone=America/New_York`, `is_trading_day=false`, `status=closed`, no calendar error. |
| Freshness | Every quote and last trade was `stale`; maximum source and receipt age remained two seconds. A new HTTP receipt did not refresh a Friday source event. |
| Host configuration | Same workspace, plan, one instance, original 1 GB disk at `/var/data`, service auto-deploy `no`, HTTP health path `/health`. |

### Actual closed-session observations

These are provider observations returned during the verification above, not current executable market prices or strategy candidates. The feed is **IEX**, not a claim of consolidated all-exchange coverage. Both quote and last-trade freshness are stale.

| Symbol | Bid | Ask | Quote source, UTC | Last trade | Trade source, UTC | Quote / trade status |
| --- | --- | --- | --- | --- | --- | --- |
| AAPL | 328.76 | 363.43 | 2026-10-16T20:00:00.001061859Z | 343.39 | 2026-10-16T19:59:58.574873201Z | stale / stale |
| MSFT | 503.8 | 558.35 | 2026-10-16T20:00:00.002019307Z | 528.99 | 2026-10-16T19:59:59.275067202Z | stale / stale |
| SPY | 763.5 | 764.6 | 2026-10-16T20:44:45.870458146Z | 763.31 | 2026-10-16T20:47:54.540934863Z | stale / stale |

All rows in the later read retained receipt `2026-10-18T22:37:18.795119Z`, collection `2026-10-18T22:37:18.795172Z` and publication heartbeat `2026-10-18T22:37:19.569444+00:00`. The regular-session calendar check was `2026-10-18T22:35:03.755409Z`, within its 300-second validity window at verification.

### Direct-request and visual verification limits

The initial deployment's unauthenticated `GET /health` returned HTTP 200, and unauthenticated `GET /api/readiness` without an Origin header returned HTTP 401. Those are historical checks described below.

A repeat direct request for this new deployment could not be completed. The local runtime cannot open network sockets; the web retrieval tool could not access the service. **Automatic approval review rejected escalated network execution**, stating that the proposed read-only, unauthenticated checks were low intrinsic risk but this environment is prohibited from approving escalated network execution. The repeat HTTP 200/401 pair is therefore **not claimed** for this revision. Current deployment and workload evidence comes from Render's live status/startup log and the continuing owner-bound cloud observations.

The new signed-in hosted Market Watch screen has **not** been visually observed from this session. Lovable reported signed-out desktop/mobile checks of the initial Market Watch implementation with no page errors or page-level horizontal overflow; Overview retained its demo controls. The final correction was checked through tests, build diagnostics and independent source review. Current cloud rows establish backend receipt, not a signed-in rendered screenshot.

## Implementation verification

The frozen worker increment passed **51 focused tests**: 23 new Market Watch cases, 14 existing observer cases and 14 existing publisher cases. Run from `worker/`:

```sh
PYTHONPATH=.:tests python -m unittest test_worker_market_watch test_worker_observation test_worker_publisher -v
```

The result was **51 tests in 0.655 seconds, OK**. The tests use injected provider responses, newly created temporary journals and blocked networking. They establish per-symbol error isolation; independent source/receipt/trade freshness; nanosecond ordering; regular-session cache/early-close behavior; bounded owner-state publication; the actual default observer without strategy metadata/history; retained quotes after later accounting failure; and the unchanged GET-only execution boundary.

The first Lovable Market Watch build reported 40 new tests and 62 regressions passing. Its independent review identified four concrete corrections: cached calendar state across open/close, missing quotes with valid independent trades, exact provider/publication validation and cached broker green status after query failure. The corrected build reported **84 focused tests passed**: 57 Market Watch, 8 worker-query and 19 backend regressions, with automatic TypeScript/build diagnostics `build OK`. The final source at `5fbbb8fc7490a534b107ded2d24723bc9fbc6873` passed a separate read-only review against the worker contract.

The frontend reads explicit owner-filtered fields from the existing `worker_state` table and validates returned owner, paper mode, provenance and publication ordering. Quote/trade freshness expires on a separate wall-clock timer and focus changes, independently of the thirty-second worker heartbeat. The regular calendar does not assert extended-hours data availability. Missing values and fixed error messages never become fabricated quotes, balances, fees or returns.

## Earlier deployment and provisioning evidence

The first launch stopped with exit 128 before Python output. Commit `2127b32f6f78a4eeb572c37b64f3f15656e794b1` added the executable `serve-observe` launcher, retaining the existing container entrypoint and privilege-drop logic. Two scoped launcher subprocess tests passed, using a child-only root-identity stub because the local sandbox could not change UID ownership. Those checks did not establish container privileges or disk behavior. The original 171 offline tests remain a separate earlier milestone.

After the user securely supplied the host credentials, deploy `dep-dbajaf5g1s2s73cb18f0` of source `22b22576886c263a201d37a95d1bb5895e78386d` became live at `2026-10-18T21:08:34.510614Z`. Initial checks verified public liveness, unauthenticated private-request rejection, fresh GET-only Paper account observations and advancing owner-bound publication. The watchlist was blank at that historical account-only stage; the current Market Watch selection deliberately supersedes it. The accounting-history anchor remains blank.

User-provided Connections screenshots at `21:23 UTC` and `21:27 UTC` verified Supabase Verified, worker Connected/observing, broker Connected/worker-reported and Trading Disabled. A screenshot at `2026-10-18T21:41:37Z` separately showed Blueprint `momentum-lab-observer` in Derek's workspace, `Auto Sync = No`, branch `main` and path `worker/render-observe.yaml`. These are historical visual observations, not new Market Watch screenshots.

## Secure provisioning status

| Setting | Established state |
| --- | --- |
| `OWNER_USER_ID` | The verified owner UUID receives hosted status; the existing owner/account binding is preserved. |
| `SUPABASE_URL` | Dedicated project `https://wusnuhzjslikzswkahle.supabase.co`; owner-bound publication works. |
| `SUPABASE_SERVICE_ROLE_KEY` | User provisioned in Render; successful publication verifies usable server authorization. The value was not inspected. |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | User provisioned in Render; GET-only Paper account and IEX observations work. Values were not inspected; the saved app link remains separate. |
| `WORKER_CONTROL_TOKEN` | Initial unauthenticated private-request rejection established route authentication. No authenticated private-control request or token inspection was performed. |
| Execution settings | `MOMENTUM_MODE=paper` and `PAPER_TRADING_ENABLED=false`; this observer cannot submit or cancel orders. |

Credentials belong only in secure server settings. Missing cloud URL/key/owner configuration prevents HTTP startup. Missing broker credentials fail account observation. Market Watch does not require a new browser broker credential or database migration.

## Remaining acceptance

1. Open the signed-in Market Watch preview and confirm that the actual quote rows, Price history values, dated intervals, missing-interval labels and timestamps render. The backend transport, corrected parsing/source review, tests and actual-data pure-helper acceptance have evidence; the signed-in rendered view is still unverified.
2. During an active supported IEX session, confirm advancing provider source times, quote consistency, request/publication latency and UI expiry. The worker polls approximately every five seconds while quotes expire after two seconds, so visible stale intervals are expected. Keep calendar scope limited to the regular session.
3. Verify durable halt acknowledgment, restart persistence, stale-heartbeat behavior, journal backup/restore and measured disk growth. Existing startup and one-disk/instance checks do not complete these operational tests.
4. Deliberately supply accounting history/opening evidence and complete execution/fee coverage before relying on authoritative P&L. Corporate-action comparability, float/news, same-clock RVOL, candidate discovery and any future execution release remain separate work.

No order execution is enabled. See [MARKET_WATCH.md](MARKET_WATCH.md) for the quote contract and [OBSERVATION.md](OBSERVATION.md) for the wider operational boundaries.

## Primary references

- [Alpaca stock snapshots](https://docs.alpaca.markets/us/reference/stocksnapshots-1)
- [Alpaca regular-session calendar](https://docs.alpaca.markets/us/reference/legacycalendar)
- [Alpaca market-data plans and IEX coverage](https://docs.alpaca.markets/us/docs/about-market-data-api)
- [Render deployment lifecycle](https://render.com/docs/deploys)
- [Render health checks](https://render.com/docs/health-checks)
- [Render environment-variable updates](https://render.com/docs/configure-environment-variables)
