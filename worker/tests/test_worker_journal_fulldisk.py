"""Follow-up to the journal growth fix: full-volume behaviour and strict (armed) writes."""
from contextlib import contextmanager
import errno
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from momentum.journal import Journal
from test_worker_observation import Fixture




@contextmanager
def _fail_schema_with(error):
    class FakeDB:
        def executescript(self, _schema):
            raise error

    yield FakeDB()


class FullVolumeTests(unittest.TestCase):
    def test_full_volume_during_initial_schema_setup_is_actionable_and_preserves_journal(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "j.sqlite3"
            journal = Journal(path)
            expected = journal.append("r", "preserved", {"marker": "keep"}, key="keep")
            before = path.read_bytes()

            with mock.patch.object(Journal, "_connect", new=lambda _self: _fail_schema_with(
                    sqlite3.OperationalError("database or disk is full"))):
                with self.assertRaisesRegex(
                        sqlite3.OperationalError,
                        "^journal_volume_full_grow_the_disk_or_free_space$"):
                    Journal(path)

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(Journal(path).event_by_key("keep"), expected)

    def test_constructor_failure_after_real_wal_setup_preserves_existing_events(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "j.sqlite3"
            journal = Journal(path)
            expected = journal.append("r", "preserved", {"marker": "keep"}, key="keep")
            original_connect = Journal._connect

            @contextmanager
            def fail_schema_after_wal(self):
                with original_connect(self) as db:
                    class FailingDB:
                        def executescript(self, _schema):
                            # Exercise a real SQLite open and WAL pragma, then inject
                            # the synthetic full-volume failure before DDL proceeds.
                            db.execute("PRAGMA journal_mode=WAL")
                            raise sqlite3.OperationalError("database or disk is full")

                    yield FailingDB()

            with mock.patch.object(Journal, "_connect", new=fail_schema_after_wal):
                with self.assertRaisesRegex(
                        sqlite3.OperationalError,
                        "^journal_volume_full_grow_the_disk_or_free_space$"):
                    Journal(path)

            reopened = Journal(path)
            self.assertEqual(reopened.event_by_key("keep"), expected)
            self.assertEqual(len(reopened.events()), 1)

    def test_enospc_while_creating_journal_parent_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(Path, "mkdir", side_effect=OSError(errno.ENOSPC, "no space")):
                with self.assertRaisesRegex(
                        sqlite3.OperationalError,
                        "^journal_volume_full_grow_the_disk_or_free_space$"):
                    Journal(Path(d) / "j.sqlite3")

    def test_enospc_while_sqlite_opens_journal_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("momentum.journal.sqlite3.connect",
                            side_effect=OSError(errno.ENOSPC, "no space")):
                with self.assertRaisesRegex(
                        sqlite3.OperationalError,
                        "^journal_volume_full_grow_the_disk_or_free_space$"):
                    Journal(Path(d) / "j.sqlite3")

    def test_disk_io_error_during_initialization_is_mapped_only_when_space_is_low(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "j.sqlite3"
            with mock.patch.object(Journal, "_connect", new=lambda _self: _fail_schema_with(
                    sqlite3.OperationalError("disk I/O error"))), \
                 mock.patch.object(Journal, "_known_free_bytes", return_value=0):
                with self.assertRaisesRegex(
                        sqlite3.OperationalError,
                        "^journal_volume_full_grow_the_disk_or_free_space$"):
                    Journal(path, min_free_bytes=64 * 1024 * 1024)

            original = sqlite3.OperationalError("disk I/O error")
            with mock.patch.object(Journal, "_connect", new=lambda _self: _fail_schema_with(original)), \
                 mock.patch.object(Journal, "_known_free_bytes", return_value=10**9):
                with self.assertRaises(sqlite3.OperationalError) as ctx:
                    Journal(path)
                self.assertIs(ctx.exception, original)

            unknown_space = sqlite3.OperationalError("disk I/O error")
            with mock.patch.object(Journal, "_connect", new=lambda _self: _fail_schema_with(unknown_space)), \
                 mock.patch.object(Journal, "_known_free_bytes", return_value=None):
                with self.assertRaises(sqlite3.OperationalError) as ctx:
                    Journal(path)
                self.assertIs(ctx.exception, unknown_space)

    def test_unrelated_initialization_error_is_not_relabelled(self):
        with tempfile.TemporaryDirectory() as d:
            original = sqlite3.OperationalError("database is locked")
            with mock.patch.object(Journal, "_connect", new=lambda _self: _fail_schema_with(original)), \
                 mock.patch.object(Journal, "_free_bytes", return_value=0):
                with self.assertRaises(sqlite3.OperationalError) as ctx:
                    Journal(Path(d) / "j.sqlite3")
                self.assertIs(ctx.exception, original)

    def test_disk_io_error_on_a_nearly_empty_volume_is_not_fatal_for_telemetry(self):
        with tempfile.TemporaryDirectory() as d:
            journal = Journal(Path(d) / "j.sqlite3", sample_seconds=0)
            with mock.patch.object(Journal, "append", side_effect=sqlite3.OperationalError("disk I/O error")), \
                 mock.patch.object(Journal, "_known_free_bytes", return_value=0):
                self.assertIsNone(journal.append_sampled("r", "t", {}, key="k"))

    def test_any_operational_error_on_a_sampled_write_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            journal = Journal(Path(d) / "j.sqlite3", sample_seconds=0)
            kept = journal.append("r", "preserved", {"marker": "keep"}, key="keep")
            with mock.patch.object(Journal, "append", side_effect=sqlite3.OperationalError("disk I/O error")):
                self.assertIsNone(journal.append_sampled("r", "t", {}, key="k"))
            self.assertEqual(journal.last_write_error, "journal_write_failed")
            self.assertEqual(Journal(Path(d) / "j.sqlite3").event_by_key("keep")["payload"], kept["payload"])

    def test_observation_start_survives_any_write_operational_error(self):
        for measured_free in (0, None, 10**9):
            with self.subTest(measured_free=measured_free), tempfile.TemporaryDirectory() as d:
                fixture = Fixture(d, history=False)
                worker = fixture.worker()
                original = sqlite3.OperationalError("disk I/O error")
                with mock.patch.object(Journal, "append", side_effect=original), \
                     mock.patch.object(Journal, "_known_free_bytes", return_value=measured_free):
                    entered = worker.__enter__()
                self.assertIs(entered, worker)
                self.assertTrue(worker._active)
                expected = "database_or_disk_is_full" if measured_free == 0 else "journal_write_failed"
                self.assertEqual(worker.journal.last_write_error, expected)
                worker.__exit__(None, None, None)

    def test_start_on_full_volume_stays_up_and_preserves_rows(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = Fixture(d, history=False)
            journal = Journal(fixture.config.db_path, min_free_bytes=0)
            journal.append("paper-observation", "preserved_event", {"marker": "keep"}, key="keep")
            worker = fixture.worker()
            with mock.patch.object(Journal, "append", side_effect=sqlite3.OperationalError("database or disk is full")):
                worker.__enter__()
            self.assertTrue(worker._active)
            self.assertEqual(worker.journal.last_write_error, "database_or_disk_is_full")
            self.assertFalse(worker.state()["paper_execution_running"])
            worker.__exit__(None, None, None)
            reopened = Journal(fixture.config.db_path, min_free_bytes=0)
            self.assertEqual(reopened.event_by_key("keep")["payload"]["marker"], "keep")
            self.assertGreaterEqual(reopened._row_count(), 1)

    def test_armed_execution_cycles_are_never_sampled(self):
        # execute=True keeps strict append for risk state and cycle records (audit of live-paper orders).
        from momentum import session as session_module
        src = Path(session_module.__file__).read_text()
        self.assertIn("(self.journal.append_sampled if not execute else self.journal.append)", src)
        self.assertIn("write = self.journal.append_sampled if not execute else self.journal.append", src)


if __name__ == "__main__":
    unittest.main()
