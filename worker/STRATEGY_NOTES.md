# Experimental paper strategy: `experimental_v1`

This is a deterministic, long-only price-and-volume experiment. Its functions
produce auditable intentions; they cannot place an order or make a network call.
The experiment is not an exact implementation of Ross Cameron's full practice,
and passing its tests establishes software behavior, not trading profitability.
The source research specification remains a separate, non-executable document.

## What comes from the research, and what we chose

The starting scanner thresholds are the instructional 2024 five-pillar variant:
price from $2 through $20, gain at least 10% against the previous close, relative
volume at least 5, and float strictly below 20 million shares. The public articles
are [How to Trade the Bull Flag Pattern with Confidence](https://www.warriortrading.com/how-to-trade-the-bull-flag-pattern-with-confidence/)
and [In-Depth Guide to my MACD Scalping Strategy for Beginner Traders](https://www.warriortrading.com/in-depth-guide-to-my-macd-scalping-strategy-for-beginner-traders/),
sources S10 and S12 in the accompanying research register. This program requires
an affirmative fresh-catalyst attestation, choosing a stricter policy than some
of the news-optional variants recorded in the research.

The [Bull Flag Chart Pattern & Trading Strategies](https://www.warriortrading.com/bull-flag-trading/)
guide, source S05, supplies the qualitative impulse, lighter pullback, typical
two-to-three red candles, intrabar break of the prior red candle's high, and
pullback-low invalidation. It discusses 2:1 potential and warns about deep or
heavy-volume pullbacks. The source does not settle all numerical definitions.
The same-clock RVOL concept also appears in the separate
[Momentum Day Trading Strategies for Beginners](https://www.warriortrading.com/momentum-day-trading-strategy/)
guide, source S06, whose scanner thresholds differ. We preserve those historical
distinctions in the research rather than calling this composite Ross's invariant
or latest complete strategy.

Every executable choice below is a proposed experiment where the research leaves
a definition unresolved. None is a claim that these exact parameters have been
validated against Ross's decisions or unseen market returns.

| Component | Default definition in this experiment |
| --- | --- |
| Session | Entries from 07:00 inclusive to 10:00 exclusive in America/New_York, on a calendar-confirmed trading date |
| Price | Both the latest eligible trade and executable ask must be in $2–$20 inclusive |
| Gain | `(latest trade / previous close - 1) * 100 >= 10` |
| RVOL | Cumulative eligible volume from 04:00 to the observation cutoff divided by the mean cumulative volume to that same New York clock on exactly 30 preceding sessions |
| Float | Positive integral shares, `< 20,000,000`, effective no more than 7 days ago, already known at decision time |
| Catalyst | Explicitly confirmed; published within 24 hours and already known at decision time |
| Impulse | Exactly 3 consecutive completed green one-minute bars, each with body at least half its range; strictly higher closes and highs |
| Impulse strength | At least 2% from the first bar's open to the last bar's close; mean volume at least 10,000 shares per bar |
| Pullback | The immediately following 2–3 completed red bars; nonincreasing highs and closes; no high above the impulse peak |
| Pullback depth | Positive retracement no larger than 50% of the completed impulse's high-to-low range |
| Pullback volume | Mean no larger than 60% of impulse mean volume |
| Entry observation | A current intrabar eligible trade at or above the final completed red bar's high plus $0.01 |
| Entry price | Current ask, no more than $0.03 above the trigger; the trade also cannot be farther than $0.03 above it |
| Stop reference | Minimum completed pullback low minus $0.01 |
| Target reference | Entry plus twice the price-to-stop distance; this target must fit at or below the already observed impulse high |
| Quote freshness | Event and receipt times both at most 2 seconds old, with neither in the future |
| Entry spread | No more than $0.05 and no more than 100 basis points of the bid |
| Position management | One position, one entry, no adds or averaging down; full exit at stop, target, 600 seconds held, or 10:00 New York |

Here, “first pullback” means the first red-bar run immediately following the
locally identified three-bar impulse. It does not establish that this is the
stock's first pullback of the day. The evaluator recognizes a satisfied intrabar
trigger; it does not infer an unobserved earlier crossing or trade arrival path.
The caller must deduplicate the stable `setup_id` so repeated observations cannot
create repeated entries for the same setup.

The target is **2R of gross price geometry**. The separate sizing allowance for
execution costs reduces the net reward relative to the risk budget. The program
does not label a filled gross 2R target as a net 2R result. Fills, slippage and fees
belong in the journal's realized accounting.

The evaluator uses the known impulse high as a deliberately narrow resistance
proxy. It does not read the entire daily chart, MACD, EMA, VWAP, Level 2, tape
quality, sympathy, offering risk, or discretionary hot/cold market exceptions.
Its decisions explicitly carry `price_and_volume_only_no_depth`. Missing depth
does not mean depth passed; this is a separately named reduced-fidelity test.

## Causal input contract

The complete artificial fixture is `tests/test_strategy.py:valid_snapshot()`.
It is a made-up stock and made-up episode, not downloaded market history. Prices
are decimal strings and quantities integral values. Datetimes must carry a UTC
offset; storage and comparison normalize to UTC, and market clock calculations
use `America/New_York` with daylight saving time.

`evaluate_setup(snapshot, StrategyConfig())` expects:

- `symbol`, `feed`, `now` and a paper mode if mode is supplied.
- `session.date` in New York and `session.is_trading_day is True` from an exchange
  calendar. Weekends are rejected even if incorrectly attested. The adapter must
  supply holidays, special closures and the correct 30 prior sessions; the engine
  has no independent exchange-calendar service.
- `quote`: positive noncrossed `bid` and `ask`, `timestamp`, `received_at` and
  matching `feed`; a supplied `symbol` must match.
- `last_trade`: `price`, `timestamp`, `received_at`, matching `feed` and matching
  `symbol`. The adapter is responsible for eligible trade-condition filtering.
- `facts`: `previous_close`, `float_shares`, `float_effective_at`, `float_known_at`,
  `catalyst_confirmed`, `catalyst_published_at`, and `catalyst_known_at`.
- `bars`: contiguous completed one-minute OHLCV bars from the current session,
  carrying `start`, `feed`, `complete is True` and preferably `available_at`.
- `rvol`: `current_volume`, `as_of`, `feed`, 30 sorted distinct `prior_sessions`,
  and exactly 30 history rows with `session`, `clock`, `volume`, `feed`, `known_at`.

Bar `start` is the opening instant; the bar ends 60 seconds later. A bar cannot
enter the feature set until both its end and `available_at` are no later than the
decision. If availability is omitted, the adapter is asserting availability at
bar close; this is a synthetic-data convenience, not measured vendor latency.
The trigger trade must occur in the minute immediately after the last completed
bar. Neither the current bar's final close nor its later high or low is used.

RVOL's cutoff is no later than the decision and less than one minute old. The
fixture freezes it at the preceding completed minute. Every historical
denominator row must use exactly that New York clock, so 09:35 cumulative volume
cannot be replaced by a full-day total. History may not include the current day,
future days, repeated days, a different feed, a different session start, or data
whose availability is after the decision. The adapter must construct current
volume using only trades up to the cutoff and must carry the correct complete
calendar. The engine cannot prove those assertions by inspecting a total alone.
Missing volume for a date is not silently treated as zero or dropped from the
denominator.

The same feed must be used for current observations and the historical RVOL
baseline. A matching label alone does not verify the provider's coverage,
corporate-action convention or trade conditions. The adapter must keep those
consistent and record them. IEX-only results and consolidated-feed results are
different experiments even if both have internally consistent labels.

A boolean catalyst field is an upstream factual attestation, not permission for
news text or a language model to issue orders. A later headline or current float
cannot be backdated to label a historical entry. The previous-close value also
must come from the prior completed session with consistent adjustment handling.
Neither this engine nor its tests claim to verify the truth of manually supplied
float or catalyst facts.

The returned `Decision` has `accepted`, stable audit `reasons`, `intent` or null,
`metrics`, and `profile`. `to_dict()` converts Decimals and timestamps to JSON
strings. Every strategy decision includes a SHA-256 `config_id` for the complete
validated StrategyConfig. Accepted entries carry it too, and their `setup_id`
includes it. Different configuration overrides therefore remain distinguishable
within the same experimental family.

## Independent risk contract

`RiskManager.assess(intent, account, quote, now, connected=True,
reconciled=True)` performs a second check immediately before a paper entry can
be considered. Both operational flags default to false. The caller must affirm
current connectivity and reconciliation; a saved flag is insufficient.

The account must contain current `as_of`, New York `session_date`, `equity`,
`buying_power`, **gross** `realized_pnl`, **gross** `unrealized_pnl`, nonnegative
`fees`, `position_qty`, and `open_order_qty`. A missing account timestamp blocks;
an account more than 5 seconds old blocks. Fees are deducted exactly once in
`net_pnl = realized_pnl + unrealized_pnl - fees`. Supplying already-net P&L as
gross would double-count costs and violates this contract.

The default paper limits are $25 per trade, a $75 daily net-loss trigger, $1,000
position notional, 500 shares, and 1% of the final completed minute's volume. The
demo account starts with hypothetical $10,000 equity in the runner. These are our
configurable experiment values, not user-authorized real-money limits and not
Ross's account sizing.

For an entry price `E` and stop reference `S`, the sizing denominator is
`E - S + $0.02` per share. The $0.02 execution allowance is a proposed planning
buffer, not an assurance about attainable fills. Quantity is the minimum of:

1. The floor of `min($25, $75 + current net P&L)` divided by that denominator.
2. The floor of current buying power divided by entry price.
3. The floor of `min($1,000, account equity)` divided by entry price.
4. The floor of 1% of the final completed minute's volume.
5. The configured 500-share cap.

Every limit, price and P&L input must be finite. Prices, monetary limits and
defined risk distances must be positive; invalid or zero-capacity inputs produce
no entry. Stop must be below entry and target at least 2 times the price-to-stop
distance above entry. A changed ask above the approved entry limit also blocks.
The liquidity cap is historical-volume sizing, not displayed executable depth.

The synthetic fixture produces a 120-share intention at $6.31, a $6.22 stop
reference and a $6.49 target reference: $757.20 notional and $13.20 planned risk
including the proposed allowance. This is an arithmetic test, not evidence of
a trade available in a real market.

An accepted risk intent includes `risk_config_id`, quantity, notional and planned
risk, with `paper_only: true` and `live_enabled: false`. RiskConfig cannot enable
live mode. These modules never execute even paper orders themselves.

## Latches, commands, fills and exits

The risk caller must invoke `assess(None, ...)` on marked account events even if
the strategy produces no new entry. Otherwise an unrealized loss could go
unexamined while a position is open. Any partial position or pending opening
remainder blocks a new entry. The caller must reserve pending quantity before
submission, account for partial fills immediately, and reconcile fills and
cancels; acceptance by this pure function is not an order reservation.

Daily net P&L at or below minus $75 latches that New York session. The latch
survives a rebound and `to_state()`/`restore_state()` round trips. A new calendar
date may receive a fresh budget only from current, reconciled account values;
prior locks remain in state. The caller must reset daily accounting accurately.
Restore always demands a new reconciliation assertion. Invalid persisted state
raises an error and must stop startup, not be discarded as a fresh account.

`halt_new_entries=True` is a reversible entry gate and requests cancellation of
opening remainders. It does **not** request liquidation of a position. The caller
owns persistence and explicit resumption of this operational command.

`kill_switch=True` is a distinct emergency latch. A daily loss latch or emergency
kill blocks new entries, requests cancellation of opening remainders and emits
`flatten_required` when a position remains. The emergency latch persists even
if a later call supplies `kill_switch=False`; there is no automatic reset method.
Those flags are requests for the caller's controlled exit path, not statements
that cancellation or liquidation happened.

`evaluate_exit(position, snapshot, config, force_reason=None)` accepts a position
with symbol, integer quantity, entry price, stop reference, target reference and
opened time. It produces a full-quantity paper sell intention on an observed bid
at/below stop, at/above target, at the 600-second holding limit, at the session
cutoff, or with an explicit forced reason. There is no separately invented
“failed follow-through” timer or partial-profit scheme in this version.

Exits use the currently observed bid, including a bid below the planned stop.
A stale or invalid quote blocks construction even for a forced exit and requires
operator attention; it never produces a fictional fill at the old stop price.
An exit intention always says fill confirmation is required and does not claim
the position is flat. The caller's broker-order reconciliation remains necessary
until confirmed fills establish flatness. A loss trigger therefore cannot
guarantee the realized loss remains at its configured dollar threshold.

## Verification

From this `worker` directory, run:

```bash
PYTHONPATH=. python -m unittest discover -s tests -p 'test_*.py' -v
```

The strategy and risk unit tests cover exact scanner boundaries, missing facts,
mixed feeds and symbols, future/current-bar leakage, quote-age boundaries,
same-clock RVOL data, invalid risk math, finite sizing and quantity floors, costs
and unrealized P&L in the daily trigger, partial exposure, persistence, explicit
reconciliation, entry halt versus emergency kill, and truthful exit intentions.
Synthetic tests establish those invariants. Historical validation and broker
execution testing are separate work and cannot be inferred from this test suite.
