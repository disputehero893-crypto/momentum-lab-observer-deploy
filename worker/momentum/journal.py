"""SQLite append-only event and trade journals with conflict-detecting idempotency."""
from contextlib import contextmanager
from datetime import datetime, timezone
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

class Journal:
    def __init__(self, path, *, sample_seconds=0.0, min_free_bytes=0):
        self.path = str(path)
        # Observation telemetry (per-cycle snapshots) may be thinned and is dropped
        # first when the volume is nearly full; safety events always use append().
        self.sample_seconds = float(sample_seconds)
        self.min_free_bytes = int(min_free_bytes)
        self._sampled = {}
        self.telemetry_skipped = 0
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

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(row):
        if row is None:
            return None
        out = dict(row)
        out["payload"] = json.loads(out["payload"])
        return out

    def append(self, run_id, event_type, payload, *, key, timestamp=None):
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

    def _low_disk(self):
        if self.min_free_bytes <= 0:
            return False
        try:
            return shutil.disk_usage(Path(self.path).parent).free < self.min_free_bytes
        except OSError:
            return False

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
            if "full" not in str(exc).lower():
                raise
            self.telemetry_skipped += 1
            log.warning("journal telemetry skipped: %s", exc)
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
        return event["payload"] if event else {"enabled": False, "flatten_requested": False, "reason": None}

    def set_kill_switch(self, enabled, reason="operator", *, flatten=False, request_id=None):
        # A later ordinary halt cannot downgrade an already requested emergency.
        if enabled and self.kill_switch().get("flatten_requested"):
            flatten = True
        return self.append("control", "kill_switch_changed",
                           {"enabled": bool(enabled), "flatten_requested": bool(flatten and enabled), "reason": str(reason)[:200]},
                           key="kill:" + (request_id or uuid.uuid4().hex))

    def export_ndjson(self, run_id, destination):
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(canonical_json(e) + "\n" for e in self.events(run_id)), encoding="utf-8")
        return str(path)

