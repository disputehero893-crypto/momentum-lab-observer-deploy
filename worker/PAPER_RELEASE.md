# Paper-trading release (`serve-paper`)

This is the existing worker, not a second service and not a second journal. Applying this document does not deploy anything, and it does not change `momentum-lab-observer-deploy` or a live Render service.

`serve-observe` stays GET-only. Its cycle still calls `PaperSession` with `execute=False`, and its client still refuses submit, cancel, and replace. `worker/render-observe.yaml` keeps `PAPER_TRADING_ENABLED` false.

`serve-paper` is that same cycle with stock orders armed. It still runs the crypto paper hook (`crypto_spot_momentum_v1`, only when `CRYPTO_PAPER_EXECUTION_ENABLED=true`), the authenticated `resume_entries` path, and the OANDA practice watch. Crypto and stock share the journal kill switch, `paper_blockers()`, and the risk caps in `limits_for_risk`. Do not also start `python -m momentum.crypto_paper_loop`.

Stock submit, cancel, and replace run only when every gate below holds. Any miss leaves the cycle observation-only and puts the reason on `/health`. Every stock **entry** intent is screened with `check_paper_intent` before submit. Protective exits are not blocked by that entry screen. Live trading is not configurable. `https://api.alpaca.markets` stays refused.

## What the paper process does

The runtime uses `PaperClient`, locked to `https://paper-api.alpaca.markets`. Before a cycle can set `execute=True` it checks, in order:

1. `PAPER_TRADING_ENABLED=true`
2. `MOMENTUM_MODE=paper`
3. The broker origin is exactly `https://paper-api.alpaca.markets`
4. `GET /v2/account` confirms an active, unblocked paper account
5. The journal kill switch and halt are not latched
6. `PAPER_REVIEWED_CONFIG_HASH` matches the active strategy, risk, watchlist, and paper host

Risk limits, including position and notional caps, still run inside the session before any submit. `client_order_id` is the existing stable `mlab_` id, so a repeated setup recovers the open order instead of posting another one. Submit, cancel, and replace each write the attempt and the result to the journal. A missing fill quantity or average price stays unknown. It is not recorded as zero.

`/health` reports `paper_trading_enabled`, `paper_execution_active`, `execution_reason`, `live_execution_supported` false, `live_execution_locked` true, and `orders_submitted`.

## Start command

On the existing Render worker, change the Docker command from `serve-observe` to `serve-paper`. No extra flags. Keep the same disk (`/var/data`) and the same `JOURNAL_PATH`. Do not create another service.

Safe defaults, all off until the checklist below:

| Variable | Safe default |
| --- | --- |
| `MOMENTUM_MODE` | `paper` |
| `PAPER_TRADING_ENABLED` | `false` |
| `ALPACA_DATA_FEED` | `iex` (the free large-cap path; the entry screen rejects any other feed) |
| `CRYPTO_PAPER_EXECUTION_ENABLED` | `false` |
| `CRYPTO_WATCHLIST` | empty |
| `OANDA_ENVIRONMENT` | empty |
| `OANDA_FOREX_WATCHLIST` | empty |
| `OANDA_PRACTICE_ORDERS_ENABLED` | `false` |
| `OANDA_PRACTICE_API_TOKEN` | unset |
| `OANDA_PRACTICE_ACCOUNT_ID` | unset |
| `PAPER_REVIEWED_CONFIG_HASH` | unset until recomputed |

Recompute `PAPER_REVIEWED_CONFIG_HASH` after any env change. The worker's `review_hash()` is the value to set. A hash taken before the change does not match, and stock and crypto orders stay off.

## Activation checklist

Do this on the existing worker. Do not add a second service.

1. Change that worker's start command from `serve-observe` to `serve-paper`. Leave automatic deploys off. `PAPER_TRADING_ENABLED` stays `false`.
2. Set `MOMENTUM_MODE=paper`. Do not set a broker base URL. `https://api.alpaca.markets` and every other trading origin are refused in code.
3. Provision paper credentials in the host's secret settings. Do not put them in the Blueprint, the image, or logs.
4. Start once with the flag still false and confirm `/health` is observation-only (`paper_execution_active` false) and names the reason. Crypto, resume, and the forex watch still run in this process.
5. Confirm the account check sees an ACTIVE paper account that is not trading-blocked, account-blocked, or suspended.
6. Confirm the kill switch is clear. `/api/halt` latches it and the next cycle stops submitting stock and crypto orders.
7. Read the configuration hash from readiness and set `PAPER_REVIEWED_CONFIG_HASH` to that exact value. Changing strategy, risk, watchlist, feed, or the crypto/forex flags requires a new hash.
8. Set `PAPER_TRADING_ENABLED=true` only after the steps above. `/health` then shows `paper_execution_active` true and `execution_reason` `paper_gates_open` when the account, kill switch, and hash still agree.
9. Confirm `live_execution_supported` is false and `live_execution_locked` is true. There is no live flag to turn on.

An incomplete session ledger still blocks submits. That existing reconciliation rule is unchanged.

## Commands

From `worker/`:

```sh
python -m momentum serve-paper
python -m momentum serve-paper --once
```

`serve-observe` and `observe` stay GET-only. `serve-paper` is the command that replaces `serve-observe` on the one worker when stock paper orders should be possible.
