# Hosted Crypto Market Watch — acceptance record

**Acceptance scope:** Backend live Alpaca US spot-crypto quotes, owner-scoped Supabase publication, and existing execution-safety controls. **Signed-in Lovable UI visual acceptance remains outstanding.**

## Deployment

- Deployment target: existing Render Docker worker `momentum-lab-observer`, service `srv-dbaipqm7bikc73epsj5g`.
- Deployed GitHub merge commit: `46427143aa4f0f9b068ec8b47c4c63bd8f2de2ec`.
- Render deployment: `dep-dbanrgnbtjjs73fta6cg` finished **live** at `2026-10-19T02:18:19Z`.
- Existing service branch: `main`, automatic deploy: **off**, original worker and storage retained; no paid instance or new service created.
- `CRYPTO_WATCHLIST=BTC/USD,ETH/USD` is configured as an opt-in observer allowlist. Existing `AAPL,MSFT,SPY` IEX watchlist remains distinct and unchanged.
- No broker order path or paper/live execution was enabled.

## Server-side verification

Read-only checks against the dedicated `momentum-lab-paper` Supabase project on October 19 (UTC):

- At `2026-10-19T02:18:40Z`, the owner-scoped `worker_state` row reported a heartbeat about one second old, broker connected in Paper mode, `execution_enabled=false`, `entry_gate_open=false`, and both BTC/USD and ETH/USD rows marked **fresh**, with original Alpaca source timestamps and separately recorded collection/receipt timestamps.
- At `2026-10-19T02:19:27Z`, a further check showed advancing heartbeat and original provider timestamps for BTC/USD and ETH/USD. The stock watchlist and all three equity rows remained; stock quotes appropriately showed stale sources outside U.S. stock hours.
- At `2026-10-19T02:28:38Z`, another independent SQL check reported `status=observing`, Paper mode, active broker status, no worker error, `orders_capability=false`, both crypto pairs fresh, and exactly three equity watchlist rows.
- Read-only PostgreSQL policy inspection confirmed `worker_state_select` is granted to `authenticated` using `owner_id=auth.uid()`, with no authenticated write policy for `worker_state`. The `paper_commands` owner policies permit SELECT for the owner and inserts that are queued `halt_new_entries` requests only. This is policy inspection, not a browser/session RLS penetration test.

A successful backend response and automated tests do not certify execution quality, profitability, or authenticated browser rendering.

## Automated verification

- Python 3.12 GitHub Actions run [39187951900](https://github.com/disputehero893-crypto/momentum-lab-observer/actions/runs/39187951900) passed **303 tests** on the final crypto development-branch head before merge.
- Lovable `crypto-watch` UI evidence-driven status correction reported **346 frontend tests passing**, plus typecheck and build, at frontend commit `f3c72b184232934de5cc20752ee14edef52948d1`. The frontend remains **unpublished**. That test suite used synthetic/mocked owner-scoped data, not a signed-in, real-provider visual session.

## Outstanding acceptance steps

1. Open Lovable preview **/crypto-watch** while signed in to the same owner account linked to the Supabase worker, confirm `Read-only · Fresh observations` appears while provider timestamps are current, and verify BTC/USD and ETH/USD source/receipt age changes across refreshes. Do not use public/signed-out views to infer a connection.
2. Open **/market-watch** and **/connections** to verify AAPL/MSFT/SPY IEX data, crypto readiness, and forex pending status; check mobile and desktop layout with authenticated state.
3. Continue monitoring error-free runtime and provider entitlement/rate limits. Failure, missing rows or stale evidence must visibly degrade status; no synthetic fallback.
4. Keep trading disabled until independent paper-order, strategy, risk, execution accounting and broker acceptance milestones are fully reviewed and explicitly authorized.

This record contains neither API keys nor account identifiers nor unpublished frontend personal data.
