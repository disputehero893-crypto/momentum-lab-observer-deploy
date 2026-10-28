"""Cross-component tests for opt-in, owner-scoped spot crypto observations.

All market data, accounts, and cloud responses are fake, never live Alpaca.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import tempfile
import unittest

from momentum.config import Config, ConfigError
from momentum.crypto_observation import (
    CryptoWatchCollector, build_crypto_watch, sanitize_crypto_watch,
)
from momentum.journal import Journal
from momentum.observation import ObservationWorker
from momentum.publisher import Publisher, build_worker_state
from test_worker_observation import Fixture

PAIRS = ("BTC/USD", "ETH/USD")
OWNER = "9d59c43f-1d43-494e-afd9-1f73edabb444"
NOW = datetime(2026, 10, 16, 13, 35, 5, tzinfo=timezone.utc)


def timestamp(now):
    return now.isoformat().replace("+00:00", "Z")


def quote_report(now=NOW, pairs=PAIRS):
    source = now - timedelta(milliseconds=10)
    received = now - timedelta(milliseconds=1)
    raw = {"quotes": {
        "BTC/USD": {"bp": Decimal("64123.123456789012345678"),
                    "ap": Decimal("64123.123456789012345679"), "t": timestamp(source)},
        "ETH/USD": {"bp": "2200.12", "ap": "2200.13", "t": timestamp(source)}}}
    return build_crypto_watch(raw, pairs, received_at=timestamp(received),
                              collected_at=timestamp(now), max_age_seconds=60)


class TestCryptoConfig(unittest.TestCase):
    def test_opt_in_default_disabled(self):
        config = Config.from_env({})
        self.assertEqual(config.crypto_watchlist, ())
        self.assertEqual(config.watchlist, ())
        self.assertEqual(config.public()["crypto_watchlist"], [])

    def test_new_crypto_env_is_distinct_from_equities(self):
        config = Config.from_env({"WATCHLIST": "AAPL,MSFT",
                                  "CRYPTO_WATCHLIST": " BTC/USD , eth/usd "})
        self.assertEqual(config.watchlist, ("AAPL", "MSFT"))
        self.assertEqual(config.crypto_watchlist, PAIRS)
        self.assertFalse(config.paper_enabled)
        self.assertEqual(config.mode, "demo")
        # Read-only crypto options cannot change the reviewed STOCK execution hash.
        stock_only = Config.from_env({"WATCHLIST": "AAPL,MSFT"})
        self.assertEqual(config.review_hash(), stock_only.review_hash())

    def test_invalid_crypto_watchlists_fail_closed(self):
        for value in ("AAPL", "EUR/USD", "BTC/USDT", "BTC/USD,BTC/USD",
                      "BTC/USD,../USD", ",".join("PAIR%d/USD" % i for i in range(11))):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                Config.from_env({"CRYPTO_WATCHLIST": value})


class TestCryptoSanitizer(unittest.TestCase):
    def test_roundtrip_exact_decimal_and_pair_order(self):
        watch = sanitize_crypto_watch(quote_report(), allowed_pairs=PAIRS, now=NOW)
        self.assertEqual(watch["requested_pairs"], list(PAIRS))
        self.assertEqual(watch["rows"][0]["bid"], "64123.123456789012345678")
        self.assertEqual([r["status"] for r in watch["rows"]], ["fresh", "fresh"])
        self.assertFalse(watch["execution_enabled"])

    def test_stale_on_old_collection_without_heartbeat_promotion(self):
        old = quote_report()
        later = NOW + timedelta(seconds=61)
        checked = sanitize_crypto_watch(old, allowed_pairs=PAIRS, now=later)
        self.assertEqual([r["status"] for r in checked["rows"]], ["stale", "stale"])
        self.assertTrue(all(r["error_code"] == "quote_stale" for r in checked["rows"]))
        self.assertEqual(checked["collected_at"], old["collected_at"])

    def test_malformed_payload_is_rejected(self):
        base = quote_report()
        mutations = [
            lambda x: x.update({"source": "oanda_practice"}),
            lambda x: x.update({"observation_only": False}),
            lambda x: x.update({"execution_enabled": True}),
            lambda x: x.update({"requested_pairs": ["ETH/USD", "BTC/USD"]}),
            lambda x: x["rows"][0].update({"pair": "AAPL"}),
            lambda x: x["rows"][0].update({"bid": "1000000000000000000000"}),
            lambda x: x["rows"][0].update({"ask": "1"}),
            lambda x: x["rows"][0].update({"source_at": "2026-10-19T13:35:00Z"}),
            lambda x: x["rows"][0].update({"status": "error", "error_code": None}),
            lambda x: x.update({"max_age_seconds": 999}),
        ]
        for mutation in mutations:
            x = deepcopy(base)
            mutation(x)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                sanitize_crypto_watch(x, allowed_pairs=PAIRS, now=NOW)

    def test_invalid_optional_crypto_cannot_poison_equity_publication(self):
        report = quote_report()
        okay = build_worker_state(OWNER, "8aaf2dcc-059b-4b50-88c5-4c5415fbd477",
                                  now=NOW, crypto_watch=report, allowed_crypto_pairs=PAIRS)
        self.assertIn("crypto_watch", okay["payload"])
        self.assertEqual(okay["payload"]["data_feed"]["status"], "disconnected")
        self.assertFalse(okay["payload"]["execution_enabled"])
        wrong = deepcopy(report)
        wrong["source"] = "oanda_practice"
        ignored = build_worker_state(OWNER, "8aaf2dcc-059b-4b50-88c5-4c5415fbd477",
                                     now=NOW, crypto_watch=wrong, allowed_crypto_pairs=PAIRS)
        self.assertNotIn("crypto_watch", ignored["payload"])
        self.assertEqual(ignored["payload"]["data_feed"], okay["payload"]["data_feed"])
        disabled = build_worker_state(OWNER, "8aaf2dcc-059b-4b50-88c5-4c5415fbd477",
                                      now=NOW, crypto_watch=report)
        self.assertNotIn("crypto_watch", disabled["payload"])


class MockCryptoProvider:
    def __init__(self, venue):
        self.venue = venue
        self.reads = 0

    def latest_quotes(self, pairs):
        assert pairs == PAIRS
        self.reads += 1
        now = self.venue.now
        source = timestamp(now - timedelta(milliseconds=10))
        return {"quotes": {
            "BTC/USD": {"bp": "64123.10", "ap": "64123.11", "t": source},
            "ETH/USD": {"bp": "2200.12", "ap": "2200.13", "t": source}}}


class TestCryptoRuntime(unittest.TestCase):
    def test_disabled_config_has_no_crypto_collector_or_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, history=False)
            with fixture.worker() as worker:
                self.assertIsNone(worker.crypto_collector)
                outcome = worker.cycle()
                self.assertIsNone(outcome["error_code"], outcome)
                self.assertNotIn("crypto_watch", outcome)
                self.assertNotIn("crypto_watch", fixture.cloud.states[-1])
                self.assertNotIn("crypto_watch", Journal(fixture.config.db_path)
                                 .events(event_type="paper_observation_cycle")[-1]["payload"])
                self.assertEqual(outcome["orders_submitted"], 0)

    def test_optional_crypto_coexists_with_stocks_and_journal_is_summary_only(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, history=False)
            fixture.config = replace(fixture.config, crypto_watchlist=PAIRS)
            provider = MockCryptoProvider(fixture.venue)
            collector = CryptoWatchCollector(provider, pairs=PAIRS, clock=lambda: fixture.venue.now)
            with fixture.worker(crypto_collector=collector) as worker:
                result = worker.cycle()
                self.assertIsNone(result["error_code"], result)
                self.assertEqual(provider.reads, 1)
                self.assertEqual(result["crypto_watch"]["rows"][0]["status"], "fresh")
                self.assertIsNotNone(result["market_watch"])
                self.assertTrue(result["cloud_state_published"])
                self.assertIn("crypto_watch", fixture.cloud.states[-1])
                self.assertEqual(fixture.cloud.states[-1]["crypto_watch"]["rows"][1]["pair"], "ETH/USD")
                self.assertFalse(result["execution_enabled"])
                self.assertEqual(result["orders_submitted"], 0)
                self.assertEqual(result["session"]["execute_requested"], False)
                journal = Journal(fixture.config.db_path)
                entries = journal.events(event_type="paper_observation_cycle")
                event_payload = entries[-1]["payload"]
                self.assertNotIn("rows", event_payload.get("crypto_watch", {}))
                self.assertNotIn("64123.10", json.dumps(event_payload))
                fixture.venue.advance(61)
                stale = worker.state()["latest_cycle"]["crypto_watch"]
                self.assertTrue(all(r["status"] == "stale" for r in stale["rows"]))

    def test_crypto_failure_cannot_block_stock_cycles(self):
        class BadCollector:
            def collect(self):
                raise RuntimeError("RAW_PROVIDER_SECRET_SHOULD_NOT_LEAK")
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, history=False)
            fixture.config = replace(fixture.config, crypto_watchlist=PAIRS)
            with fixture.worker(crypto_collector=BadCollector()) as worker:
                result = worker.cycle()
                self.assertIsNone(result["error_code"])
                self.assertIsNone(result["crypto_watch"])
                self.assertIsNotNone(result["market_watch"])
                self.assertIn("crypto_watch_unavailable", result["blockers"])
                self.assertNotIn("RAW_PROVIDER_SECRET", str(result))
                self.assertNotIn("RAW_PROVIDER_SECRET", str(fixture.cloud.states[-1]))

    def test_publisher_emits_crypto_only_in_authorized_owner_row(self):
        config = Config(mode="paper", crypto_watchlist=PAIRS,
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
                                      crypto_watch=quote_report())
        self.assertEqual(len(published), 1)
        row = published[0]
        self.assertEqual(row["owner_id"], OWNER)
        self.assertEqual(row["mode"], "paper")
        self.assertFalse(row["payload"]["execution_enabled"])
        self.assertEqual(row["payload"]["crypto_watch"]["source"], "alpaca_crypto")
        self.assertNotIn("crypto_watch", row["payload"]["data_feed"])
