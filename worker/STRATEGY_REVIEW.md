# Observed-symbol strategy requirements

The requirements review connects the existing Market Watch observations to the
selection inputs required by `experimental_v1`. It covers the worker's configured
watchlist, currently the approved AAPL, MSFT and SPY connection-test symbols on
IEX. It does not discover or rank the whole market.

`momentum/strategy_review.py` is a pure projection. The publisher constructs it
from its sanitized Market Watch report and the worker's current validated
`StrategyConfig`. It adds no provider request, accounting read, database table,
engine evaluation, order instruction or background execution path. The observer
continues to use GET-only broker access and `execute=False`.

## What can be checked now

| Requirement | Current evidence and behavior |
| --- | --- |
| Price range | Both the observed ask and last trade must be valid, fresh and within the configured inclusive range. The default is $2 through $20. |
| Daily gain | Unavailable: a verified previous-session close and its provenance are not in Market Watch. |
| Relative volume | Unavailable: the current cumulative volume and complete same-clock historical baseline are not in Market Watch. |
| Float | Unavailable: no point-in-time float estimate is supplied. |
| Catalyst | Unavailable: no source-linked catalyst evidence and confirmation are supplied. |

Every row and the complete report therefore have `overall: "incomplete"`.
Passing the price check does not identify a tradable setup. A fresh price outside
the band is an observed failure of that one rule; it does not turn the other
four unknown requirements into failures. Pattern and independent risk assessment
remain unevaluated.

The range check does not independently verify trade conditions, security/listing
eligibility, displayed liquidity or fill availability. It uses the observations
already reported by Market Watch. There is no midpoint substitute for a missing
last trade, no inference from a connected account, and no synthetic fallback.

## Rule source and experiment identity

