# TactiRise autonomous trading implementation

Owner direction, October 19, 2026: customers set their parameters and turn on
an AI trading bot that analyzes markets and submits/manages orders automatically.
Research, market observations and strategy evaluation are internal supporting
capabilities, not the customer-facing product. The previous research-only scope
is superseded. This document describes an implementation increment, not a launch.

## Implemented in this change

- Separate `python -m momentum serve-autopilot --account-id <paper-account-uuid>`
  runtime, with a serial execution loop independent of the browser.
- Reuses the existing strategy, independent risk manager, order idempotency,
  reconciliation, partial-fill recovery and journal. It actually calls
  `PaperSession.cycle(execute=True)` after authorization instead of unconditionally
  passing `False` as the observer does.
- Starts paused on a new journal. Control commands are persisted before receipt,
  bound to one verified owner, broker account and exact configuration version.
- Pause stops new entries and cancels opening remainders while maintaining
  protective exits. Emergency stop remains latched; acknowledging a command is
  never represented as proof of liquidation.
- Restart reuses durable authorization and order IDs, but performs fresh
  account/order/position/accounting reconciliation before any submission.
- Fixed server-authenticated state/command endpoints. The worker control token
  stays server-side and must never be put into frontend environment variables.
- Foreign account, stale state, missing ledger, duplicate command, and loss-limit
  cases are covered by local fake-provider tests.

## Private API

`GET /api/autopilot/state` returns the actual current runner state.

`POST /api/autopilot/commands` accepts exactly:

```json
{
  "owner_id": "authenticated-owner-uuid",
  "action": "activate",
  "configuration_version": "exact-reviewed-worker-configuration-hash",
  "request_id": "unique-command-id",
  "consent": true
}
```

Actions are `activate`, `pause`, `emergency_stop`. The server bearer secret is
required. The trusted gateway must derive owner identity from a verified customer
session; a caller-supplied UUID alone is not authentication. A duplicate command
returns its original receipt and never overrides a newer pause. Pause/emergency
stop remain available from a stale settings screen; activation alone requires
the exact current version. These endpoints
are not the browser-facing Supabase contract; a gateway is still required.

## What is not complete

| Required capability | Evidence / remaining work |
| --- | --- |
| Hosted execution | This source is under review, not deployed. Render still runs `serve-observe`. |
| Complete execution accounting | The existing REST activity importer explicitly reports execution and fee coverage incomplete. Its output must not be promoted to complete merely because pagination ends. An independently reconciled opening baseline, order/fill coverage, fee policy, and current projection are required. |
| Source strategy inputs | Existing strategy needs actual point-in-time float/catalyst facts, eligible trades and sufficient same-clock volume history. AAPL/MSFT/SPY connection-test quotes do not meet the strategy's small-cap criteria. |
| Per-customer configuration and activation | The new `control-plane/` settings service is deployed, with validated versioned owner-scoped persistence and concurrency checks. Mapping those saved versions to reviewed worker configurations, execution gateway, worker routing, credential binding and consent still need end-to-end integration. The runner handles one owner/account per instance; it is not a multi-tenant fleet manager. |
| AI learning | Source research exists for an experimental momentum strategy. No deployed model currently discovers, learns, validates or promotes strategies. Add permitted sources with attribution, a constrained proposal schema, historical data, costs/slippage, chronological holdouts, forward paper results and immutable version promotion. Customer risk limits remain outside model control. |
| Proven performance | No real historical profitability evidence or forward paper acceptance has been established. Unit tests and synthetic fills cannot establish this. |
| Live money | The reusable adapter is currently paper-only. Live credentials, broker authorization, live execution adapter and user-driven launch have not been integrated or tested. |
| Crypto/forex execution | Crypto quotes exist; fractional quantities, allowed order types/time-in-force, fees and 24/7 management need their own tested execution path. Forex provider integration is incomplete. |
| Browser acceptance | Real signed-in desktop/mobile testing is still required before frontend publication, per the owner instruction. |

## Verification

25 targeted fake-provider/API tests cover the new loop and control paths, including
shutdown during a data request before an order can be sent. The full
existing worker suite is also run after the final patch; its result is recorded
in the pull request. No real broker orders are submitted by these tests. The
observer command, its GET-only transport, and deployed runtime remain unchanged.

## Relevant primary contracts

- Alpaca order creation: https://docs.alpaca.markets/us/reference/postorder
- Order idempotency lookup: https://docs.alpaca.markets/us/reference/getorderbyclientorderid
- Crypto-specific execution: https://docs.alpaca.markets/us/docs/crypto-trading
- Strategy source distinctions: `STRATEGY_NOTES.md` (attributed public education
  and explicitly experimental implementation choices).
- Validation design: https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/research-guide

Do not mark missing capabilities complete by removing their readiness checks.
Implement them and replace each blocker with observed acceptance evidence.
