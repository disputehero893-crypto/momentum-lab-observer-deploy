"""Offline journal tooling: WAL checkpoint, retention plan, and verified compaction into a NEW file.

The events/trades tables are append-only by trigger, so retention never deletes in place.
``compact`` writes a separate destination journal that omits aged observation telemetry,
archives every omitted row to NDJSON.gz with a SHA-256 manifest, and proves every audit event
(everything that is not telemetry) was copied byte-for-byte. The source file is opened
read-only and is never modified. Run it only against a stopped service's copy or snapshot.

    python -m momentum.journal_tools plan    --src J.sqlite3 --keep-days 7
    python -m momentum.journal_tools compact --src J.sqlite3 --dst J.compact.sqlite3 \
        --archive telemetry-archive.ndjson.gz --keep-days 7
"""
import argparse
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

# Per-cycle observation telemetry only. Everything else (orders, fills, kill switch, account
# binding, start/stop, errors recorded as their own events, autopilot commands, ...) is audit.
TELEMETRY_TYPES = ("paper_observation_cycle", "paper_cycle", "paper_risk_state")
KEEP_LATEST_PER_TYPE = ("paper_risk_state",)  # restored on restart


class CompactionError(RuntimeError):
    pass


def _ro(path):
    return sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=15)


def wal_checkpoint(db_path, mode="TRUNCATE"):
    """Fold the WAL back into the database file. Returns (busy, wal_pages, checkpointed_pages)."""
    if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
        raise ValueError("unsupported checkpoint mode")
    db = sqlite3.connect(str(db_path), timeout=15)
    try:
        return tuple(db.execute(f"PRAGMA wal_checkpoint({mode})").fetchone())
    finally:
        db.close()


def _cutoff(keep_days, now):
    return ((now or datetime.now(timezone.utc)) - timedelta(days=keep_days)).isoformat()


def _droppable_clause():
    marks = ",".join("?" * len(TELEMETRY_TYPES))
    keep = " AND ".join(
        f"seq != (SELECT MAX(seq) FROM events e2 WHERE e2.event_type='{t}')" for t in KEEP_LATEST_PER_TYPE)
    return f"event_type IN ({marks}) AND recorded_at < ?" + (f" AND {keep}" if keep else "")


def plan(src, *, keep_days=7, now=None):
    """Read-only: row counts and payload bytes by event type, and how many rows would be archived."""
    cutoff = _cutoff(keep_days, now)
    db = _ro(src)
    try:
        by_type = {t: {"rows": r, "payload_bytes": b or 0} for t, r, b in db.execute(
            "SELECT event_type, COUNT(*), SUM(LENGTH(payload)) FROM events GROUP BY 1")}
        drop = db.execute(f"SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)),0) FROM events WHERE {_droppable_clause()}",
                          (*TELEMETRY_TYPES, cutoff)).fetchone()
        return {"cutoff_recorded_at": cutoff, "keep_days": keep_days, "events_by_type": by_type,
                "audit_rows_always_kept": sum(v["rows"] for k, v in by_type.items() if k not in TELEMETRY_TYPES),
                "telemetry_rows_to_archive": drop[0], "telemetry_payload_bytes_to_archive": drop[1],
                "source_bytes": Path(src).stat().st_size}
    finally:
        db.close()


