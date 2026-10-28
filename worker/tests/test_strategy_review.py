"""Pure selection projection and publisher wiring with invented observations."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import localcontext
import json
import unittest
from unittest.mock import patch

from momentum.config import Config
from momentum.market_observation import MarketWatchCollector
from momentum.publisher import Publisher, build_worker_state
from momentum.strategy import StrategyConfig
from momentum.strategy_review import build_strategy_review


NOW = datetime(2026, 10, 16, 12, 0, 0, 654321, tzinfo=timezone.utc)
OWNER = "00000000-0000-4000-8000-000000000091"
CONFIG = Config(watchlist=("DEMO-A",), data_feed="iex")


class InventedProvider:
    def calendar(self, start, end):
        return [{"date": start, "open": "09:30", "close": "16:00"}]

    def snapshots(self, symbols):
        return {symbol: {"latestQuote": {"bp": "6.29", "ap": "6.31", "t": "2026-10-16T12:00:00.654320999Z"},
                         "latestTrade": {"p": "6.30", "t": "2026-10-16T12:00:00.654320998Z"}}
                for symbol in symbols}


def invented_watch():
    return MarketWatchCollector(InventedProvider(), CONFIG, clock=lambda: NOW).collect()


def keys_in(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(keys_in(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(keys_in(child) for child in value))
    return set()


class StrategyReviewTests(unittest.TestCase):
    def test_fractional_nanosecond_limit_never_rounds_up_for_quote_or_trade(self):
        limit = "1.000000000999999999999999999999999999"
        for precision in (8, 28, 50):
            with localcontext() as context:
                context.prec = precision
                cfg = StrategyConfig(max_quote_age_seconds=limit)
                for evidence in ("quote", "trade"):
                    for source, expected in (
                        ("2026-10-16T11:59:59.654321000Z", {"state": "pass", "reason": "both_prices_in_range"}),
                        ("2026-10-16T11:59:59.654320999Z", {"state": "unavailable", "reason": evidence + "_stale"}),
                    ):
                        with self.subTest(precision=precision, evidence=evidence, source=source):
                            watch = invented_watch()
                            observed = watch["rows"][0] if evidence == "quote" else watch["rows"][0]["last_trade"]
                            observed["source_at"] = observed["received_at"] = source
                            before = json.dumps(watch, sort_keys=True)
                            review = build_strategy_review(watch, cfg, now=NOW)
                            self.assertEqual(review["rows"][0]["price"], expected)
                            self.assertEqual(review["settings"]["max_quote_age_seconds"], limit)
                            self.assertEqual(review["config_id"], cfg.config_id)
                            self.assertEqual(json.dumps(watch, sort_keys=True), before)

    def test_subnanosecond_and_tiny_exponent_limits_allow_only_zero_age(self):
        for limit in ("0.000000000999999999999999999999999999", "1E-1000000000"):
            with localcontext():
                cfg = StrategyConfig(max_quote_age_seconds=limit)
                watch = invented_watch()
                row = watch["rows"][0]
                row["source_at"] = row["received_at"] = "2026-10-16T12:00:00.654321000Z"
                row["last_trade"]["source_at"] = row["last_trade"]["received_at"] = row["source_at"]
                self.assertEqual(build_strategy_review(watch, cfg, now=NOW)["rows"][0]["price"],
                                 {"state": "pass", "reason": "both_prices_in_range"})
                for evidence in ("quote", "trade"):
                    with self.subTest(limit=limit, evidence=evidence):
                        aged = deepcopy(watch)
                        observed = aged["rows"][0] if evidence == "quote" else aged["rows"][0]["last_trade"]
                        observed["source_at"] = "2026-10-16T12:00:00.654320999Z"
                        review = build_strategy_review(aged, cfg, now=NOW)
                        self.assertEqual(review["rows"][0]["price"],
                                         {"state": "unavailable", "reason": evidence + "_stale"})

    def test_projection_keeps_original_market_watch_bytes_and_has_no_engine_or_risk_call(self):
        watch = invented_watch()
        original = json.dumps(watch, sort_keys=True)
        with patch("momentum.strategy.evaluate_setup", side_effect=AssertionError("setup engine must not run")), \
                patch("momentum.risk.RiskManager.assess", side_effect=AssertionError("risk engine must not run")), \
                patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network must not run")):
            review = build_strategy_review(watch, StrategyConfig(), now=NOW)
        self.assertEqual(json.dumps(watch, sort_keys=True), original)
        self.assertEqual(review["collected_at"], watch["collected_at"])
        self.assertEqual(review["rows"][0]["price"]["state"], "pass")
        self.assertEqual(review["overall"], "incomplete")
        self.assertEqual(review["evidence_ref"], "market_watch")
        self.assertEqual(review["calendar_ref"], "market_watch.session")
        self.assertFalse(keys_in(review) & {"accepted", "intent", "qty", "entry", "entry_limit",
                                            "order_eligible", "engine_decision", "bid", "ask",
                                            "source_at", "received_at", "last_trade"})

    def test_every_published_setting_comes_from_current_validated_config(self):
        cfg = StrategyConfig(min_price="1.25", max_price="15", min_gain_pct="12", min_rvol="6",
                             max_float_exclusive=5_000_000, max_float_age_days=4,
                             max_catalyst_age_hours=12, max_quote_age_seconds="1.25",
                             trade_start_et="08:15", trade_end_et="09:45")
        review = build_strategy_review(invented_watch(), cfg, now=NOW)
        self.assertEqual(review["config_id"], cfg.config_id)
        self.assertEqual(review["settings"], {
            "min_price": "1.25", "max_price": "15", "min_gain_pct": "12", "min_rvol": "6",
            "max_float_exclusive": 5_000_000, "rvol_sessions": 30, "rvol_session_start_et": "04:00",
            "max_float_age_days": 4, "max_catalyst_age_hours": 12, "catalyst_required": True,
            "max_quote_age_seconds": "1.25", "trade_start_et": "08:15", "trade_end_et": "09:45"})
        self.assertEqual(review["experimental_window"]["state"], "outside")
        self.assertFalse(review["source_basis"]["thresholds_match"])
        self.assertEqual(review["source_basis"]["differing_threshold_fields"],
                         ["min_price", "max_price", "min_gain_pct", "min_rvol", "max_float_exclusive"])

    def test_pattern_change_is_distinct_from_matching_numeric_instructional_thresholds(self):
        cfg = StrategyConfig(target_r="3", max_entry_chase="0.02")
        review = build_strategy_review(invented_watch(), cfg, now=NOW)
        self.assertTrue(review["source_basis"]["thresholds_match"])
        self.assertEqual(review["source_basis"]["differing_threshold_fields"], [])
        self.assertFalse(review["source_basis"]["experimental_defaults_match"])
        self.assertEqual(review["source_basis"]["differing_experimental_fields"], ["max_entry_chase", "target_r"])
        self.assertEqual(review["config_id"], cfg.config_id)

    def test_publisher_derives_report_without_accepting_an_upstream_review(self):
        watch = invented_watch()
        watch["strategy_review"] = {"accepted": True, "intent": "INVENTED_PRIVATE_SENTINEL"}
        watch["config_id"] = "INVENTED_PRIVATE_SENTINEL"
        cfg = replace(CONFIG, supabase_url="https://fixture.supabase.co",
                      supabase_service_role_key="sb_secret_INVENTED_TEST_ONLY", owner_user_id=OWNER,
                      strategy_overrides={"min_price": "2", "max_price": "5"})
        calls = []
        def cloud(method, url, headers, payload):
            calls.append((method, url, deepcopy(payload)))
            return [deepcopy(payload)]
        publisher = Publisher(cfg, transport=cloud, clock=lambda: NOW)
        row = publisher.publish_worker_state(instance_id="requirements-test", market_watch=watch)
        review = row["payload"]["strategy_review"]
        self.assertEqual(review["config_id"], StrategyConfig(**cfg.strategy_overrides).config_id)
        self.assertEqual(review["evaluated_at"], row["heartbeat_at"])
        self.assertEqual(review["rows"][0]["price"],
                         {"state": "price_outside_range", "reason": "both_prices_outside_range"})
        self.assertEqual(row["owner_id"], OWNER)
        self.assertEqual(row["payload"]["market_watch"]["rows"][0]["source_at"],
                         "2026-10-16T12:00:00.654320999Z")
        self.assertNotIn("INVENTED_PRIVATE_SENTINEL", json.dumps(row))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].startswith("https://fixture.supabase.co/rest/v1/worker_state?"))
        self.assertFalse(row["payload"]["execution_enabled"])
        self.assertFalse(row["payload"]["capabilities"]["orders"])

    def test_absent_market_watch_omits_review_and_invalid_config_cannot_be_published(self):
        row = build_worker_state(OWNER, "requirements-test", now=NOW)
        self.assertNotIn("market_watch", row["payload"])
        self.assertNotIn("strategy_review", row["payload"])
        with self.assertRaises(ValueError):
            build_strategy_review(invented_watch(), {"min_price": "0"}, now=NOW)
        cfg = StrategyConfig()
        object.__setattr__(cfg, "min_price", "-1")
        with self.assertRaises(ValueError):
            build_strategy_review(invented_watch(), cfg, now=NOW)


if __name__ == "__main__":
    unittest.main()
