# Strategy presets (paper only)

The worker's default strategy and the default watchlist are the same liquid
names. Nothing here enables live trading, buys a market-data feed, or turns on
`PAPER_TRADING_ENABLED`.

## Default — `liquid_large_cap_iex_v2` (enabled)

Strategy Lab's free-IEX retune (FINDINGS.md). Chosen as the default because
`liquid_large_cap_iex_v1` produced **0** trades over ~12 months of IEX bars on
AAPL/MSFT/SPY. Still research-grade: Lab did **not** clear paper-arming gates
(edge fails at $0.04 slip). Do not arm without Derek's yes.

- **Universe / watchlist:** `AAPL`, `MSFT`, `SPY`
- **Feed:** Alpaca free **IEX** only. SIP is a paper blocker for this preset.
- **Impulse volume:** `impulse_vol_vs_same_clock_median = 1.2` × same-clock IEX
  bar-volume median supplied on the snapshot (absolute `min_impulse_mean_volume`
  is unused). Adapter must fit medians on prior sessions only.
- **Shape (vs v1):** body ≥ 0.25, ≥2/3 green impulse bars, net-up closes (not
  strict rising), pullback 1–3 red bars with closes non-increasing, retrace ≤65%,
  2R target **may** exceed impulse peak
- **Floors:** gain 0.10%, RVOL 1.2×, impulse gain 0.10%
- **Entry window:** 09:35–15:45 America/New_York
- **Empty `WATCHLIST`:** fills `AAPL,MSFT,SPY`

`StrategyConfig()`, `StrategyConfig.liquid_iex()`, and
`StrategyConfig.liquid_iex_v2()` all build this preset.

## Legacy — `liquid_large_cap_iex_v1` (available, not default)

Original PR#9 liquid thresholds (`min_impulse_mean_volume=50000`, strict 3-bar
rising greens, 2–3 bar pullback, target within impulse peak). Construct with
`StrategyConfig.liquid_iex_v1()`. Kept for comparison; expect ~0 IEX setups.

## Gated — `small_cap_catalyst_v1` / profile `experimental_v1` (disabled)

This is the original experimental small-cap first-pullback: float strictly under
20 million, a confirmed catalyst, relative volume of at least 5, a $2–$20 band,
and entries from 07:00 to 10:00 America/New_York. Construct it with
`StrategyConfig.small_cap_catalyst(...)`.

It does not run unless `enable_gated_presets` is true. Even then, paper
execution stays blocked unless the data feed is SIP (or an equivalent full tape),
point-in-time float metadata is present, and the catalyst checks pass. Free IEX
is rejected. Synthetic `demo` and `synthetic` feeds can still exercise the pure
function in unit tests and the fixed demo replay; those feeds cannot be selected
as `ALPACA_DATA_FEED` and cannot reach the paper broker adapter.

An active watchlist that contains a symbol outside the active preset's universe
is a paper blocker (`watchlist_outside_strategy_universe`). The small-cap preset
has an empty universe because its names would come from a scanner, so that
blocker is not how the gate is enforced. `point_in_time_symbol_metadata_missing`
is a paper blocker only when the active preset requires float and catalyst
evidence.

## Ownership

Strategy research and backtests belong to TactiRise Strategy Lab. This change
implements Lab's retuned IEX preset in the paper runtime; it does not arm paper
trading.


## Observe-mode reminder

Default watchlist and feed for the paper observer:

```
WATCHLIST=AAPL,MSFT,SPY
ALPACA_DATA_FEED=iex
MOMENTUM_MODE=paper
PAPER_TRADING_ENABLED=false
```

Run offline strategy/preset tests with:

```sh
cd worker && python -m unittest tests.test_strategy tests.test_liquid_iex_v2 tests.test_presets_and_universe -v
```
