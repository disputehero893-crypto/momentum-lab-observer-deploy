# TactiRise stock market discovery — free IEX snapshot ranking

**Development branch only. Not merged, not deployed, no live discovery call accepted.**
Branch: `feature/stock-discovery-readonly` (draft PR #3). Host service `momentum-lab-observer` continues serving AAPL/MSFT/SPY observations from its deployed image.

## Overnight change (2026-10-28 CT)

Switched from Alpaca **SIP** screener endpoints (`/v1beta1/screener/stocks/movers` and `most-actives`) to free **IEX** multi-snapshots:

- `GET https://data.alpaca.markets/v2/stocks/snapshots?symbols=...&feed=iex`
- Local ranking into gainers / losers / most-actives over a bounded liquid universe
- `source=alpaca_iex_snapshots`, `feed=iex`
- SIP feed configs and SIP screener paths are rejected (need Derek's paid-data yes)

## Purpose

Preliminary **research-only** candidate ranking for the paper watchlist neighborhood (default universe includes AAPL, MSFT, SPY and nearby large caps). Not a complete momentum strategy, not execution, not a silent watchlist rewrite.

## Safety

- Isolated module: no imports into `ObservationWorker`, `Publisher`, `PaperSession`, or Lovable.
- Paper-mode credentials only. GET-only. No order/cancel routes. No redirects.
- Response bounded (≤256 KiB). Universe ≤50 symbols. Top ≤25.
- Every candidate stays `strategy_eligible=false`, `trade_eligible=false`, `execution_enabled=false`.
- Remaining requirements still include fresh IEX quotes, same-clock RVOL, float, catalyst, risk review, and paper-order approval.

## Tests

```bash
cd worker && python -m unittest tests.test_stock_discovery -v
```

GitHub Actions `.github/workflows/stock-discovery-tests.yml` runs worker regressions on this branch. Prefer `paper-autopilot-tests.yml` workflow_dispatch for the shared offline suite.

## Not in scope without Derek's yes

- Purchasing SIP entitlement or calling SIP screeners
- Wiring discovery into the deployed worker / publisher
- Changing the trade allowlist or arming paper orders
