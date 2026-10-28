"""Independent, invented price-history acceptance fixtures; no provider network.

All prices/calendars are fabricated and every runtime journal is a newly created
temporary file. Config is constructed explicitly; environment secrets and saved
worker state are never read by these tests.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from momentum.activities import timestamp_parts
from momentum.alpaca import BrokerError, PaperDisabled
from momentum.config import Config
from momentum.journal import Journal
from momentum.market_observation import MarketWatchCollector
from momentum.models import NY
from momentum.observation import ObservationWorker, ReadOnlyPaperClient
from momentum.price_history import PriceHistoryCollector, price_history_summary, sanitize_price_history
from momentum.publisher import Publisher


SYMBOLS = ("AAPL", "MSFT", "SPY")
NOW = datetime(2026, 10, 16, 13, 45, 30, tzinfo=timezone.utc)
OWNER = "00000000-0000-4000-8000-000000000041"
ACCOUNT = "00000000-0000-4000-8000-000000000042"
INSTANCE = "00000000-0000-4000-8000-000000000043"
EXACT_PRICE = "123.123456789012345678"
PRIVATE = "INVENTED_HISTORY_EXTRA_MUST_NOT_BE_PUBLISHED"


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def nano_stamp(value, nanos=0):
    utc = value.astimezone(timezone.utc)
    fraction = utc.microsecond * 1000 + nanos
    assert 0 <= fraction < 1_000_000_000
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction:09d}Z"


def ns(value):
    return timestamp_parts(value)[1]


def session(day, *, close="16:00"):
    return {"date": day, "open": "09:30", "close": close}


def midnight(day):
    return stamp(datetime.combine(date.fromisoformat(day), datetime.min.time(), NY))


def provider_bar(start, *, close=EXACT_PRICE, volume=100):
    return {"t": stamp(start), "o": EXACT_PRICE, "h": "124", "l": "122", "c": close,
            "v": volume, "raw_extra": PRIVATE}


def daily_page(day):
    start = datetime.fromisoformat(midnight(day).replace("Z", "+00:00"))
    return {"bars": {symbol: [provider_bar(start)] for symbol in SYMBOLS}, "next_page_token": None}


def minute_page(day="2026-10-16", *, start_clock="09:35", count=10):
    start = datetime.fromisoformat(day + "T" + start_clock).replace(tzinfo=NY)
    return {"bars": {symbol: [provider_bar(start + timedelta(minutes=i)) for i in range(count)]
                     for symbol in SYMBOLS}, "next_page_token": None}


class Clock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class HistoryProvider:
    """Returns explicit fixtures rather than calculating the collector's window."""
    def __init__(self, *, clock=None):
        self.clock = clock or Clock()
        self.calls = []
        self.records = [session("2026-10-15"), session("2026-10-16")]
        self.daily = daily_page("2026-10-15")
        self.minutes = minute_page()
        self.calendar_error = None
        self.daily_error = None
        self.minute_error = None
        self.daily_delay = 0
        self.minute_delay = 0

    def calendar(self, start, end):
        self.calls.append({"kind": "calendar", "start": start, "end": end})
        if self.calendar_error:
            raise self.calendar_error
        return deepcopy(self.records)

    def observation_bars(self, symbols, *, timeframe, start, end):
        self.calls.append({"kind": "bars", "symbols": tuple(symbols), "timeframe": timeframe,
                           "start": start, "end": end})
        if timeframe == "1Day":
            self.clock.advance(self.daily_delay)
            if self.daily_error:
                raise self.daily_error
            return deepcopy(self.daily)
        if timeframe == "1Min":
            self.clock.advance(self.minute_delay)
            if self.minute_error:
                raise self.minute_error
            return deepcopy(self.minutes)
        raise AssertionError("Unexpected timeframe outside the observation contract")


def config_for(directory=None, *, cloud=False):
    values = dict(mode="paper", paper_enabled=False, api_key="FAKE-HISTORY-KEY",
                  api_secret="FAKE-HISTORY-SECRET", watchlist=SYMBOLS, data_feed="iex",
                  accounting_history_start="", quote_max_age_seconds=2)
    if directory is not None:
        values.update(db_path=str(Path(directory) / "new-history-test.sqlite3"),
                      metadata_path=str(Path(directory) / "absent-metadata.json"))
    if cloud:
        values.update(supabase_url="https://fixture.supabase.co",
                      supabase_service_role_key="sb_secret_FAKE_TEST_ONLY", owner_user_id=OWNER)
    return Config(**values)


class ObservationProvider:
    """Invented GET responses behind the real read-only broker adapter."""
    def __init__(self, config, *, clock=None):
        self.clock = clock or Clock()
        self.calls = []
        self.history = HistoryProvider(clock=self.clock)
        self.client = ReadOnlyPaperClient(config, transport=self.request, clock=self.clock)

    def request(self, method, url, headers, payload):
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        self.calls.append({"method": method, "host": parsed.netloc, "path": parsed.path,
                           "params": params, "payload": deepcopy(payload)})
        if method != "GET" or payload is not None:
            raise AssertionError("Mutation reached the invented broker transport")
        if parsed.path == "/v2/calendar":
            start, end = params["start"][0], params["end"][0]
            return deepcopy([row for row in self.history.records if start <= row["date"] <= end])
        if parsed.path == "/v2/stocks/bars":
            return self.history.observation_bars(tuple(params["symbols"][0].split(",")),
                timeframe=params["timeframe"][0], start=params["start"][0], end=params["end"][0])
        if parsed.path == "/v2/stocks/snapshots":
            source = stamp(self.clock() - timedelta(milliseconds=250))
            return {symbol: {"latestQuote": {"bp": "250", "ap": "250.02", "bs": 100, "as": 100, "t": source},
                             "latestTrade": {"p": "250.01", "s": 20, "t": source},
                             "prevDailyBar": {"c": "1", "t": midnight("2026-10-14")},
                             "raw_extra": PRIVATE} for symbol in SYMBOLS}
        if parsed.path == "/v2/account":
            return {"id": ACCOUNT, "status": "ACTIVE", "trading_blocked": False,
                    "account_blocked": False, "trade_suspended_by_user": False,
                    "equity": "10000", "buying_power": "10000"}
        if parsed.path in {"/v2/positions", "/v2/orders"}:
            return []
        raise AssertionError("Unexpected provider route in invented history fixture")


