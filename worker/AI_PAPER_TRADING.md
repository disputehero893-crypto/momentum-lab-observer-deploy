# Gemini paper trading (`AI_ADVISOR_MODE=paper_execute`)

PAPER ONLY. Gemini makes the crypto entry and exit decisions on the Alpaca
**paper** account. It never builds an order, never picks prices, and it can't
get around any existing safety check. Live hosts are still refused.

Module: `worker/momentum/ai_paper_trader.py`. Tests: `worker/tests/test_ai_paper_trading.py`.

## Modes

| `AI_ADVISOR_MODE` | What Gemini does |
|---|---|
| `off` (code default) | nothing |
| `advise` (current worker setting) | reads stock charts and logs what it would do, without trading |
| `cotrade` | existing stock AI-only entries (unchanged) |
| `paper_execute` (new) | **decides crypto paper trades**. Stock chart reads behave like `advise` until #57 lands. |

`paper_execute` only does anything when all of these are true:
`AI_ADVISOR_ENABLED=true`, `CRYPTO_PAPER_EXECUTION_ENABLED=true`, and the
`PAPER_REVIEWED_CONFIG_HASH` covers the AI settings. The AI settings are added
to the reviewed hash only in this mode, so today's `advise` hash is unchanged.

## Decision → order path (no bypass)

1. Every cycle, the deterministic **1% protective stop** runs first on each held
   pair. It isn't gated by the AI.
2. Gemini's latest decision for a pair is used only when all of these hold:
   - It parsed strictly (exact keys, `enter|exit|hold`, both numbers 0–1, same pair, reason ≤ 240 chars).
   - It is ≤ `decision_max_age_seconds` old.
   - The mid price hasn't moved more than `max_drift_bps` since the decision.
   - Its confidence meets the threshold: entry ≥ `min_confidence`, exit ≥ `exit_min_confidence`.

   Malformed output, the wrong pair, low confidence, a timeout, a 429 or a
   missing key all mean **no trade**. A 429 also stops calls for 5 minutes.
3. An entry also needs these to pass: the AI daily entry cap, no loss-streak
   pause, and the rule's own entry screen (stale quote, spread, $75 daily loss,
   3 positions). Quantity is the rule's maximum size (per-trade risk at a 1%
   stop, notional, exposure room) × the AI `size_fraction` × the learning
   multiplier. Both factors are ≤ 1, so the AI can only trade smaller.
