"""Market Watch acceptance cases using invented quotes and offline transports.

These tests read no saved worker state, environment credentials or market data.
Every journal is created in a new temporary directory. The urllib opener is also
disabled, so an accidental omission of an injected transport cannot contact a
provider. Prices and sessions below are synthetic examples, not market history.
"""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from momentum.alpaca import BrokerError, PaperDisabled, parse_time
from momentum.config import Config
from momentum.market_observation import (
    MarketWatchCollector,
    market_data_evidence,
    sanitize_market_watch,
)
from momentum.models import NY
from momentum.observation import ObservationWorker, ReadOnlyPaperClient
from momentum.publisher import Publisher


SYMBOLS = ("AAPL", "MSFT", "SPY")
NOW = datetime(2026, 10, 16, 13, 35, 5, tzinfo=timezone.utc)
OWNER = "00000000-0000-4000-8000-000000000031"
ACCOUNT = "00000000-0000-4000-8000-000000000032"
INSTANCE = "00000000-0000-4000-8000-000000000033"
PRIVATE_SENTINEL = "INVENTED_PRIVATE_PROVIDER_TEXT_MUST_NOT_BE_PUBLISHED"


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


class Clock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def quote_response(now=NOW):
    values = {}
    for symbol, bid, age in (("AAPL", "250.00", 0.25), ("MSFT", "420.00", 0.5), ("SPY", "600.00", 1.0)):
        source = stamp(now - timedelta(seconds=age))
        values[symbol] = {
            "latestQuote": {"bp": bid, "ap": str(Decimal(bid) + Decimal("0.02")),
                            "bs": 200, "as": 100, "t": source},
            "latestTrade": {"p": str(Decimal(bid) + Decimal("0.01")), "s": 25, "t": source},
            "prevDailyBar": {"c": "1.00"},
            "raw_private_extra": PRIVATE_SENTINEL,
        }
    return values


def config_for(directory=None, *, cloud=False):
    values = dict(mode="paper", paper_enabled=False, api_key="FAKE-MARKET-KEY",
                  api_secret="FAKE-MARKET-SECRET", watchlist=SYMBOLS, data_feed="iex",
                  accounting_history_start="", quote_max_age_seconds=2)
    if directory is not None:
        values.update(db_path=str(Path(directory) / "new-test-journal.sqlite3"),
                      metadata_path=str(Path(directory) / "intentionally-absent-metadata.json"))
    if cloud:
        values.update(supabase_url="https://fixture.supabase.co",
                      supabase_service_role_key="sb_secret_FAKE_TEST_ONLY", owner_user_id=OWNER)
    return Config(**values)


class ProviderFixture:
    """A fake HTTP transport behind the real GET-only observation client."""

    def __init__(self, config, *, clock=None):
        self.clock = clock or Clock()
        self.calls = []
        self.raw = quote_response(self.clock())
        self.calendar = [{"date": self.clock().astimezone(NY).date().isoformat(),
                          "open": "09:30", "close": "16:00"}]
        self.calendar_error = None
        self.snapshot_error = None
        self.snapshot_delay = 0
        self.client = ReadOnlyPaperClient(config, transport=self.request, clock=self.clock)

    def request(self, method, url, headers, payload):
        parsed = urlsplit(url)
        self.calls.append({"method": method, "host": parsed.netloc, "path": parsed.path,
                           "params": parse_qs(parsed.query), "payload": deepcopy(payload)})
        if method != "GET" or payload is not None:
            raise AssertionError("A broker mutation reached the fake transport")
        if parsed.path == "/v2/calendar":
            if self.calendar_error:
                raise self.calendar_error
            return deepcopy(self.calendar)
        if parsed.path == "/v2/stocks/snapshots":
            self.clock.advance(self.snapshot_delay)
            if self.snapshot_error:
                raise self.snapshot_error
            return deepcopy(self.raw)
        if parsed.path == "/v2/account":
            return {"id": ACCOUNT, "status": "ACTIVE", "trading_blocked": False,
                    "account_blocked": False, "trade_suspended_by_user": False,
                    "equity": "10000", "buying_power": "10000"}
        if parsed.path in {"/v2/positions", "/v2/orders"}:
            return []
        raise AssertionError("Unexpected read outside this fixture's contract: " + parsed.path)


