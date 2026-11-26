"""Pure candle-pattern detectors (1m and 5m). Every number is invented."""
from datetime import datetime, timedelta
from decimal import Decimal
import unittest

from momentum import candle_patterns as cp
from momentum.models import NY
from day_trade_fixtures import DAY, bar

V = Decimal("5.00")
P1 = cp.ONE_MINUTE


def seq(rows, *, symbol="SMCP", start=(9, 30), minutes=1):
    """rows: (o, h, l, c, v). Consecutive bars from ``start``; 5m rows become DTBar5."""
    t0 = datetime(DAY.year, DAY.month, DAY.day, *start, tzinfo=NY)
    out = []
    for k, (o, h, l, c, v) in enumerate(rows):
        t = t0 + timedelta(minutes=k * minutes)
        b = bar(symbol, t.hour, t.minute, o, h, l, c, v)
        if minutes == 5:
            b = cp.DTBar5(**{f: getattr(b, f) for f in ("symbol", "start", "open", "high", "low", "close",
                                                       "volume", "vwap")},
                          received_at=b.start + timedelta(minutes=5, seconds=2))
        out.append(b)
    return out


def flat_vwap(bars, value=V):
    return [Decimal(value)] * len(bars)


FLAG = [(5.00, 5.06, 4.98, 5.05, 1000), (5.05, 5.12, 5.04, 5.11, 1200), (5.11, 5.20, 5.10, 5.19, 1500),
        (5.19, 5.30, 5.18, 5.29, 1800), (5.28, 5.29, 5.22, 5.23, 600), (5.23, 5.25, 5.20, 5.21, 500),
        (5.21, 5.33, 5.21, 5.32, 3000)]
MICRO = [(5.00, 5.04, 4.99, 5.03, 800), (5.03, 5.06, 5.02, 5.05, 800), (5.05, 5.08, 5.04, 5.07, 800),
         (5.07, 5.15, 5.06, 5.14, 1500), (5.14, 5.25, 5.13, 5.24, 1800), (5.24, 5.35, 5.23, 5.34, 2000),
         (5.34, 5.35, 5.30, 5.31, 700), (5.31, 5.42, 5.31, 5.41, 3000)]
ORB = [(5.00, 5.10, 4.95, 5.08, 2000), (5.08, 5.15, 5.05, 5.12, 1500), (5.12, 5.20, 5.10, 5.15, 1500),
       (5.15, 5.18, 5.08, 5.10, 1000), (5.10, 5.16, 5.06, 5.14, 1000), (5.14, 5.19, 5.12, 5.17, 800),
       (5.17, 5.19, 5.14, 5.18, 800), (5.18, 5.30, 5.17, 5.29, 4000)]
RECLAIM = [(5.00, 5.08, 4.99, 5.06, 1000), (5.06, 5.09, 5.03, 5.05, 1000), (5.05, 5.06, 4.99, 5.01, 1000),
           (5.01, 5.02, 4.96, 4.97, 1000), (4.97, 4.98, 4.92, 4.94, 1000), (4.94, 5.08, 4.93, 5.07, 3000)]
HIGHER_LOW = [(5.10, 5.15, 5.08, 5.12, 1000), (5.12, 5.13, 5.02, 5.04, 1000), (5.05, 5.20, 5.05, 5.18, 1000),
              (5.18, 5.22, 5.15, 5.16, 900), (5.16, 5.17, 5.08, 5.10, 800), (5.10, 5.18, 5.09, 5.17, 900),
              (5.17, 5.30, 5.16, 5.29, 3000)]


def replace_row(rows, k, row):
    rows = list(rows)
    rows[k] = row
    return rows