4. Every order goes through the unchanged `CryptoPaperLoop._submit` →
   `CryptoPaperClient.submit_order`, with these checks:
   - `paper-api.alpaca.markets` only, and `api.alpaca.markets` refused.
   - The `PAPER_TRADING_ENABLED` lock and `paper_blockers()`, including the config hash.
   - The journal kill switch, checked before and after journaling.
   - `check_paper_intent` (#8) and the RiskConfig caps.
5. At most one order per cycle, long only.

Stocks: `stock_entry_gate` requires an open broker clock, a weekday and the
09:35–15:45 ET window. `stock_ai_intent` then returns
`stock_ai_execution_requires_serve_paper_57`. It doesn't place stock orders
until #57 adds an equity order path. The AI module doesn't touch the #57
files (`alpaca.py`, `paper_runtime.py`, serve-paper, Render config).

## Learning loop (bounded, inspectable, free tier)

The log is `<AI_ADVISOR_LOG_DIR or journal dir/ai_advisor>/ai_paper_decisions.jsonl`
(20 MB cap). On the worker that is `/var/data/ai_advisor/ai_paper_decisions.jsonl`.
It holds three record types:

- `decision`: every Gemini reply or failure, with the features it saw:
  - bid, ask and mid
  - spread in bps
  - quote age
  - the last 8 mids and the change in bps across them
  - whether the pair is held, and the entry price and unrealized bps if it is
- `entry`: a decision that became a submitted paper order, with its qty, price and client order id.
- `outcome`: the realized result when that position closed (stop or AI exit):
  - P&L in USD and the R multiple (1R is the 1% stop)
  - win or loss
  - hold time and exit reason

  The price basis is the quote at exit, before the fill.

Each prompt includes `your_recent_performance`, built from the last
`learning_window` closed AI trades:
- trade count, wins and hit rate
- total P&L and average R
- the current loss streak
- the 2 best and 2 worst trades, with their features and reasons

It also includes the number of AI entries left today.

Automatic risk-down. It never increases size:
- A loss streak of `loss_streak_reduce` (2) or more halves the size.
- A hit rate below `min_hit_rate` (40%) over at least 10 trades also halves the size.
- A loss streak of `loss_streak_pause` (3) or more pauses AI entries for
  `pause_hours` (12 h) after the last loss. Exits and stops still run.

The log is reloaded on restart. There's no fine-tuning and no extra paid API.

## Settings

All of these except the key are included in the reviewed hash when armed.

| Env var | Default | Range |
|---|---|---|
| `AI_ADVISOR_MODE` | `off` (worker: `advise`) | `paper_execute` arms it |
| `GEMINI_API_KEY` | none | Read only from the worker env. Never logged, hashed or returned. |
| `GEMINI_MODEL` | `gemini-3.8-flash` | |
| `AI_EXECUTE_MIN_CONFIDENCE` | 0.7 | 0.5–1 |
| `AI_EXECUTE_EXIT_MIN_CONFIDENCE` | 0.5 | 0–1 |
| `AI_EXECUTE_MAX_ENTRIES_PER_DAY` | 4 | 0–20 (New York day) |
| `AI_EXECUTE_DECISION_INTERVAL_SECONDS` | 600 | 60–86400, per pair |
| `AI_EXECUTE_DECISION_MAX_AGE_SECONDS` | 120 | 5–900 |
| `AI_EXECUTE_MAX_DRIFT_BPS` | 30 | 1–500 |
| `AI_EXECUTE_LOSS_STREAK_REDUCE` | 2 | |
| `AI_EXECUTE_LOSS_STREAK_PAUSE` | 3 | |
| `AI_EXECUTE_PAUSE_HOURS` | 12 | 0.5–168 |
| `AI_EXECUTE_LEARNING_WINDOW` | 20 | 5–200 |
| `AI_EXECUTE_MIN_HIT_RATE` | 0.4 | |
| `AI_EXECUTE_MAX_CALLS_PER_DAY` | 150 | 0–1500. Kept separate from the stock advisor's 200/day. |

An invalid value turns `paper_execute` off (logged); it doesn't crash the worker.
With 3 pairs at a 600 s interval, the loop asks at most 432 times a day, and
the 150 cap stops it before that. When no decision is available, nothing trades.

## Arming (needs Derek's typed yes; not done in this PR)

1. Merge and deploy this PR. With the env unchanged, nothing changes: the mode
   is still `advise`, the hash is unchanged and the rule loop still runs.
2. On Render `srv-dbh0uevlk1mc739lbo1g`, change one env var:
   `AI_ADVISOR_MODE=advise` → `AI_ADVISOR_MODE=paper_execute`.
   Optional `AI_EXECUTE_*` overrides go in the same step.
3. Recompute the hash in the worker's own environment. This prints only a hash:
   ```
   cd /app
   python -c "from momentum.config import Config; print(Config.from_env().review_hash())"
   ```
   Set `PAPER_REVIEWED_CONFIG_HASH` to that value. For reference, this is what
   the inferred worker env gives offline (`WATCHLIST=AAPL,MSFT,SPY`, iex,
   default risk, crypto on with `BTC/USD,ETH/USD,SOL/USD`, OANDA off, no
   `AI_EXECUTE_*` overrides). That env reproduces today's live hash
   `2e443731…daaba5` on both main and this branch:
   - after arming: `95c1b69a56951716af165f2f3481285c270bc79283a296e2a51729494f6588b6`

   Always use the value printed in the worker's own environment. Adding any
   other env var (OANDA, `AI_EXECUTE_*`, accounting) changes it. Then do one restart or deploy.
   Until the hash matches, every paper order (equity and crypto) stays blocked
   by `exact_paper_configuration_not_reviewed`.
4. To roll back, set `AI_ADVISOR_MODE=advise`, restore the previous hash and
   restart. The rule loop then decides again.
