"""Shared day-trade pieces: session clock, bar freshness, VWAP/ATR series, sizing, counters."""
from datetime import date, time, timedelta
from decimal import Decimal
import unittest

from momentum.day_trade_core import (DTBar, DayCounters, SessionClock, SessionSeries, bar_is_fresh,
                                     cents_down, cents_up, cost_allowance, size_shares)
from day_trade_fixtures import DAY, at, bar, flat, rising


class SessionClockTests(unittest.TestCase):
    def test_normal_day_cutoffs(self):
        clock = SessionClock.for_day(DAY)
        self.assertFalse(clock.early_close)
        self.assertEqual(clock.entry_cutoff, time(15, 30))
        self.assertEqual(clock.flatten_at, time(15, 50))
        self.assertTrue(clock.entries_allowed(at(15, 29)))
        self.assertFalse(clock.entries_allowed(at(15, 30)))
        self.assertFalse(clock.entries_allowed(at(9, 29)))
        self.assertFalse(clock.must_flatten(at(15, 49)))
        self.assertTrue(clock.must_flatten(at(15, 50)))

    def test_known_early_close_day_after_thanksgiving(self):
        clock = SessionClock.for_day(date(2026, 11, 27))
        self.assertTrue(clock.early_close)
        self.assertEqual((clock.entry_cutoff, clock.flatten_at), (time(12, 30), time(12, 50)))
        day = date(2026, 11, 27)
        self.assertTrue(clock.entries_allowed(at(12, 29, day)))
        self.assertFalse(clock.entries_allowed(at(12, 30, day)))
        self.assertTrue(clock.must_flatten(at(12, 50, day)))
        self.assertFalse(clock.in_regular_session(at(13, 0, day)))

    def test_broker_calendar_close_wins(self):
        clock = SessionClock.for_day(DAY, time(13, 0))
        self.assertEqual(clock.flatten_at, time(12, 50))

    def test_other_day_must_flatten_and_cannot_enter(self):
        clock = SessionClock.for_day(DAY)
        tomorrow = DAY + timedelta(days=1)
        self.assertTrue(clock.must_flatten(at(10, 0, tomorrow)))
        self.assertFalse(clock.entries_allowed(at(10, 0, tomorrow)))

    def test_regular_session_only(self):
        clock = SessionClock.for_day(DAY)
        self.assertFalse(clock.in_regular_session(at(9, 29)))
        self.assertTrue(clock.in_regular_session(at(9, 30)))
        self.assertFalse(clock.in_regular_session(at(16, 0)))


