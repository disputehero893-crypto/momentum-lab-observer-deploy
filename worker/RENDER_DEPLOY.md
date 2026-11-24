# Observation deploy notes

This file describes the paper observation worker. Applying it does not deploy anything, and it does not change `momentum-lab-observer-deploy` or a Render service.

The Blueprint in `worker/render-observe.yaml` is observation-only:

- `dockerCommand: serve-observe`
- `PAPER_TRADING_ENABLED` is `false`
- `JOURNAL_SAMPLE_SECONDS` is `60`
- `JOURNAL_MIN_FREE_MB` is `64`
- `/health` is the health check path

`serve-observe` listens on `/health` before it opens the journal. If that open hits a full disk, a file that is not a SQLite database, or another SQLite error, the process stays up. It does not exit 1. `/health` returns 200 with `status: degraded`, `execution_disabled: true`, and `execution_locked: true`. Existing journal rows are not deleted.

A full-disk write of `observation_started` is recorded as `disk.last_write_error: database_or_disk_is_full` and does not stop the process. High-volume samples are skipped while free space is below `JOURNAL_MIN_FREE_MB`. Cycle rows stored in SQLite are small status records. The WAL is checkpointed without removing events.

`Journal.__init__` still raises on a full volume. The one-shot `observe` command does not catch that, so an open failure there can still exit 1. The service command is `serve-observe`.

Do not resume a disk-full image by deleting `momentum.sqlite3`. Copy an existing snapshot aside first. `archive-journal` can export old rows after the file is readable. It does not prune them. Free space has to come from growing the volume or from an operator action outside this process.
