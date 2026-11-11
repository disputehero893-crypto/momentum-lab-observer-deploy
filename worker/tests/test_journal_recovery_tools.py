"""Recovery tooling: disk status, WAL checkpoint, retention plan and verified compaction."""
from collections import namedtuple
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from momentum import journal_tools
from momentum.config import Config
from momentum.diskstatus import disk_status
from momentum.journal import Journal
from momentum.server import Application

Usage = namedtuple("Usage", "total used free")
NOW = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)


def stamp(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


class RecoveryToolTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.src = self.root / "journal.sqlite3"
        journal = Journal(self.src)
        journal.append("paper-observation", "observation_account_bound", {"environment": "paper"},
                       key="observation:account-binding", timestamp=stamp(10))
        journal.append("paper-observation", "observation_started", {"instance_id": "a"}, key="start:a", timestamp=stamp(10))
        journal.set_kill_switch(True, "operator", request_id="old-halt")
        for day in (9, 8, 3, 0.5):
            for i in range(3):
                journal.append("paper-observe-x", "paper_observation_cycle", {"d": day, "i": i}, key=f"c:{day}:{i}", timestamp=stamp(day))
                journal.append("paper-state", "paper_risk_state", {"state": {"d": day, "i": i}}, key=f"r:{day}:{i}", timestamp=stamp(day))
                journal.append("r", "paper_cycle", {"d": day, "i": i}, key=f"p:{day}:{i}", timestamp=stamp(day))
        journal.append_trade("r", "t1", {"qty": 1})
        # recorded_at is wall clock; age the rows to match their timestamps for the retention cutoff.
        db = sqlite3.connect(self.src)
        db.execute("DROP TRIGGER events_no_update")
        db.execute("UPDATE events SET recorded_at=timestamp")
        db.execute("CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events is append-only'); END")
        db.commit(); db.close()

    def test_disk_status_numbers_only_and_low_flag(self):
        with mock.patch("momentum.diskstatus.shutil.disk_usage", return_value=Usage(1000, 990, 10)):
            status = disk_status(self.src, min_free_bytes=64)
        self.assertEqual((status["free_bytes"], status["total_bytes"], status["used_percent"], status["low"]), (10, 1000, 99.0, True))
        self.assertGreater(status["journal_bytes"], 0)
        with mock.patch("momentum.diskstatus.shutil.disk_usage", side_effect=OSError):
            self.assertFalse(disk_status(self.src)["available"])

    def test_readiness_and_health_report_disk(self):
        cfg = Config(db_path=str(self.src), export_dir=str(self.root / "exports"))
        app = Application(cfg)
        self.assertTrue(app.readiness()["journal_disk"]["available"])
        self.assertEqual(set(app.journal_disk(coarse=True)), {"available", "used_percent", "low"})

    def test_plan_is_read_only_and_counts_archivable_rows(self):
        before = self.src.read_bytes()
        result = journal_tools.plan(self.src, keep_days=2, now=NOW)
        self.assertEqual(self.src.read_bytes(), before)
        # 9d, 8d, 3d telemetry (27 rows) older than 2d.
        self.assertEqual(result["telemetry_rows_to_archive"], 27)
        self.assertEqual(result["audit_rows_always_kept"], 3)

    def test_compact_preserves_all_audit_events_and_triggers(self):
        dst, archive = self.root / "out.sqlite3", self.root / "arch.ndjson.gz"
        manifest = journal_tools.compact(self.src, dst, archive, keep_days=2, now=NOW)
        old, new = Journal(self.src), Journal(dst)
        audit = lambda j: [e for e in j.events() if e["event_type"] not in journal_tools.TELEMETRY_TYPES]
        self.assertEqual(audit(old), audit(new))
        self.assertEqual(manifest["archived_rows"], 27)
        self.assertEqual(manifest["source_events"], len(old.events()))
        self.assertEqual(manifest["destination_events"] + manifest["archived_rows"], manifest["source_events"])
        self.assertEqual(new.trades(), old.trades())
        self.assertEqual(new.kill_switch(), old.kill_switch())
        # The newest risk state survives so a restart restores the same latch state.
        self.assertEqual(new.latest("paper_risk_state")["payload"], old.latest("paper_risk_state")["payload"])
        with gzip.open(archive, "rt") as fh:
            self.assertEqual(len(fh.readlines()), 27)
        self.assertTrue(Path(str(archive) + ".manifest.json").exists())
        # Append-only triggers are recreated, and seq never goes backwards.
        probe = sqlite3.connect(dst)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                probe.execute("DELETE FROM events")
        finally:
            probe.rollback(); probe.close()
        top = max(e["seq"] for e in old.events())
        self.assertGreater(new.append("r", "x", {}, key="after")["seq"], top)

    def test_compact_never_modifies_source_and_refuses_overwrite(self):
        dst, archive = self.root / "out.sqlite3", self.root / "arch.ndjson.gz"
        before = [e for e in Journal(self.src).events()]
        journal_tools.compact(self.src, dst, archive, keep_days=2, now=NOW)
        self.assertEqual(before, Journal(self.src).events())
        with self.assertRaises(journal_tools.CompactionError):
            journal_tools.compact(self.src, dst, archive, keep_days=2, now=NOW)
        with self.assertRaises(journal_tools.CompactionError):
            journal_tools.compact(self.src, self.src, self.root / "a2.gz", keep_days=2, now=NOW)

    def test_latest_risk_state_is_kept_even_when_everything_is_old(self):
        future = NOW + timedelta(days=365)
        result = journal_tools.plan(self.src, keep_days=1, now=future)
        self.assertEqual(result["telemetry_rows_to_archive"], 36 - 1)  # all telemetry except the newest risk state

    def test_wal_checkpoint_returns_counters(self):
        busy, wal_pages, done = journal_tools.wal_checkpoint(self.src)
        self.assertEqual(busy, 0)


if __name__ == "__main__":
    unittest.main()
