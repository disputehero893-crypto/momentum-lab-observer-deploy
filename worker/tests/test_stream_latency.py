"""Deterministic latency checks; no Alpaca connectivity or order placement."""
import unittest
from momentum.stream_latency import TradeTrace, latency_summary, percentile


class LatencyTests(unittest.TestCase):
    def test_complete_trace(self):
        times = iter([1_000_000_000, 1_005_000_000, 1_020_000_000])
        t = TradeTrace("paper-1", clock_ns=lambda: next(times))
        for stage in ("received", "signal", "acknowledged"):
            t.mark(stage)
        self.assertEqual(t.duration_ms("received", "acknowledged"), 20)
        self.assertEqual(t.duration_ms("received", "exit_filled"), None)

    def test_missing_is_not_zero(self):
        t = TradeTrace("paper-2")
        t.mark("received")
        summary = latency_summary([t], "received", "entry_filled")
        self.assertEqual(summary["missing"], 1)
        self.assertIsNone(summary["p95_ms"])

    def test_reject_duplicate_and_invalid(self):
        t = TradeTrace("paper-3")
        t.mark("received")
        with self.assertRaises(ValueError):
            t.mark("received")
        with self.assertRaises(ValueError):
            t.mark("made_up")

    def test_percentiles(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5], .95), 5)
        self.assertIsNone(percentile([], .50))


if __name__ == "__main__":
    unittest.main()
