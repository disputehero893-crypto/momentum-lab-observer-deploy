"""SQLite append-only event and trade journals with conflict-detecting idempotency."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import errno
import gzip
import hashlib
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

log = logging.getLogger(__name__)

class IdempotencyConflict(ValueError):
    pass

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _refuse_unreadable_journal(path):
    """Do not open a non-database file for write. Leave its bytes in place."""
    source = Path(path)
    if not source.is_file():
        return
    try:
        with source.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        return
    if header.startswith(b"SQLite format 3\x00"):
        return
    raise sqlite3.DatabaseError("file is not a database")


def _verify_gzip_archive(path, expected_sha256, expected_rows):
    digest = hashlib.sha256()
    rows = 0
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.endswith("\n"):
                line += "\n"
            digest.update(line.encode("utf-8"))
            json.loads(line)
            rows += 1
    verified = rows == expected_rows and digest.hexdigest() == expected_sha256
    if not verified:
        raise RuntimeError("archive_verification_failed")
    return {"rows": rows, "sha256": digest.hexdigest(), "verified": True}

class Journal:
    def __init__(self, path, *, sample_seconds=0.0, min_free_bytes=0):
        self.path = str(path)
        # Observation telemetry (per-cycle snapshots) may be thinned and is dropped
        # first when the volume is nearly full; safety events always use append().
        self.sample_seconds = float(sample_seconds)
        self.min_free_bytes = int(min_free_bytes)
        self._sampled = {}
        self.telemetry_skipped = 0
        self.last_write_error = None
        self.quarantined_events = 0
        self._quarantine_seen = set()
        self._readonly = False
        try:
            _refuse_unreadable_journal(self.path)
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
                CREATE TABLE IF NOT EXISTS trades (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS trades_run ON trades(run_id, seq);
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS trades_no_update BEFORE UPDATE ON trades
                BEGIN SELECT RAISE(ABORT, 'trades is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS trades_no_delete BEFORE DELETE ON trades
                BEGIN SELECT RAISE(ABORT, 'trades is append-only'); END;
                """)
        except (OSError, sqlite3.OperationalError) as exc:
            if not self._is_full_volume_error(exc):
                raise
            message = "journal_volume_full_grow_the_disk_or_free_space"
            log.error("journal initialization failed: %s", message)
            raise sqlite3.OperationalError(message) from None

    @classmethod
    def open_readonly(cls, path):
        """Open an existing journal without creating, migrating, or writing it.

        Returns None when the file is absent or cannot be read. The original
        file is left untouched either way.
        """
        source = Path(path)
        if not source.is_file():
            return None
        try:
            _refuse_unreadable_journal(source)
        except sqlite3.DatabaseError:
            return None
        journal = cls.__new__(cls)
        journal.path = str(source)
        journal.sample_seconds = 0.0
        journal.min_free_bytes = 0
        journal._sampled = {}
        journal.telemetry_skipped = 0
        journal.last_write_error = None
        journal.quarantined_events = 0
        journal._quarantine_seen = set()
        journal._readonly = True
        try:
            with journal._connect() as db:
                db.execute("SELECT seq FROM events ORDER BY seq DESC LIMIT 1")
        except (sqlite3.Error, OSError):
            return None
        return journal

    @contextmanager
    def _connect(self):
        if self._readonly:
            uri = "file:" + Path(self.path).resolve().as_posix() + "?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=15)
        else:
            db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
            if not self._readonly:
                db.commit()
        except BaseException:
            if not self._readonly:
                db.rollback()
            raise
        finally:
            db.close()

    def _event(self, row):
        if row is None:
            return None
        out = dict(row)
        raw = out["payload"]
        try:
            out["payload"] = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError, UnicodeError):
            # Keep the stored bytes. Readers see a quarantine marker and must
            # not treat the row as an empty or zero-valued event.
            out["payload"] = {"_quarantined": True, "reason": "payload_not_json"}
            self._record_quarantine(out.get("seq"), out.get("event_type"))
        return out

    def _record_quarantine(self, seq, event_type):
        """Append a seq/reason index beside the journal. Never copy the payload."""
        if seq in self._quarantine_seen:
            return
        self._quarantine_seen.add(seq)
        self.quarantined_events += 1
        if self._readonly or self._low_disk():
            return
        try:
            path = Path(str(self.path) + ".quarantine.ndjson")
            line = canonical_json({"seq": seq, "event_type": event_type, "reason": "payload_not_json"})
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            return

    def append(self, run_id, event_type, payload, *, key, timestamp=None):
        if self._readonly:
            raise sqlite3.OperationalError("journal_read_only")
        encoded = canonical_json(payload)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM events WHERE idempotency_key=?", (key,)).fetchone()
            if old:
                if old["run_id"] != run_id or old["event_type"] != event_type or old["payload"] != encoded:
                    raise IdempotencyConflict(f"Conflicting event for idempotency key {key}")
                return self._event(old)
            stamp = timestamp or utc_now()
            db.execute(
                "INSERT INTO events(event_id,idempotency_key,run_id,event_type,timestamp,recorded_at,payload) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid5(uuid.NAMESPACE_URL, "momentum-lab:" + key)), key, run_id, event_type, stamp, utc_now(), encoded),
            )
            return self._event(db.execute("SELECT * FROM events WHERE idempotency_key=?", (key,)).fetchone())

    def _is_full_volume_error(self, exc):
        """Classify only storage exhaustion during startup; preserve other failures."""
        if isinstance(exc, OSError):
            return exc.errno == errno.ENOSPC
        code = getattr(exc, "sqlite_errorcode", None)
        if (type(code) is int
                and code & 0xff == getattr(sqlite3, "SQLITE_FULL", 13)):
            return True
        message = str(exc).lower()
        if any(marker in message for marker in (
                "database or disk is full",
                "database is full",
                "disk is full",
                "disk full",
                "no space left on device",
        )):
            return True
        # SQLite can report ENOSPC while opening or initializing WAL as "disk I/O error".
        # Map that ambiguous message only when a real free-space measurement is available.
        if "i/o" in message:
            free_bytes = self._known_free_bytes()
            return (free_bytes is not None
                    and free_bytes < max(self.min_free_bytes, 1 << 20))
        return False

    def _known_free_bytes(self):
        try:
            return shutil.disk_usage(Path(self.path).parent).free
        except OSError:
            return None

    def _free_bytes(self):
        # Telemetry policy remains conservative if disk usage cannot be measured.
        free_bytes = self._known_free_bytes()
        return 0 if free_bytes is None else free_bytes

    def _low_disk(self):
        if self.min_free_bytes <= 0:
            return False
        free_bytes = self._known_free_bytes()
        if free_bytes is None:
            return False
        return free_bytes < self.min_free_bytes

    def disk_report(self):
        free_bytes = self._known_free_bytes()
        return {
            "free_bytes": free_bytes,
            "free_mb": None if free_bytes is None else round(free_bytes / (1024 * 1024), 3),
            "min_free_bytes": self.min_free_bytes,
            "min_free_mb": round(self.min_free_bytes / (1024 * 1024), 3),
            "below_floor": self._low_disk(),
            "last_write_error": self.last_write_error,
        }

    def note_write_error(self, exc):
        """Record a write failure without deleting rows or echoing the raw message."""
        self.last_write_error = ("database_or_disk_is_full" if self._is_full_volume_error(exc)
                                 else "journal_write_failed")
        self.telemetry_skipped += 1
        log.warning("journal write skipped: %s", self.last_write_error)

    def checkpoint_wal(self):
        """Copy WAL frames back into the database. This does not delete event rows."""
        if self._readonly:
            return {"checkpointed": False, "reason": "journal_read_only"}
        db = sqlite3.connect(self.path, timeout=15)
        db.isolation_level = None
        try:
            row = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.Error as exc:
            if isinstance(exc, sqlite3.OperationalError):
                self.note_write_error(exc)
            return {"checkpointed": False, "reason": self.last_write_error or "journal_write_failed"}
        finally:
            db.close()
        return {"checkpointed": True, "result": None if row is None else [row[0], row[1], row[2]]}

    def append_sampled(self, run_id, event_type, payload, *, key, timestamp=None, signature=None):
        """Best-effort telemetry append.

        Writes at most once per ``sample_seconds`` per event type unless ``signature``
        (or the payload) changed, and is skipped, never fatal, when free space is below
        ``min_free_bytes`` or SQLite reports the volume full. Returns the event or None.
        """
        sig = signature if signature is not None else canonical_json(payload)
        now = time.monotonic()
        last = self._sampled.get(event_type)
        if self.sample_seconds > 0 and last and last[0] == sig and now - last[1] < self.sample_seconds:
            return None
        if self._low_disk():
            self.telemetry_skipped += 1
            if self.telemetry_skipped == 1 or self.telemetry_skipped % 1000 == 0:
                log.warning("journal telemetry skipped: free space below %d bytes (%d skipped)",
                            self.min_free_bytes, self.telemetry_skipped)
            return None
        try:
            event = self.append(run_id, event_type, payload, key=key, timestamp=timestamp)
        except sqlite3.OperationalError as exc:
            # Any sampled write failure is non-fatal. Rows already stored stay.
            self.note_write_error(exc)
            return None
        self._sampled[event_type] = (sig, now)
        return event

    def event_by_key(self, key):
        with self._connect() as db:
            return self._event(db.execute("SELECT * FROM events WHERE idempotency_key=?", (key,)).fetchone())

    def events(self, run_id=None, *, after=0, limit=None, event_type=None):
        query = "SELECT * FROM events WHERE seq>?"
        values = [after]
        if run_id is not None:
            query += " AND run_id=?"
            values.append(run_id)
        if event_type is not None:
            query += " AND event_type=?"
            values.append(event_type)
        query += " ORDER BY seq"
        if limit is not None:
            query += " LIMIT ?"
            values.append(limit)
        with self._connect() as db:
            return [self._event(r) for r in db.execute(query, values)]

    def latest(self, event_type, run_id=None):
        query = "SELECT * FROM events WHERE event_type=?"
        values = [event_type]
        if run_id is not None:
            query += " AND run_id=?"
            values.append(run_id)
        query += " ORDER BY seq DESC LIMIT 1"
        with self._connect() as db:
            return self._event(db.execute(query, values).fetchone())

    def append_trade(self, run_id, trade_id, payload):
        encoded = canonical_json(payload)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
            if old:
                if old["run_id"] != run_id or old["payload"] != encoded:
                    raise IdempotencyConflict(f"Conflicting trade {trade_id}")
                return {**dict(old), "payload": json.loads(old["payload"])}
            db.execute("INSERT INTO trades(trade_id,run_id,recorded_at,payload) VALUES(?,?,?,?)", (trade_id, run_id, utc_now(), encoded))
            row = db.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
            return {**dict(row), "payload": json.loads(row["payload"])}

    def trades(self, run_id=None):
        with self._connect() as db:
            rows = db.execute("SELECT * FROM trades" + (" WHERE run_id=?" if run_id else "") + " ORDER BY seq",
                              (run_id,) if run_id else ())
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def kill_switch(self):
        event = self.latest("kill_switch_changed", "control")
        if not event:
            return {"enabled": False, "flatten_requested": False, "reason": None}
        if isinstance(event["payload"], dict) and event["payload"].get("_quarantined") is True:
            # An unreadable latch is not evidence that the halt was cleared.
            return {"enabled": True, "flatten_requested": True,
                    "reason": "journal_payload_unreadable_fail_closed"}
        return event["payload"]

    def set_kill_switch(self, enabled, reason="operator", *, flatten=False, request_id=None):
        # A later ordinary halt cannot downgrade an already requested emergency.
        if enabled and self.kill_switch().get("flatten_requested"):
            flatten = True
        return self.append("control", "kill_switch_changed",
                           {"enabled": bool(enabled), "flatten_requested": bool(flatten and enabled), "reason": str(reason)[:200]},
                           key="kill:" + (request_id or uuid.uuid4().hex))

    def archive_older_than(self, destination, *, older_than_days, now=None):
        """Copy old events to a gzip NDJSON file and verify the copy.

        Nothing in the source journal is deleted, updated, or vacuumed.
        """
        if type(older_than_days) is not int or older_than_days < 1 or older_than_days > 3650:
            raise ValueError("older_than_days must be an integer from 1 to 3650")
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise ValueError("archive clock must include a time zone")
        if destination is None:
            raise ValueError("archive destination is required")
        path = None if destination == "-" else Path(destination)
        if path is not None:
            source = Path(self.path).resolve()
            resolved = path.resolve()
            protected = {source, Path(str(source) + "-wal"), Path(str(source) + "-shm")}
            if resolved in protected:
                raise ValueError("archive destination must not replace the journal")
        cutoff = moment - timedelta(days=older_than_days)
        before = self._row_count()
        digest = hashlib.sha256()
        exported = 0

        def consume(handle):
            nonlocal exported
            with self._connect() as db:
                rows = db.execute(
                    "SELECT seq,event_id,idempotency_key,run_id,event_type,timestamp,recorded_at,payload "
                    "FROM events ORDER BY seq")
                for row in rows:
                    try:
                        stamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if stamp.tzinfo is None or stamp >= cutoff:
                        continue
                    record = {key: row[key] for key in row.keys()}
                    line = canonical_json(record)
                    handle.write(line + "\n")
                    digest.update((line + "\n").encode("utf-8"))
                    exported += 1

        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                consume(handle)
            verified = _verify_gzip_archive(path, digest.hexdigest(), exported)
        else:
            import sys
            with gzip.GzipFile(fileobj=sys.stdout.buffer, mode="wb", mtime=0) as raw:
                class Text:
                    def write(self, value):
                        raw.write(value.encode("utf-8"))
                consume(Text())
            verified = {"rows": exported, "sha256": digest.hexdigest(), "verified": False,
                        "reason": "stdout_not_reread"}
        after = self._row_count()
        if after != before:
            raise RuntimeError("archive_changed_source_row_count")
        return {"destination": None if path is None else str(path), "rows": exported,
                "sha256": digest.hexdigest(), "cutoff": cutoff.isoformat(),
                "source_rows": after, "deleted": False, "verified": verified["verified"]}

    def _row_count(self):
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def export_ndjson(self, run_id, destination):
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(canonical_json(e) + "\n" for e in self.events(run_id)), encoding="utf-8")
        return str(path)

