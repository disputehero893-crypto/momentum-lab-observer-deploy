# AI advisor (Google Gemini): chart reads that trade alongside the rules

**Paper trading only.** The AI advisor is optional and **off by default**. When it
is on, it reads chart images with Google Gemini *at the same time* as the rule
strategy (`liquid_large_cap_iex_v2`) runs. The rules never wait for it. Its calls
are combined with the rule signals, tagged by source, logged, and reviewed later
in a pattern report. Nothing here can place a live trade, change `MOMENTUM_MODE`,
or change `PAPER_TRADING_ENABLED`.

Code: `momentum/ai_advisor.py` (Gemini client, background reads, combiner, log),
`momentum/ai_cotrade.py` (deterministic AI-only intents), `momentum/ai_chart.py`
(PNG charts, standard library only), `momentum/ai_report.py` (pattern report),
`momentum/ai_research.py` (daily note). Tests: `tests/test_ai_advisor.py` (fake
Gemini client; no network).

## 1. Turn it on

1. **Get a free key.** Go to <https://aistudio.google.com>, sign in with a Google
   account, open **Get API key** (or **API keys**), click **Create API key** and
   copy it. The free tier needs no billing account. Don't add billing if you want
   to stay free: with billing linked, usage is charged.
2. **Set these environment variables on the worker** (Render → service →
   Environment). Put the key in as a secret, never in git.

   | Variable | Value |
   | --- | --- |
   | `GEMINI_API_KEY` | the key from AI Studio |
   | `AI_ADVISOR_ENABLED` | `true` |
   | `AI_ADVISOR_MODE` | `advise` to start (log only), later `cotrade`, or `paper_execute` for Gemini-decided crypto paper trades (see AI_PAPER_TRADING.md) |

   Both `AI_ADVISOR_ENABLED=true` and a mode other than `off` are needed. With
   either one missing, the advisor does nothing at all.
3. **Recommended order:** run `advise` for a while (it costs nothing and changes no
   trades), check the report (section 5), and only then switch to `cotrade`.
4. **Re-review for `cotrade` only.** Its settings (mode, model, `AI_MIN_CONFIDENCE`,
   `AI_CONFLUENCE_MIN_CONFIDENCE`, `AI_MAX_TRADES_PER_DAY`,
   `AI_SIGNAL_MAX_AGE_SECONDS`) are part of the reviewed paper configuration hash.
   After you switch to `cotrade`, `python -m momentum readiness` shows a new
   `paper_configuration_hash` and paper entries stay blocked
   (`exact_paper_configuration_not_reviewed`) until `PAPER_REVIEWED_CONFIG_HASH`
   is updated to that value. This is on purpose: AI-only entries change what the
   bot trades, so they need the same explicit review as any other rule change.
   `off` and `advise` do not change the hash.