class CloudTransport:
    def __init__(self):
        self.calls, self.states = [], []

    def __call__(self, method, url, headers, payload):
        path = urlsplit(url).path
        self.calls.append({"method": method, "path": path})
        if method == "GET" and path == "/rest/v1/paper_commands":
            return []
        if method == "POST" and path == "/rest/v1/worker_state":
            self.states.append(deepcopy(payload))
            return [deepcopy(payload)]
        raise AssertionError("Unexpected publication route in invented history fixture")


class BytesResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.body[:limit]


class PriceHistoryContractTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError(
            "Network access is forbidden in price-history tests"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def collector(self, *, clock=None):
        provider = HistoryProvider(clock=clock)
        cfg = SimpleNamespace(watchlist=SYMBOLS, data_feed="iex")
        return PriceHistoryCollector(provider, cfg, clock=provider.clock), provider

    @staticmethod
    def rows(value):
        return {row["symbol"]: row for row in value["rows"]}

    @staticmethod
    def sanitize(value, *, now=NOW, symbols=SYMBOLS, feed="iex"):
        return sanitize_price_history(value, allowed_symbols=symbols, feed=feed, now=now)

    def test_baseline_contract_preserves_exact_prices_and_explicit_limits(self):
        collector, provider = self.collector()
        value = collector.collect()
        self.assertEqual(value["schema_version"], "1.0")
        self.assertEqual(value["source"], "alpaca_market_data")
        self.assertIs(value["observation_only"], True)
        self.assertEqual(value["feed"], "iex")
        self.assertEqual(value["adjustment"], "raw")
        self.assertEqual(value["currency"], "USD")
        self.assertEqual(value["symbol_mapping"], "disabled")
        self.assertEqual(value["requested_symbols"], list(SYMBOLS))
        self.assertEqual(value["refresh_interval_seconds"], 60)
        self.assertEqual(value["max_age_seconds"], 120)
        self.assertIs(value["revisions_possible"], True)
        self.assertIs(value["official_close"], False)
        self.assertIs(value["full_session_coverage"], False)
        self.assertEqual(value["corporate_action_comparability"], "not_assessed")
        self.assertEqual(value["calendar"]["anchor_date"], "2026-10-16")
        self.assertEqual(value["calendar"]["previous_session"]["date"], "2026-10-15")
        self.assertEqual(value["calendar"]["target_session"]["date"], "2026-10-16")
        self.assertEqual(value["calendar"]["max_age_seconds"], 300)
        for row in value["rows"]:
            close, candles = row["previous_close"], row["minute_bars"]
            self.assertEqual(close["status"], "observed")
            self.assertEqual(close["close"], EXACT_PRICE)
            self.assertEqual(close["session_date"], "2026-10-15")
            self.assertEqual(ns(close["source_at"]), ns(midnight("2026-10-15")))
            self.assertEqual(ns(close["requested_at"]), ns(NOW))
            self.assertEqual(ns(close["received_at"]), ns(NOW))
            self.assertEqual(candles["status"], "observed")
            self.assertEqual(candles["scope"], "regular")
            self.assertEqual(candles["interval_seconds"], 60)
            self.assertEqual(candles["expected_intervals"], 10)
            self.assertEqual(candles["missing_intervals"], [])
            self.assertEqual(len(candles["bars"]), 10)
            self.assertEqual(ns(candles["window_start_at"]), ns("2026-10-16T13:35:00Z"))
            self.assertEqual(ns(candles["cutoff_at"]), ns("2026-10-16T13:45:00Z"))
            for bar in candles["bars"]:
                self.assertEqual(bar["close"], EXACT_PRICE)
                self.assertEqual(bar["volume"], "100")
                self.assertEqual(ns(bar["end_at"]) - ns(bar["start_at"]), 60_000_000_000)
                self.assertLessEqual(ns(bar["end_at"]), ns(candles["cutoff_at"]))
        self.assertNotIn(PRIVATE, json.dumps(value))
        self.assertEqual([call.get("timeframe") for call in provider.calls if call["kind"] == "bars"], ["1Day", "1Min"])
        self.assertTrue(all(call["symbols"] == SYMBOLS for call in provider.calls if call["kind"] == "bars"))

    def test_weekend_uses_friday_calendar_date_and_rejects_thursday_reference(self):
        sunday = datetime(2026, 10, 18, 17, 0, tzinfo=timezone.utc)
        collector, provider = self.collector(clock=Clock(sunday))
        provider.daily = daily_page("2026-10-16")
        provider.minutes = minute_page(start_clock="15:50")
        provider.daily["bars"]["AAPL"] = daily_page("2026-10-15")["bars"]["AAPL"]
        value = collector.collect()
        self.assertIs(value["calendar"]["is_trading_day"], False)
        self.assertEqual(value["calendar"]["previous_session"]["date"], "2026-10-16")
        self.assertEqual(value["calendar"]["target_session"]["date"], "2026-10-16")
        rows = self.rows(value)
        self.assertEqual(rows["AAPL"]["previous_close"]["error_code"], "daily_bar_session_mismatch")
        self.assertIsNone(rows["AAPL"]["previous_close"]["close"])
        self.assertEqual(rows["MSFT"]["previous_close"]["status"], "observed")
        self.assertEqual(rows["MSFT"]["previous_close"]["session_date"], "2026-10-16")
        self.assertEqual(ns(rows["MSFT"]["minute_bars"]["cutoff_at"]), ns("2026-10-16T20:00:00Z"))

    def test_dst_midnight_is_new_york_day_start_not_utc_date_only(self):
        winter = datetime(2026, 11, 3, 15, 45, 30, tzinfo=timezone.utc)
        collector, provider = self.collector(clock=Clock(winter))
        provider.records = [session("2026-11-02"), session("2026-11-03")]
        provider.daily = daily_page("2026-11-02")
        provider.minutes = minute_page("2026-11-03", start_clock="10:35")
        provider.daily["bars"]["AAPL"][0]["t"] = "2026-11-02T04:00:00Z"
        value = collector.collect()
        rows = self.rows(value)
        self.assertEqual(ns(rows["MSFT"]["previous_close"]["source_at"]), ns("2026-11-02T05:00:00Z"))
        self.assertEqual(rows["MSFT"]["previous_close"]["status"], "observed")
        self.assertNotEqual(rows["AAPL"]["previous_close"]["status"], "observed")
        self.assertIsNone(rows["AAPL"]["previous_close"]["close"])

    def test_early_close_and_holiday_follow_supplied_calendar(self):
        after_close = datetime(2026, 11, 27, 20, 0, tzinfo=timezone.utc)
        collector, provider = self.collector(clock=Clock(after_close))
        provider.records = [session("2026-11-25"), session("2026-11-27", close="13:00")]
        provider.daily = daily_page("2026-11-25")
        provider.minutes = minute_page("2026-11-27", start_clock="12:50")
        value = collector.collect()
        self.assertEqual(value["calendar"]["previous_session"]["date"], "2026-11-25")
        for row in value["rows"]:
            self.assertEqual(row["minute_bars"]["status"], "observed")
            self.assertEqual(ns(row["minute_bars"]["cutoff_at"]), ns("2026-11-27T18:00:00Z"))
            self.assertEqual(len(row["minute_bars"]["bars"]), 10)

    def test_premarket_and_exact_open_are_known_empty_without_yesterday_fallback(self):
        for now in (datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc),
                    datetime(2026, 10, 16, 13, 30, tzinfo=timezone.utc)):
            with self.subTest(now=now):
                collector, provider = self.collector(clock=Clock(now))
                value = collector.collect()
                self.assertEqual(value["calendar"]["target_session"]["date"], "2026-10-16")
                self.assertEqual([call["timeframe"] for call in provider.calls if call["kind"] == "bars"], ["1Day"])
                for row in value["rows"]:
                    candles = row["minute_bars"]
                    self.assertEqual(candles["status"], "not_started")
                    self.assertEqual(candles["expected_intervals"], 0)
                    self.assertEqual(candles["bars"], [])
                    self.assertEqual(candles["missing_intervals"], [])
                    for field in ("window_start_at", "cutoff_at", "requested_at", "received_at"):
                        self.assertIsNone(candles[field])

    def test_one_elapsed_minute_and_inclusive_end_never_publish_current_interval(self):
        at_first_end = datetime(2026, 10, 16, 13, 31, tzinfo=timezone.utc)
        collector, provider = self.collector(clock=Clock(at_first_end))
        provider.minutes = minute_page(start_clock="09:30", count=2)
        value = collector.collect()
        for row in value["rows"]:
            candles = row["minute_bars"]
            self.assertEqual(candles["expected_intervals"], 1)
            self.assertEqual(len(candles["bars"]), 1)
            self.assertEqual(ns(candles["bars"][0]["start_at"]), ns("2026-10-16T13:30:00Z"))
            self.assertEqual(ns(candles["bars"][0]["end_at"]), ns(at_first_end))

    def test_minute_cutoff_is_frozen_after_daily_request_and_before_minute_response(self):
        collector, provider = self.collector()
        provider.daily_delay = 35
        provider.minute_delay = 60
        provider.minutes = minute_page(start_clock="09:36")
        value = collector.collect()
        row = value["rows"][0]
        self.assertEqual(ns(row["previous_close"]["requested_at"]), ns(NOW))
        self.assertEqual(ns(row["previous_close"]["received_at"]), ns(NOW + timedelta(seconds=35)))
        self.assertEqual(ns(row["minute_bars"]["requested_at"]), ns(NOW + timedelta(seconds=35)))
        self.assertEqual(ns(row["minute_bars"]["received_at"]), ns(NOW + timedelta(seconds=95)))
        self.assertEqual(ns(row["minute_bars"]["cutoff_at"]), ns("2026-10-16T13:46:00Z"))
        self.assertTrue(all(ns(bar["end_at"]) <= ns("2026-10-16T13:46:00Z") for bar in row["minute_bars"]["bars"]))

    def test_calendar_failure_and_invalid_shapes_never_become_known_closed(self):
        for invalid in (None, {}, [], [session("not-a-date")],
                        [session("2026-10-15"), session("2026-10-15")],
                        [session("2026-10-15"), {"date": "2026-10-16", "open": "16:00", "close": "09:30"}]):
            with self.subTest(kind=type(invalid).__name__):
                collector, provider = self.collector()
                provider.records = invalid
                value = collector.collect()
                self.assertEqual(value["calendar"]["error_code"], "calendar_invalid")
                self.assertIsNone(value["calendar"]["is_trading_day"])
                self.assertFalse(any(call["kind"] == "bars" for call in provider.calls))
        collector, provider = self.collector()
        provider.calendar_error = BrokerError(PRIVATE)
        value = collector.collect()
        self.assertEqual(value["calendar"]["error_code"], "calendar_unavailable")
        self.assertIsNone(value["calendar"]["is_trading_day"])
        self.assertNotIn(PRIVATE, json.dumps(value))

    def test_nonempty_page_token_refuses_partial_history_without_followup(self):
        for component, attr in (("previous_close", "daily"), ("minute_bars", "minutes")):
            with self.subTest(component=component):
                collector, provider = self.collector()
                getattr(provider, attr)["next_page_token"] = "INVENTED_TOKEN"
                value = collector.collect()
                for row in value["rows"]:
                    self.assertEqual(row[component]["status"], "error")
                    self.assertEqual(row[component]["error_code"], "history_page_incomplete")
                self.assertEqual(sum(call["kind"] == "bars" for call in provider.calls), 2)
                self.assertNotIn("INVENTED_TOKEN", json.dumps(value))

    def test_provider_exceptions_and_malformed_pages_keep_independent_components(self):
        for attr, component in (("daily", "previous_close"), ("minutes", "minute_bars")):
            for malformed in (None, [], {"bars": []}, {"raw_error": PRIVATE}):
                with self.subTest(component=component, kind=type(malformed).__name__):
                    collector, provider = self.collector()
                    setattr(provider, attr, malformed)
                    value = collector.collect()
                    for row in value["rows"]:
                        self.assertEqual(row[component]["error_code"], "history_response_invalid")
                    self.assertNotIn(PRIVATE, json.dumps(value))
        for attr, component in (("daily_error", "previous_close"), ("minute_error", "minute_bars")):
            collector, provider = self.collector()
            setattr(provider, attr, RuntimeError(PRIVATE))
            value = collector.collect()
            other = "minute_bars" if component == "previous_close" else "previous_close"
            for row in value["rows"]:
                self.assertEqual(row[component]["error_code"], "history_unavailable")
                self.assertEqual(row[other]["status"], "observed")
            self.assertNotIn(PRIVATE, json.dumps(value))

    def test_missing_symbol_and_bad_bar_are_isolated_from_good_rows(self):
        collector, provider = self.collector()
        del provider.daily["bars"]["MSFT"]
        provider.daily["bars"]["SPY"][0]["c"] = "NaN"
        provider.minutes["bars"]["AAPL"][0]["h"] = "121"
        value = collector.collect()
        rows = self.rows(value)
        self.assertEqual(rows["AAPL"]["previous_close"]["status"], "observed")
        self.assertEqual(rows["MSFT"]["previous_close"]["error_code"], "symbol_missing")
        self.assertEqual(rows["SPY"]["previous_close"]["error_code"], "daily_bar_invalid")
        self.assertEqual(rows["AAPL"]["minute_bars"]["error_code"], "minute_bar_invalid")
        self.assertEqual(rows["MSFT"]["minute_bars"]["status"], "observed")
        self.assertEqual(rows["SPY"]["minute_bars"]["status"], "observed")

    def test_missing_eligible_minutes_remain_gaps_and_zero_volume_is_rejected(self):
        collector, provider = self.collector()
        provider.minutes["bars"]["AAPL"] = [bar for i, bar in enumerate(provider.minutes["bars"]["AAPL"]) if i not in (2, 8)]
        provider.minutes["bars"]["MSFT"] = []
        provider.minutes["bars"]["SPY"][0]["v"] = 0
        provider.daily["bars"]["SPY"][0]["v"] = 0
        rows = self.rows(collector.collect())
        aapl = rows["AAPL"]["minute_bars"]
        self.assertEqual(aapl["status"], "observed")
        self.assertEqual(len(aapl["bars"]), 8)
        self.assertEqual(aapl["expected_intervals"], 10)
        self.assertEqual([ns(value) for value in aapl["missing_intervals"]],
                         [ns("2026-10-16T13:37:00Z"), ns("2026-10-16T13:43:00Z")])
        self.assertEqual(rows["MSFT"]["minute_bars"]["status"], "empty")
        self.assertEqual(rows["MSFT"]["minute_bars"]["bars"], [])
        self.assertEqual(len(rows["MSFT"]["minute_bars"]["missing_intervals"]), 10)
        self.assertEqual(rows["SPY"]["minute_bars"]["error_code"], "minute_bar_invalid")
        self.assertEqual(rows["SPY"]["minute_bars"]["bars"], [])
        self.assertEqual(rows["SPY"]["previous_close"]["error_code"], "daily_bar_invalid")

    def test_volume_and_price_precision_reject_bad_values_without_decimal_rounding(self):
        for field, bad in (("v", "1.1"), ("v", -1), ("v", True), ("v", "Infinity"),
                           ("v", "1000000000000000001"), ("c", "123.1234567890123456789"),
                           ("c", 0), ("c", "Infinity"), ("c", "1000000000000000001")):
            with self.subTest(field=field, kind=type(bad).__name__):
                collector, provider = self.collector()
                provider.minutes["bars"]["AAPL"][0][field] = bad
                value = collector.collect()
                rows = self.rows(value)
                self.assertEqual(rows["AAPL"]["minute_bars"]["error_code"], "minute_bar_invalid")
                self.assertEqual(rows["MSFT"]["minute_bars"]["status"], "observed")

    def test_cached_observation_retains_original_receipts_and_refreshes_at_sixty_seconds(self):
        collector, provider = self.collector()
        first = collector.collect()
        original = deepcopy(first)
        calls = len(provider.calls)
        provider.clock.advance(59.999999)
        second = collector.collect()
        self.assertEqual(len(provider.calls), calls)
        self.assertEqual(second, original)
        provider.clock.advance(0.000001)
        third = collector.collect()
        self.assertGreater(len(provider.calls), calls)
        self.assertEqual(first, original)
        self.assertNotEqual(third["rows"][0]["previous_close"]["received_at"], first["rows"][0]["previous_close"]["received_at"])
        third["rows"][0]["previous_close"]["close"] = "999"
        self.assertNotEqual(collector.collect()["rows"][0]["previous_close"]["close"], "999")

    def test_request_and_receipt_age_are_inclusive_to_the_nanosecond(self):
        collector, _ = self.collector()
        value = collector.collect()
        boundary = NOW + timedelta(seconds=120)
        allowed = self.sanitize(value, now=nano_stamp(boundary))
        expired = self.sanitize(value, now=nano_stamp(boundary, 1))
        for row in allowed["rows"]:
            self.assertEqual(row["previous_close"]["status"], "observed")
            self.assertEqual(row["minute_bars"]["status"], "observed")
        for row in expired["rows"]:
            self.assertEqual(row["previous_close"]["status"], "stale")
            self.assertEqual(row["minute_bars"]["status"], "stale")
        # A fresh receipt must not conceal a request already older than 120s.
        # These fixtures preserve calendar -> request -> receipt causality.
        slow_daily, daily_provider = self.collector(clock=Clock(NOW - timedelta(seconds=121)))
        daily_provider.daily_delay = 121
        rows = self.rows(slow_daily.collect())
        self.assertEqual(rows["AAPL"]["previous_close"]["status"], "stale")
        self.assertEqual(ns(rows["AAPL"]["previous_close"]["received_at"]), ns(NOW))
        self.assertEqual(rows["AAPL"]["minute_bars"]["status"], "observed")
        slow_minute, minute_provider = self.collector(clock=Clock(NOW - timedelta(seconds=121)))
        minute_provider.minute_delay = 121
        minute_provider.minutes = minute_page(start_clock="09:33")
        rows = self.rows(slow_minute.collect())
        self.assertEqual(rows["AAPL"]["minute_bars"]["status"], "stale")
        self.assertEqual(ns(rows["AAPL"]["minute_bars"]["received_at"]), ns(NOW))

    def test_future_receipt_and_unaligned_minute_one_nanosecond_are_not_observed(self):
        collector, provider = self.collector()
        provider.minutes["bars"]["AAPL"][0]["t"] = "2026-10-16T13:35:00.000000001Z"
        collected = collector.collect()
        self.assertEqual(self.rows(collected)["AAPL"]["minute_bars"]["error_code"], "minute_bar_invalid")
        collector, _ = self.collector()
        original = collector.collect()
        for component in ("previous_close", "minute_bars"):
            for field in ("requested_at", "received_at"):
                with self.subTest(component=component, field=field):
                    changed = deepcopy(original)
                    changed["rows"][0][component][field] = nano_stamp(NOW, 1)
                    rows = self.rows(self.sanitize(changed))
                    self.assertNotEqual(rows["AAPL"][component]["status"], "observed")
                    self.assertEqual(rows["AAPL"][component]["error_code"], "history_timestamp_invalid")
                    self.assertEqual(rows["MSFT"][component]["status"], "observed")

    def test_calendar_expiry_and_midnight_rollover_prevent_current_claims(self):
        collector, _ = self.collector()
        value = collector.collect()
        exact = self.sanitize(value, now=nano_stamp(NOW + timedelta(seconds=300)))
        self.assertIsNone(exact["calendar"]["error_code"])
        expired = self.sanitize(value, now=nano_stamp(NOW + timedelta(seconds=300), 1))
        self.assertEqual(expired["calendar"]["error_code"], "calendar_stale")
        rollover = self.sanitize(value, now="2026-10-17T04:00:00Z")
        self.assertIsNotNone(rollover["calendar"]["error_code"])
        self.assertTrue(all(row["previous_close"]["status"] != "observed" for row in rollover["rows"]))
        future = deepcopy(value)
        future["calendar"]["checked_at"] = nano_stamp(NOW, 1)
        self.assertEqual(self.sanitize(future)["calendar"]["error_code"], "calendar_invalid")

    def test_sanitizer_is_bounded_projects_allowlist_and_never_promotes_stale(self):
        collector, _ = self.collector()
        value = collector.collect()
        value["raw_extra"] = PRIVATE
        extra = deepcopy(value["rows"][0]); extra["symbol"] = "UNREQUESTED"
        value["rows"].append(extra)
        value["rows"][0]["previous_close"].update(status="stale", error_code="history_stale")
        projected = self.sanitize(value)
        self.assertEqual([row["symbol"] for row in projected["rows"]], list(SYMBOLS))
        self.assertEqual(projected["rows"][0]["previous_close"]["status"], "stale")
        self.assertNotIn(PRIVATE, json.dumps(projected))
        for bad in (None, [], {**value, "schema_version": "other"}, {**value, "feed": "sip"},
                    {**value, "adjustment": "all"}, {**value, "currency": "EUR"},
                    {**value, "symbol_mapping": "automatic"}, {**value, "rows": value["rows"] * 100}):
            with self.subTest(kind=type(bad).__name__), self.assertRaises(ValueError):
                self.sanitize(bad)
        duplicate = deepcopy(projected)
        duplicate["rows"].append(deepcopy(duplicate["rows"][0]))
        with self.assertRaises(ValueError):
            self.sanitize(duplicate)
        for allowed in (("AAPL", "AAPL"), ("bad symbol",), tuple("X" + str(i) for i in range(11))):
            with self.subTest(allowed=allowed), self.assertRaises(ValueError):
                self.sanitize(projected, symbols=allowed)

    def test_error_states_and_malformed_statuses_cannot_be_promoted_or_break_other_rows(self):
        collector, _ = self.collector()
        original = collector.collect()
        for component in ("previous_close", "minute_bars"):
            for status in ("error", "missing", [], {}):
                with self.subTest(component=component, kind=type(status).__name__):
                    value = deepcopy(original)
                    value["rows"][0][component].update(status=status, error_code="history_unavailable")
                    rows = self.rows(self.sanitize(value))
                    self.assertNotEqual(rows["AAPL"][component]["status"], "observed")
                    self.assertEqual(rows["MSFT"][component]["status"], "observed")
            value = deepcopy(original)
            value["rows"][0][component]["error_code"] = "history_unavailable"
            self.assertNotEqual(self.rows(self.sanitize(value))["AAPL"][component]["status"], "observed")
        for code in ("calendar_invalid", "calendar_unavailable", PRIVATE):
            value = deepcopy(original)
            value["calendar"]["error_code"] = code
            result = self.sanitize(value)
            self.assertIsNotNone(result["calendar"]["error_code"])
            self.assertTrue(all(row["previous_close"]["status"] != "observed" for row in result["rows"]))
            self.assertNotIn(PRIVATE, json.dumps(result))

    def test_calendar_anchor_must_match_original_collection_day_as_well_as_publication(self):
        before_midnight = datetime(2026, 10, 18, 3, 59, 59, tzinfo=timezone.utc)
        collector, provider = self.collector(clock=Clock(before_midnight))
        provider.daily = daily_page("2026-10-16")
        provider.minutes = minute_page(start_clock="15:50")
        value = collector.collect()
        self.assertEqual(value["calendar"]["anchor_date"], "2026-10-17")
        value["calendar"]["anchor_date"] = "2026-10-18"
        result = self.sanitize(value, now="2026-10-18T04:00:00Z")
        self.assertEqual(result["calendar"]["error_code"], "calendar_stale")
        self.assertTrue(all(row["previous_close"]["status"] != "observed" for row in result["rows"]))
        self.assertEqual(ns(result["collected_at"]), ns(before_midnight))

    def test_expected_interval_count_requires_actual_bounded_integer(self):
        for now, count in ((datetime(2026, 10, 16, 13, 30, tzinfo=timezone.utc), False),
                           (datetime(2026, 10, 16, 13, 31, tzinfo=timezone.utc), True),
                           (NOW, "10"), (NOW, 10.0), (NOW, 11), (NOW, -1)):
            with self.subTest(kind=type(count).__name__, count=count):
                collector, provider = self.collector(clock=Clock(now))
                if now.hour == 13 and now.minute == 31:
                    provider.minutes = minute_page(start_clock="09:30", count=1)
                value = collector.collect()
                value["rows"][0]["minute_bars"]["expected_intervals"] = count
                rows = self.rows(self.sanitize(value, now=now))
                self.assertEqual(rows["AAPL"]["minute_bars"]["error_code"], "minute_bar_invalid")
                self.assertIn(rows["MSFT"]["minute_bars"]["status"], ("observed", "not_started"))

    def test_history_summary_contains_only_status_counts_and_stable_hash(self):
        collector, _ = self.collector()
        value = collector.collect()
        summary = price_history_summary(value)
        self.assertEqual(summary["archive"], "not_recorded")
        self.assertEqual(summary["requested_symbols"], list(SYMBOLS))
        self.assertEqual(len(summary["history_sha256"]), 64)
        self.assertEqual(summary, price_history_summary(deepcopy(value)))
        self.assertEqual(set(summary), {"schema_version", "collected_at", "feed", "requested_symbols",
                                        "history_sha256", "rows", "archive"})
        for row in summary["rows"]:
            self.assertEqual(set(row), {"symbol", "previous_close_status", "minute_bars_status", "bar_count"})
            self.assertEqual(row["bar_count"], 10)
        self.assertNotIn(EXACT_PRICE, json.dumps(summary))
        self.assertNotIn(PRIVATE, json.dumps(summary))
        changed = deepcopy(value)
        changed["rows"][0]["minute_bars"]["bars"][0]["close"] = "123.1"
        self.assertNotEqual(price_history_summary(changed)["history_sha256"], summary["history_sha256"])

    def test_real_observation_bar_request_is_bounded_raw_single_page_get(self):
        cfg = config_for()
        fixture = ObservationProvider(cfg)
        start, end = "2026-10-16T13:35:00Z", "2026-10-16T13:45:00Z"
        result = fixture.client.observation_bars(SYMBOLS, timeframe="1Min", start=start, end=end)
        self.assertEqual(result, fixture.history.minutes)
        call = fixture.calls[-1]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["host"], "data.alpaca.markets")
        self.assertEqual(call["path"], "/v2/stocks/bars")
        self.assertIsNone(call["payload"])
        self.assertEqual(set(call["params"]), {"symbols", "timeframe", "start", "end", "limit", "adjustment",
                                              "feed", "sort", "asof", "currency"})
        for key, expected in {"symbols": ",".join(SYMBOLS), "timeframe": "1Min", "limit": "1000",
                              "adjustment": "raw", "feed": "iex", "sort": "asc", "asof": "-", "currency": "USD"}.items():
            self.assertEqual(call["params"][key], [expected])
        count = len(fixture.calls)
        for symbols, frame, left, right in (((), "1Min", start, end), (("AAPL", "AAPL"), "1Min", start, end),
                                           (("bad symbol",), "1Min", start, end), (SYMBOLS, "1Hour", start, end),
                                           (SYMBOLS, "1Min", end, start),
                                           (SYMBOLS, "1Min", start, "2026-10-16T13:45:00.000000001Z")):
            with self.subTest(frame=frame, symbols=symbols), self.assertRaises(ValueError):
                fixture.client.observation_bars(symbols, timeframe=frame, start=left, end=right)
        for operation in (lambda: fixture.client.submit({}), lambda: fixture.client.cancel({}),
                          lambda: fixture.client._request("POST", "/v2/orders", payload={}),
                          lambda: fixture.client._request("DELETE", "/v2/orders/fixture")):
            with self.assertRaises(PaperDisabled):
                operation()
        self.assertEqual(len(fixture.calls), count)

    def test_real_stock_bar_json_preserves_decimal_literals_and_rejects_duplicates(self):
        client = ReadOnlyPaperClient(config_for(), clock=Clock())
        exact = b'{"bars":{"AAPL":[{"t":"2026-10-16T13:35:00Z","o":123.123456789012345678,"h":124,"l":122,"c":123.123456789012345678,"v":100}]},"next_page_token":null}'
        with patch.object(client._opener, "open", return_value=BytesResponse(exact)):
            page = client.observation_bars(("AAPL",), timeframe="1Min",
                start="2026-10-16T13:35:00Z", end="2026-10-16T13:45:00Z")
        self.assertIsInstance(page["bars"]["AAPL"][0]["c"], Decimal)
        self.assertEqual(str(page["bars"]["AAPL"][0]["c"]), EXACT_PRICE)
        for invalid in (b'{"bars":{},"bars":{}}', b'{"bars":{"AAPL":[{"c":NaN}]}}',
                        b'{"bars":{"AAPL":[{"c":Infinity}]}}'):
            with self.subTest(body_length=len(invalid)), patch.object(client._opener, "open", return_value=BytesResponse(invalid)):
                with self.assertRaises(BrokerError) as caught:
                    client.observation_bars(("AAPL",), timeframe="1Min",
                        start="2026-10-16T13:35:00Z", end="2026-10-16T13:45:00Z")
                self.assertNotIn(invalid.decode(), str(caught.exception))

    def test_publisher_history_is_optional_and_malformed_history_cannot_suppress_quotes(self):
        cfg = config_for(cloud=True)
        fixture, cloud = ObservationProvider(cfg), CloudTransport()
        publisher = Publisher(cfg, transport=cloud, clock=fixture.clock)
        history = PriceHistoryCollector(fixture.client, cfg, clock=fixture.clock).collect()
        watch = MarketWatchCollector(fixture.client, cfg, clock=fixture.clock).collect()
        def publish(**kwargs):
            return publisher.publish_worker_state(instance_id=INSTANCE, broker_verified=True,
                broker_checked_at=stamp(fixture.clock()), market_watch=watch, **kwargs)
        baseline = publish()
        self.assertNotIn("price_history", baseline["payload"])
        expected = deepcopy(baseline)
        full = publish(price_history=history)
        self.assertEqual(full["payload"].pop("price_history"), self.sanitize(history))
        self.assertEqual(full, expected)
        for malformed in ([], {}, {**history, "feed": "sip"}, {**history, "currency": "EUR"},
                          {**history, "requested_symbols": ["UNREQUESTED"]},
                          {**history, "collected_at": nano_stamp(NOW, 1)},
                          {**history, "rows": history["rows"] * 100}):
            with self.subTest(kind=type(malformed).__name__):
                row = publish(price_history=malformed)
                self.assertEqual(row, expected)
                self.assertEqual(row["payload"]["broker"]["status"], "connected")
                self.assertEqual(row["payload"]["data_feed"]["status"], "connected")
                self.assertIs(row["payload"]["execution_enabled"], False)
        injected = deepcopy(history)
        injected["order_intent"] = PRIVATE
        injected["rows"][0]["previous_close"]["raw_extra"] = PRIVATE
        row = publish(price_history=injected)
        self.assertIn("price_history", row["payload"])
        self.assertNotIn(PRIVATE, json.dumps(row))
        self.assertEqual(row["payload"]["strategy_review"], baseline["payload"]["strategy_review"])
        self.assertEqual(len(cloud.states), 10)

    def test_publisher_retains_cached_history_receipts_and_expires_it_independently(self):
        cfg = config_for(cloud=True)
        fixture, cloud = ObservationProvider(cfg), CloudTransport()
        publisher = Publisher(cfg, transport=cloud, clock=fixture.clock)
        history = PriceHistoryCollector(fixture.client, cfg, clock=fixture.clock).collect()
        before = deepcopy(history)
        fixture.clock.advance(121)
        watch = MarketWatchCollector(fixture.client, cfg, clock=fixture.clock).collect()
        row = publisher.publish_worker_state(instance_id=INSTANCE, broker_verified=True,
            broker_checked_at=stamp(fixture.clock()), market_watch=watch, price_history=history)
        self.assertEqual(row["payload"]["data_feed"]["status"], "connected")
        self.assertTrue(all(value["previous_close"]["status"] == "stale" for value in row["payload"]["price_history"]["rows"]))
        self.assertEqual(row["payload"]["price_history"]["rows"][0]["previous_close"]["received_at"],
                         history["rows"][0]["previous_close"]["received_at"])
        self.assertEqual(history, before)

    def test_default_observer_collects_history_before_quotes_and_journals_summary_only(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = config_for(directory, cloud=True)
            fixture, cloud = ObservationProvider(cfg), CloudTransport()
            publisher = Publisher(cfg, transport=cloud, clock=fixture.clock)
            with patch("momentum.data.Path.read_text", side_effect=AssertionError("Metadata reads are outside this milestone")), \
                 patch.object(fixture.client, "bars", side_effect=AssertionError("Unbounded strategy history must remain dormant")), \
                 ObservationWorker(cfg, provider=fixture.client, publisher=publisher,
                                   clock=fixture.clock, instance_id=INSTANCE) as worker:
                first = worker.cycle()
                self.assertIsNone(first["error_code"])
                self.assertTrue(first["broker"]["verified"])
                self.assertTrue(first["data_feed"]["verified"])
                self.assertFalse(first["accounting"]["complete"])
                self.assertIsNone(first["accounting"]["fees"])
                self.assertIn("accounting_history_start_missing", first["blockers"])
                self.assertIs(first["execution_enabled"], False)
                self.assertEqual(first["orders_submitted"], 0)
                self.assertTrue(first["cloud_state_published"])
                self.assertEqual(len(first["price_history"]["rows"][0]["minute_bars"]["bars"]), 10)
                self.assertEqual(cloud.states[-1]["payload"]["price_history"], first["price_history"])
                paths = [call["path"] for call in fixture.calls]
                bars_at = [i for i, path in enumerate(paths) if path == "/v2/stocks/bars"]
                self.assertEqual(len(bars_at), 2)
                self.assertLess(max(bars_at), paths.index("/v2/stocks/snapshots"))
                self.assertNotIn("/v2/account/activities", paths)
                fixture.clock.advance(1)
                second = worker.cycle()
                self.assertEqual(second["price_history"], first["price_history"])
                self.assertEqual(sum(call["path"] == "/v2/stocks/bars" for call in fixture.calls), 2)
                self.assertTrue(all(call["method"] == "GET" and call["payload"] is None for call in fixture.calls))
                self.assertEqual(worker.state()["latest_cycle"]["price_history"], first["price_history"])
                events = Journal(cfg.db_path).events(event_type="paper_observation_cycle")
                self.assertEqual(len(events), 2)
                for event in events:
                    self.assertEqual(event["payload"]["price_history"], price_history_summary(first["price_history"]))
                all_events = Journal(cfg.db_path).events()
                self.assertNotIn(EXACT_PRICE, json.dumps(all_events))
                self.assertNotIn(PRIVATE, json.dumps(all_events))
                self.assertFalse(Journal(cfg.db_path).events(event_type="paper_order_requested"))
                fixture.clock.advance(120)
                state = worker.state()
                self.assertFalse(state["data_connected"])
                self.assertEqual(state["latest_cycle"]["price_history"]["rows"][0]["minute_bars"]["status"], "stale")
                self.assertEqual(state["latest_cycle"]["price_history"]["collected_at"], first["price_history"]["collected_at"])

    def test_default_observer_history_failure_is_isolated_from_account_and_quote_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = config_for(directory, cloud=True)
            fixture, cloud = ObservationProvider(cfg), CloudTransport()
            publisher = Publisher(cfg, transport=cloud, clock=fixture.clock)
            failing = SimpleNamespace(collect=lambda: (_ for _ in ()).throw(RuntimeError(PRIVATE)))
            with ObservationWorker(cfg, provider=fixture.client, publisher=publisher,
                    price_history_collector=failing, clock=fixture.clock, instance_id=INSTANCE) as worker:
                result = worker.cycle()
                self.assertIsNone(result["error_code"])
                self.assertTrue(result["broker"]["verified"])
                self.assertTrue(result["data_feed"]["verified"])
                self.assertIn("price_history_unavailable", result["blockers"])
                self.assertIsNone(result["price_history"])
                self.assertTrue(result["cloud_state_published"])
                self.assertNotIn("price_history", cloud.states[-1]["payload"])
                self.assertEqual(cloud.states[-1]["payload"]["data_feed"]["status"], "connected")
                self.assertNotIn(PRIVATE, json.dumps(result))
                self.assertNotIn(PRIVATE, json.dumps(Journal(cfg.db_path).events()))


if __name__ == "__main__":
    unittest.main()
