"""Cross-component tests for opt-in OANDA practice forex observation.

All prices, accounts, and cloud responses are invented. No OANDA network calls.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import tempfile
import unittest

from momentum.config import Config, ConfigError
from momentum.forex_observation import ForexWatchCollector, build_forex_watch
from momentum.journal import Journal
from momentum.publisher import Publisher
from test_worker_observation import Fixture

PAIRS = ("EUR/USD", "GBP/USD", "USD/JPY")
OWNER = "9d59c43f-1d43-494e-afd9-1f73edabb444"
NOW = datetime(2026, 11, 24, 15, 35, 5, tzinfo=timezone.utc)
TOKEN = "FAKE_PRACTICE_TOKEN_NOT_REAL"
ACCOUNT = "001-001-1234567-001"


def timestamp(now):
    return now.isoformat().replace("+00:00", "Z")


def quote_report(now=NOW, pairs=PAIRS):
    source = now - timedelta(milliseconds=10)
    received = now - timedelta(milliseconds=1)
    raw = {"prices": [
        {"instrument": pair.replace("/", "_"), "tradeable": True, "time": timestamp(source),
         "bids": [{"price": "1.08321" if pair != "USD/JPY" else "155.11"}],
         "asks": [{"price": "1.08322" if pair != "USD/JPY" else "155.13"}]}
        for pair in pairs]}
    return build_forex_watch(raw, pairs, received_at=timestamp(received),
                             collected_at=timestamp(now), max_age_seconds=60)


class MockForexProvider:
    def __init__(self, venue):
        self.venue = venue
        self.reads = 0

    def pricing(self, pairs):
        self.reads += 1
        source = timestamp(self.venue.now - timedelta(milliseconds=10))
        return {"prices": [
            {"instrument": pair.replace("/", "_"), "tradeable": True, "time": source,
             "bids": [{"price": "1.08321" if pair != "USD/JPY" else "155.11"}],
             "asks": [{"price": "1.08322" if pair != "USD/JPY" else "155.13"}]}
            for pair in pairs]}


class TestForexConfig(unittest.TestCase):
    def test_opt_in_default_disabled_and_hash_stable(self):
        config = Config.from_env({})
        self.assertEqual(config.forex_watchlist, ())
        self.assertEqual(config.public()["forex_watchlist"], [])
        self.assertFalse(config.public()["oanda_practice_configured"])
        filled = Config.from_env({"WATCHLIST": "AAPL,MSFT",
                                  "OANDA_FOREX_WATCHLIST": " EUR_USD , gbp_usd, USD/JPY ",
                                  "OANDA_PRACTICE_API_TOKEN": TOKEN,
                                  "OANDA_PRACTICE_ACCOUNT_ID": ACCOUNT})
        self.assertEqual(filled.watchlist, ("AAPL", "MSFT"))
        self.assertEqual(filled.forex_watchlist, PAIRS)
        self.assertFalse(filled.paper_enabled)
        stock_only = Config.from_env({"WATCHLIST": "AAPL,MSFT"})
        self.assertEqual(filled.review_hash(), stock_only.review_hash())
        self.assertNotIn(TOKEN, repr(filled))
        self.assertNotIn(TOKEN, str(filled.public()))

    def test_invalid_forex_watchlists_fail_closed(self):
        for value in ("AAPL", "BTC/USD", "EUR/USD,EUR_USD", "USD/TRY",
                      ",".join("PAIR%d/USD" % i for i in range(11))):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                Config.from_env({"OANDA_FOREX_WATCHLIST": value})


class TestForexRuntime(unittest.TestCase):
    def test_disabled_config_has_no_forex_collector_or_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, history=False)
            with fixture.worker() as worker:
                self.assertIsNone(worker.forex_collector)
                outcome = worker.cycle()
                self.assertIsNone(outcome["error_code"], outcome)
                self.assertNotIn("forex_watch", outcome)
                self.assertNotIn("forex_watch", fixture.cloud.states[-1])
                self.assertEqual(outcome["orders_submitted"], 0)
                self.assertFalse(outcome["execution_enabled"])

    def test_optional_forex_coexists_with_stocks_and_journal_has_no_prices_or_token(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, history=False)
            fixture.config = replace(fixture.config, forex_watchlist=PAIRS,
                                     oanda_practice_api_token=TOKEN,
                                     oanda_practice_account_id=ACCOUNT)
            provider = MockForexProvider(fixture.venue)
            collector = ForexWatchCollector(provider, pairs=PAIRS, clock=lambda: fixture.venue.now)
            with fixture.worker(forex_collector=collector) as worker:
                result = worker.cycle()
                self.assertIsNone(result["error_code"], result)
                self.assertEqual(provider.reads, 1)
                self.assertEqual(result["forex_watch"]["rows"][0]["status"], "fresh")
                self.assertEqual(result["forex_watch"]["source"], "oanda_practice")
                self.assertIsNotNone(result["market_watch"])
                self.assertTrue(result["cloud_state_published"])
                self.assertIn("forex_watch", fixture.cloud.states[-1])
                self.assertFalse(result["execution_enabled"])
                self.assertEqual(result["orders_submitted"], 0)
                self.assertEqual(result["session"]["execute_requested"], False)
                payload = Journal(fixture.config.db_path).events(event_type="paper_observation_cycle")[-1]["payload"]
                self.assertNotIn("rows", payload.get("forex_watch", {}))
                dumped = json.dumps(payload)
                self.assertNotIn("1.08321", dumped)
                self.assertNotIn(TOKEN, dumped)
                fixture.venue.advance(61)
                stale = worker.state()["latest_cycle"]["forex_watch"]
                self.assertTrue(all(r["status"] == "stale" for r in stale["rows"]))

    def test_forex_failure_cannot_block_stock_cycles(self):
        class BadCollector:
            def collect(self):
                raise RuntimeError("RAW_OANDA_SECRET_SHOULD_NOT_LEAK")
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, history=False)
            fixture.config = replace(fixture.config, forex_watchlist=PAIRS)
            with fixture.worker(forex_collector=BadCollector()) as worker:
                result = worker.cycle()
                self.assertIsNone(result["error_code"])
                self.assertIsNone(result["forex_watch"])
                self.assertIsNotNone(result["market_watch"])
                self.assertIn("forex_watch_unavailable", result["blockers"])
                self.assertNotIn("RAW_OANDA_SECRET", str(result))
                self.assertNotIn("RAW_OANDA_SECRET", str(fixture.cloud.states[-1]))

    def test_publisher_emits_forex_only_in_authorized_owner_row(self):
        config = Config(mode="paper", forex_watchlist=PAIRS,
                        supabase_url="https://project.supabase.co",
                        supabase_service_role_key="sb_secret_FAKE_ONLY",
                        owner_user_id=OWNER)
        published = []
        def transport(method, url, headers, payload):
            assert method == "POST"
            published.append(deepcopy(payload))
            return [deepcopy(payload)]
        publisher = Publisher(config, transport=transport, clock=lambda: NOW)
        publisher.publish_worker_state(instance_id="8aaf2dcc-059b-4b50-88c5-4c5415fbd477",
                                      forex_watch=quote_report())
        self.assertEqual(len(published), 1)
        row = published[0]
        self.assertEqual(row["owner_id"], OWNER)
        self.assertEqual(row["mode"], "paper")
        self.assertFalse(row["payload"]["execution_enabled"])
        self.assertEqual(row["payload"]["forex_watch"]["source"], "oanda_practice")
        self.assertNotIn(TOKEN, json.dumps(row))
        self.assertNotIn("forex_watch", row["payload"]["data_feed"])


if __name__ == "__main__":
    unittest.main()