The source comparison refers to Ross Cameron's specific instructional selection
variant in [How to Trade the Bull Flag Pattern with Confidence](https://www.warriortrading.com/how-to-trade-the-bull-flag-pattern-with-confidence/)
and [In-Depth Guide to my MACD Scalping Strategy for Beginner Traders](https://www.warriortrading.com/in-depth-guide-to-my-macd-scalping-strategy-for-beginner-traders/),
S10 and S12 in the research register. Those articles describe $2–20 prices,
at least 10% daily gain, relative volume of at least 5, and float below 20 million.
The MACD article also emphasizes breaking news. They do not establish the
complete numeric or discretionary rule set used for every trade.

`source_basis.thresholds_match` compares only `min_price`, `max_price`,
`min_gain_pct`, `min_rvol` and `max_float_exclusive` with those named thresholds.
The report separately compares all 27 current StrategyConfig fields against the
default experiment. Changed field names are listed and the full validated
configuration has its own SHA-256 `config_id`.

Neither match flag establishes fidelity to the trader's complete or current
practice, a historical test result, profitability, or approval to apply settings.
The 30-session same-clock RVOL from 04:00 New York, seven-day float age,
mandatory confirmed catalyst within 24 hours, freshness limits, bar definitions,
holding period and risk budgets are our experimental choices. See
[STRATEGY_NOTES.md](STRATEGY_NOTES.md) for those definitions and limitations.

## Publication contract

The existing owner-scoped `worker_state.payload` gains an optional
`strategy_review` object. Older payloads without Market Watch omit it entirely.
No schema migration is required.

| Field | Meaning |
| --- | --- |
| `schema_version` / `kind` | `1.0` / `selection_requirements` |
| `observation_only` / `profile` | `true` / `experimental_v1` |
| `config_id` | SHA-256 of the complete validated current strategy configuration |
| `source_basis` | Named source threshold comparison, default-experiment comparison, and bounded changed-field lists |
| `settings` | Current selection thresholds, freshness policies, RVOL convention, and experiment window |
| `evidence_ref` / `calendar_ref` | `market_watch` / `market_watch.session` |
| `collected_at` | The original timestamp of the referenced Market Watch report |
| `evaluated_at` | The same publication instant as the worker heartbeat |
| `feed` / `requested_symbols` | Same feed and ordered, unique maximum-ten-symbol list as Market Watch |
| `experimental_window` | New York date and whether the clock is inside the configured half-open window |
| `overall` | Always `incomplete` in this version |
| `rows` | Exactly one bounded requirements result per configured symbol |

Prices and their precise source/receipt timestamps remain exclusively in Market
Watch. The review references that same report by collection instant and symbol;
it does not copy prices into a second mutable representation. The existing
worker envelope continues to state `execution_enabled: false` and
`entry_gate_open: false`.

Each `price` result uses one of these combinations:

| State | Reason codes |
| --- | --- |
| `pass` | `both_prices_in_range` |
| `price_outside_range` | `last_trade_outside_range`, `ask_outside_range`, `both_prices_outside_range` |
| `unavailable` | `quote_missing`, `quote_invalid`, `quote_stale`, `trade_missing`, `trade_invalid`, `trade_stale` |

The other criterion objects have `state: "unavailable"` and respectively
`previous_close_missing`, `same_clock_history_missing`,
`point_in_time_float_missing`, and `catalyst_evidence_missing`. Extra fields in a
provider response cannot manufacture evidence for these requirements.

## Freshness and clock behavior

Publication revalidates Market Watch, then requires both quote and trade to
remain fresh under the smaller of the strategy's age limit and Market Watch's
age limit. Source ≤ receipt ≤ collection ≤ evaluation is checked at nanosecond
precision. A source or receipt exactly two seconds old is allowed under the
default; one nanosecond beyond it is stale. A prior unavailable or stale result
cannot be promoted by choosing a looser setting.

The dashboard must enforce its owner, authentication, query-error, provenance,
Paper-mode, disabled-execution and heartbeat gates. It must match the review to
the same Market Watch report and expire any price pass or range failure as the
referenced quote or trade ages. The heartbeat does not refresh a price. The
five-second polling cadence and two-second evidence limit can legitimately show
stale intervals; this feature is not a streaming execution feed.

The regular-session calendar and the experiment's clock window are different
facts. For the default 07:00 inclusive to 10:00 exclusive New York window, 08:00
on a trading date is clock-inside while the regular session remains closed.
Sunday at 08:00 can also be clock-inside, but its calendar still says nontrading
day. Neither clock state establishes selection, pattern, risk or order eligibility.
Dashboard clock diagnostics use current wall time, not the synthetic playback
clock, and stale calendar evidence remains unknown.

## Dashboard and draft workflow

Scanner separates `view=observed` from `view=synthetic`. Observed symbols use
actual owner-scoped worker evidence. The fixed test cases retain the immutable
Python export and its synthetic label. The application shell identifies the
selected mode so synthetic playback controls do not appear in observed review.

Strategy compares the current draft form to the fixed synthetic reference.
Seven fields have direct mappings: hypothetical account size, per-trade risk,
daily loss limit, pullback retracement percentage, minimum and maximum pullback
candles, and volume lookback. Maximum positions and an independently editable
stop buffer have no direct exported reference mapping. Matching seven values is
not a complete configuration match. Signed-in drafts are owner-scoped records;
signed-out drafts are browser-local. Saving, importing and editing remain draft
operations and do not change the worker or the fixed replay.

The separate [offline decision replay](DECISION_REPLAY.md) evaluates explicitly
supplied snapshots with supported strategy overrides. It does not run a browser
draft, simulate an account or establish historical performance.

## Verification and remaining evidence

The initial backend review verification passed 34 focused tests: five projection
tests, 15 independent contract tests and 14 existing publisher regressions.
Tests cover both-price boundaries, independent stale/missing evidence, exact
nanosecond expiry/future times, stricter configured freshness, source/config
identity, regular-session versus experiment window, optional-payload
compatibility, raw-field exclusion, and absent order/acceptance fields.

Run from `worker/`:

```sh
PYTHONPATH=.:tests python -m unittest test_strategy_review test_strategy_review_contract test_worker_publisher -v
```

These tests use artificial data and injected transports. Current runtime commit,
deployment, hosted observations, frontend checks and their limitations belong in
[DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md). This review does not resolve the
still-missing historical volume, point-in-time metadata, whole-market discovery,
accounting completeness, or real-session operational acceptance.

The completed increment passed the full **237-test worker suite**, including
22 selection-review tests (seven implementation/precision and 15 independent
contract tests) and 19 new offline replay tests. The final precision regressions
cover more than 28 significant decimal digits and extremely small scientific
age limits without ambient Decimal rounding or unbounded exponent allocation.
The corresponding frontend correction reported **216 passing regression tests
across nine files**, including 66 selection-review tests, and a successful
TypeScript/build check. Its source and changes received independent review;
signed-in hosted rendering was not observed during that verification.