class CandleHelperTests(unittest.TestCase):
    def test_strong_close_engulfing_hammer(self):
        strong, weak = seq([(5.0, 5.10, 4.98, 5.09, 1), (5.0, 5.10, 4.98, 5.03, 1)])
        self.assertTrue(cp.strong_close(strong))
        self.assertFalse(cp.strong_close(weak))
        red, green = seq([(5.10, 5.11, 5.00, 5.02, 1), (5.01, 5.14, 5.00, 5.12, 1)])
        self.assertTrue(cp.bullish_engulfing(red, green))
        self.assertFalse(cp.bullish_engulfing(green, red))
        (ham,) = seq([(5.08, 5.10, 4.90, 5.09, 1)])
        self.assertTrue(cp.hammer(ham))
        (not_ham,) = seq([(5.00, 5.20, 4.99, 5.10, 1)])
        self.assertFalse(cp.hammer(not_ham))

    def test_session_surge_is_relative_to_own_bars(self):
        bars = seq([(5, 5.1, 4.9, 5.0, 100)] * 4 + [(5, 5.1, 4.9, 5.0, 300)])
        self.assertEqual(cp.session_surge(bars, 4, P1), Decimal(3))
        # Scaling every volume by 40x (IEX vs full tape) does not change the ratio.
        big = seq([(5, 5.1, 4.9, 5.0, 4000)] * 4 + [(5, 5.1, 4.9, 5.0, 12000)])
        self.assertEqual(cp.session_surge(big, 4, P1), Decimal(3))
        self.assertIsNone(cp.session_surge(bars[:3], 2, P1))  # not enough history

    def test_pace_rvol_adjusts_for_time_of_day(self):
        early = datetime(2026, 11, 23, 10, 0, tzinfo=NY)
        late = datetime(2026, 11, 23, 16, 0, tzinfo=NY)
        self.assertEqual(cp.cumulative_volume_fraction(early), Decimal("0.22"))
        self.assertEqual(cp.cumulative_volume_fraction(late), Decimal("1.00"))
        self.assertEqual(cp.cumulative_volume_fraction(datetime(2026, 11, 23, 9, 30, tzinfo=NY)), Decimal("0.06"))
        self.assertEqual(cp.pace_rvol(Decimal(110), Decimal(100), early), Decimal(5))
        self.assertIsNone(cp.pace_rvol(Decimal(10), Decimal(0), early))

    def test_aggregate_five_minute_bars(self):
        bars = seq([(5.0, 5.1, 4.9, 5.05, 100)] * 5 + [(5.05, 5.3, 5.0, 5.2, 50)] * 3)
        five = cp.aggregate_bars(bars)
        self.assertEqual(len(five), 1)  # second bucket incomplete
        self.assertEqual((five[0].open, five[0].high, five[0].low, five[0].close, five[0].volume),
                         (Decimal("5.0"), Decimal("5.1"), Decimal("4.9"), Decimal("5.05"), 500))
        self.assertEqual(five[0].end - five[0].start, timedelta(minutes=5))
        self.assertEqual(len(cp.aggregate_bars(bars, include_partial=True)), 2)
        # A missing IEX minute is not forward-filled; the next bucket still closes the previous one.
        gappy = [b for k, b in enumerate(seq([(5.0, 5.1, 4.9, 5.05, 100)] * 7)) if k != 4]
        self.assertEqual(cp.aggregate_bars(gappy)[0].volume, 400)

    def test_first_pullback_label(self):
        clean = seq([(5, 5.1, 4.9, 5.05, 1), (5.05, 5.2, 5.0, 5.15, 1), (5.15, 5.3, 5.1, 5.25, 1)])
        self.assertTrue(cp._first_pullback(clean, 3))
        dipped = seq([(5, 5.2, 4.9, 5.1, 1), (5.1, 5.15, 5.0, 5.05, 1), (5.05, 5.3, 5.0, 5.25, 1)])
        self.assertFalse(cp._first_pullback(dipped, 3))


