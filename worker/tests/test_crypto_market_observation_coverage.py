"""Offline branch coverage for crypto and Market Watch observation guards.

Tests only. Every price, timestamp and session below is an invented synthetic
example; no provider, broker, network, credentials or saved worker state are
used. Each case asserts a fail-closed outcome: an invalid input is rejected or
projected as an error row, never promoted to a fresh observation.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from momentum import crypto_observation as co
from momentum import market_observation as mo
from momentum.crypto_observation import (
    CryptoDataError, CryptoQuoteReader, CryptoWatchCollector, build_crypto_watch,
    crypto_watch_summary, sanitize_crypto_watch,
)
from momentum.market_observation import (
    MarketWatchCollector, market_watch_from_snapshots, sanitize_market_watch,
)

PAIRS = ("BTC/USD", "ETH/USD")
SOURCE = "2026-10-18T23:59:59.100Z"
RECEIPT = "2026-10-18T23:59:59.200Z"
COLLECTED = "2026-10-18T23:59:59.300Z"
NOW = "2026-10-18T23:59:59.400Z"


def crypto_raw():
    return {"quotes": {"BTC/USD": {"bp": "100.00", "ap": "100.01", "t": SOURCE},
                       "ETH/USD": {"bp": "20.00", "ap": "20.01", "t": SOURCE}}}


def crypto_report():
    return build_crypto_watch(crypto_raw(), PAIRS, received_at=RECEIPT, collected_at=COLLECTED)


class CryptoHelperTests(unittest.TestCase):
    def test_redirect_is_refused(self):
        with self.assertRaises(CryptoDataError):
            co._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://example.invalid/")

    def test_timestamp_rejects_naive_and_non_string(self):
        with self.assertRaises(ValueError):
            co._timestamp(datetime(2026, 10, 18, 12, 0, 0))
        for bad in (123, None, "2026-10-18T00:00:00Z" + "0" * 60):
            with self.assertRaises(ValueError):
                co._timestamp(bad)
        # Aware datetime is accepted and normalized to UTC.
        stamp, _ = co._timestamp(datetime(2026, 10, 18, 7, 0, tzinfo=timezone(timedelta(hours=-5))))
        self.assertEqual(stamp, "2026-10-18T12:00:00Z")

    def test_price_rejects_overlong_literal(self):
        with self.assertRaises(ValueError):
            co._price("1" + "0" * 70)

    def test_build_rejects_bad_max_age_and_non_dict_quote(self):
        for bad in (0, 121, True, "60"):
            with self.assertRaises(ValueError):
                build_crypto_watch(crypto_raw(), PAIRS, received_at=RECEIPT,
                                   collected_at=COLLECTED, max_age_seconds=bad)
        data = crypto_raw()
        data["quotes"]["ETH/USD"] = ["not", "a", "dict"]
        rows = build_crypto_watch(data, PAIRS, received_at=RECEIPT, collected_at=COLLECTED)["rows"]
        self.assertEqual(rows[0]["status"], "fresh")
        self.assertEqual((rows[1]["status"], rows[1]["error_code"], rows[1]["bid"]),
                         ("error", "quote_invalid", None))

    def test_nonfinite_json_constant_is_refused(self):
        with self.assertRaises(ValueError):
            co._constant("NaN")


class CryptoSanitizeTests(unittest.TestCase):
    def sanitize(self, report, now=NOW, maximum=60):
        return sanitize_crypto_watch(report, allowed_pairs=PAIRS, now=now, max_age_seconds=maximum)

    def test_valid_report_round_trips_fresh(self):
        out = self.sanitize(crypto_report())
        self.assertEqual([r["status"] for r in out["rows"]], ["fresh", "fresh"])
        self.assertIs(out["execution_enabled"], False)

    def test_configured_freshness_must_be_int_in_range(self):
        for bad in (0, 121, 60.0, True):
            with self.assertRaises(ValueError):
                self.sanitize(crypto_report(), maximum=bad)

    def test_future_collection_rejected(self):
        with self.assertRaises(ValueError):
            self.sanitize(crypto_report(), now="2026-10-18T23:59:59.250Z")

    def test_receipt_after_collection_rejected(self):
        report = crypto_report()
        report["rows"][0]["received_at"] = "2026-10-18T23:59:59.350Z"
        with self.assertRaises(ValueError):
            self.sanitize(report)

    def test_fresh_claim_with_error_or_stale_without_code_rejected(self):
        report = crypto_report()
        report["rows"][0]["error_code"] = "quote_stale"
        with self.assertRaises(ValueError):
            self.sanitize(report)
        report = crypto_report()
        report["rows"][0]["status"] = "stale"
        with self.assertRaises(ValueError):
            self.sanitize(report)
        report = crypto_report()
        report["rows"][0]["received_at"] = None
        with self.assertRaises(ValueError):
            self.sanitize(report)

    def test_error_row_with_price_or_without_code_rejected(self):
        base = crypto_report()
        base["rows"][1] = {"pair": "ETH/USD", "status": "error", "bid": None, "ask": None,
                           "source_at": None, "received_at": RECEIPT, "error_code": "quote_invalid"}
        out = self.sanitize(deepcopy(base))
        self.assertEqual((out["rows"][1]["status"], out["rows"][1]["error_code"]), ("error", "quote_invalid"))
        for field, value in (("bid", "1"), ("ask", "1"), ("source_at", SOURCE), ("error_code", None)):
            bad = deepcopy(base)
            bad["rows"][1][field] = value
            with self.assertRaises(ValueError):
                self.sanitize(bad)

    def test_missing_row_without_receipt_passes_and_old_report_expires(self):
        report = crypto_report()
        report["rows"][1] = {"pair": "ETH/USD", "status": "missing", "bid": None, "ask": None,
                             "source_at": None, "received_at": None, "error_code": "pair_missing"}
        out = self.sanitize(report, now="2026-10-19T00:05:00Z")
        self.assertEqual(out["rows"][0]["status"], "stale")
        self.assertEqual(out["rows"][0]["error_code"], "quote_stale")
        self.assertEqual(out["rows"][1]["status"], "missing")

    def test_summary_bounds(self):
        self.assertIsNone(crypto_watch_summary("nope"))
        self.assertIsNone(crypto_watch_summary({"rows": "x"}))
        self.assertIsNone(crypto_watch_summary({"rows": [{}] * 11}))
        self.assertEqual(crypto_watch_summary({"rows": [{"status": "fresh"}, 7]})["statuses"],
                         ["fresh", "error"])


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CryptoReaderHttpTests(unittest.TestCase):
    def reader(self):
        return CryptoQuoteReader(SimpleNamespace(mode="paper", api_key="k", api_secret="s"))

    def test_http_parses_bounded_json_object(self):
        reader = self.reader()
        body = json.dumps({"quotes": {}}).encode()
        with patch.object(reader._opener, "open", return_value=_Response(body)):
            self.assertEqual(reader.latest_quotes(PAIRS), {"quotes": {}})

    def test_http_rejects_oversize_non_object_duplicate_and_nan(self):
        bodies = (b" " * (co.MAX_RESPONSE_BYTES + 1), b"[1, 2]",
                  b'{"a": 1, "a": 2}', b'{"a": NaN}', b"not json")
        for body in bodies:
            reader = self.reader()
            with patch.object(reader._opener, "open", return_value=_Response(body)):
                with self.assertRaises(CryptoDataError) as ctx:
                    reader.latest_quotes(PAIRS)
                self.assertEqual(str(ctx.exception), "market_data_unavailable")

    def test_transport_generic_error_is_collapsed(self):
        def boom(*_):
            raise KeyError("PRIVATE_TEXT")
        reader = CryptoQuoteReader(SimpleNamespace(mode="paper", api_key="k", api_secret="s"), transport=boom)
        with self.assertRaises(CryptoDataError) as ctx:
            reader.latest_quotes(PAIRS)
        self.assertNotIn("PRIVATE", str(ctx.exception))

    def test_collector_rejects_bad_max_age(self):
        for bad in (0, 121, "60", None):
            with self.assertRaises(ValueError):
                CryptoWatchCollector(SimpleNamespace(), PAIRS, max_age_seconds=bad)


# ---------------------------------------------------------------- Market Watch
SYMBOLS = ("AAPL", "MSFT")
MNOW = datetime(2026, 10, 16, 14, 0, 0, tzinfo=timezone.utc)  # 10:00 AM New York, a Friday


def z(dt):
    return dt.isoformat().replace("+00:00", "Z")


def mrow(symbol="AAPL", **extra):
    row = {"symbol": symbol, "feed": "iex", "bid": "10.00", "ask": "10.01",
           "source_at": z(MNOW - timedelta(seconds=0.5)), "received_at": z(MNOW - timedelta(seconds=0.2))}
    row.update(extra)
    return row


def session(**extra):
    value = {"scope": "regular", "source": "alpaca_calendar", "timezone": "America/New_York",
             "date": "2026-10-16", "is_trading_day": True, "checked_at": z(MNOW - timedelta(seconds=1)),
             "open_at": "2026-10-16T13:30:00Z", "close_at": "2026-10-16T20:00:00Z"}
    value.update(extra)
    return value


def envelope(rows=None, sess=None):
    return {"schema_version": "1.0", "source": "alpaca_market_data", "feed": "iex",
            "collected_at": z(MNOW - timedelta(seconds=0.1)), "session": sess,
            "rows": [mrow()] if rows is None else rows}


def sanitize(value):
    return sanitize_market_watch(value, allowed_symbols=SYMBOLS, feed="iex", now=MNOW)


class MarketHelperTests(unittest.TestCase):
    def test_maximum_and_price_guards(self):
        for bad in (0, 5.5, float("nan"), True, "2"):
            with self.assertRaises(ValueError):
                mo._maximum(bad)
        for bad in ("abc", True, None, "1" * 70, "-1", "0"):
            with self.assertRaises(ValueError):
                mo._price(bad)
        self.assertEqual(mo._price("10.500"), "10.5")

    def test_rows_must_be_bounded_list(self):
        for rows in ("x", [mrow()] * 11):
            with self.assertRaises(ValueError):
                sanitize(envelope(rows=rows))


class MarketRowTests(unittest.TestCase):
    def test_unknown_incoming_status_is_quote_invalid(self):
        out = sanitize(envelope(rows=[mrow(status="promoted")]))
        self.assertEqual((out["rows"][0]["status"], out["rows"][0]["error_code"]), ("error", "quote_invalid"))

    def test_trade_with_unknown_status_is_trade_invalid(self):
        trade = {"price": "10.00", "source_at": z(MNOW - timedelta(seconds=0.5)),
                 "received_at": z(MNOW - timedelta(seconds=0.2)), "status": "fresh!"}
        out = sanitize(envelope(rows=[mrow(last_trade=trade)]))
        self.assertIsNone(out["rows"][0]["last_trade"])
        self.assertEqual(out["rows"][0]["last_trade_error_code"], "trade_invalid")


class MarketSessionTests(unittest.TestCase):
    def check(self, sess):
        return sanitize(envelope(sess=sess))["session"]

    def test_valid_open_session(self):
        out = self.check(session())
        self.assertEqual((out["status"], out["error_code"]), ("open", None))

    def test_non_dict_and_wrong_identity_are_invalid(self):
        for sess in ("open", session(scope="extended"), session(source="other"), session(timezone="UTC")):
            out = self.check(sess)
            self.assertEqual((out["status"], out["error_code"]), ("unknown", "calendar_invalid"))

    def test_non_bool_trading_day_invalid(self):
        out = self.check(session(is_trading_day="yes"))
        self.assertEqual((out["status"], out["error_code"]), ("unknown", "calendar_invalid"))

    def test_open_not_before_close_invalid(self):
        out = self.check(session(open_at="2026-10-16T20:00:00Z", close_at="2026-10-16T13:30:00Z"))
        self.assertEqual(out["error_code"], "calendar_invalid")

    def test_closed_day_with_hours_invalid_and_without_hours_closed(self):
        out = self.check(session(is_trading_day=False))
        self.assertEqual(out["error_code"], "calendar_invalid")
        out = self.check(session(is_trading_day=False, open_at=None, close_at=None))
        self.assertEqual((out["status"], out["error_code"]), ("closed", None))


class FakeProvider:
    def __init__(self, calendar=None, snapshots=None):
        self.cal = calendar
        self.snaps = snapshots
        self.calendar_calls = 0

    def calendar(self, start, end):
        self.calendar_calls += 1
        if isinstance(self.cal, Exception):
            raise self.cal
        return self.cal

    def snapshots(self, symbols):
        return self.snaps


def snap(symbol="AAPL", **record):
    return {"latestQuote": {"bp": "10.00", "ap": "10.01", "t": z(MNOW - timedelta(seconds=0.5))}, **record}


class MarketCollectorTests(unittest.TestCase):
    def collector(self, provider, clock=None):
        config = SimpleNamespace(watchlist=list(SYMBOLS), data_feed="iex", quote_max_age_seconds=2)
        return MarketWatchCollector(provider, config, clock=clock or (lambda: MNOW))

    def test_unsupported_feed_rejected(self):
        config = SimpleNamespace(watchlist=["AAPL"], data_feed="otc", quote_max_age_seconds=2)
        with self.assertRaises(ValueError):
            MarketWatchCollector(FakeProvider(), config)

    def test_calendar_cache_reused_within_window(self):
        provider = FakeProvider([{"date": "2026-10-16", "open": "09:30", "close": "16:00"}],
                                {"AAPL": snap(), "MSFT": snap()})
        collector = self.collector(provider)
        first = collector.collect()
        second = collector.collect()
        self.assertEqual(first["session"]["status"], "open")
        self.assertEqual(second["session"]["status"], "open")
        self.assertEqual(provider.calendar_calls, 1)

    def test_malformed_calendar_record_invalid(self):
        for cal in ([{"date": "2026-10-16", "open": "9:30", "close": "16:00"}],
                    [{"date": "2026-10-15", "open": "09:30", "close": "16:00"}],
                    [{}, {}], "x"):
            provider = FakeProvider(cal, {"AAPL": snap(), "MSFT": snap()})
            out = self.collector(provider).collect()
            self.assertEqual((out["session"]["status"], out["session"]["error_code"]),
                             ("unknown", "calendar_invalid"), cal)

    def test_calendar_provider_error_unavailable(self):
        provider = FakeProvider(RuntimeError("PRIVATE"), {"AAPL": snap(), "MSFT": snap()})
        out = self.collector(provider).collect()
        self.assertEqual(out["session"]["error_code"], "calendar_unavailable")

    def test_invalid_quote_and_trade_shapes_isolated(self):
        provider = FakeProvider([], {"AAPL": {"latestQuote": "bad"},
                                     "MSFT": snap(latestTrade="bad")})
        out = self.collector(provider).collect()
        aapl, msft = out["rows"]
        self.assertEqual((aapl["status"], aapl["error_code"]), ("error", "quote_invalid"))
        self.assertEqual(msft["status"], "fresh")
        self.assertEqual(msft["last_trade_error_code"], "trade_invalid")
        self.assertEqual(out["session"]["status"], "closed")


class InjectedSnapshotTests(unittest.TestCase):
    def build(self, snapshots):
        return market_watch_from_snapshots(snapshots, allowed_symbols=SYMBOLS, feed="iex", now=MNOW)

    def test_bounds_and_synthetic_refusal(self):
        for bad in ("x", [{}] * 11):
            with self.assertRaises(ValueError):
                self.build(bad)
        with self.assertRaises(ValueError):
            self.build([{"symbol": "AAPL", "provenance": {"synthetic": True}, "quote": {}}])

    def test_non_dict_items_and_quotes_are_skipped(self):
        out = self.build(["junk", {"symbol": "AAPL", "quote": "bad"}])
        self.assertEqual([r["error_code"] for r in out["rows"]], ["symbol_missing", "symbol_missing"])
        self.assertEqual(out["session"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
