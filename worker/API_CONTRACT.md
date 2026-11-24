# Observation API contract

`serve-observe` is the paper observation process. It does not submit, cancel, or replace broker orders. `worker/render-observe.yaml` keeps `PAPER_TRADING_ENABLED` false. Public `/health` is liveness. A 200 with `execution_disabled: true` does not mean the broker or the ledger is complete.

## `GET /health`

No bearer token. The body is JSON.

| Field | Meaning |
| --- | --- |
| `status` | `ok` when the journal is writable and startup succeeded. `degraded` when the process is up but the journal cannot be written, free space is below `JOURNAL_MIN_FREE_MB`, or startup failed. |
| `execution_disabled` | `true` for this observation process, including when `PAPER_TRADING_ENABLED` is true. |
| `execution_locked` | `true` for `serve-observe`. The flag does not arm order submission. |
| `paper_trading_enabled` | The configured flag. It does not start order submission in this process. |
| `paper_execution_running` | `false` for `serve-observe`. |
| `live_execution_supported` | `false`. |
| `orders_submitted` | `0`. |
| `journal_status` | `writable`, `degraded`, `read_only`, `unavailable`, or `unopened`. |
| `disk.free_bytes` | Measured free space on the journal volume, or null when it cannot be measured. |
| `disk.below_floor` | `true` when free space is below `JOURNAL_MIN_FREE_MB`. |
| `disk.last_write_error` | `database_or_disk_is_full` or `journal_write_failed` after a caught write error. Otherwise null. |
| `startup_error` | A short code such as `journal_volume_full_grow_the_disk_or_free_space`, `journal_unavailable`, or `configuration_invalid`. Otherwise null. |

`serve-observe` binds this route before it opens the journal and before it starts observation. A journal-open failure does not exit 1. The process stays up and this route reports `degraded`.

Other routes keep their bearer token. When the journal is not writable, mutating routes return 503 and do not change stored rows.