class DetectorTests(unittest.TestCase):
    def detect(self, rows, fn=None, vwap=V, params=P1):
        bars = seq(rows)
        vw = flat_vwap(bars, vwap)
        if fn is None:
            return cp.scan_patterns(bars, vw, len(bars) - 1, params)
        return fn(bars, vw, len(bars) - 1, params)

    def test_first_pullback_flag(self):
        sig, why = self.detect(FLAG)
        self.assertIsNotNone(sig, why)
        self.assertEqual(sig.kind, "first_pullback")
        self.assertEqual(sig.trigger, Decimal("5.26"))
        self.assertEqual(sig.stop_low, Decimal("5.20"))
        self.assertIn("strong_close", sig.confirmations)
        self.assertLess(sig.metrics["retrace"], Decimal("0.5"))
        self.assertLess(sig.metrics["flag_volume_ratio"], 1)

    def test_flag_rejections(self):
        deep = replace_row(FLAG, 5, (5.23, 5.25, 5.05, 5.21, 500))
        self.assertEqual(self.detect(deep, cp.detect_pullback_breakout)[1], "flag_retrace_over_50pct")
        self.assertEqual(self.detect(FLAG, cp.detect_pullback_breakout, vwap="5.21")[1], "flag_broke_vwap")
        heavy = replace_row(replace_row(FLAG, 4, (5.28, 5.29, 5.22, 5.23, 2500)), 5, (5.23, 5.25, 5.20, 5.21, 2500))
        heavy = replace_row(heavy, 6, (5.21, 5.33, 5.21, 5.32, 9000))
        self.assertEqual(self.detect(heavy, cp.detect_pullback_breakout)[1], "flag_pullback_volume_not_lighter")
        weak = replace_row(FLAG, 6, (5.24, 5.33, 5.21, 5.27, 3000))
        self.assertEqual(self.detect(weak, cp.detect_pullback_breakout)[1], "flag_no_candle_confirmation")
        quiet = replace_row(FLAG, 6, (5.21, 5.33, 5.21, 5.32, 1000))
        self.assertEqual(self.detect(quiet, cp.detect_pullback_breakout)[1], "flag_no_volume_surge")
        waiting = replace_row(FLAG, 6, (5.21, 5.25, 5.19, 5.22, 3000))
        self.assertIsNone(self.detect(waiting, cp.detect_pullback_breakout)[0])

    def test_micro_pullback(self):
        sig, why = self.detect(MICRO)
        self.assertIsNotNone(sig, why)
        self.assertEqual(sig.kind, "micro_pullback")
        self.assertEqual(sig.trigger, Decimal("5.36"))
        self.assertEqual(sig.stop_low, Decimal("5.30"))
        deep = replace_row(MICRO, 6, (5.34, 5.35, 5.15, 5.20, 700))
        self.assertEqual(self.detect(deep, cp.detect_micro_pullback)[1], "micro_pause_too_deep")
        self.assertEqual(self.detect(MICRO, cp.detect_micro_pullback, params=cp.FIVE_MINUTE)[1], "micro_1m_only")

    def test_opening_range_breakout(self):
        sig, why = self.detect(ORB)
        self.assertIsNotNone(sig, why)
        self.assertEqual(sig.kind, "opening_range_breakout")
        self.assertEqual(sig.trigger, Decimal("5.21"))
        self.assertEqual(sig.stop_low, Decimal("5.12"))  # breakout base, never below the OR low
        red_or = replace_row(ORB, 4, (5.10, 5.16, 4.96, 4.98, 1000))
        self.assertEqual(self.detect(red_or, cp.detect_opening_range_breakout)[1], "orb_range_not_green")
        late = seq(ORB, start=(10, 25))
        self.assertEqual(cp.detect_opening_range_breakout(late, flat_vwap(late), 7, P1)[1],
                         "orb_session_start_missing")

    def test_vwap_reclaim(self):
        sig, why = self.detect(RECLAIM)
        self.assertIsNotNone(sig, why)
        self.assertEqual(sig.kind, "vwap_reclaim")
        self.assertEqual(sig.trigger, Decimal("5.01"))
        self.assertEqual(sig.stop_low, Decimal("4.92"))
        self.assertEqual(set(sig.confirmations) & {"strong_close", "bullish_engulfing"},
                         {"strong_close", "bullish_engulfing"})
        never = [(4.9, 4.95, 4.85, 4.9, 1000)] * 5 + [(4.94, 5.08, 4.93, 5.07, 3000)]
        self.assertEqual(self.detect(never, cp.detect_vwap_reclaim)[1], "reclaim_never_above_vwap")
        too_long = [(5.0, 5.08, 4.99, 5.06, 1000)] + never[:1] * 9 + never[-1:]
        self.assertEqual(self.detect(too_long, cp.detect_vwap_reclaim)[1], "reclaim_too_long_below_vwap")

    def test_higher_low_breakout(self):
        sig, why = self.detect(HIGHER_LOW)
        self.assertIsNotNone(sig, why)
        self.assertEqual(sig.kind, "higher_low_breakout")
        self.assertEqual(sig.trigger, Decimal("5.23"))
        self.assertEqual(sig.stop_low, Decimal("5.08"))
        lower = replace_row(HIGHER_LOW, 4, (5.16, 5.17, 5.00, 5.10, 800))
        self.assertEqual(self.detect(lower, cp.detect_higher_low_breakout)[1], "higher_low_not_higher")

    def test_detectors_never_look_ahead(self):
        bars = seq(FLAG + [(5.32, 5.33, 4.00, 4.10, 9000)])
        vw = flat_vwap(bars)
        sig, _ = cp.scan_patterns(bars, vw, 6, P1)
        self.assertEqual(sig.kind, "first_pullback")  # a later crash bar does not change bar 6's answer

    def test_five_minute_flag(self):
        rows = [(5.00, 5.20, 4.95, 5.18, 10000), (5.18, 5.45, 5.15, 5.42, 14000), (5.42, 5.44, 5.30, 5.32, 5000),
                (5.32, 5.52, 5.31, 5.50, 16000)]
        bars = seq(rows, minutes=5)
        sig, why = cp.scan_patterns(bars, flat_vwap(bars), 3, cp.FIVE_MINUTE)
        self.assertIsNotNone(sig, why)
        self.assertEqual((sig.kind, sig.timeframe), ("first_pullback", "5m"))

    def test_enabled_filter(self):
        bars = seq(FLAG)
        sig, reasons = cp.scan_patterns(bars, flat_vwap(bars), 6, P1, enabled=("vwap_reclaim",))
        self.assertIsNone(sig)
        self.assertTrue(reasons)


if __name__ == "__main__":
    unittest.main()
