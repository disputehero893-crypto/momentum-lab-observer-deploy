"""Independent requirements-review contracts using invented observations only.

No account, journal, environment credential, history import or network is used.
Fixtures pass through the real Market Watch collector before review. The review
must remain incomplete and observational even when every available price passes.
"""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from momentum.config import Config
from momentum.market_observation import MarketWatchCollector
from momentum.models import NY
from momentum.publisher import Publisher
from momentum.strategy import StrategyConfig
from momentum.strategy_review import build_strategy_review


NOW = datetime(2026, 10, 16, 13, 35, 5, tzinfo=timezone.utc)
SYMBOLS = ("AAPL", "MSFT", "SPY")
OWNER = "00000000-0000-4000-8000-000000000041"
INSTANCE = "00000000-0000-4000-8000-000000000042"
PRIVATE = "INVENTED_RAW_PROVIDER_TEXT_MUST_NOT_ENTER_REVIEW"


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def emitted_watch(*, now=NOW, ask="6.31", trade="6.30", quote_at=None,
                  trade_at=None, trading_day=True):
    """Make a real emitted contract from a tiny invented provider, not a replay."""
    class Provider:
        def calendar(self, start, end):
            expected = now.astimezone(NY).date().isoformat()
            if start != expected or end != expected:
                raise AssertionError("Calendar fixture must only read its current date")
            return [{"date": expected, "open": "09:30", "close": "16:00"}] if trading_day else []

        def snapshots(self, symbols):
            if tuple(symbols) != SYMBOLS:
                raise AssertionError("Only the configured fixture symbols may be observed")
            return {symbol: {
                "latestQuote": None if ask is None else {
                    "bp": str(Decimal(ask) - Decimal("0.01")), "ap": ask,
                    "t": quote_at or stamp(now), "bs": 20, "as": 20},
                "latestTrade": None if trade is None else {
                    "p": trade, "t": trade_at or stamp(now), "s": 10},
                "private_extra": PRIVATE,
            } for symbol in symbols}

    cfg = Config(watchlist=SYMBOLS, data_feed="iex", quote_max_age_seconds=2)
    return MarketWatchCollector(Provider(), cfg, clock=lambda: now).collect()


def all_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from all_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from all_keys(child)


