# Worker API contract

`serve-observe` is the paper observation process. It does not submit, cancel, or replace broker orders. `worker/render-observe.yaml` keeps `PAPER_TRADING_ENABLED` false. Public `/health` is liveness. A 200 with `execution_disabled: true` does not mean the broker or the ledger is complete.

`serve-paper` is that same process with stock paper orders armed. It keeps the crypto hook, `resume_entries`, and the OANDA practice watch on one journal. It is not a second service. `worker/render-paper.yaml` records the command and still defaults `PAPER_TRADING_ENABLED` to false. See [PAPER_RELEASE.md](PAPER_RELEASE.md) for the start-command change. Live trading is locked.

## `GET /health`

No bearer token. The body is JSON.

| Field | Meaning |
| --- | --- |
| `status` | `ok` when the journal is writable and startup succeeded. `degraded` when the process is up but the journal cannot be written, free space is below `JOURNAL_MIN_FREE_MB`, or startup failed. |
| `execution_disabled` | `true` for `serve-observe`, including when `PAPER_TRADING_ENABLED` is true. On `serve-paper`, `false` only while `paper_execution_active` is true. |
| `execution_locked` | `true` for `serve-observe`. On `serve-paper` it stays `true` because live execution is locked. |
| `paper_trading_enabled` | The configured flag. On `serve-observe` it does not start order submission. |
| `paper_execution_active` | `false` for `serve-observe`. On `serve-paper`, `true` only when every paper gate holds. |
| `execution_reason` | Why paper execution is or is not active. `serve-observe` reports `observation_runtime_cannot_execute_orders` (or `configuration_invalid` when startup failed). |
| `paper_execution_running` | `false` for `serve-observe`. On `serve-paper` it matches `paper_execution_active`. |
| `live_execution_supported` | `false`. |
| `live_execution_locked` | `true`. No configuration enables live trading. |
| `orders_submitted` | `0` for `serve-observe`. On `serve-paper`, the count of journaled acknowledged paper submits. |
| `journal_status` | `writable`, `degraded`, `read_only`, `unavailable`, or `unopened`. |
| `disk.free_bytes` | Measured free space on the journal volume, or null when it cannot be measured. |
| `disk.below_floor` | `true` when free space is below `JOURNAL_MIN_FREE_MB`. |
| `disk.last_write_error` | `database_or_disk_is_full` or `journal_write_failed` after a caught write error. Otherwise null. |
| `startup_error` | A short code such as `journal_volume_full_grow_the_disk_or_free_space`, `journal_unavailable`, or `configuration_invalid`. Otherwise null. |

`serve-observe` and `serve-paper` bind this route before they open the journal. A journal-open failure does not exit 1. The process stays up and this route reports `degraded`. A refused live broker URL or any other invalid configuration also stays up, with `paper_execution_active` false.

Other routes keep their bearer token. When the journal is not writable, mutating routes return 503 and do not change stored rows.

## Paper gates

`serve-paper` sets `paper_execution_active` only when all of these hold. The first miss is `execution_reason`:

| Check | Reason when it fails |
| --- | --- |
| `PAPER_TRADING_ENABLED=true` | `paper_trading_not_enabled` |
| `MOMENTUM_MODE=paper` | `mode_not_paper` |
| Broker origin is `https://paper-api.alpaca.markets` | `live_broker_url_refused` or `broker_url_not_paper_endpoint` |
| Account read confirms an active paper account | `paper_account_check_failed` |
| Kill switch and halt are clear | `kill_switch_latched` |
| `PAPER_REVIEWED_CONFIG_HASH` matches the running config | `exact_paper_configuration_not_reviewed` |

`https://api.alpaca.markets` is refused. Risk caps still reject an order before submit even when the gates are open. Unknown fill amounts stay null.
