"""Offline replay harness for small_cap_candle_patterns_iex_v1 (synthetic bars, no network)."""
from decimal import Decimal
import unittest

from momentum import candle_replay as cr
from day_trade_fixtures import DAY
from test_candle_patterns import FLAG, seq

SYM = "SMCP"


def day_input(rows, *, prior="4.40", avg="2000", floats=None, minutes=1):
    bars = seq(rows, minutes=minutes)
    return cr.DayInput(DAY, {SYM: bars}, {SYM: Decimal(prior)}, {SYM: Decimal(avg)},
                       {SYM: 8_000_000} if floats is None else floats)


class ReplayTests(unittest.TestCase):
    def test_winner_half_at_2r_then_breakeven(self):
        tail = [(5.31, 5.40, 5.30, 5.38, 2000), (5.38, 5.60, 5.37, 5.55, 3000), (5.50, 5.51, 5.30, 5.35, 3000)]
        trades, stats = cr.replay_day(day_input(FLAG + tail))
        self.assertEqual(stats["signals"], 1)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t.kind, t.entry, t.stop, t.qty), ("first_pullback", Decimal("5.31"), Decimal("5.19"), 166))
        self.assertEqual([e["reason"] for e in t.exits], ["first_target_half", "breakeven_stop"])
        self.assertGreater(t.r_multiple, 0)
        self.assertEqual(cr.summarize(trades)["win_rate"], 1.0)

    def test_loser_stops_with_cost(self):
        tail = [(5.31, 5.33, 5.25, 5.27, 2000), (5.27, 5.28, 5.10, 5.12, 3000)]
        trades, _ = cr.replay_day(day_input(FLAG + tail))
        t = trades[0]
        self.assertEqual(t.exits[0]["reason"], "stop")
        self.assertEqual(Decimal(t.exits[0]["price"]), Decimal("5.17"))  # 5.19 stop minus $0.02 cost
        self.assertLess(t.r_multiple, -1)

    def test_entry_not_filled_when_price_runs_away(self):
        tail = [(5.40, 5.50, 5.39, 5.48, 2000)]
        trades, stats = cr.replay_day(day_input(FLAG + tail))
        self.assertEqual(trades, [])
        self.assertEqual(stats["unfilled"], 1)

    def test_big_float_or_low_rvol_never_trades(self):
        self.assertEqual(cr.replay_day(day_input(FLAG, floats={SYM: 50_000_000}))[1]["signals"], 0)
        self.assertEqual(cr.replay_day(day_input(FLAG, avg="10000000"))[1]["signals"], 0)

    def test_summary_and_caveats(self):
        self.assertEqual(cr.summarize([]), {"trades": 0})
        caveats = cr.report_caveats("fmp-5m")
        self.assertIn("5-minute", caveats[0])
        self.assertTrue(any("CURRENT float" in c for c in caveats))


if __name__ == "__main__":
    unittest.main()
