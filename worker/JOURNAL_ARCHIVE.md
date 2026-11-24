# Journal archive

The observation journal is append-only. `archive-journal` copies old rows out. It does not delete them, and nothing in the service prunes them automatically.

From `worker/`:

```bash
python3 -m momentum archive-journal --older-than-days 30 --output /path/outside/the/disk/journal-archive.jsonl.gz
```

The command writes gzip NDJSON, reads it back, and checks the row count and SHA-256. The receipt says `deleted: false`. The destination must not be the journal file, its `-wal`, or its `-shm`. A verified copy is not a license to remove the source. This repository does not implement a prune step.

Sampled observation rows are already smaller than the old full quote payload, and the WAL is checkpointed without deleting events. Those controls slow growth. They do not free a disk that is already full. Exports and checkpoints leave every existing event row in place.