class OfflineCase(unittest.TestCase):
    def setUp(self):
        blocker = patch("urllib.request.OpenerDirector.open",
                        side_effect=AssertionError("Network access is forbidden in Market Watch tests"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def collector(self, *, clock=None):
        cfg = config_for()
        fixture = ProviderFixture(cfg, clock=clock)
        return MarketWatchCollector(fixture.client, cfg, clock=fixture.clock), fixture

    @staticmethod
    def rows(watch):
        return {row["symbol"]: row for row in watch["rows"]}

    @staticmethod
    def revalidate(watch, *, now=NOW, allowed=SYMBOLS):
        return sanitize_market_watch(watch, allowed_symbols=allowed, feed="iex",
                                     now=now, max_age_seconds=2)


class MarketWatchCollectionTests(OfflineCase):
    def test_three_symbols_preserve_prices_feed_source_and_receipt_times(self):
        collector, fixture = self.collector()
        watch = collector.collect()
        rows = self.rows(watch)
        self.assertEqual(watch["source"], "alpaca_market_data")
        self.assertEqual(watch["feed"], "iex")
        self.assertEqual(watch["requested_symbols"], list(SYMBOLS))
        self.assertEqual(list(rows), list(SYMBOLS))
        self.assertEqual(watch["session"]["status"], "open")
        self.assertEqual(watch["session"]["scope"], "regular")
        self.assertEqual(watch["session"]["max_age_seconds"], 300)
        for symbol in SYMBOLS:
            with self.subTest(symbol=symbol):
                row = rows[symbol]
                self.assertEqual(row["status"], "fresh")
                self.assertEqual(row["feed"], "iex")
                self.assertEqual(Decimal(row["bid"]), Decimal(fixture.raw[symbol]["latestQuote"]["bp"]))
                self.assertEqual(Decimal(row["ask"]), Decimal(fixture.raw[symbol]["latestQuote"]["ap"]))
                self.assertEqual(parse_time(row["source_at"]), parse_time(fixture.raw[symbol]["latestQuote"]["t"]))
                self.assertEqual(parse_time(row["received_at"]), NOW)
                self.assertEqual(row["last_trade"]["status"], "fresh")
        evidence = market_data_evidence(watch)
        self.assertTrue(evidence["verified"])
        self.assertEqual(parse_time(evidence["as_of"]), NOW - timedelta(seconds=1))
        self.assertEqual(parse_time(evidence["received_at"]), NOW)
        self.assertNotIn(PRIVATE_SENTINEL, json.dumps(watch))
        self.assertEqual([call["path"] for call in fixture.calls],
                         ["/v2/calendar", "/v2/stocks/snapshots"])
        calendar = fixture.calls[0]
        self.assertEqual(calendar["params"], {"start": ["2026-10-16"], "end": ["2026-10-16"]})
        self.assertEqual(fixture.calls[1]["host"], "data.alpaca.markets")
        self.assertEqual(fixture.calls[1]["params"]["feed"], ["iex"])

    def test_missing_and_malformed_symbols_do_not_discard_healthy_rows(self):
        collector, fixture = self.collector()
        del fixture.raw["MSFT"]
        fixture.raw["SPY"] = ["not", "a", "snapshot"]
        watch = collector.collect()
        rows = self.rows(watch)
        self.assertEqual(rows["AAPL"]["status"], "fresh")
        self.assertEqual(rows["MSFT"]["status"], "missing")
        self.assertEqual(rows["MSFT"]["error_code"], "symbol_missing")
        self.assertEqual(rows["SPY"]["status"], "error")
        self.assertEqual(rows["SPY"]["error_code"], "symbol_invalid")
        self.assertIsNone(rows["MSFT"]["bid"])
        self.assertIsNone(rows["SPY"]["ask"])
        self.assertFalse(market_data_evidence(watch)["verified"])

    def test_missing_crossed_nonfinite_and_nonpositive_quotes_never_become_zero(self):
        cases = (
            (None, "quote_missing"),
            ({"bp": "250.03", "ap": "250.02", "t": stamp(NOW)}, "quote_invalid"),
            ({"bp": "NaN", "ap": "250.02", "t": stamp(NOW)}, "quote_invalid"),
            ({"bp": "250.00", "ap": "Infinity", "t": stamp(NOW)}, "quote_invalid"),
            ({"bp": 0, "ap": "250.02", "t": stamp(NOW)}, "quote_invalid"),
            ({"bp": True, "ap": "250.02", "t": stamp(NOW)}, "quote_invalid"),
        )
        for quote, expected in cases:
            with self.subTest(quote=quote):
                collector, fixture = self.collector()
                fixture.raw["AAPL"]["latestQuote"] = quote
                rows = self.rows(collector.collect())
                self.assertIn(rows["AAPL"]["status"], {"missing", "error"})
                self.assertEqual(rows["AAPL"]["error_code"], expected)
                self.assertIsNone(rows["AAPL"]["bid"])
                self.assertIsNone(rows["AAPL"]["ask"])
                self.assertEqual(rows["MSFT"]["status"], "fresh")

    def test_freshness_boundary_and_fresh_receipt_cannot_refresh_old_source(self):
        collector, fixture = self.collector()
        fixture.raw["AAPL"]["latestQuote"]["t"] = stamp(NOW - timedelta(seconds=2))
        fixture.raw["MSFT"]["latestQuote"]["t"] = stamp(NOW - timedelta(seconds=2, microseconds=1))
        rows = self.rows(collector.collect())
        self.assertEqual(rows["AAPL"]["status"], "fresh")
        self.assertEqual(rows["MSFT"]["status"], "stale")
        self.assertEqual(rows["MSFT"]["error_code"], "quote_stale")
        self.assertEqual(parse_time(rows["MSFT"]["received_at"]), NOW)
        collector, fixture = self.collector()
        fixture.snapshot_delay = 3
        delayed = collector.collect()
        self.assertTrue(all(row["status"] == "stale" for row in delayed["rows"]))
        self.assertFalse(market_data_evidence(delayed)["verified"])

    def test_future_and_naive_quote_timestamps_are_per_symbol_errors(self):
        for invalid in (stamp(NOW + timedelta(microseconds=1)), "2026-10-16T13:35:05", None, "bad-time"):
            with self.subTest(timestamp=invalid):
                collector, fixture = self.collector()
                fixture.raw["AAPL"]["latestQuote"]["t"] = invalid
                watch = collector.collect()
                rows = self.rows(watch)
                self.assertEqual(rows["AAPL"]["status"], "error")
                self.assertEqual(rows["AAPL"]["error_code"], "quote_timestamp_invalid")
                self.assertEqual(rows["SPY"]["status"], "fresh")
                self.assertFalse(market_data_evidence(watch)["verified"])

    def test_nanosecond_timestamps_are_preserved_and_submicrosecond_future_is_rejected(self):
        collector, fixture = self.collector()
        fixture.raw["AAPL"]["latestQuote"]["t"] = "2026-10-16T13:35:04.999999999Z"
        fixture.raw["MSFT"]["latestQuote"]["t"] = "2026-10-16T13:35:05.000000001Z"
        fixture.raw["SPY"]["latestQuote"]["t"] = "2026-10-16T13:35:05.0000000001Z"
        rows = self.rows(collector.collect())
        self.assertEqual(rows["AAPL"]["status"], "fresh")
        self.assertEqual(rows["AAPL"]["source_at"], "2026-10-16T13:35:04.999999999Z")
        for symbol in ("MSFT", "SPY"):
            self.assertEqual(rows[symbol]["status"], "error")
            self.assertEqual(rows[symbol]["error_code"], "quote_timestamp_invalid")

    def test_price_magnitude_precision_and_encoded_length_are_bounded(self):
        for value in ("1000000000000000001", "250.1234567890123456789", "0" * 70 + "250.01", "1e1000000"):
            with self.subTest(value=value):
                collector, fixture = self.collector()
                fixture.raw["AAPL"]["latestQuote"].update(bp=value, ap=value)
                rows = self.rows(collector.collect())
                self.assertEqual(rows["AAPL"]["status"], "error")
                self.assertEqual(rows["AAPL"]["error_code"], "quote_invalid")
                self.assertIsNone(rows["AAPL"]["bid"])
                self.assertEqual(rows["MSFT"]["status"], "fresh")

    def test_last_trade_missing_stale_or_invalid_is_separate_from_quote_health(self):
        collector, fixture = self.collector()
        fixture.raw["AAPL"]["latestTrade"] = None
        fixture.raw["MSFT"]["latestTrade"]["t"] = stamp(NOW - timedelta(minutes=5))
        fixture.raw["SPY"]["latestTrade"]["p"] = "NaN"
        rows = self.rows(collector.collect())
        self.assertTrue(all(row["status"] == "fresh" for row in rows.values()))
        self.assertIsNone(rows["AAPL"]["last_trade"])
        self.assertEqual(rows["AAPL"]["last_trade_error_code"], "trade_missing")
        self.assertEqual(rows["MSFT"]["last_trade"]["status"], "stale")
        self.assertEqual(rows["MSFT"]["last_trade_error_code"], "trade_stale")
        self.assertIsNone(rows["SPY"]["last_trade"])
        self.assertEqual(rows["SPY"]["last_trade_error_code"], "trade_invalid")

    def test_provider_failure_and_malformed_batch_are_not_reported_as_market_closure(self):
        for raw in (None, [], "malformed provider response"):
            with self.subTest(raw=raw):
                collector, fixture = self.collector()
                fixture.raw = raw
                watch = collector.collect()
                self.assertEqual(watch["session"]["status"], "open")
                self.assertTrue(all(row["status"] == "error" for row in watch["rows"]))
                self.assertTrue(all(row["error_code"] == "market_data_unavailable" for row in watch["rows"]))
        collector, fixture = self.collector()
        fixture.snapshot_error = BrokerError(PRIVATE_SENTINEL, 503)
        watch = collector.collect()
        self.assertEqual(watch["session"]["status"], "open")
        self.assertTrue(all(row["error_code"] == "market_data_unavailable" for row in watch["rows"]))
        self.assertNotIn(PRIVATE_SENTINEL, json.dumps(watch))

    def test_successful_empty_calendar_is_closed_but_bad_calendar_is_unknown(self):
        collector, fixture = self.collector()
        fixture.calendar = []
        watch = collector.collect()
        self.assertEqual(watch["session"]["status"], "closed")
        self.assertIs(watch["session"]["is_trading_day"], False)
        self.assertIsNone(watch["session"]["error_code"])
        self.assertTrue(all(row["status"] == "fresh" for row in watch["rows"]))
        for calendar in (None, {}, [{"date": "2026-10-16"}],
                         [{"date": "2026-10-15", "open": "09:30", "close": "16:00"}]):
            with self.subTest(calendar=calendar):
                collector, fixture = self.collector()
                fixture.calendar = calendar
                watch = collector.collect()
                self.assertEqual(watch["session"]["status"], "unknown")
                self.assertIsNone(watch["session"]["is_trading_day"])
                self.assertEqual(watch["session"]["error_code"], "calendar_invalid")
                self.assertEqual(self.rows(watch)["AAPL"]["status"], "fresh")
        collector, fixture = self.collector()
        fixture.calendar_error = BrokerError(PRIVATE_SENTINEL, 503)
        watch = collector.collect()
        self.assertEqual(watch["session"]["status"], "unknown")
        self.assertEqual(watch["session"]["error_code"], "calendar_unavailable")
        self.assertNotIn(PRIVATE_SENTINEL, json.dumps(watch))

    def test_regular_session_transition_is_recomputed_from_cached_calendar(self):
        clock = Clock(datetime(2026, 10, 16, 19, 59, 59, tzinfo=timezone.utc))
        collector, fixture = self.collector(clock=clock)
        self.assertEqual(collector.collect()["session"]["status"], "open")
        clock.advance(2)
        after = collector.collect()
        self.assertEqual(after["session"]["status"], "closed")
        self.assertIs(after["session"]["is_trading_day"], True)
        self.assertEqual(sum(call["path"] == "/v2/calendar" for call in fixture.calls), 1)
        clock.advance(301)
        fixture.calendar_error = BrokerError(PRIVATE_SENTINEL, 503)
        expired = collector.collect()
        self.assertEqual(expired["session"]["status"], "unknown")
        self.assertFalse(expired["session"]["status"] == "closed")

    def test_session_date_is_new_york_date_not_utc_date(self):
        clock = Clock(datetime(2026, 10, 17, 0, 30, tzinfo=timezone.utc))
        collector, fixture = self.collector(clock=clock)
        watch = collector.collect()
        self.assertEqual(watch["session"]["date"], "2026-10-16")
        self.assertEqual(watch["session"]["status"], "closed")
        self.assertEqual(fixture.calls[0]["params"]["start"], ["2026-10-16"])


class MarketWatchProjectionTests(OfflineCase):
    def test_publication_revalidates_age_without_retimestamping_source_or_receipt(self):
        collector, _ = self.collector()
        original = collector.collect()
        later = self.revalidate(original, now=NOW + timedelta(seconds=3))
        self.assertTrue(all(row["status"] == "stale" for row in later["rows"]))
        for before, after in zip(original["rows"], later["rows"]):
            self.assertEqual(parse_time(before["source_at"]), parse_time(after["source_at"]))
            self.assertEqual(parse_time(before["received_at"]), parse_time(after["received_at"]))
        self.assertEqual(parse_time(original["collected_at"]), parse_time(later["collected_at"]))
        self.assertFalse(market_data_evidence(later)["verified"])

    def test_future_receipt_and_source_after_receipt_cannot_be_asserted_fresh(self):
        collector, _ = self.collector()
        original = collector.collect()
        for field, value in (("received_at", stamp(NOW + timedelta(seconds=1))),
                             ("received_at", stamp(NOW - timedelta(seconds=1))),
                             ("source_at", stamp(NOW + timedelta(seconds=1)))):
            with self.subTest(field=field, value=value):
                altered = deepcopy(original)
                altered["rows"][0][field] = value
                altered["rows"][0]["status"] = "fresh"
                result = self.revalidate(altered)
                row = self.rows(result)["AAPL"]
                self.assertEqual(row["status"], "error")
                self.assertEqual(row["error_code"], "quote_timestamp_invalid")
                self.assertFalse(market_data_evidence(result)["verified"])

    def test_allowlist_projection_omits_unrequested_symbols_and_raw_fields(self):
        collector, fixture = self.collector()
        fixture.raw["UNREQUESTED"] = deepcopy(fixture.raw["AAPL"])
        watch = collector.collect()
        self.assertEqual([row["symbol"] for row in watch["rows"]], list(SYMBOLS))
        watch["raw_provider_body"] = PRIVATE_SENTINEL * 500
        watch["rows"][0]["raw_private_extra"] = PRIVATE_SENTINEL * 500
        watch["rows"].append({**deepcopy(watch["rows"][0]), "symbol": "UNREQUESTED"})
        projected = self.revalidate(watch)
        self.assertEqual([row["symbol"] for row in projected["rows"]], list(SYMBOLS))
        self.assertNotIn(PRIVATE_SENTINEL, json.dumps(projected))
        self.assertLess(len(json.dumps(projected)), 16000)

    def test_quote_row_with_mixed_feed_cannot_be_labeled_as_current_iex(self):
        collector, _ = self.collector()
        watch = collector.collect()
        watch["rows"][0]["feed"] = "sip"
        result = self.revalidate(watch)
        self.assertNotEqual(self.rows(result)["AAPL"]["status"], "fresh")
        self.assertEqual(self.rows(result)["MSFT"]["status"], "fresh")
        self.assertFalse(market_data_evidence(result)["verified"])

    def test_duplicate_row_cannot_hide_conflicting_data_for_one_symbol(self):
        collector, _ = self.collector()
        watch = collector.collect()
        conflict = deepcopy(watch["rows"][0])
        conflict["bid"] = "999.00"
        conflict["ask"] = "999.02"
        watch["rows"].append(conflict)
        with self.assertRaises(ValueError):
            self.revalidate(watch)

    def test_invalid_envelope_allowlist_and_oversized_rows_fail_before_publication(self):
        collector, _ = self.collector()
        original = collector.collect()
        for changed in (None, [], {**original, "schema_version": "unexpected"},
                        {**original, "feed": "sip"},
                        {**original, "rows": [{"symbol": "UNREQUESTED"}] * 1001}):
            with self.subTest(kind=type(changed).__name__), self.assertRaises(ValueError):
                self.revalidate(changed)
        for allowed in (("AAPL", "AAPL"), ("AAPL", "bad symbol"), tuple("S" + str(i) for i in range(11))):
            with self.subTest(allowed=allowed), self.assertRaises(ValueError):
                self.revalidate(original, allowed=allowed)

    def test_projection_does_not_promote_a_previously_rejected_or_stale_row(self):
        collector, _ = self.collector()
        original = collector.collect()
        for status, code in (("stale", "quote_stale"), ("error", "quote_invalid")):
            with self.subTest(status=status):
                changed = deepcopy(original)
                changed["rows"][0].update(status=status, error_code=code)
                projected = self.revalidate(changed)
                self.assertNotEqual(self.rows(projected)["AAPL"]["status"], "fresh")
                self.assertEqual(self.rows(projected)["MSFT"]["status"], "fresh")

    def test_future_or_stale_calendar_evidence_cannot_claim_current_session(self):
        collector, _ = self.collector()
        watch = collector.collect()
        for checked in (NOW + timedelta(seconds=1), NOW - timedelta(seconds=301)):
            with self.subTest(checked=checked):
                changed = deepcopy(watch)
                changed["session"]["checked_at"] = stamp(checked)
                projected = self.revalidate(changed)
                self.assertEqual(projected["session"]["status"], "unknown")

    def test_real_publisher_bounds_and_revalidates_watch_without_execution_or_raw_extras(self):
        collector, _ = self.collector()
        watch = collector.collect()
        watch["unexpected_provider_field"] = PRIVATE_SENTINEL
        watch["rows"][0]["status"] = "fresh"
        watch["rows"][0]["source_at"] = stamp(NOW - timedelta(minutes=10))
        calls = []
        def cloud(method, url, headers, payload):
            calls.append((method, url, deepcopy(payload)))
            return [deepcopy(payload)]
        publisher = Publisher(config_for(cloud=True), transport=cloud, clock=lambda: NOW)
        result = publisher.publish_worker_state(instance_id=INSTANCE, market_watch=watch)
        payload = result["payload"]
        published = payload["market_watch"]
        self.assertEqual(len(published["rows"]), 3)
        self.assertEqual(self.rows(published)["AAPL"]["status"], "stale")
        self.assertNotIn(PRIVATE_SENTINEL, json.dumps(result))
        self.assertLess(len(json.dumps(result)), 20000)
        self.assertFalse(payload["execution_enabled"])
        self.assertFalse(payload["entry_gate_open"])
        self.assertFalse(payload["capabilities"]["orders"])
        self.assertEqual(payload["trading"], "disabled")
        self.assertEqual(len(calls), 1)
        self.assertTrue(urlsplit(calls[0][1]).path.endswith("/worker_state"))


class MarketWatchRuntimeTests(OfflineCase):
    def test_default_observer_publishes_watch_without_history_metadata_or_broker_mutations(self):
        with tempfile.TemporaryDirectory(prefix="market-watch-test-") as directory:
            cfg = config_for(directory, cloud=True)
            fixture = ProviderFixture(cfg)
            cloud_calls = []
            def cloud(method, url, headers, payload):
                cloud_calls.append((method, url, deepcopy(payload)))
                if method == "GET" and urlsplit(url).path.endswith("/paper_commands"):
                    return []
                if method == "POST" and urlsplit(url).path.endswith("/worker_state"):
                    return [deepcopy(payload)]
                raise AssertionError("Unexpected fake cloud operation")
            def forbidden_history(*args, **kwargs):
                raise AssertionError("Blank accounting anchor must not trigger an activity-history import")
            publisher = Publisher(cfg, transport=cloud, clock=fixture.clock)
            with patch("momentum.data.Path.read_text", side_effect=AssertionError(
                    "The default observer must not read strategy metadata")) as metadata_read, \
                    ObservationWorker(cfg, provider=fixture.client, publisher=publisher,
                    activity_fetcher=forbidden_history, clock=fixture.clock, instance_id=INSTANCE) as worker:
                result = worker.cycle()
                metadata_read.assert_not_called()
                self.assertIsNone(result["error_code"])
                self.assertEqual(cfg.accounting_history_start, "")
                self.assertFalse(cfg.paper_enabled)
                self.assertFalse(result["execution_enabled"])
                self.assertEqual(result["orders_submitted"], 0)
                self.assertFalse(result["accounting"]["complete"])
                self.assertIsNone(result["accounting"]["fees"])
                self.assertIsNone(result["accounting"]["realized_pnl"])
                self.assertIn("accounting_history_start_missing", result["blockers"])
                self.assertEqual([row["symbol"] for row in result["market_watch"]["rows"]], list(SYMBOLS))
                self.assertTrue(all(row["status"] == "fresh" for row in result["market_watch"]["rows"]))
                self.assertTrue(result["cloud_state_published"])
                self.assertFalse(result["session"]["execute_requested"])
                before = len(fixture.calls)
                for operation in (lambda: worker.provider.submit({}), lambda: worker.provider.cancel({}),
                                  lambda: fixture.client._request("POST", "/v2/orders", payload={}),
                                  lambda: fixture.client._request("DELETE", "/v2/orders/example")):
                    with self.assertRaises(PaperDisabled):
                        operation()
                self.assertEqual(len(fixture.calls), before)
                posted = [body for method, _, body in cloud_calls if method == "POST"][-1]
                self.assertEqual(posted["payload"]["market_watch"]["feed"], "iex")
                self.assertFalse(posted["payload"]["execution_enabled"])
                self.assertNotIn(PRIVATE_SENTINEL, json.dumps(posted))
                for secret in (cfg.api_key, cfg.api_secret, cfg.supabase_service_role_key):
                    self.assertNotIn(secret, json.dumps(result))
                    self.assertNotIn(secret, json.dumps(posted))
                fixture.clock.advance(3)
                aged = worker.state()
                self.assertFalse(aged["data_connected"])
                self.assertTrue(all(row["status"] == "stale"
                    for row in aged["latest_cycle"]["market_watch"]["rows"]))
                self.assertEqual(aged["latest_cycle"]["market_watch"]["collected_at"],
                                 result["market_watch"]["collected_at"])
            self.assertTrue(all(call["method"] == "GET" and call["payload"] is None for call in fixture.calls))
            self.assertFalse(any(call["path"] == "/v2/account/activities" for call in fixture.calls))
            self.assertFalse(any(call["path"] == "/v2/stocks/bars" for call in fixture.calls))

    def test_accounting_failure_retains_and_reages_quotes_without_echoing_private_error(self):
        for failure_delay in (0, 3):
            with self.subTest(failure_delay=failure_delay), \
                    tempfile.TemporaryDirectory(prefix="market-watch-failure-") as directory:
                cfg = config_for(directory, cloud=True)
                fixture = ProviderFixture(cfg)
                posted = []
                def cloud(method, url, headers, payload):
                    if method == "GET" and urlsplit(url).path.endswith("/paper_commands"):
                        return []
                    if method == "POST" and urlsplit(url).path.endswith("/worker_state"):
                        posted.append(deepcopy(payload))
                        return [deepcopy(payload)]
                    raise AssertionError("Unexpected fake cloud operation")
                def fail_accounting(*args, **kwargs):
                    self.assertIn("/v2/stocks/snapshots", [call["path"] for call in fixture.calls])
                    fixture.clock.advance(failure_delay)
                    raise BrokerError(PRIVATE_SENTINEL)
                publisher = Publisher(cfg, transport=cloud, clock=fixture.clock)
                with ObservationWorker(cfg, provider=fixture.client, publisher=publisher,
                        clock=fixture.clock, instance_id=INSTANCE) as worker:
                    with patch.object(worker, "_accounting", side_effect=fail_accounting) as accounting:
                        result = worker.cycle()
                    accounting.assert_called_once()
                    self.assertEqual(result["error_code"], "activity_accounting_failed")
                    self.assertIn("activity_accounting_failed", result["blockers"])
                    self.assertFalse(result["accounting"]["complete"])
                    self.assertIsNone(result["accounting"]["realized_pnl"])
                    self.assertIsNone(result["accounting"]["fees"])
                    self.assertIsNone(result["session"])
                    self.assertFalse(result["broker"]["verified"])
                    self.assertFalse(result["execution_enabled"])
                    self.assertEqual(result["orders_submitted"], 0)
                    self.assertTrue(result["cloud_state_published"])
                    self.assertEqual(len(posted), 1)
                    self.assertEqual(posted[0]["payload"]["last_error_code"], "observation_failed")
                    self.assertFalse(posted[0]["payload"]["execution_enabled"])
                    expected_status = "fresh" if failure_delay == 0 else "stale"
                    for watch in (result["market_watch"], posted[0]["payload"]["market_watch"]):
                        self.assertEqual([row["symbol"] for row in watch["rows"]], list(SYMBOLS))
                        self.assertEqual(watch["session"]["max_age_seconds"], 300)
                        for row in watch["rows"]:
                            self.assertEqual(row["status"], expected_status)
                            self.assertEqual(Decimal(row["bid"]),
                                             Decimal(fixture.raw[row["symbol"]]["latestQuote"]["bp"]))
                            self.assertEqual(parse_time(row["source_at"]),
                                             parse_time(fixture.raw[row["symbol"]]["latestQuote"]["t"]))
                            self.assertEqual(parse_time(row["received_at"]), NOW)
                    serialized = json.dumps({"result": result, "published": posted[0]})
                    for forbidden in (PRIVATE_SENTINEL, cfg.api_key, cfg.api_secret,
                                      cfg.supabase_service_role_key):
                        self.assertNotIn(forbidden, serialized)
                    self.assertEqual([call["path"] for call in fixture.calls],
                                     ["/v2/account", "/v2/calendar", "/v2/calendar", "/v2/stocks/snapshots"])
                    # The separate history collector first requests its bounded
                    # prior-session calendar. This quote-only fixture cannot
                    # supply a prior session, so no historical bars are read;
                    # current quotes still survive the later accounting error.
                    self.assertEqual(fixture.calls[1]["params"],
                                     {"start": ["2026-10-02"], "end": ["2026-10-16"]})
                    self.assertEqual(fixture.calls[2]["params"],
                                     {"start": ["2026-10-16"], "end": ["2026-10-16"]})
                    self.assertEqual(result["price_history"]["calendar"]["error_code"], "calendar_invalid")
                    self.assertTrue(all(call["method"] == "GET" and call["payload"] is None
                                        for call in fixture.calls))


if __name__ == "__main__":
    unittest.main()
