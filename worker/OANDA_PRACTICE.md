# OANDA practice (paper) forex adapter

**Status: DRAFT — not merged, not deployed, no OANDA account or token configured in this pull request.**

This branch adds a paper-only OANDA v20 adapter pinned to the **practice** hosts, an optional GET-only forex watchlist for the existing observation worker, and a leverage-capped live-price practice order path that stays **OFF by default**. Live `fxTrade` hosts are refused in code. Nothing in this change signs up for an OANDA account, stores a token, or deploys to Render.

## Host allowlist

Hosts are constants in `momentum/oanda.py`, not configuration:

| Role | Origin |
| --- | --- |
| REST | `https://api-fxpractice.oanda.com` |
| Streaming | `https://stream-fxpractice.oanda.com` |

Any other host is refused, including `api-fxtrade.oanda.com` and `stream-fxtrade.oanda.com`. The check is an **allowlist** (exact `https` + exact hostname, no port, no userinfo, no trailing DNS dot, yielding `non_practice_host_refused`). Case variants, subdomain tricks, and host/URL override environment variables also raise.

## Environment names

Set these only as **server-side** worker environment variables (for example the Render service `srv-dbh0uevlk1mc739lbo1g`). They are not committed, not read from `VITE_` / `PUBLIC_` / `NEXT_PUBLIC_` prefixes, and are omitted from `Config.public()` and object reprs.

| Name | Kind | Default | Purpose |
| --- | --- | --- | --- |
| `OANDA_PRACTICE_API_TOKEN` | secret | `""` | Practice personal access token (Bearer). |
| `OANDA_PRACTICE_ACCOUNT_ID` | secret | `""` | Practice account id used in `/v3/accounts/{id}/…` paths. |
| `OANDA_ENVIRONMENT` | config | `""` | Must be `"practice"` for any forex order/cancel/close call. Any value other than `""` or `"practice"` is refused at config load. |
| `OANDA_PRACTICE_ORDERS_ENABLED` | boolean flag | `false` | Separate explicit gate for practice forex order placement, in addition to the existing paper lock. Forex stays watch-only while `false`. |
| `OANDA_FOREX_WATCHLIST` | optional, non-secret | `""` | Comma-separated pairs, underscore or slash form. Example: `EUR_USD,GBP_USD,USD_JPY`. |
| `OANDA_FOREX_RISK_CONFIG_JSON` | optional JSON | `{}` | Optional overrides for `ForexRiskConfig` (validated fail-closed; effective leverage cannot exceed `5.0`, stop loss cannot be disabled). |

Approved pairs (at most ten, unique): EUR/USD, GBP/USD, USD/JPY, EUR/GBP, AUD/USD, USD/CHF, USD/CAD, NZD/USD, EUR/JPY, GBP/JPY.

The equity paper lock is unchanged: `MOMENTUM_MODE`, `PAPER_TRADING_ENABLED`, and `PAPER_REVIEWED_CONFIG_HASH`. An opt-in forex watchlist does **not** change the reviewed equity configuration hash while `OANDA_PRACTICE_ORDERS_ENABLED=false`. When `OANDA_PRACTICE_ORDERS_ENABLED=true`, `OANDA_ENVIRONMENT`, the practice host, and `ForexRiskConfig.config_id` become part of `Config.review_hash()`, so arming or changing leverage caps requires re-computing `PAPER_REVIEWED_CONFIG_HASH`.

## Runtime behavior and gates

- Observation (`serve-observe`) stays GET-only. If `OANDA_FOREX_WATCHLIST` is set, the worker collects practice pricing in parallel with stocks (and optional crypto). A forex failure cannot fail a stock cycle. Execution remains disabled by default.
- `OandaPracticeClient` GET methods (account, summary, pricing, instruments, candles, positions, trades, open trades, pending orders) require `MOMENTUM_MODE=paper` and practice credentials. They work while `OANDA_PRACTICE_ORDERS_ENABLED=false` and `PAPER_TRADING_ENABLED=false`.
- Order placement (`OandaPracticeClient.submit_order`, `ForexPracticeTrader.place_order_from_live_price`), `cancel_order`, and `close_trade` require **all** of:
  1. `MOMENTUM_MODE=paper`
  2. `OANDA_ENVIRONMENT=practice`
  3. `OANDA_PRACTICE_ORDERS_ENABLED=true` (separate flag, **default `false`**)
  4. `PAPER_TRADING_ENABLED=true`, matching `PAPER_REVIEWED_CONFIG_HASH`, and empty `Config.paper_blockers()` (no bypass flag of any kind)
- New entries also pass **both**:
  - `#8` `check_paper_intent` with `feed="oanda_practice"`
  - `check_forex_leverage_intent` in `momentum/forex_risk.py` (margin-aware notional caps, max 2:1 effective leverage default, mandatory directional `stopLossOnFill`, daily loss cap, max open positions, spread and quote-age limits).
- Protective `close_trade` does not run the entry-only screens, but still requires the full execution gate above.
- See [OANDA_LEVERAGE_REVIEW.md](OANDA_LEVERAGE_REVIEW.md) for the leverage/margin risk review.

## What Derek still has to do

1. Choose the sign-up email and create an OANDA **fxTrade Practice** account (not done here).
2. Paste `OANDA_PRACTICE_API_TOKEN` and `OANDA_PRACTICE_ACCOUNT_ID` into the Render worker `srv-dbh0uevlk1mc739lbo1g` as secrets, plus an optional `OANDA_FOREX_WATCHLIST` (do not put credentials in chat, Git, or frontend env).
3. Review and merge this draft, then explicitly approve arming (`OANDA_ENVIRONMENT=practice`, `OANDA_PRACTICE_ORDERS_ENABLED=true`, `PAPER_TRADING_ENABLED=true`, and updated `PAPER_REVIEWED_CONFIG_HASH`) and a deploy. Auto-deploy stays off.

Official references: [OANDA v20 development guide](https://developer.oanda.com/rest-live-v20/development-guide/) and [pricing endpoint](https://developer.oanda.com/rest-live-v20/pricing-ep/).
