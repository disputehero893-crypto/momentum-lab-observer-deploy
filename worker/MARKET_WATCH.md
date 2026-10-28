# Market Watch: read-only quote observation

## Approved scope

This increment observes AAPL, MSFT and SPY using the existing Alpaca IEX access and displays the observations in the signed-in dashboard. It is a connection-test watchlist. The worker cannot submit or cancel orders, and the accounting-history anchor remains unconfigured.

The existing single Render service, single persistent journal, owner binding and private-API authentication remain the deployment boundary. The frontend reads the existing owner-scoped worker_state row. No new database table, browser broker credential or market-data subscription is required by this increment.

## Observation contract

The optional worker_state.payload.market_watch object carries bounded, normalized provider observations. The following is a schema description, not an example of actual market prices:

```json
{
  "schema_version": "1.0",
  "source": "alpaca_market_data",
  "feed": "iex | sip",
  "collected_at": "UTC RFC3339 timestamp",
  "max_age_seconds": "number > 0 and <= 5; currently 2",
  "requested_symbols": "array of <=10 distinct configured uppercase equity symbols",
  "session": {
    "scope": "regular",
    "status": "open | closed | unknown",
    "date": "YYYY-MM-DD in America/New_York",
    "timezone": "America/New_York",
    "is_trading_day": "boolean | null",
    "open_at": "UTC RFC3339 | null",
    "close_at": "UTC RFC3339 | null",
    "checked_at": "UTC RFC3339 | null",
    "source": "alpaca_calendar",
    "max_age_seconds": 300,
    "error_code": "null | calendar_unavailable | calendar_invalid | calendar_stale"
  },
  "rows": [
    {
      "symbol": "one requested symbol",
      "feed": "same envelope feed",
      "status": "fresh | stale | missing | error",
      "bid": "positive decimal string | null",
      "ask": "positive decimal string | null",
      "source_at": "UTC RFC3339 | null",
      "received_at": "UTC RFC3339 | null",
      "error_code": "null | symbol_missing | symbol_invalid | quote_missing | quote_invalid | quote_timestamp_invalid | quote_stale | market_data_unavailable",
      "last_trade": {
        "price": "positive decimal string",
        "source_at": "UTC RFC3339",
        "received_at": "UTC RFC3339",
        "status": "fresh | stale"
      },
      "last_trade_error_code": "null | trade_missing | trade_invalid | trade_timestamp_invalid | trade_stale"
    }
  ]
}
```

The last_trade object may be null. Requested symbols and rows are limited to ten distinct configured equity symbols. A missing market_watch object means the worker has not reported this format; the frontend must not substitute synthetic values.

Prices remain positive finite decimal strings, bounded to 1e18 and eighteen fractional digits. Provider timestamps retain up to nine fractional digits. Quote and last-trade observations have independent timestamps and freshness states. Raw provider responses, balances, credentials, news text, arbitrary remote errors and trading instructions are outside this payload.

## Freshness and session meaning

The default source/receipt age limit is two seconds, independently of the worker's thirty-second heartbeat window. Both source and actual HTTP receipt must be within the quote limit, neither may be in the future, and source <= receipt <= collection <= publication must hold. A fresh heartbeat or successful HTTP response cannot refresh an old market event.

The frontend must recompute age using wall-clock time between network polls. Valid old prices may remain visible as stale, with original timestamps; they must not remain labeled fresh when they age out. Last-trade freshness is evaluated independently of bid/ask freshness. Query failure, an owner change or stale worker evidence prevents a current-data indicator.

The existing worker polls approximately every five seconds, so intervals in which a quote exceeds the two-second threshold are expected. This view is not a continuous tick stream.

Session state refers only to the regular U.S. equity session reported by the Alpaca calendar. It uses the New York date, dated open/close times and calendar evidence no more than five minutes old. Provider/calendar failures produce unknown session state. A closed regular session does not establish whether premarket, after-hours or overnight data is available.

## Collection and publication

The normal observer collects one bounded snapshot batch and calendar evidence for Market Watch independently of strategy metadata and historical-volume inputs. Missing float/news/history or incomplete accounting must not be invented to make prices visible. Selected-watchlist quote observation is not a whole-market scanner or a strategy acceptance result.

Per-symbol missing or invalid observations are reported separately so one symbol does not hide other valid rows. The publisher validates and reconstructs the allowlisted contract before writing the existing owner-bound state. The dashboard uses authenticated owner-filtered reads and does not write worker status or acknowledgment data.

The existing synthetic scanner, charts, replay and performance remain separate demonstration views. This increment does not publish paper_runs or paper_events, calculate broker performance, select daily momentum candidates or enable execution.

## Verification and operational acceptance

Focused tests cover partial responses, malformed/oversized fields, exact source timestamps, stale/future observations, independent last-trade age, regular-session evidence, bounded publication and GET-only observer behavior. Frontend checks cover owner mismatch, query failure, parser bounds and freshness expiry between polls. Actual executed results and deploy identifiers belong in [DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md).

Hosted acceptance proceeds in two parts:

1. Confirm actual provider observations reach the expected owner's worker_state and keep their source timestamps. During a closed regular session, old quotes should be explicitly stale.
2. During an active supported market-data session, confirm advancing source times, quote consistency, observed request/publication latency and correct UI expiry.

An initial closed-session read cannot establish open-session latency, full-market data coverage, strategy profitability or order-execution readiness. Durable halt/restart, backup/restore and complete financial accounting remain separate outstanding checks.

## Primary references

- [Alpaca stock snapshots](https://docs.alpaca.markets/us/reference/stocksnapshots-1): GET /v2/stocks/snapshots returns latest quote/trade information for requested symbols and an explicitly selected feed.
- [Alpaca U.S. market calendar](https://docs.alpaca.markets/us/reference/legacycalendar): GET /v2/calendar supplies regular-session dates and open/close times, including early closures.
- [Alpaca market-data plans](https://docs.alpaca.markets/us/docs/about-market-data-api): Basic IEX access differs from consolidated all-exchange coverage.


## Hosted receipt

The approved increment is deployed from runtime commit `7fe2554460d6041a69d434c0861032be1d617686`. On October 18, 2026, the correct owner's cloud row received actual AAPL, MSFT and SPY IEX observations with advancing worker heartbeats and HTTP receipt times. Friday quote/trade source times remained unchanged and were explicitly stale; the valid Sunday regular-session calendar was closed. The corrected Lovable source is `5fbbb8fc7490a534b107ded2d24723bc9fbc6873`.

See [DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md) for the exact deployment, observed timestamps, focused verification results and remaining active-session/visual checks. Backend receipt does not itself verify signed-in screen rendering.

