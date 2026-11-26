"""Offline coverage for strategy reject branches, worker Config validation and model contracts.

All market data are synthetic fixtures. Nothing here touches a network, a broker,
or any execution path; every accepted result is only a paper intent object.
"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest

from momentum.config import Config, ConfigError, flag
from momentum.models import (Bar, InputError, NY, Quote, decimal_value, session_date, timestamp)
from momentum.strategy import StrategyConfig, compute_rvol, evaluate_exit, evaluate_setup
from tests.test_strategy import experimental, valid_snapshot


class _ExplodingSnapshot(Mapping):
    """A mapping whose lookups raise TypeError, to reach the generic input-error path."""

    def __getitem__(self, key):
        raise TypeError("boom")

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


class StrategyConfigBoundsTests(unittest.TestCase):
    def test_ordered_bounds_and_trading_window_fail_closed(self):
        # Default (liquid IEX) price band is 5..1000, so min_price must exceed 1000 to invert it.
        for changes in ({"min_price": "2000"}, {"min_pullback_bars": 4},
                        {"trade_start_et": "not-a-time"}, {"trade_start_et": "03:59"},
                        {"trade_end_et": "16:01"}, {"trade_start_et": "10:00", "trade_end_et": "09:00"},
                        {"trade_start_et": "07:00+00:00"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    StrategyConfig(**changes)

    def test_generic_exception_becomes_invalid_snapshot_not_a_crash(self):
        for result in (evaluate_setup(_ExplodingSnapshot()),
                       evaluate_exit({"symbol": "DEMO"}, _ExplodingSnapshot())):
            self.assertFalse(result.accepted)
            self.assertEqual(result.reasons, ("invalid_snapshot",))
            self.assertEqual(result.metrics["error_type"], "TypeError")
            self.assertIsNone(result.intent)


class RvolBranchTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = valid_snapshot()
        self.rvol = self.snapshot["rvol"]

    def reject(self, code, rvol=None, sessions=30):
        with self.assertRaises(InputError) as ctx:
            compute_rvol(self.rvol if rvol is None else rvol, self.snapshot["now"], "demo", sessions)
        self.assertEqual(ctx.exception.code, code)

    def test_definition_staleness_feed_and_session_start(self):
        self.reject("invalid_rvol", rvol=[])
        self.reject("unsupported_rvol_definition", sessions=20)
        self.rvol["as_of"] = "2026-10-16T13:34:00+00:00"
        self.reject("stale_rvol")
        self.rvol = valid_snapshot()["rvol"]
        self.rvol["feed"] = "sip"
        self.reject("mixed_feed")
        self.rvol = valid_snapshot()["rvol"]
        self.rvol["session_start_et"] = "09:30"
        self.reject("mixed_rvol_session_definition")

    def test_calendar_and_history_rows(self):
        self.rvol["prior_sessions"] = list(reversed(self.rvol["prior_sessions"]))
        self.reject("incomplete_rvol_history")
        self.rvol = valid_snapshot()["rvol"]
        self.rvol["prior_sessions"] = self.rvol["prior_sessions"][1:] + ["2026-10-16"]
        self.reject("lookahead_rvol_history")
        self.rvol = valid_snapshot()["rvol"]
        self.rvol["history"] = "not-a-list"
        self.reject("incomplete_rvol_history")
        self.rvol = valid_snapshot()["rvol"]
        self.rvol["history"][0] = "row"
        self.reject("invalid_rvol_history_row")

    def test_row_level_checks(self):
        cases = [
            (lambda r: r["history"][0].update(session="2026-10-16"), "lookahead_rvol_history"),
            (lambda r: r["history"][1].update(session=r["history"][0]["session"]), "duplicate_rvol_session"),
            (lambda r: r["history"][0].update(feed="sip"), "mixed_feed"),
            (lambda r: r["history"][0].update(session_start_et="09:30"), "mixed_rvol_session_definition"),
            (lambda r: r["history"][0].update(clock="nope"), "invalid_rvol_clock"),
            (lambda r: r["history"][0].update(clock="09:36:00"), "mismatched_rvol_clock"),
            (lambda r: r["history"][0].update(known_at="2026-08-01T00:00:00+00:00"), "lookahead_rvol_availability"),
        ]
        for mutate, code in cases:
            with self.subTest(code=code):
                self.rvol = valid_snapshot()["rvol"]
                mutate(self.rvol)
                self.reject(code)

    def test_history_calendar_mismatch_and_zero_denominator(self):
        # Swap one history row for an unlisted (but earlier) weekday: set mismatch.
        first = date.fromisoformat(self.rvol["prior_sessions"][0])
        other = first - timedelta(days=7)
        self.rvol["history"][0].update(session=other.isoformat(),
                                       known_at=datetime(other.year, other.month, other.day, 9, 35, 1, tzinfo=NY).isoformat())
        self.reject("incomplete_rvol_history")
        self.rvol = valid_snapshot()["rvol"]
        for row in self.rvol["history"]:
            row["volume"] = 0
        self.reject("zero_rvol_denominator")


class SetupRejectBranchTests(unittest.TestCase):
    # The synthetic DEMO fixture is outside the default liquid IEX universe, so the reject
    # branches are exercised with the gated small-cap rules opened for this fixture only
    # (the same `experimental()` helper test_strategy uses). The default-config refusal is
    # asserted separately below.
    def assert_reject(self, snapshot, reason, config=None):
        result = evaluate_setup(snapshot, experimental() if config is None else config)
        self.assertFalse(result.accepted, result.to_dict())
        self.assertIn(reason, result.reasons, result.to_dict())
        self.assertIsNone(result.intent)
        return result

    def mutated(self, mutate):
        snapshot = valid_snapshot()
        mutate(snapshot)
        return snapshot

    def test_baseline_fixture_is_still_accepted(self):
        self.assertTrue(evaluate_setup(valid_snapshot(), experimental()).accepted)

    def test_default_liquid_preset_refuses_synthetic_symbol(self):
        result = evaluate_setup(valid_snapshot())
        self.assertFalse(result.accepted)
        self.assertEqual(result.reasons, ("symbol_not_in_preset_universe",))
        self.assertIsNone(result.intent)

    def test_market_envelope_rejects(self):
        self.assert_reject([], "invalid_snapshot")
        self.assert_reject(self.mutated(lambda s: s.update(symbol="X" * 25)), "missing_or_invalid_symbol")
        self.assert_reject(self.mutated(lambda s: s.update(feed="")), "missing_feed")
        self.assert_reject(self.mutated(lambda s: s.update(quote=["bid"])), "invalid_quote")
        self.assert_reject(self.mutated(lambda s: s["quote"].update(symbol="ELSE")), "quote_symbol_mismatch")

    def test_session_spread_and_trade_rejects(self):
        self.assert_reject(self.mutated(lambda s: s.update(mode="live")), "live_mode_forbidden")
        self.assert_reject(self.mutated(lambda s: s["session"].update(date="2026-10-15")), "session_ineligible")
        self.assert_reject(self.mutated(lambda s: s["quote"].update(ask="6.40")), "spread_too_wide")
        self.assert_reject(self.mutated(lambda s: s.update(last_trade=["x"])), "invalid_trade")
        self.assert_reject(self.mutated(lambda s: s["last_trade"].update(symbol="ELSE")), "trade_symbol_mismatch")
        self.assert_reject(self.mutated(lambda s: s["last_trade"].update(timestamp="2026-10-16T13:35:02+00:00")),
                           "future_or_invalid_trade")
        self.assert_reject(self.mutated(lambda s: s["last_trade"].update(timestamp="2026-10-16T13:34:58+00:00")),
                           "stale_trade")

    def test_fact_age_rejects(self):
        self.assert_reject(self.mutated(lambda s: s.update(facts=["x"])), "invalid_facts")
        self.assert_reject(self.mutated(lambda s: s["facts"].update(
            float_effective_at="2026-10-01T12:00:00+00:00", float_known_at="2026-10-01T12:00:01+00:00")), "float_too_old")
        self.assert_reject(self.mutated(lambda s: s["facts"].update(
            catalyst_published_at="2026-10-15T12:00:00+00:00", catalyst_known_at="2026-10-15T12:00:01+00:00")),
            "catalyst_too_old")
        result = self.assert_reject(self.mutated(lambda s: s["rvol"].update(current_volume=100000)), "rvol_below_threshold")
        self.assertEqual(Decimal(str(result.metrics["rvol"])), Decimal("1"))

    def test_bar_sequence_rejects(self):
        self.assert_reject(self.mutated(lambda s: s.update(bars=s["bars"][:4])), "insufficient_completed_bars")

        def previous_day(s):
            start = datetime.fromisoformat(s["bars"][0]["start"]) - timedelta(days=1)
            s["bars"][0].update(start=start.isoformat(), available_at=(start + timedelta(minutes=1)).isoformat())
        self.assert_reject(self.mutated(previous_day), "bar_outside_session")

        def gap(s):
            start = datetime.fromisoformat(s["bars"][0]["start"]) - timedelta(minutes=1)
            s["bars"][0].update(start=start.isoformat(), available_at=(start + timedelta(minutes=1)).isoformat())
        self.assert_reject(self.mutated(gap), "noncontiguous_bars")
        self.assert_reject(self.mutated(lambda s: s["quote"].update(timestamp="2026-10-16T13:34:59.500000+00:00")),
                           "quote_before_trigger_bar")

    def test_pattern_shape_rejects(self):
        self.assert_reject(self.mutated(lambda s: s["bars"][-1].update(close="6.29")), "pullback_length_invalid")
        self.assert_reject(self.mutated(lambda s: s["bars"][2].update(open="6.47", close="6.10")), "insufficient_impulse_bars")
        self.assert_reject(self.mutated(lambda s: s["bars"][0].update(open="5.89", close="5.70")), "impulse_not_rising")
        self.assert_reject(self.mutated(lambda s: s["bars"][0].update(close="5.72")), "impulse_body_too_small")
        self.assert_reject(self.mutated(lambda s: s["bars"][1].update(open="5.70", high="5.90", low="5.69", close="5.90")),
                           "impulse_not_rising")
        self.assert_reject(valid_snapshot(), "impulse_too_small", replace(experimental(), min_impulse_gain_pct=Decimal("50")))
        self.assert_reject(self.mutated(lambda s: s["bars"][3].update(high="6.55")), "pullback_shape_invalid")
        self.assert_reject(valid_snapshot(), "impulse_volume_too_low", replace(experimental(), min_impulse_mean_volume=100_000))

    def test_breakout_and_entry_chase_rejects(self):
        result = self.assert_reject(self.mutated(lambda s: s["last_trade"].update(price="6.29")), "waiting_for_breakout")
        self.assertEqual(result.metrics["trigger"], Decimal("6.30"))
        self.assert_reject(self.mutated(lambda s: s["quote"].update(ask="6.34")), "entry_extended_or_inconsistent")


class ExitBranchTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = valid_snapshot()
        self.position = {"symbol": "DEMO", "mode": "paper", "qty": 120,
                         "entry_price": "6.31", "stop_price": "6.22", "target_price": "6.49",
                         "opened_at": "2026-10-16T13:35:00Z"}

    def test_invalid_position_risk_and_force_reason(self):
        self.assertEqual(evaluate_exit(["DEMO"], self.snapshot).reasons, ("invalid_position",))
        bad = dict(self.position, stop_price="6.40")
        self.assertEqual(evaluate_exit(bad, self.snapshot).reasons, ("invalid_position_risk",))
        future = dict(self.position, opened_at="2026-10-16T13:36:00Z")
        self.assertEqual(evaluate_exit(future, self.snapshot).reasons, ("invalid_position_risk",))
        for reason in ("", "   ", 7):
            with self.subTest(reason=reason):
                result = evaluate_exit(self.position, self.snapshot, force_reason=reason)
                self.assertEqual(result.reasons, ("invalid_force_reason",))
                self.assertIsNone(result.intent)

    def test_overnight_position_exits_at_session_end_with_paper_intent(self):
        position = dict(self.position, opened_at="2026-10-15T13:35:00Z")
        result = evaluate_exit(position, self.snapshot)
        self.assertTrue(result.accepted)
        self.assertEqual(result.reasons, ("session_end",))
        self.assertEqual(result.intent["mode"], "paper")
        self.assertEqual(result.intent["side"], "sell")
        self.assertTrue(result.metrics["fill_confirmation_required"])


class WorkerConfigTests(unittest.TestCase):
    def test_flag_parsing(self):
        self.assertFalse(flag(None, "X"))
        self.assertFalse(flag("", "X"))
        self.assertTrue(flag("TRUE", "X"))
        self.assertTrue(flag("1", "X"))
        self.assertFalse(flag("0", "X"))
        with self.assertRaises(ConfigError):
            flag("yes", "X")

    def test_constructor_rejects_out_of_range_values(self):
        for changes in ({"mode": "live"}, {"data_feed": "otc"}, {"watchlist": ("aapl",)},
                        {"watchlist": tuple(f"S{i}" for i in range(11))},
                        {"port": 0}, {"journal_sample_seconds": 3601}, {"journal_min_free_mb": -1},
                        {"poll_seconds": 0.5}, {"quote_max_age_seconds": 0}, {"order_ttl_seconds": 301},
                        {"demo_equity": "-1"}, {"cors_origin": "*"}, {"accounting_cost_policy": "free"},
                        {"accounting_history_start": "2026-10-01T00:00:00"},
                        {"accounting_baseline_path": "/tmp/b.json"},
                        {"strategy_overrides": {"mode": "live"}}, {"risk_overrides": {"bogus": 1}},
                        {"crypto_watchlist": ("DOGE/USD",)}):
            with self.subTest(changes=changes):
                with self.assertRaises(ConfigError):
                    Config(**changes)

    def test_from_env_json_and_numeric_errors(self):
        with self.assertRaises(ConfigError):
            Config.from_env({"STRATEGY_CONFIG_JSON": "{nope"})
        with self.assertRaises(ConfigError):
            Config.from_env({"RISK_CONFIG_JSON": "[1, 2]"})
        with self.assertRaises(ConfigError) as ctx:
            Config.from_env({"PORT": "eighty"})
        self.assertIn("numeric", str(ctx.exception))
        with self.assertRaises(ConfigError) as ctx:
            Config.from_env({"PAPER_TRADING_ENABLED": "maybe"})
        self.assertIn("PAPER_TRADING_ENABLED", str(ctx.exception))

    def test_defaults_stay_demo_and_never_support_live(self):
        cfg = Config.from_env({})
        self.assertEqual(cfg.mode, "demo")
        self.assertFalse(cfg.paper_enabled)
        public = cfg.public()
        self.assertFalse(public["live_execution_supported"])
        self.assertIn("demo_mode", public["paper_blockers"])
        self.assertNotIn("api_key", repr(cfg).replace("api_key=", ""))


class ModelContractTests(unittest.TestCase):
    def bar(self, **changes):
        start = datetime(2026, 10, 16, 13, 30, tzinfo=timezone.utc)
        value = {"start": start.isoformat(), "open": "1.00", "high": "1.10", "low": "0.95", "close": "1.05",
                 "volume": 10, "feed": "iex", "complete": True}
        value.update(changes)
        return value

    def code(self, fn, *args, **kwargs):
        with self.assertRaises(InputError) as ctx:
            fn(*args, **kwargs)
        return ctx.exception.code

    def test_numbers_timestamps_and_dates(self):
        self.assertEqual(self.code(decimal_value, "abc", "x"), "missing_or_invalid_number")
        self.assertEqual(self.code(decimal_value, True, "x"), "missing_or_invalid_number")
        self.assertEqual(self.code(decimal_value, "1e19", "x"), "nonfinite_or_out_of_range_number")
        self.assertEqual(self.code(timestamp, "2026-10-16T09:30:00"), "naive_timestamp")
        self.assertEqual(self.code(timestamp, None), "missing_or_invalid_timestamp")
        self.assertEqual(self.code(session_date, datetime(2026, 10, 16, tzinfo=timezone.utc)), "invalid_session_date")
        self.assertEqual(self.code(session_date, "16/10/2026"), "invalid_session_date")
        self.assertEqual(session_date(date(2026, 10, 16)), date(2026, 10, 16))

    def test_bar_contract(self):
        self.assertEqual(Bar.from_dict(self.bar(), "iex").end, datetime(2026, 10, 16, 13, 31, tzinfo=timezone.utc))
        self.assertEqual(self.code(Bar.from_dict, ["x"], "iex"), "invalid_bar")
        self.assertEqual(self.code(Bar.from_dict, self.bar(complete=False), "iex"), "bar_not_complete")
        self.assertEqual(self.code(Bar.from_dict, self.bar(start="2026-10-16T13:30:05+00:00"), "iex"), "bar_not_minute_aligned")
        self.assertEqual(self.code(Bar.from_dict, self.bar(feed="sip"), "iex"), "mixed_feed")
        self.assertEqual(self.code(Bar.from_dict, self.bar(high="1.00"), "iex"), "invalid_ohlc")
        self.assertEqual(self.code(Bar.from_dict, self.bar(available_at="2026-10-16T13:30:59+00:00"), "iex"),
                         "bar_available_before_close")

    def test_quote_contract(self):
        now = "2026-10-16T13:30:00+00:00"
        good = {"bid": "1.00", "ask": "1.01", "timestamp": now, "received_at": now, "feed": "iex"}
        self.assertEqual(self.code(Quote.from_dict, ["x"]), "invalid_quote")
        self.assertEqual(self.code(Quote.from_dict, dict(good, bid="1.02")), "crossed_quote")
        self.assertEqual(self.code(Quote.from_dict, dict(good, received_at="2026-10-16T13:29:59+00:00")),
                         "quote_received_before_event")
        self.assertEqual(self.code(Quote.from_dict, dict(good, feed="")), "missing_feed")
        self.assertEqual(self.code(Quote.from_dict, good, "sip"), "mixed_feed")
        quote = Quote.from_dict(dict(good, timestamp="2026-10-16T13:29:59+00:00"))
        moment = datetime(2026, 10, 16, 13, 30, 1, tzinfo=timezone.utc)
        self.assertEqual(self.code(quote.check_time, moment, Decimal("1.5")), "stale_quote")
        self.assertEqual(self.code(quote.check_time, moment - timedelta(seconds=2), Decimal("5")), "future_quote")
        late = Quote.from_dict(dict(good, timestamp="2026-10-16T13:29:59+00:00"))
        late = replace(late, timestamp=moment - timedelta(seconds=0.5), received_at=moment - timedelta(seconds=3))
        self.assertEqual(self.code(late.check_time, moment, Decimal("2")), "stale_quote")


if __name__ == "__main__":
    unittest.main()