class BarTests(unittest.TestCase):
    def test_fresh_within_ten_seconds(self):
        self.assertTrue(bar_is_fresh(bar("SPY", 10, 0, 1, 1.1, 0.9, 1, lag=10)))
        self.assertFalse(bar_is_fresh(bar("SPY", 10, 0, 1, 1.1, 0.9, 1, lag=11)))
        self.assertFalse(bar_is_fresh(bar("SPY", 10, 0, 1, 1.1, 0.9, 1, lag=None)))
        self.assertFalse(bar_is_fresh(bar("SPY", 10, 0, 1, 1.1, 0.9, 1, lag=-5)))  # before bar end

    def test_from_mapping_and_validation(self):
        b = DTBar.from_mapping("SPY", {"t": "2026-11-23T14:30:00Z", "o": 1, "h": 2, "l": 1, "c": 1.5, "v": 10,
                                       "vw": 1.4, "received_at": "2026-11-23T14:31:02Z"})
        self.assertEqual(b.vwap, Decimal("1.4"))
        self.assertTrue(bar_is_fresh(b))
        with self.assertRaises(ValueError):
            DTBar.from_mapping("SPY", {"t": "2026-11-23T14:30:00Z", "o": 1, "h": 0.5, "l": 1, "c": 1, "v": 1})
        with self.assertRaises(ValueError):
            DTBar.from_mapping("SPY", {"t": "2026-11-23T14:30:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1})
        with self.assertRaises(ValueError):
            DTBar.from_mapping("SPY", {"o": 1})

    def test_vwap_falls_back_to_typical_price_and_rejects_bad_prices(self):
        b = DTBar.from_mapping("SPY", {"t": "2026-11-23T14:30:00Z", "o": 2, "h": 3, "l": 1, "c": 2, "v": 10})
        self.assertEqual(b.price_for_vwap, Decimal("2"))
        with self.assertRaises(ValueError):
            DTBar.from_mapping("SPY", {"t": "2026-11-23T14:30:00Z", "o": 0, "h": 1, "l": 0, "c": 1, "v": 1})

    def test_cent_rounding(self):
        self.assertEqual(cents_up(Decimal("100.4914")), Decimal("100.50"))
        self.assertEqual(cents_down(Decimal("100.1893")), Decimal("100.18"))


class SeriesTests(unittest.TestCase):
    def test_vwap_uses_bar_vwap_and_resets_per_series(self):
        s = SessionSeries("SPY", DAY)
        s.add(bar("SPY", 9, 30, 10, 11, 9, 10, v=100, vwap=10))
        s.add(bar("SPY", 9, 31, 10, 13, 10, 12, v=300, vwap=12))
        self.assertEqual(s.vwaps[-1], Decimal("11.5"))
        self.assertEqual(len(SessionSeries("SPY", DAY)), 0)

    def test_dedupe_and_out_of_order(self):
        s = SessionSeries("SPY", DAY)
        self.assertTrue(s.add(bar("SPY", 9, 31, 10, 11, 9, 10)))
        self.assertFalse(s.add(bar("SPY", 9, 31, 10, 11, 9, 10)))
        self.assertFalse(s.add(bar("SPY", 9, 30, 10, 11, 9, 10)))
        self.assertEqual(len(s), 1)

    def test_zero_volume_keeps_previous_vwap(self):
        s = SessionSeries("SPY", DAY)
        s.add(bar("SPY", 9, 30, 10, 10, 10, 10, v=0))
        s.add(bar("SPY", 9, 31, 10, 10, 10, 10, v=0))
        self.assertEqual(s.vwaps[-1], Decimal("10"))

    def test_atr_sma14_first_tr_is_high_low(self):
        s = SessionSeries("SPY", DAY)
        for b in flat("SPY", 13):
            s.add(b)
        self.assertIsNone(s.atr())
        s.add(bar("SPY", 9, 43, 100.1, 100.6, 100.0, 100.5))   # TR 0.6
        self.assertEqual(s.true_ranges[0], Decimal("0.2"))
        self.assertEqual(s.atr(), (Decimal("0.2") * 13 + Decimal("0.6")) / 14)
        s.add(bar("SPY", 9, 44, 101.0, 101.1, 101.0, 101.05))  # gap: TR uses prior close
        self.assertEqual(s.true_ranges[-1], Decimal("0.6"))
        self.assertIsNone(s.atr(99))

    def test_ema(self):
        s = SessionSeries("SPY", DAY)
        for b in rising("SPY", 12):
            s.add(b)
        self.assertIsNone(s.ema(9, 5))
        self.assertGreater(s.ema(9), s.ema(9, 9))


class SizingTests(unittest.TestCase):
    def test_cost_allowance(self):
        self.assertEqual(cost_allowance(Decimal("50")), Decimal("0.02"))
        self.assertEqual(cost_allowance(Decimal("200")), Decimal("0.0400"))
        self.assertEqual(cost_allowance(Decimal("50"), global_allowance=Decimal("0.03")), Decimal("0.03"))

    def test_size_formula_takes_the_smallest_limit(self):
        common = dict(price=Decimal("100"), stop=Decimal("99.5"), cost=Decimal("0.02"), max_shares=500)
        q = size_shares(risk_left=Decimal("25"), portfolio_risk_left=Decimal("50"),
                        symbol_notional_left=Decimal("2500"), gross_notional_left=Decimal("5000"), **common)
        self.assertEqual(q, 25)       # notional 2500/100
        q = size_shares(risk_left=Decimal("5"), portfolio_risk_left=Decimal("50"),
                        symbol_notional_left=Decimal("2500"), gross_notional_left=Decimal("5000"), **common)
        self.assertEqual(q, 9)        # 5 / 0.52
        q = size_shares(risk_left=Decimal("25"), portfolio_risk_left=Decimal("50"),
                        symbol_notional_left=Decimal("2500"), gross_notional_left=Decimal("50"), **common)
        self.assertEqual(q, 0)        # under one share: skip
        self.assertEqual(size_shares(price=Decimal("10"), stop=Decimal("11"), cost=Decimal("0"),
                                     risk_left=Decimal("25"), portfolio_risk_left=Decimal("25"),
                                     symbol_notional_left=Decimal("1000"), gross_notional_left=Decimal("1000"),
                                     max_shares=10), 0)


class CounterTests(unittest.TestCase):
    def test_round_trip_and_roll(self):
        c = DayCounters(session_date=DAY.isoformat(), campaigns_total=2, campaigns_by_symbol={"SPY": 2},
                        disabled_symbols=["AAPL"], cooldown_until={"SPY": at(10, 0).isoformat()})
        restored = DayCounters.from_state(c.to_state())
        self.assertEqual(restored, c)
        self.assertTrue(restored.in_cooldown("SPY", at(9, 59)))
        self.assertFalse(restored.in_cooldown("SPY", at(10, 0)))
        self.assertFalse(restored.in_cooldown("MSFT", at(9, 59)))
        restored.roll(DAY)
        self.assertEqual(restored.campaigns_total, 2)
        restored.roll(DAY + timedelta(days=1))
        self.assertEqual((restored.campaigns_total, restored.disabled_symbols), (0, []))
        self.assertEqual(DayCounters.from_state(None), DayCounters())


if __name__ == "__main__":
    unittest.main()
