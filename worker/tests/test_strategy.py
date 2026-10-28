"""Causal fixtures and strategy boundaries; all market data here are synthetic."""

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import unittest

from momentum.models import InputError, NY, timestamp
from momentum.strategy import StrategyConfig, compute_rvol, evaluate_exit, evaluate_setup


def valid_snapshot():
    """A reproducible artificial setup, not a real stock or historical result.

    Public contract fixture for the demo adapter. Thirty prior NYSE weekdays in
    this particular date span exclude Labor Day; a real adapter uses its exchange
    calendar and actual feed provenance, never this fixture's date generator.
    """
    day = date(2026, 10, 16)
    prior = []
    cursor = day - timedelta(days=1)
    while len(prior) < 30:
        if cursor.weekday() < 5 and cursor != date(2026, 9, 7):
            prior.append(cursor)
        cursor -= timedelta(days=1)
    prior.sort()
    now = "2026-10-16T13:35:01+00:00"
    rows = [
        ("5.70", "5.91", "5.68", "5.89", 25000),
        ("5.89", "6.12", "5.87", "6.10", 30000),
        ("6.10", "6.50", "6.08", "6.47", 35000),
        ("6.46", "6.48", "6.24", "6.28", 14000),
        ("6.28", "6.29", "6.23", "6.24", 12000),
    ]
    bars = []
    for i, (op, hi, lo, cl, volume) in enumerate(rows):
        start = datetime(2026, 10, 16, 9, 30 + i, tzinfo=NY)
        bars.append({"start": start.isoformat(), "open": op, "high": hi,
                     "low": lo, "close": cl, "volume": volume, "feed": "demo",
                     "complete": True, "available_at": (start + timedelta(minutes=1)).isoformat()})
    return {
        "symbol": "DEMO", "mode": "paper", "now": now, "feed": "demo",
        "session": {"date": day.isoformat(), "is_trading_day": True},
        "quote": {"symbol": "DEMO", "bid": "6.30", "ask": "6.31", "feed": "demo",
                  "timestamp": now, "received_at": now},
        "last_trade": {"symbol": "DEMO", "price": "6.30", "feed": "demo",
                       "timestamp": now, "received_at": now},
        "facts": {"previous_close": "5.00", "float_shares": 9_000_000,
                  "float_effective_at": "2026-10-15T12:00:00+00:00",
                  "float_known_at": "2026-10-15T12:00:01+00:00",
                  "catalyst_confirmed": True,
                  "catalyst_published_at": "2026-10-16T12:30:00+00:00",
                  "catalyst_known_at": "2026-10-16T12:30:01+00:00"},
        "bars": bars,
        "rvol": {"as_of": "2026-10-16T13:35:00+00:00", "feed": "demo",
                 "current_volume": 500000, "session_start_et": "04:00",
                 "prior_sessions": [x.isoformat() for x in prior],
                 "history": [{"session": x.isoformat(), "clock": "09:35:00",
                              "feed": "demo", "volume": 100000,
                              "known_at": datetime(x.year, x.month, x.day, 9, 35, 1, tzinfo=NY).isoformat()}
                             for x in prior]},
    }


def shift_prices(snapshot, amount):
    """Keep geometry while moving this synthetic pattern to another price band."""
    amount = Decimal(amount)
    for bar in snapshot["bars"]:
        for key in ("open", "high", "low", "close"):
            bar[key] = str(Decimal(bar[key]) + amount)
    for key in ("bid", "ask"):
        snapshot["quote"][key] = str(Decimal(snapshot["quote"][key]) + amount)
    snapshot["last_trade"]["price"] = str(Decimal(snapshot["last_trade"]["price"]) + amount)


