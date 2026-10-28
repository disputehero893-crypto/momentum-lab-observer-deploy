# Spot crypto read-only observation — integration candidate

**Status (October 18 Chicago / October 19 UTC): DEVELOPMENT BRANCH ONLY.** The `feature/crypto-readonly-observer` branch now wires its optional Alpaca US spot-crypto quote collector into `ObservationWorker` and the owner-scoped `Publisher`. It is not merged or deployed; the existing Render service still runs the previous equities-only `main` code. No actual Alpaca crypto quote has been verified and neither paper nor live orders have been enabled.

## Feature and opt-in

- Default `CRYPTO_WATCHLIST` is empty; this preserves the exact current equities behavior, publisher invocation and execution hash.
- Optional `CRYPTO_WATCHLIST=BTC/USD,ETH/USD` enables GET-only crypto collection. This first release strictly accepts 1–10 unique uppercase pairs from `BTC/USD`, `ETH/USD`, `SOL/USD`. Slashes are **never** passed to the stock watchlist/strategy engine.
- `CryptoQuoteReader` sends a bounded GET only to Alpaca US crypto latest quotes, with server-held Alpaca **paper** keys; it has no order endpoint, submits no request body, blocks redirects and sanitizes all upstream exceptions.
- The collector separately records original source, receipt and collection timestamps. Its `Decimal` representation retains exact precision. Missing pairs, crossed/nonpositive quotes, malformed JSON and stale source evidence are explicitly unavailable or stale.
- A separate fixed `payload.crypto_watch` sibling is emitted by the existing owner-bound `worker_state` publisher, with `asset_class: spot_crypto`, `source: alpaca_crypto`, `observation_only: true`, `execution_enabled: false`, an allowlisted ordered `requested_pairs` and independently checked quote rows. The stock `market_watch` IEX, `price_history`, `strategy_review`, broker summary, accounting and durable emergency halt logic are unchanged.
- A failed crypto collector does not invalidate stock quote collection or execution-disabled state. The publisher projects only configured pair identities and rechecks timestamp causality, decimal safety, stale claims and age at publication, omitting invalid optional crypto input. A newer heartbeat **cannot** make old crypto source data fresh.
- The five-second journal stores only a bounded pair/status summary, not repeated raw quote payloads. The in-memory current report retains original timestamps.

## Verification actually performed

A dedicated GitHub Actions workflow `.github/workflows/crypto-readonly-tests.yml` runs the **complete** existing worker test suite with Python 3.12, including new crypto/stock integration tests. Run **39187746567** passed **303 tests in 7.854 seconds**, with no genuine broker calls: https://github.com/disputehero893-crypto/momentum-lab-observer/actions/runs/39187746567

All tests use injected fake provider responses and an in-memory/fake cloud publisher. The passing test suite is evidence of code behavior against fixtures; **not** evidence that live crypto quotes or authenticated Lovable pages have been observed.

## Remaining gates before production

1. Independently review the branch and compile the full integration diff. Check that no fake-provider assumption becomes a claim about actual crypto market data.
2. Update the Lovable Crypto Watch page status to derive from **fresh, authorized, actual** `crypto_watch` reports rather than its static “not connected” illustration. Keep the existing owner, paper, provenance, heartbeat, disabled-execution and exact timestamp gates.
3. Merge only after review; explicitly approve a separate controlled read-only Render deployment. Service auto-deploy remains **off**. Configure `CRYPTO_WATCHLIST=BTC/USD,ETH/USD` only at approved rollout, after preserving the previous deployment as rollback. Do not change real-money permissions or enter credentials in chat.
4. Verify actual Alpaca crypto response shape, user entitlement, source/receipt freshness, owner-scoped `worker_state.payload.crypto_watch` and signed-in Crypto Watch rendering. Verify existing AAPL/MSFT/SPY IEX observation, stop controls, identity and publishing after rollout; roll back on any regression.
5. Reconcile any needed market-data rate limits and provider entitlements. There is no trade execution, profit calculation or complete crypto strategy in this release.

No new paid service, database schema change, external secret, new account, order endpoint, or production deployment has been introduced.

References:
- Lovable `docs/backend-multi-asset-handoff.md` and `src/lib/multi-asset-watch.ts`
- Alpaca API: https://docs.alpaca.markets/us/reference/cryptolatestquotes-1
