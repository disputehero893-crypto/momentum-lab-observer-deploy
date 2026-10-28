import sqlite3
import tempfile
import unittest
from pathlib import Path

from momentum.journal import Journal, IdempotencyConflict
from momentum.lease import WorkerLease, AlreadyRunning


class JournalTests(unittest.TestCase):
    def test_restart_idempotency_conflicts_and_append_only_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/"journal.sqlite3")
            first = Journal(path)
            row = first.append("run", "event", {"qty": 10}, key="unique")
            second = Journal(path)
            self.assertEqual(row, second.append("run", "event", {"qty": 10}, key="unique"))
            self.assertEqual(len(second.events()), 1)
            with self.assertRaises(IdempotencyConflict):
                second.append("run", "event", {"qty": 11}, key="unique")
            first.append_trade("run", "trade-1", {"net_pnl": "1.23"})
            second.append_trade("run", "trade-1", {"net_pnl": "1.23"})
            self.assertEqual(len(second.trades()), 1)
            with sqlite3.connect(path) as connection:
                for query in ("UPDATE events SET event_type='changed'", "DELETE FROM events", "DELETE FROM trades"):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(query)

    def test_halt_survives_restart_and_cannot_downgrade_emergency(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/"journal.sqlite3")
            Journal(path).set_kill_switch(True, "emergency", flatten=True, request_id="stop")
            again = Journal(path)
            again.set_kill_switch(True, "ordinary halt", flatten=False, request_id="halt")
            self.assertTrue(again.kill_switch()["flatten_requested"])

    def test_one_owner_for_same_persistent_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/"journal.sqlite3")
            with WorkerLease(path):
                with self.assertRaises(AlreadyRunning):
                    with WorkerLease(path):
                        pass
            with WorkerLease(path):
                pass