def _row_digest(row):
    return hashlib.sha256(json.dumps(list(row), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def compact(src, dst, archive, *, keep_days=7, now=None):
    src, dst, archive = Path(src), Path(dst), Path(archive)
    if dst.exists() or archive.exists():
        raise CompactionError("destination or archive already exists; refusing to overwrite")
    if src.resolve() == dst.resolve():
        raise CompactionError("destination must differ from source")
    cutoff = _cutoff(keep_days, now)
    s = _ro(src)
    d = None
    try:
        schema = s.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                           "AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name").fetchall()
        dst.parent.mkdir(parents=True, exist_ok=True)
        d = sqlite3.connect(str(dst), timeout=15)
        d.execute("PRAGMA journal_mode=WAL")
        for _kind, _name, sql in schema:
            d.execute(sql)
        tables = [n for k, n, _ in schema if k == "table"]
        drop_where = _droppable_clause()
        manifest = {"source": src.name, "cutoff_recorded_at": cutoff, "archived_rows": 0,
                    "copied": {}, "telemetry_types": list(TELEMETRY_TYPES)}
        digest = hashlib.sha256()
        with gzip.open(archive, "wt", encoding="utf-8") as out:
            for row in s.execute(f"SELECT * FROM events WHERE {drop_where} ORDER BY seq", (*TELEMETRY_TYPES, cutoff)):
                line = json.dumps(list(row), sort_keys=True, separators=(",", ":")) + "\n"
                out.write(line)
                digest.update(line.encode())
                manifest["archived_rows"] += 1
        manifest["archive_sha256_of_lines"] = digest.hexdigest()
        for table in tables:
            cols = [c[1] for c in s.execute(f"PRAGMA table_info({table})")]
            names = ",".join(cols)
            marks = ",".join("?" * len(cols))
            if table == "events":
                cur = s.execute(f"SELECT {names} FROM events WHERE NOT ({drop_where}) ORDER BY seq", (*TELEMETRY_TYPES, cutoff))
            else:
                cur = s.execute(f"SELECT {names} FROM {table} ORDER BY 1")
            n = 0
            for row in cur:
                d.execute(f"INSERT INTO {table}({names}) VALUES({marks})", tuple(row))
                n += 1
            manifest["copied"][table] = n
        # AUTOINCREMENT high-water: never reuse a seq that existed in the source.
        for name, seq in s.execute("SELECT name, seq FROM sqlite_sequence").fetchall():
            d.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?", (seq, name))
            if d.execute("SELECT changes()").fetchone()[0] == 0:
                d.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", (name, seq))
        d.commit()
        # Verification: every audit event identical; kept + archived == source; other tables equal.
        marks = ",".join("?" * len(TELEMETRY_TYPES))
        audit_sql = f"SELECT * FROM events WHERE event_type NOT IN ({marks}) ORDER BY seq"
        a, b = (hashlib.sha256(), hashlib.sha256())
        for row in s.execute(audit_sql, TELEMETRY_TYPES): a.update(_row_digest(row).encode())
        for row in d.execute(audit_sql, TELEMETRY_TYPES): b.update(_row_digest(row).encode())
        total_src = s.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        total_dst = d.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        if a.hexdigest() != b.hexdigest():
            raise CompactionError("audit events differ between source and destination")
        if total_dst + manifest["archived_rows"] != total_src:
            raise CompactionError("row accounting mismatch (kept + archived != source)")
        for table in tables:
            if table != "events" and (s.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                                      != d.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]):
                raise CompactionError(f"table {table} row count differs")
        for kind, name, _ in schema:
            if kind == "trigger" and not d.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone():
                raise CompactionError(f"trigger {name} missing from destination")
        manifest.update(source_events=total_src, destination_events=total_dst, audit_digest=a.hexdigest(),
                        destination_bytes=None)
        d.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        d.close(); d = None
        manifest["destination_bytes"] = dst.stat().st_size
        Path(str(archive) + ".manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        return manifest
    except BaseException:
        if d is not None:
            d.close()
        for p in (dst, Path(str(dst) + "-wal"), Path(str(dst) + "-shm"), archive):
            try:
                p.unlink()
            except OSError:
                pass
        raise
    finally:
        s.close()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("plan"); a.add_argument("--src", required=True); a.add_argument("--keep-days", type=int, default=7)
    c = sub.add_parser("compact"); c.add_argument("--src", required=True); c.add_argument("--dst", required=True)
    c.add_argument("--archive", required=True); c.add_argument("--keep-days", type=int, default=7)
    args = p.parse_args(argv)
    if args.cmd == "plan":
        print(json.dumps(plan(args.src, keep_days=args.keep_days), indent=2, sort_keys=True))
    else:
        print(json.dumps(compact(args.src, args.dst, args.archive, keep_days=args.keep_days), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
