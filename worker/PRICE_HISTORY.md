# Price history observations

The optional `worker_state.payload.price_history` adds two bounded observations
to the existing owner-scoped dashboard: a raw daily-bar close for the previous
calendar trading session, and up to ten elapsed regular-session minute
intervals. The configured watchlist remains AAPL, MSFT and SPY on IEX.

This is an input collection increment. It does not evaluate a setup, calculate
daily percentage gain, construct an order, or activate execution. The existing
Market Watch and selection-review version 1.0 contracts remain unchanged.

## What the values establish

The daily close is checked against the latest calendar trading date strictly
before the collection's New York date. The worker explicitly requests that
date's historical `1Day` bar. It requires its timestamp to identify midnight
New York on that exact date, valid OHLC and positive volume, and a completed
calendar session before the request. It does not use the snapshot field name
`prevDailyBar` to infer a date.

The value is the selected feed's **raw daily-bar close**. It is not an official
closing-auction price, a consolidated-market close, or a price verified to be
comparable across corporate actions. Historical symbol remapping is explicitly
disabled. There is no fallback to another symbol, feed or adjustment policy.

Minute bars describe elapsed intervals in the selected regular session. Their
timestamps identify interval starts. Each interval must end by a cutoff frozen
before its historical request starts. A later response cannot make a candle
that was unfinished at request time qualify retroactively. An elapsed interval
can still be revised by the provider; `revisions_possible` is always true.

## Calendar and window policy

| Calendar situation on the New York collection date | Selected minute window |
| --- | --- |
| Trading day before the regular open, or before the first minute has ended | Current session, no elapsed intervals; no historical-minute request and no previous-day fallback |
| Trading day during the regular session | The last ten elapsed minute intervals, or fewer near the open |
| Trading day after the regular close | The last ten regular-session intervals ending at that day's calendar close |
| Nontrading day | The last ten regular-session intervals of the preceding calendar trading session |
| Calendar unavailable or invalid | Explicitly unavailable; no weekday or holiday guess |

The calendar supplies the regular opening and closing times, including early
closes. The worker uses the `America/New_York` time zone rather than a fixed UTC
offset. This window is regular-session only. It does not provide the experiment's
04:00-start cumulative volume, its 30-session same-clock volume baseline, or
premarket pattern evidence.

An exhausted response can contain gaps because the feed did not produce a bar
for every expected interval. `missing_intervals` lists those interval starts.
No empty slot becomes a zero-volume candle, a carried-forward price, or proof
of continuity. `observed` means that the returned bars passed this observation
contract, even when the bounded window contains gaps.

## Requests, caching and isolation

`PriceHistoryCollector` uses the existing GET-only observation client. The
historical-bars method requests the configured symbols in one batch, with
`feed` fixed to the configuration, `adjustment=raw`, `asof=-`, `currency=USD`,
`sort=asc`, and `limit=1000`. Only `1Day` and `1Min` are allowed by this method.

There is one bounded page per daily/minute request. A nonempty
`next_page_token` is an explicit `history_page_incomplete` failure; a short page
is not treated as evidence that pagination has finished. The collector does
not silently truncate a larger result, issue an unbounded history scan, or
switch to the dormant strategy snapshot builder.

The whole history report is cached for 60 seconds, retaining its original
collection, request and receipt timestamps. Calendar evidence is cached for at
most 300 seconds. Historical collection precedes the current snapshot request,
so these extra reads do not age quotes already collected in the same cycle.
Daily and minute data have separate component errors, and one malformed symbol
does not discard healthy symbols. A history failure does not grant strategy
readiness or discard otherwise valid broker/quote observations.

The history query's JSON decimal price literals are decoded without a binary
floating-point round trip. Published prices and volumes are bounded decimal
strings. No credential, arbitrary provider field or upstream error body belongs
in this contract.

## Publication contract

The existing owner-scoped JSON payload gains one optional sibling. No new table,
RLS policy, credential, environment variable or infrastructure resource is
required.

| Envelope field | Contract |
| --- | --- |
| `schema_version`, `source` | `1.0`, `alpaca_market_data` |
| `observation_only` | `true` |
| `feed`, `adjustment`, `symbol_mapping`, `currency` | Configured `iex` or `sip`, `raw`, `disabled`, `USD` |
| `collected_at` | Original history collection timestamp, never refreshed by a new heartbeat |
| `refresh_interval_seconds`, `max_age_seconds` | `60`, `120` |
| `requested_symbols` | Same ordered, unique configured watchlist, at most ten |
| `revisions_possible`, `official_close`, `full_session_coverage` | `true`, `false`, `false` |
| `corporate_action_comparability` | `not_assessed` |
| `calendar` | Source, New York anchor date, trading-day state, original checked time, 300-second age limit, previous and target sessions, fixed error |
| `rows` | One bounded daily-close and minute-window result per configured symbol |

Each session contains `date`, `open_at` and `close_at`. The previous session must
precede the anchor date. The target session is the anchor date when it is a
trading day, and the previous session on a nontrading day.

Each `previous_close` contains `status`, `session_date`, `close`, `source_at`,
`requested_at`, `received_at`, and `error_code`. Its status is `observed`,
`stale`, `missing` or `error`.

