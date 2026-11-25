# Automatic paper crypto orders

Paper only. The bot can buy and sell spot crypto by itself when every gate is open. With `CRYPTO_PAPER_EXECUTION_ENABLED` left false, the observer does what it does today and this loop does not place an order.

The equity strategy is an intraday bull flag. It needs a New York session, a $2–$20 price, float, and a news catalyst. Crypto quotes are a bid and an ask, and the market runs all week, so that rule does not fit. The crypto rule is the smaller version of the same idea.

## Rule (`crypto_spot_momentum_v1`)

Long only. No shorts. At most one order each cycle.

- Remember the last few mids for each pair on the watchlist (`BTC/USD`, `ETH/USD`, `SOL/USD`).
- **Buy** when the newest mid is at least 0.5% above the mid from three quotes earlier, and each step in between was flat or higher. The order is a limit buy at the ask, `time_in_force=gtc`, with a fractional quantity.
- Size comes from the existing risk settings: the smaller of the max notional and a 1% stop against `per_trade_risk`. The shared risk screen still has to pass (exposure, daily loss, open positions, quote age, kill switch).
- **Sell** the whole position when the mid falls to 1% under the entry. That order is a market sell with `time_in_force=ioc`. It still needs the paper lock and a clear kill switch.
- A wide spread (more than the existing basis-point cap) or a quote older than the existing quote-age cap is skipped. The stock rule's five-cent spread cap is not used; it would reject every normal Bitcoin quote.
- Weekend and overnight quotes are allowed. Crypto does not use the equity session clock.

Orders go only to `https://paper-api.alpaca.markets/v2/orders`. The live host is refused by the order client.

## Where it runs

The observer calls this once per cycle, after it has already collected the crypto watch. Stock orders stay off (`execute=False`). The crypto step is skipped entirely when the flag is false, and a crypto failure does not fail the stock cycle.

`python -m momentum.crypto_paper_loop` runs the same cycle on a timer. Use one process, not both, against one paper account.

After the equity `serve-paper` release lands, that process can call `CryptoPaperLoop.cycle(watch)` with the watch it already has. This change does not edit `serve-paper`, `paper_runtime.py`, or `alpaca.py`.

## How the reviewed hash is produced

Do not paste a hash from this repository. The worker computes it from the environment it is actually running with:

`Config.from_env().review_hash()`

That is the same value `Config.public()["paper_configuration_hash"]` shows. Set `PAPER_REVIEWED_CONFIG_HASH` to that hex string in the server environment. Turning `CRYPTO_PAPER_EXECUTION_ENABLED` on changes the hash, because the hash then includes the paper host, the crypto pairs, and `gtc`/`ioc`. A hash taken while the flag was off will not match.

## Env names that would have to be set to arm it

Names only. This pull request does not set them.

- `CRYPTO_PAPER_EXECUTION_ENABLED`
- `CRYPTO_WATCHLIST`
- `PAPER_TRADING_ENABLED`
- `PAPER_REVIEWED_CONFIG_HASH`

`MOMENTUM_MODE`, `ALPACA_API_KEY`, and `ALPACA_SECRET_KEY` stay the paper values already used for quotes. The order path reads those same key names and still refuses the live trading host.
