# OANDA practice leverage and margin risk review

**Status: DRAFT — pre-arming review for `worker/momentum/forex_risk.py` and `worker/momentum/forex_execution.py`.**

## 1. How leverage and margin work on OANDA practice

- **Account-level leverage up to 50:1:** OANDA practice accounts (US-style defaults) allow up to 50:1 leverage (`marginRate = 0.02` = 2% on major pairs such as `EUR/USD`, and 3%–5% on pairs such as `GBP/USD`, `USD/CAD`, `USD/CHF`, or crosses). Practice accounts simulate the same margin engine as live accounts using virtual funds.
- **Units-based sizing:** OANDA v20 orders are sized in **units** of the base currency (minimum 1 unit, not 100k standard lots). For `EUR/USD` at `1.08`, `100` units = `$108` USD notional; for `USD/JPY`, `100` units = `$100` USD notional regardless of the JPY quote price.
- **Mark-to-market margin and closeout:** Margin used is `notional_usd * pair_margin_rate`. `NAV = balance + unrealizedPL`. OANDA triggers automatic **margin closeout** if `marginCloseoutPercent` reaches 100% (when `NAV` falls to `50%` of `marginUsed`). High leverage turns small pip moves into fast margin exhaustion.

## 2. Conservative caps chosen and why (`ForexRiskConfig`)

All entries must pass both the `#8` `check_paper_intent` (`feed="oanda_practice"`) screen and `check_forex_leverage_intent`:

| Control | Default | Why |
| --- | --- | --- |
| `max_effective_leverage` | `2.0` (hard ceiling `5.0`) | Caps total open forex notional at **2x account NAV** (25x lower than OANDA's 50:1 default) and blocks config overrides above `5.0`. |
| `max_margin_utilization` | `0.20` (20% of NAV) | Ensures projected `marginUsed <= 20%` of NAV and `required_margin <= marginAvailable`, leaving an 80%+ cushion against drawdowns and margin closeout. |
| `max_order_notional_usd` | `$100` | Matches the `#8` paper risk per-order notional limit (`RiskLimits.max_order_notional`). |
| `max_notional_per_pair_usd` | `$250` | Limits concentration in any single currency pair. |
| `max_total_open_exposure_usd` | `$1,000` | Matches `#8` `RiskLimits.max_total_exposure`. |
| `max_units_per_order` | `1,000` units | Hard cap at 1 micro-lot per order regardless of pair price. |
| `require_stop_loss` | `True` (immutable) | Every order must attach a broker-side `stopLossOnFill` on the protective side of entry, within `[0.05%, 5.0%]` of entry price. |
| `max_risk_per_trade_usd` / `pct` | `min($25, 1% NAV)` | Caps dollar loss from entry to stop-loss price at 1% of NAV and at most `$25`. |
| `max_daily_loss_usd` | `$50` | Matches `#8` `RiskLimits.max_daily_loss`; halts new entries once hit. |
| `max_open_positions` | `3` | Matches `#8` `RiskLimits.max_open_positions`. |
| `max_feed_age_ms` / `max_spread_bps` | `2000 ms` / `15 bps` | Rejects stale quotes and wide-spread / illiquid sessions before entry. |

## 3. Key failure modes and mitigations

- **Price gaps (news events, Sunday open):** Standard OANDA `stopLossOnFill` orders are **not guaranteed stops**; a weekend or central-bank gap can fill worse than the stop price. Mitigations: 2:1 effective leverage cap, 20% max margin utilization, 1% max planned stop risk, and `max_spread_bps=15`.
- **Weekend closure & rollover widening:** Forex closes Friday ~5:00 PM ET to Sunday ~5:00 PM ET, and spreads widen sharply around daily 5:00 PM ET rollover. Mitigations: `tradeable=True` is required on pricing rows, `max_feed_age_ms=2000` rejects halted quotes, and `max_spread_bps=15` blocks wide rollover spreads.
- **Slippage on `MARKET` orders:** Fast-moving practice prices can move between quote read and order fill. Mitigation: `ForexPracticeTrader` defaults to attaching `priceBound` (`max_slippage_bps=10`, capped at `50` bps) on `MARKET` orders and screens size against the live ask/bid.
- **Stream / network disconnect:** A lost connection leaves open positions unmanaged on our side. Mitigations: every entry requires broker-held `stopLossOnFill` at submission time so downside protection rests on OANDA's engine, and `close_trade` bypasses entry-only caps so protective flattens are never blocked.
- **Duplicate orders:** Retries after timeouts can create double positions. Mitigations: every order requires a bounded `clientExtensions.id`, `seen_order_ids` blocks client-side duplicates in both `#8` and `ForexRiskState`, and `OandaPracticeClient` disables HTTP redirects and enforces timeouts.

## 4. What remains before arming

1. **OANDA practice account & secrets:** Derek chooses an email, creates an OANDA fxTrade Practice account, and puts `OANDA_PRACTICE_API_TOKEN` and `OANDA_PRACTICE_ACCOUNT_ID` into Render `srv-dbh0uevlk1mc739lbo1g` as secrets.
2. **Practice round-trip verification:** Run a read-only `/pricing` and `/summary` check against `api-fxpractice.oanda.com` using the practice account, then verify account currency (`USD`) and pair margin rates.
3. **Explicit arming approval:** Keep `OANDA_PRACTICE_ORDERS_ENABLED=false` (watch-only) until Derek explicitly approves arming `OANDA_ENVIRONMENT=practice`, `OANDA_PRACTICE_ORDERS_ENABLED=true`, `PAPER_TRADING_ENABLED=true`, and the updated `PAPER_REVIEWED_CONFIG_HASH`.
