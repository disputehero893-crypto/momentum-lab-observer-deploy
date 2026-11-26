# Stock day-trade profiles (paper only, OFF by default)

Three opt-in profiles hold Derek's stock day-trading rules in code. **None is on.**
The worker keeps trading the default `liquid_large_cap_iex_v2` strategy, and the
reviewed config hash for today's configuration is unchanged.

| Profile | Rules from | Data it needs | State in this release |
| --- | --- | --- | --- |
| `large_cap_day_trade_v1` | The bot's v1 large-cap rules (ChatGPT chat "Day Trading Strategies Comparison", draft PR #14) | Free Alpaca IEX 1-minute bars | Off. Engine + tests only; not wired into the paper loop yet |
| `ross_cameron_small_cap_v1` | Ross Cameron's public small-cap momentum rules (same chat, asked Nov 26 2026) | Share float, news catalysts, full-market gainers, SIP feed | Off **and hard-gated**: refuses to trade, no paid provider exists |
| `small_cap_candle_patterns_iex_v1` | Ross's selection + risk rules, with candle patterns and IEX-relative volume (Derek, Nov 26 2026: no SIP, "watch candles for patterns") | Free IEX bars, Alpaca movers (+ news when available), FMP Starter float | Off. Engine, replay and tests only; not wired into the paper loop yet |

Source files: `momentum/day_trade_core.py` (session clock, bars, VWAP/ATR,
sizing, counters), `momentum/large_cap_day_trade_v1.py`,
`momentum/ross_cameron_small_cap_v1.py`, `momentum/day_trade_profiles.py`
(selection, blockers, hash block). Tests: `tests/test_day_trade_*.py`,
`tests/test_large_cap_day_trade_v1.py`, `tests/test_ross_cameron_small_cap_v1.py`.

## How a profile is selected (and why nothing happens yet)

`DAY_TRADE_PROFILE` is empty by default. Allowed values: empty/`off`,
`large_cap_day_trade_v1`, `ross_cameron_small_cap_v1`. Anything else fails config
validation.

When a profile is selected:

1. `Config.review_hash()` gains a `day_trade_profile` block (below), so
   `PAPER_REVIEWED_CONFIG_HASH` must be recomputed and approved by Derek.
2. `Config.paper_blockers()` gains `day_trade_profile_runtime_not_wired` until the
   follow-up wiring PR lands. **Because every paper path (stocks, crypto, OANDA)
   requires an empty blocker list, selecting a profile today would pause all paper
   order placement.** Do not set it before the wiring PR.
3. `/ready` → `configuration.day_trade_profile` shows the selection, whether it is
   active, and every blocker. `launch_blockers` lists the same reasons.
4. The unchanged `RiskManager` still rejects intents from both profile ids
   (`unapproved_intent_mode_or_profile`) until the wiring PR adds them to
   `APPROVED_PAPER_PROFILES`.

The wiring PR is kept separate because it touches `observation.py`,
`session.py`, `alpaca.py` and `paper_runtime.py`, which PR #57 owns.

## large_cap_day_trade_v1 (BOT/v1 rules)

- Universe AAPL, MSFT, SPY; long only; regular-session bars and entries only;
  entries carry `extended_hours=false`.
- Indicators: completed 1-minute bars, 15-bar warmup, ATR = SMA of 14 true
  ranges (first bar = high-low), session VWAP from each bar's VWAP (IEX proxy),
  reset daily, no forward fill (a missing minute drops any setup in progress).