class SetupTests(unittest.TestCase):
    def assert_reject(self, snapshot, reason):
        result = evaluate_setup(snapshot)
        self.assertFalse(result.accepted, result.to_dict())
        self.assertIn(reason, result.reasons, result.to_dict())
        self.assertIsNone(result.intent)
        self.assertEqual(len(result.metrics["config_id"]), 64)

    def test_complete_baseline_and_serialization(self):
        result = evaluate_setup(valid_snapshot())
        self.assertTrue(result.accepted, result.to_dict())
        self.assertEqual(result.intent["entry_limit"], "6.31")
        self.assertEqual(result.intent["stop_price"], "6.22")
        self.assertEqual(result.intent["target_price"], "6.49")
        self.assertEqual(Decimal(result.metrics["rvol"]), Decimal(5))
        self.assertEqual(result.intent["mode"], "paper")
        self.assertNotIn("qty", result.intent)
        self.assertEqual(json.loads(json.dumps(result.to_dict()))["profile"], "experimental_v1")

    def test_price_bounds_are_inclusive(self):
        for amount, price, previous_close in (("-4.30", "2.00", "1.50"), ("13.70", "20.00", "16.00")):
            with self.subTest(price=price):
                snapshot = valid_snapshot()
                shift_prices(snapshot, amount)
                snapshot["quote"]["bid"] = snapshot["quote"]["ask"] = price
                snapshot["last_trade"]["price"] = price
                snapshot["facts"]["previous_close"] = previous_close
                self.assertTrue(evaluate_setup(snapshot).accepted, evaluate_setup(snapshot).to_dict())
        for price in ("1.99", "20.01"):
            snapshot = valid_snapshot()
            snapshot["quote"]["bid"] = snapshot["quote"]["ask"] = price
            snapshot["last_trade"]["price"] = price
            self.assert_reject(snapshot, "price_outside_profile")

    def test_gain_and_float_exact_boundaries(self):
        snapshot = valid_snapshot()
        shift_prices(snapshot, "0.30")
        snapshot["facts"]["previous_close"] = "6.00"
        snapshot["facts"]["float_shares"] = 19_999_999
        result = evaluate_setup(snapshot)
        self.assertTrue(result.accepted, result.to_dict())
        self.assertEqual(Decimal(result.metrics["gain_pct"]), Decimal(10))
        snapshot["facts"]["previous_close"] = "6.01"
        self.assert_reject(snapshot, "gain_below_threshold")
        snapshot["facts"]["previous_close"] = "6.00"
        snapshot["facts"]["float_shares"] = 20_000_000
        self.assert_reject(snapshot, "float_above_threshold")

    def test_unknown_and_future_facts_block(self):
        for field, value, reason in (
            ("float_shares", None, "float_unconfirmed"),
            ("catalyst_confirmed", "true", "catalyst_unconfirmed"),
            ("float_known_at", "2026-10-16T13:36:00Z", "float_not_known_at_decision"),
            ("catalyst_known_at", "2026-10-16T13:36:00Z", "catalyst_not_known_at_decision"),
            ("float_effective_at", "2026-10-01T12:00:00Z", "float_too_old"),
            ("catalyst_published_at", "2026-10-14T12:00:00Z", "catalyst_too_old"),
        ):
            with self.subTest(field=field):
                snapshot = valid_snapshot()
                snapshot["facts"][field] = value
                self.assert_reject(snapshot, reason)

    def test_missing_session_and_mixed_feed_trade_block(self):
        snapshot = valid_snapshot()
        del snapshot["session"]
        self.assert_reject(snapshot, "session_eligibility_unconfirmed")
        for field, value, reason in (("feed", "sip", "mixed_feed"), ("symbol", "ELSE", "trade_symbol_mismatch")):
            snapshot = valid_snapshot()
            snapshot["last_trade"][field] = value
            self.assert_reject(snapshot, reason)

    def test_unknown_or_wrongly_typed_inputs_fail_closed(self):
        for snapshot in (None, [], {}, {**valid_snapshot(), "facts": None},
                         {**valid_snapshot(), "quote": []}, {**valid_snapshot(), "last_trade": None},
                         {**valid_snapshot(), "rvol": None}):
            with self.subTest(snapshot_type=type(snapshot).__name__):
                self.assertFalse(evaluate_setup(snapshot).accepted)
        for value in ("NaN", "Infinity", "-1", True, None):
            snapshot = valid_snapshot()
            snapshot["quote"]["ask"] = value
            self.assertFalse(evaluate_setup(snapshot).accepted)

    def test_current_completed_bar_and_delayed_bar_cannot_leak(self):
        snapshot = valid_snapshot()
        future = deepcopy(snapshot["bars"][-1])
        future["start"] = "2026-10-16T13:35:00Z"
        future["available_at"] = "2026-10-16T13:36:00Z"
        snapshot["bars"].append(future)
        self.assert_reject(snapshot, "lookahead_bar")
        snapshot = valid_snapshot()
        snapshot["bars"][-1]["complete"] = False
        self.assert_reject(snapshot, "bar_not_complete")
        snapshot = valid_snapshot()
        snapshot["bars"][-1]["available_at"] = "2026-10-16T13:35:02Z"
        self.assert_reject(snapshot, "lookahead_bar")

    def test_intrabar_trigger_uses_prior_red_high(self):
        snapshot = valid_snapshot()
        snapshot["last_trade"]["price"] = "6.29"
        self.assert_reject(snapshot, "waiting_for_breakout")
        snapshot["last_trade"]["price"] = "6.30"
        self.assertTrue(evaluate_setup(snapshot).accepted)
        snapshot["quote"]["ask"] = "6.34"
        self.assert_reject(snapshot, "entry_extended_or_inconsistent")

    def test_quote_age_boundary_future_quote_and_gap(self):
        snapshot = valid_snapshot()
        snapshot["now"] = "2026-10-16T13:35:03Z"
        snapshot["last_trade"]["timestamp"] = snapshot["last_trade"]["received_at"] = snapshot["now"]
        self.assertTrue(evaluate_setup(snapshot).accepted)
        snapshot["now"] = "2026-10-16T13:35:03.001Z"
        self.assert_reject(snapshot, "stale_quote")
        snapshot = valid_snapshot()
        snapshot["quote"]["timestamp"] = snapshot["quote"]["received_at"] = "2026-10-16T13:35:02Z"
        self.assert_reject(snapshot, "future_quote")
        snapshot = valid_snapshot()
        snapshot["bars"][1]["start"] = "2026-10-16T13:30:00Z"
        self.assert_reject(snapshot, "noncontiguous_bars")

    def test_pattern_volume_and_known_resistance_reject(self):
        snapshot = valid_snapshot()
        snapshot["bars"][-1]["volume"] = 40000
        self.assert_reject(snapshot, "pullback_volume_too_high")
        snapshot = valid_snapshot()
        snapshot["bars"][-1]["low"] = "6.08"
        self.assert_reject(snapshot, "pullback_too_deep")
        snapshot = valid_snapshot()
        snapshot["bars"][-1]["low"] = "6.20"
        self.assert_reject(snapshot, "insufficient_known_resistance_room")

    def test_configuration_hash_changes_setup_identity(self):
        snapshot = valid_snapshot()
        first = evaluate_setup(snapshot)
        second = evaluate_setup(snapshot, replace(StrategyConfig(), max_hold_seconds=599))
        self.assertTrue(first.accepted and second.accepted)
        self.assertNotEqual(first.intent["config_id"], second.intent["config_id"])
        self.assertNotEqual(first.intent["setup_id"], second.intent["setup_id"])
        self.assertEqual(first.intent["setup_id"], evaluate_setup(snapshot).intent["setup_id"])

    def test_configuration_cannot_enable_live_or_undefined_math(self):
        for changes in ({"mode": "live"}, {"profile": "exact_ross"}, {"max_spread": "NaN"},
                        {"min_price": 0}, {"max_retracement": "1.01"},
                        {"rvol_sessions": 50}, {"target_r": "1.9"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    StrategyConfig(**changes)


class RvolTests(unittest.TestCase):
    def compute(self, snapshot):
        return compute_rvol(snapshot["rvol"], snapshot["now"], snapshot["feed"])

    def test_same_clock_thirty_prior_sessions_and_threshold(self):
        snapshot = valid_snapshot()
        self.assertEqual(self.compute(snapshot), Decimal(5))
        snapshot["rvol"]["current_volume"] = 499999
        self.assertIn("rvol_below_threshold", evaluate_setup(snapshot).reasons)

    def test_future_session_full_day_clock_and_mixed_feed_are_rejected(self):
        for field, value, reason in (("session", "2026-10-16", "lookahead_rvol_history"),
                                     ("clock", "16:00:00", "mismatched_rvol_clock"),
                                     ("feed", "iex", "mixed_feed"),
                                     ("known_at", "2026-10-16T13:36:00Z", "lookahead_rvol_availability")):
            snapshot = valid_snapshot()
            snapshot["rvol"]["history"][-1][field] = value
            with self.subTest(field=field), self.assertRaises(InputError) as caught:
                self.compute(snapshot)
            self.assertEqual(caught.exception.code, reason)

    def test_missing_duplicate_zero_and_future_numerator(self):
        snapshot = valid_snapshot()
        snapshot["rvol"]["history"].pop()
        with self.assertRaisesRegex(InputError, "incomplete_rvol_history"):
            self.compute(snapshot)
        snapshot = valid_snapshot()
        snapshot["rvol"]["history"][-1] = deepcopy(snapshot["rvol"]["history"][-2])
        with self.assertRaisesRegex(InputError, "duplicate_rvol_session"):
            self.compute(snapshot)
        snapshot = valid_snapshot()
        for row in snapshot["rvol"]["history"]:
            row["volume"] = 0
        with self.assertRaisesRegex(InputError, "zero_rvol_denominator"):
            self.compute(snapshot)
        snapshot = valid_snapshot()
        snapshot["rvol"]["as_of"] = "2026-10-16T13:36:00Z"
        with self.assertRaisesRegex(InputError, "invalid_rvol_cutoff"):
            self.compute(snapshot)


class ExitTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = valid_snapshot()
        self.position = {"symbol": "DEMO", "mode": "paper", "qty": 120,
                         "entry_price": "6.31", "stop_price": "6.22", "target_price": "6.49",
                         "opened_at": "2026-10-16T13:35:00Z"}

    def set_quote(self, bid, now=None):
        self.snapshot["quote"]["bid"] = bid
        self.snapshot["quote"]["ask"] = str(Decimal(bid) + Decimal("0.01"))
        if now:
            self.snapshot["now"] = now
            self.snapshot["quote"]["timestamp"] = self.snapshot["quote"]["received_at"] = now

    def test_hold_stop_gap_and_target_use_observed_bid(self):
        self.assertEqual(evaluate_exit(self.position, self.snapshot).reasons, ("hold",))
        self.set_quote("6.10")
        result = evaluate_exit(self.position, self.snapshot)
        self.assertTrue(result.accepted)
        self.assertEqual(result.intent["limit_price"], "6.10")
        self.assertEqual(result.reasons, ("stop_reached",))
        self.assertFalse(result.metrics["position_flat"])
        self.set_quote("6.49")
        self.assertEqual(evaluate_exit(self.position, self.snapshot).reasons, ("target_reached",))

    def test_maximum_hold_and_session_end(self):
        self.set_quote("6.30", "2026-10-16T13:45:00Z")
        self.assertEqual(evaluate_exit(self.position, self.snapshot).reasons, ("maximum_holding_time",))
        self.set_quote("6.30", "2026-10-16T14:00:00Z")
        self.assertEqual(evaluate_exit(self.position, self.snapshot).reasons, ("session_end",))

    def test_forced_exit_still_requires_fresh_quote_and_fill_confirmation(self):
        result = evaluate_exit(self.position, self.snapshot, force_reason="daily_loss_limit")
        self.assertTrue(result.accepted)
        self.assertTrue(result.metrics["fill_confirmation_required"])
        self.snapshot["now"] = "2026-10-16T13:35:04Z"
        result = evaluate_exit(self.position, self.snapshot, force_reason="daily_loss_limit")
        self.assertFalse(result.accepted)
        self.assertEqual(result.reasons, ("stale_quote",))
        self.assertIsNone(result.intent)

    def test_exit_symbol_mismatch_and_live_mode_block(self):
        self.position["symbol"] = "ELSE"
        self.assertFalse(evaluate_exit(self.position, self.snapshot).accepted)
        self.position["symbol"] = "DEMO"
        self.position["mode"] = "live"
        self.assertIn("live_mode_forbidden", evaluate_exit(self.position, self.snapshot).reasons)


if __name__ == "__main__":
    unittest.main()