Each `minute_bars` contains `status`, `session_date`, `interval_seconds: 60`,
`scope: regular`, `window_start_at`, `cutoff_at`, `requested_at`, `received_at`,
`expected_intervals`, `missing_intervals`, `bars`, and `error_code`. Its status
is `observed`, `stale`, `empty`, `not_started`, `missing` or `error`. A bar holds
`start_at`, `end_at`, `open`, `high`, `low`, `close`, and `volume`. There are at
most ten bars and ten explicitly missing intervals per symbol.

`not_started` means no regular interval had ended at collection: zero expected
intervals, empty arrays and null window/request/receipt timestamps. It is not
an assertion about what a future browser clock will observe.

Fixed error codes are `calendar_unavailable`, `calendar_invalid`,
`calendar_stale`, `history_unavailable`, `history_response_invalid`,
`history_page_incomplete`, `symbol_missing`, `daily_bar_missing`,
`daily_bar_invalid`, `daily_bar_session_mismatch`, `minute_bar_invalid`,
`history_timestamp_invalid`, and `history_stale`. Calendar errors use only the
three calendar codes. Provider error messages are not published.

## Causal and freshness checks

Timestamp ordering is checked at nanosecond precision. Minute evidence must
satisfy all of the following:

```text
window_start <= bar.start
bar.end = bar.start + 60 seconds
bar.end <= cutoff <= requested_at <= received_at <= collected_at <= publication
calendar.checked_at <= requested_at
```

The window is derived from the original request: its cutoff is the earlier of
the request floored to a minute and the calendar close, and its start is the
later of the calendar open and ten minutes before that cutoff. A shorter,
older window inside those bounds cannot substitute for the requested window.
Retained stale values must still pass the same structural and causal checks.
Missing or failed responses contain no invented prices or bars; a failed
request can legitimately have a request timestamp without a receipt.

The age of each actual request and receipt must be at most 120 seconds to keep
an observed claim current. A historical source timestamp is not subject to the
two-second quote rule. It can correctly identify a Friday interval in a Sunday
history response, with Sunday recorded as the actual retrieval time. Old source
dates are never rewritten as new dates. Calendar checks have a separate
300-second bound, and the New York anchor date must still match.

The dashboard must additionally enforce its existing owner, authentication,
query-error, Paper-mode, observation-only, disabled-execution and heartbeat
gates. It must match feed and ordered symbols to the same worker's Market Watch,
and require history collection no later than Market Watch collection and the
worker heartbeat. Quote freshness remains independent of history retrieval.
Missing or invalid history must not hide valid quotes.

## Display and remaining inputs

Market Watch shows per-symbol daily-close provenance and an expandable exact
OHLCV/interval table. Visible gaps and source/receipt timestamps take precedence
over a compact chart that would hide absent minutes. Values can remain visible
as last reported after their verification expires, with an explicit stale state.

The selection review still requires a **comparable** previous-session close.
The raw observation here does not establish corporate-action comparability, so
it does not resolve that gain requirement. Float, catalyst evidence, same-clock
volume, full setup inputs, independent risk checks and execution remain separate
unfinished milestones. No percentage return or historical performance is
calculated from this report.

The full report is kept only in bounded current worker state and its latest
owner-scoped publication. Repeated cycle journal entries retain a safe summary
and canonical report hash rather than duplicating every bar every five seconds.
This feature is not a durable archive of provider captures. A summary hash alone
cannot reconstruct a historical input or establish a replay/backtest result.

## Offline verification

The complete worker suite passed **271 tests in 3.080 seconds** on
2026-10-19 UTC. It includes six implementation/decoder tests and 28 independently
written price-history contract tests, together with all existing regressions.
The checks cover exact decimal decoding, nanosecond ordering, calendar/DST and
early-close dates, request-derived windows, interval gaps, status/error
consistency, component isolation, bounded publication, cache expiry, summary-only
cycle journals and the GET-only transport boundary. They use invented provider
responses and temporary journals, with no real order or network request.

`examples/price-history.synthetic.json` is a shared contract fixture with
invented DEMO-A/DEMO-B values, an explicit missing interval, eighteen-decimal
prices and nine-digit timestamp fractions. It is not an actual provider capture
or a request to expand the deployed watchlist.

## Primary references

Checked 2026-10-18:

- [Alpaca historical bars](https://docs.alpaca.markets/us/reference/stockbars):
  timeframe, inclusive bounds, feed, raw adjustments, literal-symbol mapping,
  currency, sorting, total page limit and continuation tokens.
- [Alpaca market data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq):
  New York daily-bar dates, minute interval starts, feed differences, aggregation
  rules, absent bars and historical symbol mapping.
- [Alpaca real-time stock data](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data):
  ended minute bars can receive later updates from late trades.
- [Alpaca market calendar](https://docs.alpaca.markets/us/reference/legacycalendar):
  trading dates and session times including early closures.

Local synthetic verification and actual hosted observation receipts are recorded
in [DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md). Synthetic tests do not establish
active-session latency, signed-in rendering, trade availability or profitability.