## 2. All settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `AI_ADVISOR_ENABLED` | `false` | Master switch. |
| `AI_ADVISOR_MODE` | `off` | `off`, `advise` (log and tag only) or `cotrade` (AI-only candidates allowed). |
| `GEMINI_API_KEY` | none | Secret. Sent only in the `x-goog-api-key` header to `generativelanguage.googleapis.com`. Never logged. |
| `GEMINI_MODEL` | `gemini-3.8-flash` | A free-tier multimodal Flash model (checked against Google's model and pricing pages, Nov 2026). `gemini-3.5-flash-lite` is a lighter free option. |
| `AI_MIN_CONFIDENCE` | `0.7` | Minimum confidence for an **AI-only** candidate (cotrade). |
| `AI_CONFLUENCE_MIN_CONFIDENCE` | `0.5` | Minimum confidence for an AI buy to count as agreeing with a rule candidate. |
| `AI_MAX_TRADES_PER_DAY` | `2` | AI-only paper entries per New York day. Rebuilt from the log after a restart. |
| `AI_ANALYSIS_INTERVAL_SECONDS` | `120` | Per symbol: at most one chart read per interval, and only when a new bar exists. |
| `AI_SIGNAL_MAX_AGE_SECONDS` | `180` | Older reads are ignored. |
| `AI_ADVISOR_MIN_INTERVAL_SECONDS` | `4` | Minimum spacing between any two Gemini calls. |
| `AI_ADVISOR_MAX_CALLS_PER_DAY` | `200` | Hard daily call cap. After it, the rules trade alone. |
| `AI_ADVISOR_TIMEOUT_SECONDS` | `6` | HTTP timeout per call (background thread). |
| `AI_CYCLE_WAIT_SECONDS` | `0` | Optional wait, capped at 1 s, for an in-flight read before entries are decided. Keep it at 0 so the rules never wait. |
| `AI_ADVISOR_LOG_DIR` | `<JOURNAL_PATH dir>/ai_advisor` | Feedback log location (`/var/data/ai_advisor` on Render). |
| `AI_ADVISOR_LOG_MAX_MB` | `20` | Hard size cap for the log and for saved charts. Past it, records are dropped (with a warning) instead of filling the disk. |
| `AI_ADVISOR_SAVE_CHARTS` | `false` | Also save each chart PNG under `charts/`. |

An invalid value turns the advisor **off** and logs an error. It never stops the
worker.

**Free-tier limits.** Google shows your actual per-model limits in AI Studio
(Rate limit page). The defaults (3 watchlist symbols, one read per symbol every
2 minutes during the entry window, 4 s spacing, 200/day cap) are meant to stay
inside the free tier. If you see `rate_limited_429` in the log, raise
`AI_ANALYSIS_INTERVAL_SECONDS` or lower `AI_ADVISOR_MAX_CALLS_PER_DAY`.

## 3. How it works

### Rules and AI side by side

Every worker cycle:

1. The worker queues a **background chart read** for each watchlist symbol that
   is due. It sends a 800×480 PNG of up to 60 recent one-minute bars (candles,
   volume, orange 9-bar SMA, blue 20-bar SMA) plus a few numbers, and asks Gemini
   for `{"symbol","call","pattern","confidence","reason"}`. The call is `buy`,
   `sell` or `wait`. The pattern is one of pullback, breakout, bull_flag,
   bear_flag, pennant, double_top, double_bottom, head_and_shoulders, range,
   trend_up, trend_down, reversal, gap, none or other. This returns immediately.
   Results land in a per-symbol cache when they arrive, usually by the next
   cycle.
2. The **rule strategy and risk manager run exactly as before**, without waiting.
3. The **combiner** compares the rule result with the latest fresh AI read for
   that symbol:
   - rules accept and AI says buy (confidence ≥ `AI_CONFLUENCE_MIN_CONFIDENCE`) → **`confluence`**
   - rules accept, AI disagrees or has no read → **`rules`**
   - rules reject, AI says buy with confidence ≥ `AI_MIN_CONFIDENCE` and the daily AI cap isn't reached → **`ai`** (cotrade only)
4. **advise mode:** rule entries go out exactly as without the advisor. They are
   only tagged `confluence` or `rules` and logged. Would-be AI-only candidates are
   logged as `advise_only_not_traded` and never traded.
5. **cotrade mode:** candidates are ordered confluence → rules → ai. Each is then
   **re-checked by the same `RiskManager`** against the current account, so the
   one-exposure-at-a-time rule, daily loss lock, kill switch, halts, spread and
   quote-age checks, per-trade risk budget and quantity caps all apply. Each one
   also goes through the same paper-only order validation and submission guard.

### What the AI can and cannot do

- **Can:** say buy/sell/wait for a watchlist symbol, with a pattern, confidence
  and reason.
- **Cannot:** pick quantity (always `RiskManager`), pick prices, pick a symbol
  outside the watchlist (its reply must echo the requested symbol, and the
  intent builder checks the watchlist and preset universe), go short (`sell`
  calls are only logged; the engine is long-only and exits stay with the rules'
  stop/target/time logic), or touch mode, `PAPER_TRADING_ENABLED`, the reviewed
  hash or anything else in the config.
- **AI-only intents are built by code, not by the AI**
  (`momentum/ai_cotrade.py`). Entry is the current ask. Stop is the lowest low of
  the last 5 completed bars minus one tick. Target is entry + max(strategy
  `target_r`, risk `min_reward_r`) × risk. The builder uses the rules' own gates:
  entry window 09:35–15:45 ET, confirmed trading day, paper mode, preset
  universe, feed, spread limit and price range. It refuses if the price moved
  more than 0.5% since the AI read.

### Failure handling

| Situation | Result |
| --- | --- |
| No key, daily cap, timeout, HTTP error, 429 | No AI signal; the rules trade alone. Logged as `status: error`. |
| Reply isn't exactly the schema (extra keys like `qty` or `mode`, wrong symbol, out-of-range confidence, NaN, duplicate keys, prose, injected instructions) | Read discarded (`status: invalid`); it can never produce a trade. |
| Any advisor bug | Caught. The cycle continues on the rules alone. A rule candidate is never dropped because of an advisor fault. |
| Env says `cotrade` but the reviewed config doesn't include it | Behaves as `advise`. |

Gemini is called with `responseMimeType: application/json` plus a strict JSON
schema, and the reply is still validated strictly on our side.

## 4. Daily research note

```
cd worker
python -m momentum.ai_research --from-alpaca --out-dir ./exports/research   # read-only market data
python -m momentum.ai_research --bars-file bars.json --out-dir ./exports/research
```

Writes `<out-dir>/<New York date>/research.md`, `research.json` and one PNG chart
per symbol. With `AI_ADVISOR_ENABLED=true` and a key, Gemini writes a short
plain-English note from the charts (one call per run). Otherwise, or on any
error, it writes a rules-only numeric note (last price, change, range, SMA9,
SMA20, trend). `--from-alpaca` uses the existing read-only data client and never
places orders. Nothing schedules this yet; run it by hand or add a cron later
(with Derek's OK, because that is a hosting change).

## 5. Reading the pattern report

```
cd worker
python -m momentum.ai_report            # reads $AI_ADVISOR_LOG_DIR or <JOURNAL_PATH dir>/ai_advisor
python -m momentum.ai_report --json
```

The feedback log (`advisor_feedback.jsonl`) has three record types:

- `signal`: every AI read (status, call, pattern, confidence, reason).
- `candidate`: every entry candidate with its `source` tag (`confluence`, `rules`
  or `ai`), the AI pattern and call, and whether it was submitted.
- `outcome`: written when the paper position's exit is placed. Includes exit
  reason, entry, stop, exit price, R multiple and win/loss.

The report joins candidates to outcomes by `setup_id` and prints tables **by
source**, **by AI pattern**, **by source + pattern** and **by AI call**:
candidates, trades, wins, win %, average R.

How to read it:
- **By source** is the main comparison. If `confluence` beats `rules` on win %
  and average R, the AI is adding value as a filter. If `ai` (AI-only) has a
  positive average R over a meaningful sample, AI-only entries are worth keeping.
  Otherwise lower `AI_MAX_TRADES_PER_DAY` or go back to `advise`.
- **By pattern** shows which chart patterns actually paid off.
- Average R is in units of planned risk: +2.0 means the trade made twice what it
  risked, −1.0 means a full stop-out.
- Outcomes use the exit order's limit price (the bid) before fill, so they are
  approximate. Small samples (fewer than ~30 trades per row) are mostly noise.
- This is the "learning" loop: a human reviews the numbers and adjusts settings.
  **No model is trained**, and nothing changes automatically.

## 6. Data privacy

- Sent to Google: the symbol, recent one-minute OHLCV numbers, the current
  bid/ask, and a chart image of public market data. **No** account numbers,
  balances, positions, order IDs, Alpaca keys or personal data are sent.
- **On the free tier, Google may use prompts and responses to improve its
  products, and humans may review them.** That's acceptable for public price
  charts. Don't add anything private to the prompts. A paid (billing-linked)
  project is not used for product improvement, but it costs money and needs
  Derek's explicit OK.
- The key lives only in the worker environment. It is never written to the
  journal, the feedback log or error messages.
- The feedback log stays on the worker disk with a hard size cap.

## 7. Turning it off

Set `AI_ADVISOR_MODE=off` or `AI_ADVISOR_ENABLED=false` (or delete the key) and
restart. Behavior is then identical to a worker without the advisor. If you were
in `cotrade`, set `PAPER_REVIEWED_CONFIG_HASH` back to the hash `readiness`
shows.
