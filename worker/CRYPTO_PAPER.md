# Alpaca paper crypto orders

Paper trading only. This path can submit a spot-crypto order to Alpaca's paper account. It cannot send that order to the live trading host.

Crypto quotes stay the existing read-only watch (`CRYPTO_WATCHLIST`). Placing an order is a separate switch, **off by default**.

## Flag

| Name | Default | What it does |
| --- | --- | --- |
| `CRYPTO_PAPER_EXECUTION_ENABLED` | `false` | When false, crypto order code refuses before any HTTP call. Quotes behave as they do today. |

The flag is not in Render config. Nothing in `serve-observe` or the equity paper process calls this client, so leaving the flag false keeps the running worker observation-only.

## What has to be true before an order can be sent

All of these, together:

1. `CRYPTO_PAPER_EXECUTION_ENABLED=true`
2. `CRYPTO_WATCHLIST` includes the pair (`BTC/USD`, `ETH/USD`, and/or `SOL/USD`)
3. The existing equity paper lock is clear:
   - `MOMENTUM_MODE=paper`
   - `PAPER_TRADING_ENABLED=true`
   - `PAPER_REVIEWED_CONFIG_HASH` equals `Config.review_hash()`
   - `Config.paper_blockers()` is empty
4. Alpaca **paper** keys are already in `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`
5. A caller invokes `CryptoPaperClient.submit_order`. No loop in this change does that.

Turning the flag on changes `review_hash()` (host, pairs, and `gtc`/`ioc`). The previous equity hash will not match until someone recomputes `PAPER_REVIEWED_CONFIG_HASH`. While the flag is false, a crypto watchlist does not change that hash.

The order itself still has to pass the existing risk screen (`check_paper_intent` on the `alpaca_crypto_paper` feed) and `RiskConfig` (max notional, daily loss, max shares, and per-trade risk when a stop is supplied). A latched journal kill switch or a locked `RiskManager` blocks the order. Crypto qty may be fractional. `time_in_force` is `gtc` or `ioc` (`day` is refused). There is no equity session check: crypto is treated as 24/7, and `extended_hours` is not sent.

The request URL is fixed in code to `https://paper-api.alpaca.markets/v2/orders`. `https://api.alpaca.markets` is refused.

## Not done here

- No deploy, no `render.yaml` / `render-observe.yaml` edits, no flag flipped on.
- Not wired into `serve-observe` or the separate equity `serve-paper` work.
- No live trading host and no new API key names.