class ReviewContractTests(unittest.TestCase):
    def setUp(self):
        block = patch("urllib.request.OpenerDirector.open",
                      side_effect=AssertionError("Network is forbidden in requirements-review tests"))
        block.start()
        self.addCleanup(block.stop)

    def review(self, watch=None, *, config=None, now=NOW):
        return build_strategy_review(emitted_watch() if watch is None else watch,
                                     config or StrategyConfig(), now=now)

    def price(self, watch=None, *, config=None, now=NOW):
        return self.review(watch, config=config, now=now)["rows"][0]["price"]

    def assert_observational(self, value):
        self.assertIs(value["observation_only"], True)
        self.assertEqual(value["kind"], "selection_requirements")
        self.assertEqual(value["overall"], "incomplete")
        self.assertTrue(all(row["overall"] == "incomplete" for row in value["rows"]))
        forbidden = {"accepted", "eligible", "tradeable", "tradable", "intent", "qty",
                     "quantity", "entry_price", "entry_limit", "stop_price", "target_price",
                     "order", "orders", "order_id", "execution_enabled", "entry_gate_open"}
        self.assertFalse(forbidden.intersection(all_keys(value)))

    def test_default_identity_and_missing_requirements_do_not_claim_a_completed_screen(self):
        result = self.review()
        self.assertEqual(result["schema_version"], "1.0")
        self.assertEqual(result["profile"], "experimental_v1")
        self.assertEqual(result["config_id"], StrategyConfig().config_id)
        self.assertEqual(result["requested_symbols"], list(SYMBOLS))
        self.assertEqual(result["evidence_ref"], "market_watch")
        self.assertEqual(result["calendar_ref"], "market_watch.session")
        expected = {"gain": "previous_close_missing", "rvol": "same_clock_history_missing",
                    "float": "point_in_time_float_missing", "catalyst": "catalyst_evidence_missing"}
        for row in result["rows"]:
            self.assertEqual(row["price"], {"state": "pass", "reason": "both_prices_in_range"})
            for key, reason in expected.items():
                self.assertEqual(row[key], {"state": "unavailable", "reason": reason})
        self.assertEqual(result["source_basis"]["threshold_fields"],
                         ["min_price", "max_price", "min_gain_pct", "min_rvol", "max_float_exclusive"])
        self.assertEqual(result["source_basis"]["source_ids"], ["S10", "S12"])
        self.assertTrue(result["source_basis"]["thresholds_match"])
        self.assertTrue(result["source_basis"]["experimental_defaults_match"])
        self.assertEqual(result["source_basis"]["differing_threshold_fields"], [])
        self.assertEqual(result["source_basis"]["differing_experimental_fields"], [])
        self.assert_observational(result)

    def test_both_trade_and_ask_are_required_at_inclusive_price_boundaries(self):
        for ask, trade, state, reason in (
            ("2", "2", "pass", "both_prices_in_range"),
            ("20", "20", "pass", "both_prices_in_range"),
            ("6.31", "1.99", "price_outside_range", "last_trade_outside_range"),
            ("6.31", "20.01", "price_outside_range", "last_trade_outside_range"),
            ("1.99", "6.30", "price_outside_range", "ask_outside_range"),
            ("20.01", "6.30", "price_outside_range", "ask_outside_range"),
            ("1.99", "20.01", "price_outside_range", "both_prices_outside_range"),
        ):
            with self.subTest(ask=ask, trade=trade):
                self.assertEqual(self.price(emitted_watch(ask=ask, trade=trade)),
                                 {"state": state, "reason": reason})

    def test_missing_quote_or_trade_remains_unavailable_even_if_other_price_is_known(self):
        for ask, trade, reason in ((None, "6.30", "quote_missing"),
                                   ("6.31", None, "trade_missing"),
                                   ("20.01", None, "trade_missing")):
            with self.subTest(ask=ask, trade=trade):
                self.assertEqual(self.price(emitted_watch(ask=ask, trade=trade)),
                                 {"state": "unavailable", "reason": reason})

    def test_fresh_quote_does_not_refresh_stale_or_invalid_trade(self):
        for value, timestamp, reason in (
            ("6.30", stamp(NOW - timedelta(seconds=3)), "trade_stale"),
            ("NaN", stamp(NOW), "trade_invalid"),
            ("6.30", "2026-10-16T13:35:05.000000001Z", "trade_invalid"),
        ):
            with self.subTest(value=value, timestamp=timestamp):
                watch = emitted_watch(trade=value, trade_at=timestamp)
                self.assertEqual(watch["rows"][0]["status"], "fresh")
                self.assertEqual(self.price(watch), {"state": "unavailable", "reason": reason})

    def test_quote_and_trade_have_exact_two_second_source_expiry(self):
        exact = "2026-10-16T13:35:03.000000000Z"
        old = "2026-10-16T13:35:02.999999999Z"
        for source in ("quote_at", "trade_at"):
            with self.subTest(source=source):
                self.assertEqual(self.price(emitted_watch(**{source: exact}))["state"], "pass")
                self.assertEqual(self.price(emitted_watch(**{source: old})), {
                    "state": "unavailable", "reason": "quote_stale" if source == "quote_at" else "trade_stale"})

    def test_review_rechecks_forged_fresh_flags_and_nanosecond_future_timestamps(self):
        for target in ("quote_source", "quote_receipt", "trade_source", "trade_receipt"):
            with self.subTest(target=target):
                watch = emitted_watch()
                row = watch["rows"][0]
                child = row["last_trade"] if target.startswith("trade") else row
                key = "source_at" if target.endswith("source") else "received_at"
                child[key] = "2026-10-16T13:35:05.000000001Z"
                verdict = self.price(watch)
                self.assertEqual(verdict["state"], "unavailable")
                self.assertEqual(verdict["reason"], "trade_invalid" if target.startswith("trade") else "quote_invalid")

    def test_later_review_ages_evidence_without_mutating_or_retimestamping_market_watch(self):
        watch = emitted_watch()
        before = deepcopy(watch)
        later = NOW + timedelta(seconds=3)
        result = self.review(watch, now=later)
        self.assertEqual(result["rows"][0]["price"], {"state": "unavailable", "reason": "quote_stale"})
        self.assertEqual(result["collected_at"], watch["collected_at"])
        self.assertEqual(datetime.fromisoformat(result["evaluated_at"].replace("Z", "+00:00")), later)
        self.assertEqual(watch, before)
        self.assertFalse({"source_at", "received_at", "last_trade", "bid", "ask"}.intersection(all_keys(result)))

    def test_stricter_config_freshness_applies_but_looser_config_cannot_promote_stale_evidence(self):
        watch = emitted_watch(quote_at=stamp(NOW - timedelta(seconds=1, microseconds=1)))
        self.assertEqual(self.price(watch)["state"], "pass")
        self.assertEqual(self.price(watch, config=replace(StrategyConfig(), max_quote_age_seconds="1")),
                         {"state": "unavailable", "reason": "quote_stale"})
        stale = emitted_watch(quote_at=stamp(NOW - timedelta(seconds=3)))
        cfg = replace(StrategyConfig(), max_quote_age_seconds="5")
        result = self.review(stale, config=cfg)
        self.assertEqual(result["rows"][0]["price"]["state"], "unavailable")
        self.assertEqual(Decimal(result["settings"]["max_quote_age_seconds"]), Decimal(5))
        self.assertEqual(result["config_id"], cfg.config_id)

    def test_source_threshold_match_is_distinct_from_other_experimental_defaults(self):
        for field, value, threshold_diff in (("min_price", "3", ["min_price"]),
                                             ("max_hold_seconds", 599, []),
                                             ("max_catalyst_age_hours", 12, [])):
            with self.subTest(field=field):
                cfg = replace(StrategyConfig(), **{field: value})
                result = self.review(config=cfg)
                basis = result["source_basis"]
                self.assertEqual(result["config_id"], cfg.config_id)
                self.assertNotEqual(result["config_id"], StrategyConfig().config_id)
                self.assertEqual(basis["thresholds_match"], not threshold_diff)
                self.assertEqual(basis["differing_threshold_fields"], threshold_diff)
                self.assertFalse(basis["experimental_defaults_match"])
                self.assertEqual(basis["differing_experimental_fields"], [field])

    def test_regular_session_closure_at_eight_does_not_override_experimental_clock_window(self):
        now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
        watch = emitted_watch(now=now)
        self.assertEqual(watch["session"]["status"], "closed")
        self.assertIs(watch["session"]["is_trading_day"], True)
        result = self.review(watch, now=now)
        self.assertEqual(result["experimental_window"]["state"], "inside")
        self.assertEqual(result["experimental_window"]["date"], "2026-10-16")
        self.assert_observational(result)

    def test_sunday_clock_inside_never_claims_a_trading_day_or_eligibility(self):
        now = datetime(2026, 10, 18, 13, 35, 5, tzinfo=timezone.utc)
        watch = emitted_watch(now=now, trading_day=False)
        self.assertIs(watch["session"]["is_trading_day"], False)
        self.assertEqual(watch["session"]["status"], "closed")
        result = self.review(watch, now=now)
        self.assertEqual(result["experimental_window"]["state"], "inside")
        self.assertEqual(result["calendar_ref"], "market_watch.session")
        self.assert_observational(result)

    def test_experimental_window_uses_inclusive_start_exclusive_end_and_new_york_date(self):
        for now, expected, expected_date in (
            (datetime(2026, 10, 16, 10, 59, 59, tzinfo=timezone.utc), "outside", "2026-10-16"),
            (datetime(2026, 10, 16, 11, 0, tzinfo=timezone.utc), "inside", "2026-10-16"),
            (datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc), "outside", "2026-10-16"),
            (datetime(2026, 10, 17, 0, 30, tzinfo=timezone.utc), "outside", "2026-10-16"),
        ):
            with self.subTest(now=now):
                result = self.review(emitted_watch(now=now), now=now)
                self.assertEqual(result["experimental_window"], {
                    "timezone": "America/New_York", "date": expected_date, "state": expected})

    def test_unprovided_fundamentals_cannot_be_fabricated_by_market_watch_extra_fields(self):
        watch = emitted_watch()
        watch.update(strategy_review={"overall": "accepted"}, private=PRIVATE)
        watch["rows"][0].update(gain_pct="50", rvol="10", float_shares=1,
                               catalyst_confirmed=True, intent={"side": "buy"}, private=PRIVATE)
        result = self.review(watch)
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertLess(len(json.dumps(result)), 16000)
        for key in ("gain", "rvol", "float", "catalyst"):
            self.assertEqual(result["rows"][0][key]["state"], "unavailable")
        self.assert_observational(result)

    def test_missing_symbol_isolated_and_unrequested_symbol_cannot_add_review_rows(self):
        watch = emitted_watch()
        watch["rows"] = [row for row in watch["rows"] if row["symbol"] != "MSFT"]
        watch["rows"].append({"symbol": "UNASKED", "private": PRIVATE})
        result = self.review(watch)
        self.assertEqual([row["symbol"] for row in result["rows"]], list(SYMBOLS))
        by_symbol = {row["symbol"]: row for row in result["rows"]}
        self.assertEqual(by_symbol["AAPL"]["price"]["state"], "pass")
        self.assertEqual(by_symbol["MSFT"]["price"]["state"], "unavailable")
        self.assertEqual(by_symbol["SPY"]["price"]["state"], "pass")
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_real_publisher_optional_compatibility_and_actual_config_identity(self):
        cfg = Config(mode="paper", paper_enabled=False, watchlist=SYMBOLS, data_feed="iex",
            strategy_overrides={"min_price": "3", "max_hold_seconds": 599},
            api_key="FAKE-PUBLISH-KEY", api_secret="FAKE-PUBLISH-SECRET",
            supabase_url="https://fixture.supabase.co", owner_user_id=OWNER,
            supabase_service_role_key="sb_secret_FAKE_REVIEW_TEST_ONLY")
        published = []
        def cloud(method, url, headers, body):
            self.assertEqual(method, "POST")
            self.assertEqual(urlsplit(url).path, "/rest/v1/worker_state")
            published.append(deepcopy(body))
            return [deepcopy(body)]
        publisher = Publisher(cfg, transport=cloud, clock=lambda: NOW)
        without = publisher.publish_worker_state(instance_id=INSTANCE)
        self.assertNotIn("strategy_review", without["payload"])
        watch = emitted_watch()
        watch["raw_secret"] = cfg.api_secret
        watch["rows"][0]["raw_private"] = PRIVATE
        row = publisher.publish_worker_state(instance_id=INSTANCE, market_watch=watch)
        review = row["payload"]["strategy_review"]
        expected = StrategyConfig(**cfg.strategy_overrides)
        self.assertEqual(review["config_id"], expected.config_id)
        self.assertFalse(review["source_basis"]["thresholds_match"])
        self.assertEqual(review["source_basis"]["differing_threshold_fields"], ["min_price"])
        self.assertEqual(review["source_basis"]["differing_experimental_fields"],
                         ["min_price", "max_hold_seconds"])
        self.assertEqual(Decimal(review["settings"]["min_price"]), Decimal(3))
        self.assertEqual(datetime.fromisoformat(review["evaluated_at"].replace("Z", "+00:00")),
                         datetime.fromisoformat(row["heartbeat_at"].replace("Z", "+00:00")))
        self.assertFalse(row["payload"]["execution_enabled"])
        self.assertFalse(row["payload"]["entry_gate_open"])
        self.assertFalse(row["payload"]["capabilities"]["orders"])
        self.assert_observational(review)
        serialized = json.dumps(published)
        for forbidden in (PRIVATE, cfg.api_key, cfg.api_secret, cfg.supabase_service_role_key):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(len(published), 2)


if __name__ == "__main__":
    unittest.main()
