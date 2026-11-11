"""Journal growth control: sampled telemetry and the low-disk guard (crashloop 2026-11-05)."""
from collections import namedtuple
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from momentum.config import Config, ConfigError
from momentum.journal import Journal

Usage = namedtuple("Usage", "total used free")


class SampledJournalTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "j.sqlite3"

    def test_unchanged_telemetry_is_written_once_per_window(self):
        journal = Journal(self.path, sample_seconds=60)
        clock = iter([0.0, 5.0, 10.0, 59.0, 61.0])
        with mock.patch("momentum.journal.time.monotonic", side_effect=lambda: next(clock)):
            results = [journal.append_sampled("r", "t", {"n": i}, key=f"k{i}", signature="same") for i in range(5)]
        self.assertEqual([r is not None for r in results], [True, False, False, False, True])
        self.assertEqual(len(journal.events()), 2)

    def test_changed_signature_is_always_written(self):
        journal = Journal(self.path, sample_seconds=3600)
        for i, sig in enumerate(["a", "a", "b", "b", "a"]):
            journal.append_sampled("r", "t", {"n": i}, key=f"k{i}", signature=sig)
        self.assertEqual([e["payload"]["n"] for e in journal.events()], [0, 2, 4])

    def test_zero_window_writes_every_event_like_append(self):
        journal = Journal(self.path, sample_seconds=0)
        for i in range(4):
            journal.append_sampled("r", "t", {"n": i}, key=f"k{i}", signature="same")
        self.assertEqual(len(journal.events()), 4)

    def test_event_types_are_sampled_independently(self):
        journal = Journal(self.path, sample_seconds=3600)
        journal.append_sampled("r", "a", {}, key="k1", signature="s")
        journal.append_sampled("r", "b", {}, key="k2", signature="s")
        self.assertEqual({e["event_type"] for e in journal.events()}, {"a", "b"})

    def test_low_disk_skips_telemetry_but_not_safety_events(self):
        journal = Journal(self.path, sample_seconds=0, min_free_bytes=64 * 1024 * 1024)
        with mock.patch("momentum.journal.shutil.disk_usage", return_value=Usage(1000, 990, 10)):
            self.assertIsNone(journal.append_sampled("r", "t", {}, key="k1"))
            self.assertEqual(journal.telemetry_skipped, 1)
            journal.set_kill_switch(True, "operator")  # safety writes still go through append()
        self.assertEqual([e["event_type"] for e in journal.events()], ["kill_switch_changed"])

    def test_enough_free_space_writes(self):
        journal = Journal(self.path, sample_seconds=0, min_free_bytes=1024)
        with mock.patch("momentum.journal.shutil.disk_usage", return_value=Usage(10**9, 0, 10**9)):
            self.assertIsNotNone(journal.append_sampled("r", "t", {}, key="k1"))

    def test_database_or_disk_is_full_is_not_fatal_for_telemetry(self):
        journal = Journal(self.path, sample_seconds=0)
        with mock.patch.object(Journal, "append", side_effect=sqlite3.OperationalError("database or disk is full")):
            self.assertIsNone(journal.append_sampled("r", "t", {}, key="k1"))
        self.assertEqual(journal.telemetry_skipped, 1)

    def test_other_sqlite_errors_still_raise(self):
        journal = Journal(self.path, sample_seconds=0)
        with mock.patch.object(Journal, "append", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                journal.append_sampled("r", "t", {}, key="k1")

    def test_one_day_of_five_second_cycles_is_bounded(self):
        journal = Journal(self.path, sample_seconds=60)
        now = [0.0]
        with mock.patch("momentum.journal.time.monotonic", side_effect=lambda: now[0]):
            for i in range(17280):  # 24 h at POLL_SECONDS=5
                journal.append_sampled("r", "paper_observation_cycle", {"i": i}, key=f"c{i}", signature="steady")
                now[0] += 5
        self.assertEqual(len(journal.events()), 1440)  # 12x fewer than 17,280


class ConfigTests(unittest.TestCase):
    def test_defaults_and_bounds(self):
        cfg = Config()
        self.assertEqual((cfg.journal_sample_seconds, cfg.journal_min_free_mb), (60.0, 64))
        with self.assertRaises(ConfigError):
            Config(journal_sample_seconds=-1)
        with self.assertRaises(ConfigError):
            Config(journal_min_free_mb=-1)

    def test_environment_override(self):
        cfg = Config.from_env({"JOURNAL_SAMPLE_SECONDS": "0", "JOURNAL_MIN_FREE_MB": "128"})
        self.assertEqual((cfg.journal_sample_seconds, cfg.journal_min_free_mb), (0.0, 128))


if __name__ == "__main__":
    unittest.main()