- Regime: D10 efficiency >= 0.30, VWAP up >= 0.10 ATR over 5 bars, close above
  VWAP, 8 of the last 10 closes above VWAP = UP (DOWN mirrors; else CHOP;
  zero path = CHOP; short history = NOT_READY). Only UP qualifies a setup; DOWN
  cancels. AAPL/MSFT also need a fresh same-minute SPY close above SPY VWAP and SPY
  VWAP not lower than 5 minutes earlier (feed SPY's bar before AAPL/MSFT each minute).
- ORB retest: 15-bar opening range (09:30-09:44); breakout = close > ORH + b with
  the previous close at or below (b = max($0.02, 0.10 x A0), A0 frozen at the
  breakout); retest within 5 bars with a low in [ORH - b, ORH + 0.25 A0]; a close
  below ORH - 0.5 A0 cancels; a separate confirmation bar must close above ORH + b,
  the prior bar's high + b and VWAP + b by the 6th bar; stop = lowest low after
  the breakout through confirmation - b; max one filled ORB per symbol per day.
- VWAP pullback: bar before the pullback is UP (A0 frozen there); at least one
  of the prior 10 closes >= VWAP + 1.0 A0; the first lower close starts it; 2-5
  pullback bars, one touching VWAP +/- 0.25 A0; a close below VWAP - 0.5 A0
  cancels; confirmation closes above VWAP + b and both prior highs + b; entry
  limit must be <= VWAP + 1.25 A0; stop = lowest low of the pullback through
  confirmation - b; max lifetime 6 bars; re-arming needs a fresh UP bar.
- Windows (ET): 09:45-09:50 ORB; 09:50-10:45 ORB or VWAP; 10:45-11:30 VWAP;
  11:30-13:30 nothing; 13:30-15:00 VWAP; nothing after 15:00. ORB wins a tie.
- Entry: buy limit = signal close + 0.20 A0, cancelled after 60 s, at the entry
  cutoff, on a close at the stop, or when the day halts. Skipped unless
  0.75 A0 <= entry - stop <= 2.5 A0 (the stop is never moved).
- Exits: target entry + 2R on the full position; structural stop; no-progress
  exit at 10 min if price never reached +0.5R; max hold 45 min; daily-loss and
  session flatten override. Partials, breakeven, trailing and scale-ins are off
  and cannot be switched on in v1.
- Sizing: E_ref = min(session-start equity, $100k); per campaign 0.025% ($25),
  per symbol 2.5% ($2,500), gross 5% ($5,000), committed risk 0.05% ($50), daily
  loss 0.075% ($75), 2 campaigns. **Each cap is the stricter of the spec and the
  global `RiskConfig`**, so with today's `RiskConfig` it trades at most $25 risk,
  $1,000 notional, $75 a day and one position at a time. Shares =
  floor(min(risk/(P-S+c), open-risk/(P-S+c), symbol $/P, gross $/P)), whole
  shares, c = max($0.02, 0.0002 x P, RiskConfig allowance); skip under 1 share.
  Admission also needs round-trip cost (0.5 bp a side) <= 20% of the stop distance.
- Caps and cooldowns: one active or pending campaign per symbol; 3 campaigns per
  symbol and 6 per day; a symbol is off after 2 losing campaigns; 10-minute
  cooldown after a win or breakeven, 15 after a loss. Counters, positions and
  pending orders survive a restart (`to_state()` / `state=`).
- Session: no new buys after 15:30 ET, flatten from 15:50 ET. Early closes use
  close - 30 / close - 10 (13:00 close -> 12:30 / 12:50); the broker calendar's
  close should be passed in, with Nov 27 and Dec 24 2026 built in as a fallback.
  Prior-day positions are liquidated at the first check.
- Data: a completed bar can only produce an entry if it was first received
  within 10 s of its end; stale bars block entries. RVOL is logged only.

## ross_cameron_small_cap_v1 (Ross Cameron rules)

- Five-factor gate: price $2-$20, float < 20M, RVOL >= 5x, up >= 10%, news
  catalyst published in the last 24 h. Top-5 gainer is logged as a preference.
- Gap and Go (09:30-10:00 ET, gap > 4%): close through the premarket high, or a
  1-minute opening-range breakout with the stop at the first candle's low.
- Bull flag (09:30-11:30 ET): >= 2 green impulse bars moving >= 2%, then 2-3 red
  candles (4+ rejected), retrace <= 50%, no pullback low under VWAP (a 9-EMA break
  is a logged warning); entry on a bar that breaks and closes over the last red
  high; stop at the pullback low - 1 cent; stops wider than $0.20/share skipped
  (Ross's example); no chasing more than 0.5R past the trigger.
- Exits: sell half at +2R and move the stop to entry; the first red 1-minute
  candle close exits (before or after the partial); session flatten.
- Discipline: stop for the day after 3 losing trades in a row or at the daily
  max loss; at most 5 trades a day (BOT safety cap; Ross aims for 3-5).
- Risk: $25 per trade, $75 a day, $1,000 notional, one position. Ross's own
  numbers can only be lowered by `RiskConfig`, never raised.
- **Hard gate.** `FloatProvider`, `CatalystProvider` and `GainersScanner` are
  interfaces only. With any of them missing, or with a non-SIP feed, the engine
  returns `profile_blocked` for every bar and the config lists
  `float_provider_missing`, `catalyst_provider_missing`,
  `gainers_scanner_missing`, `requires_sip_or_full_tape_feed`.

## Data the Ross profile would need (public prices, checked Nov 26 2026)

Nothing here is bought or wired. Each line needs Derek's explicit yes.

| Need | Option | Public price | Notes |
| --- | --- | --- | --- |
| Full tape (SIP) for RVOL, premarket highs, small-cap bars | Alpaca Algo Trader Plus | $99 / month | Same Alpaca account; adds SIP websocket and unlimited symbols |
| Full-market gainers | Alpaca screener `movers` endpoint | Included with Alpaca data (computed from SIP) | Top 1-50 gainers; plan coverage on Basic not verified |
| News catalyst | Alpaca News API (Benzinga) | Included with Alpaca data; real-time may need the paid plan | Alpaca said pricing could change after its beta |
| Share float | Massive (ex-Polygon) `/stocks/vX/float` | $0 Basic (5 calls/min, latest float only); Starter $29 / month | No point-in-time history |
| Share float (alt.) | Financial Modeling Prep shares-float | Starter $29 / month ($19 billed yearly) | US exchanges |

Cheapest complete set on paper: about **$99 / month** (Algo Trader Plus + free
float endpoint), before testing whether the free pieces are good enough.
Switching to SIP would also trip the current preset's `liquid_preset_requires_iex`
check, so the wiring PR has to handle the feed per strategy.

## What enabling changes in the reviewed hash

Selecting a profile adds this block to `review_hash()` (nothing else changes):

```json
"day_trade_profile": {
  "profile": "large_cap_day_trade_v1",
  "profile_config_id": "<sha256 of every LargeCapV1Params value>",
  "effective_caps": {"per_campaign_risk": "25.00000", "symbol_notional": "1000",
                     "gross_notional": "1000", "committed_risk": "25",
                     "daily_loss": "75.00000", "max_simultaneous": 1, ...},
  "universe": ["AAPL", "MSFT", "SPY"], "feed": "iex",
  "entry_extended_hours": false, "limit_ttl_seconds": 60,
  "risk_config_id": "<RiskConfig fingerprint>"
}
```

For the Ross profile the block names `ross_cameron_small_cap_v1`, its own
`profile_config_id`, `universe: ["full_market_scanner"]` and feed `sip` (and the
top-level `feed` would change to `sip` as well). Any change to a profile
parameter or to `RiskConfig` changes the block and therefore the hash. Tests pin
the current hashes so an accidental change fails CI.

## Data adapters for ross_cameron_small_cap_v1 (`momentum/day_trade_data.py`)

Real implementations of the three provider interfaces, **off by default** and not yet
wired into the paper loop (the wiring PR follows #57). They only exist as objects when
`build_provider_set(env)` is called, and that returns an empty `ProviderSet` unless
`DAY_TRADE_DATA_PROVIDERS_ENABLED` is true. They never read `DAY_TRADE_PROFILE`, are not
inputs to `PAPER_REVIEWED_CONFIG_HASH` (pinned by tests), and the profile still refuses to
trade on `ALPACA_DATA_FEED=iex` (`requires_sip_or_full_tape_feed`).

| Adapter | Interface | Source | Notes |
|---|---|---|---|
| `FmpFloatProvider` | `FloatProvider` | FMP `/stable/shares-float` (Starter plan, `FMP_API_KEY`) | Float cached per symbol per New York day; unknowns retried after 5 min; 200 calls/min budget (plan allows 300); 429/5xx start a 60 s cooldown. FMP returns the current float, so use it for live/paper scans only. |
| `AlpacaMoversScanner` | `GainersScanner` | Alpaca screener movers + snapshots + daily bars | Pre-filters to the $2-$20 / +10% window, then adds RVOL = today's daily volume / mean of the previous 20 daily volumes, both from the configured `ALPACA_DATA_FEED`. Missing history gives RVOL 0 (gate rejects). `premarket_high` is None. |
| `AlpacaNewsCatalystProvider` | `CatalystProvider` | Alpaca news (`/v1beta1/news`, sourced from fiscal.ai) | Newest item tagged to the symbol in the last 24 h and not after `as_of`; roundups tagged to more than 3 tickers are ignored; 5 min cache. |

All adapters: read-only GETs to two fixed hosts (`financialmodelingprep.com`,
`data.alpaca.markets`), no redirects, 5 s timeout, 2 MB body cap, request budgets, and any
failure means "unknown" (`None` / `[]`) so the five-factor gate rejects. Keys are never in
errors, `repr()` or `status()` (counters only).

Known gaps: the free movers list resets at 09:30 ET and shows the previous session until
then, so **premarket gappers are invisible** (a stale list returns no rows); RVOL on the
IEX feed is a same-feed proxy and reads low early in the day; free IEX bars are not the
full tape, so a SIP feed is still required before the profile can trade.

Env names a future deployment would set (names only): `DAY_TRADE_DATA_PROVIDERS_ENABLED`,
`FMP_API_KEY`, plus the existing `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` / `ALPACA_DATA_FEED`.

## small_cap_candle_patterns_iex_v1 (free data: IEX + movers/news + FMP float)

Derek did not approve the $99/mo SIP feed and asked to "see what we can do with
what we have and by watching candles for patterns". Research and sources:
`docs/handbook/11-candle-patterns-on-free-data.md`.

* Files: `momentum/candle_patterns.py` (pure detectors, 1m and 5m, plus 5m bar
  builder and IEX-relative volume helpers), `momentum/small_cap_candle_patterns_iex_v1.py`
  (engine), `momentum/candle_replay.py` (offline replay). Tests:
  `tests/test_candle_patterns.py`, `tests/test_small_cap_candle_patterns_iex_v1.py`,
  `tests/test_candle_replay.py`.
* Gate: price $2-$20, FMP float < 20M, up >= 10%, IEX pace RVOL >= 3x (the movers
  scanner's same-feed RVOL divided by the share of a normal day already traded),
  news preferred (`catalyst_policy="require"` makes it mandatory).
* Entry: one of bull flag, first pullback, micro pullback (1m), 5-minute
  opening-range breakout, VWAP reclaim or higher-low breakout; the trigger bar must
  close over the level with a strong close or bullish engulfing (or a hammer at
  the pullback low) and an in-session IEX volume surge (>= 1.5x 1m / 1.2x 5m of
  its own recent bars). 09:35-11:30 ET only; skipped when fewer than 60% of the
  last 10 minutes printed an IEX bar.
* Exits and risk: stop one tick under the pattern low (3 cents to 4% of price, 8% on 5m);
  half at 2R then stop to entry; runner exits on a close under the 9-EMA; session
  flatten; three losses in a row ends the day; global caps ($25/trade, $75/day,
  $1,000 notional, one position, 5 trades/day).
* Blockers when selected: `day_trade_profile_runtime_not_wired` (until the #57
  wiring), `float_provider_missing`, `gainers_scanner_missing`. News is optional
  and `ALPACA_DATA_FEED=iex` is allowed (so is `sip`).
* Hash: unchanged while `DAY_TRADE_PROFILE` is empty. If it were selected on the
  hosted shape the reviewed hash would become
  `2545caf816d02cbd839720d1790af9b91b5989c9caa0f6f4127809a107b2874c` (pinned in `tests/test_day_trade_profiles.py`).

### Offline replay

```
cd worker
FMP_API_KEY=... python -m momentum.candle_replay --start 2026-10-13 --end 2026-11-25 \
    --bars fmp-5m --out /tmp/candle-replay.json
# with Alpaca keys, the 1-minute IEX bars the live profile would see:
FMP_API_KEY=... ALPACA_API_KEY=... ALPACA_SECRET_KEY=... python -m momentum.candle_replay \
    --start ... --end ... --bars alpaca-iex
```

Keys come only from the environment and are never printed or written; the cache
holds market data only.

### Replay results (Nov 26 2026; honest, small, NOT evidence of an edge)

Window 2026-10-13 to 2026-11-25 (32 sessions). Bars: FMP 5-minute (the box has
no Alpaca keys, so the 1-minute IEX replay was not run). Universe: 2,224 current
listings ($1.5-$25, cap < $2B) -> 1,293 had a day whose high reached +10% -> 660
with current FMP float < 20M -> 2,628 symbol-days. 221 symbol-days passed the
gate at some bar between 09:35 and 11:30 ET; patterns fired 28 times.

| Run | Change | Trades | Win rate | Avg R | Total R | P&L at $25 risk |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | as first written (4% stop cap on every timeframe) | 2 | 50% | -0.23 | -0.46 | -$17.41 |
| 2 | 5-minute stop cap 4% -> 8% (21 of 28 signals had been rejected for stop width) | 7 | 14% | -0.72 | -5.05 | -$94.55 |

Run 2 by pattern: higher-low breakout 3 trades (1 win, -0.17R avg), VWAP reclaim
2 (0 wins), bull flag 1 (loss), ORB 1 (loss). After run 2 no more tuning was done
on purpose: with 7 trades, tuning would only fit noise ([S17]).

Caveats: 5-minute consolidated bars, not 1-minute IEX (micro pullback and all
1-minute patterns untested, IEX gaps/noise not reproduced); survivorship (today's
listings only); current float, not point-in-time; the live scanner only sees
Alpaca's top movers from 09:30 ET while the replay sees every qualifying stock;
no news filter; modelled fills (no halts, partial fills or SSR). Read this as
"the profile trades rarely and lost money in a short sample", not as a verdict.
