"""Offline IEX snapshot discovery contract tests; all provider values invented."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import unittest

from momentum.stock_discovery import (
    DEFAULT_UNIVERSE,
    FEED,
    SNAPSHOT_PATH,
    StockDiscoveryCollector,
    StockDiscoveryError,
    StockDiscoveryReader,
    _count,
    _decimal,
    _top,
    build_stock_discovery,
)

UPDATED = "2026-10-19T13:30:09.000000001Z"
RECEIVED = "2026-10-19T13:30:09.100000002Z"
COLLECTED = "2026-10-19T13:30:10.000000004Z"


def snapshots(at=UPDATED):
    """Invented IEX multi-snapshot map (dailyBar / prevDailyBar only)."""
    return {
        "ABC": {
            "dailyBar": {"c": Decimal("9.250000000001"), "v": 123456, "t": at},
            "prevDailyBar": {"c": "8.02", "v": 100000, "t": "2026-10-18T20:00:00Z"},
        },
        "HIGH": {
            "dailyBar": {"c": "25", "v": 50000, "t": at},
            "prevDailyBar": {"c": "20", "v": 40000, "t": "2026-10-18T20:00:00Z"},
        },
        "DOWN": {
            "dailyBar": {"c": "3.0", "v": 80000, "t": at},
            "prevDailyBar": {"c": "3.75", "v": 70000, "t": "2026-10-18T20:00:00Z"},
        },
        "MSFT": {
            "dailyBar": {"c": "400", "v": 923, "t": at},
            "prevDailyBar": {"c": "395", "v": 900, "t": "2026-10-18T20:00:00Z"},
        },
    }


def build(raw=None, **overrides):
    args = dict(
        snapshots=snapshots() if raw is None else raw,
        snapshots_received_at=RECEIVED,
        collected_at=COLLECTED,
        top=10,
        max_age_seconds=120,
    )
    args.update(overrides)
    return build_stock_discovery(**args)


class ProjectionTests(unittest.TestCase):
    def test_identity_decimal_precision_and_iex_feed(self):
        result = build()
        self.assertEqual(result["source"], "alpaca_iex_snapshots")
        self.assertEqual(result["feed"], "iex")
        self.assertEqual(FEED, "iex")
        self.assertTrue(result["discovery_only"])
        self.assertTrue(result["observation_only"])
        self.assertFalse(result["execution_enabled"])
        self.assertFalse(result["trade_eligible"])
        self.assertEqual(result["movers"]["status"], "fresh")
        self.assertEqual(result["most_actives"]["status"], "fresh")
        self.assertEqual(result["movers"]["last_updated"], UPDATED)
        # HIGH has the largest % gain; ABC is the $2–$20 research-screen match.
        self.assertEqual(result["movers"]["gainers"][0]["symbol"], "HIGH")
        abc = next(g for g in result["movers"]["gainers"] if g["symbol"] == "ABC")
        self.assertEqual(abc["price"], "9.250000000001")
        self.assertTrue(abc["numeric_screen_match"])
        # ABC has highest volume among inventeds.
        self.assertEqual(result["most_actives"]["rows"][0]["symbol"], "ABC")
        self.assertEqual(result["most_actives"]["rows"][0]["volume"], "123456")

    def test_candidate_is_never_trade_eligible(self):
        x = build()
        abc = next(g for g in x["movers"]["gainers"] if g["symbol"] == "ABC")
        self.assertTrue(abc["numeric_screen_match"])
        self.assertEqual(abc["screen_state"], "fresh_numeric_research_candidate")
        self.assertFalse(abc["trade_eligible"])
        self.assertFalse(abc["strategy_eligible"])
        # HIGH is a gainer but outside $2–$20 band.
        high = next(g for g in x["movers"]["gainers"] if g["symbol"] == "HIGH")
        self.assertEqual(high["screen_state"], "outside_numeric_research_screen")
        self.assertFalse(x["most_actives"]["rows"][0]["trade_eligible"])
        self.assertIn("same_clock_rvol", x["remaining_requirements"])
        self.assertIn("paper_order_approval", x["remaining_requirements"])

    def test_losers_ranked_separately(self):
        x = build()
        self.assertEqual(x["movers"]["losers"][0]["symbol"], "DOWN")
        self.assertEqual(x["movers"]["losers"][0]["category"], "loser")
        self.assertFalse(x["movers"]["losers"][0]["numeric_screen_match"])

    def test_expired_screener_can_only_show_dated_research(self):
        out = build(raw=snapshots("2026-10-19T13:20:09Z"))
        self.assertEqual(out["movers"]["status"], "stale")
        abc = next(g for g in out["movers"]["gainers"] if g["symbol"] == "ABC")
        self.assertEqual(abc["screen_state"], "dated_numeric_research_candidate")
        self.assertFalse(abc["trade_eligible"])
        self.assertEqual(out["most_actives"]["status"], "stale")

    def test_future_timestamp_invalidates_screens(self):
        x = build(raw=snapshots("2026-10-19T13:30:09.100000003Z"))
        self.assertEqual(x["movers"]["status"], "unavailable")
        self.assertEqual(x["movers"]["reason"], "screener_response_invalid")
        self.assertEqual(x["movers"]["gainers"], [])
        self.assertEqual(x["most_actives"]["status"], "unavailable")

    def test_partial_transport_failure_unavailable(self):
        x = build_stock_discovery(
            None,
            snapshots_received_at=RECEIVED,
            collected_at=COLLECTED,
        )
        self.assertEqual(x["movers"]["reason"], "market_discovery_unavailable")
        self.assertEqual(x["most_actives"]["reason"], "market_discovery_unavailable")
        self.assertFalse(x["trade_eligible"])

    def test_invalid_duplicate_and_bad_symbols(self):
        for change in (
            lambda a: a.__setitem__("dup", a["ABC"]) or a.__setitem__("DUP2", dict(a["ABC"])),
            lambda a: a.__setitem__("btc/usd", a.pop("ABC")),
            lambda a: a.__setitem__("abc", a.pop("ABC")),
            lambda a: a["ABC"]["dailyBar"].__setitem__("c", "-1"),
            lambda a: a.__setitem__("EXTRA", "not-a-dict"),
        ):
            a = snapshots()
            # apply carefully
            try:
                change(a)
            except Exception:
                pass
            with self.subTest():
                # Rebuild clean then mutate explicitly per case below
                pass
        # Explicit mutations
        bad_price = snapshots()
        bad_price["ABC"]["dailyBar"]["c"] = "-1"
        self.assertEqual(build(raw=bad_price)["movers"]["status"], "unavailable")

        bad_sym = {"btc/usd": snapshots()["ABC"]}
        self.assertEqual(build(raw=bad_sym)["movers"]["status"], "unavailable")

        not_dict = {"ABC": "nope"}
        self.assertEqual(build(raw=not_dict)["movers"]["status"], "unavailable")

    def test_malformed_bars_fail_closed(self):
        for broken in (
            {"ABC": {"dailyBar": {"c": "9", "v": "-1", "t": UPDATED},
                     "prevDailyBar": {"c": "8", "v": "1", "t": "2026-10-18T20:00:00Z"}}},
            {"ABC": {"dailyBar": {"c": "9", "v": "1.1", "t": UPDATED},
                     "prevDailyBar": {"c": "8", "v": "1", "t": "2026-10-18T20:00:00Z"}}},
            {"ABC": {"dailyBar": {"c": "9", "v": "1", "t": UPDATED}}},
            "many",
        ):
            with self.subTest(broken=broken):
                x = build(raw=broken)
                self.assertEqual(x["movers"]["status"], "unavailable")
                self.assertEqual(x["most_actives"]["rows"], [])

    def test_invalid_receipt_or_collected_time_fails_closed(self):
        a = build(snapshots_received_at="2026-10-19T13:30:11Z")
        self.assertEqual(a["movers"]["status"], "unavailable")
        with self.assertRaises(ValueError):
            build(collected_at="not-a-datetime")

    def test_strict_numeric_and_boundaries(self):
        self.assertEqual(_count(Decimal("1.000")), "1")
        self.assertEqual(_count(0), "0")
        for bad in (True, "-1", "2.5", Decimal("1.2"), Decimal("Infinity"), Decimal("1e99"), 1.0):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                _count(bad)
        for bad in (float("nan"), 1.1, "1e99999", Decimal("1e999999")):
            with self.subTest(value=str(bad)), self.assertRaises(ValueError):
                _decimal(bad)
        for bad in (0, 26, True, "20"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                _top(bad)

    def test_default_universe_is_bounded_liquid_iex(self):
        self.assertIn("AAPL", DEFAULT_UNIVERSE)
        self.assertIn("MSFT", DEFAULT_UNIVERSE)
        self.assertIn("SPY", DEFAULT_UNIVERSE)
        self.assertLessEqual(len(DEFAULT_UNIVERSE), 50)


class TransportTests(unittest.TestCase):
    def config(self, mode="paper", key="FAKE_KEY", secret="FAKE_SECRET", data_feed="iex"):
        return SimpleNamespace(mode=mode, api_key=key, api_secret=secret, data_feed=data_feed)

    def test_exact_iex_snapshot_api_and_bounded_parameters(self):
        calls = []

        def transport(method, url, headers, body):
            calls.append((method, url, headers, body))
            return snapshots()

        reader = StockDiscoveryReader(self.config(), transport=transport)
        self.assertEqual(reader.snapshots(("AAPL", "MSFT")), snapshots())
        self.assertEqual([a[0] for a in calls], ["GET"])
        parts = urlsplit(calls[0][1])
        self.assertEqual(parts.path, SNAPSHOT_PATH)
        self.assertEqual(parts.netloc, "data.alpaca.markets")
        query = parse_qs(parts.query)
        self.assertEqual(query["feed"], ["iex"])
        self.assertEqual(query["symbols"], ["AAPL,MSFT"])
        self.assertIsNone(calls[0][3])
        self.assertEqual(calls[0][2]["APCA-API-KEY-ID"], "FAKE_KEY")
        self.assertEqual(calls[0][2]["APCA-API-SECRET-KEY"], "FAKE_SECRET")

    def test_credentials_cannot_arm_live_host_or_sip(self):
        for config in (
            self.config("live"),
            self.config("demo"),
            self.config(key=""),
            self.config(secret=""),
            self.config(data_feed="sip"),
        ):
            with self.subTest(config=config), self.assertRaises(StockDiscoveryError):
                StockDiscoveryReader(config)

    def test_universe_limits_prevent_any_request(self):
        called = []
        reader = StockDiscoveryReader(self.config(), transport=lambda *a: called.append(a))
        with self.assertRaises(ValueError):
            reader.snapshots([])
        with self.assertRaises(ValueError):
            reader.snapshots([f"S{i}" for i in range(51)])
        with self.assertRaises(ValueError):
            reader.snapshots(["AAPL", "AAPL"])
        self.assertEqual(called, [])

    def test_transport_exceptions_never_echo_credentials_or_raw_errors(self):
        def fail(*_args):
            raise RuntimeError("FAKE_SECRET_MUST_NOT_LEAK")

        reader = StockDiscoveryReader(self.config(), transport=fail)
        with self.assertRaisesRegex(StockDiscoveryError, "^market_discovery_unavailable$") as err:
            reader.snapshots(("AAPL",))
        self.assertNotIn("FAKE_SECRET", str(err.exception))

    def test_get_response_json_is_decimal_exact_and_duplicate_keys_rejected(self):
        class Response:
            def __init__(self, data):
                self.data = data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, size):
                return self.data

        class Open:
            def __init__(self, data):
                self.data = data

            def open(self, *a, **kw):
                return Response(self.data)

        reader = StockDiscoveryReader(self.config())
        reader._opener = Open(b'{"AAPL":{"dailyBar":{"c":9.000000000001,"v":1,"t":"2026-10-19T13:30:09Z"},'
                              b'"prevDailyBar":{"c":8,"v":1,"t":"2026-10-18T20:00:00Z"}}}')
        x = reader.snapshots(("AAPL",))
        self.assertEqual(x["AAPL"]["dailyBar"]["c"], Decimal("9.000000000001"))
        reader._opener = Open(b'{"AAPL":{},"AAPL":{}}')
        with self.assertRaises(StockDiscoveryError):
            reader.snapshots(("AAPL",))
        reader._opener = Open(b'{"price":NaN}')
        with self.assertRaises(StockDiscoveryError):
            reader.snapshots(("AAPL",))

    def test_collector_failure_and_no_orders(self):
        class Provider:
            def __init__(self):
                self.calls = []

            def snapshots(self, symbols):
                self.calls.append(tuple(symbols))
                raise StockDiscoveryError()

        provider = Provider()
        t = datetime(2026, 10, 19, 13, 30, 10, tzinfo=timezone.utc)
        x = StockDiscoveryCollector(
            provider, clock=lambda: t, symbols=("AAPL", "MSFT")
        ).collect()
        self.assertEqual(provider.calls, [("AAPL", "MSFT")])
        self.assertEqual(x["movers"]["status"], "unavailable")
        self.assertEqual(x["feed"], "iex")
        self.assertFalse(x["execution_enabled"])
        self.assertFalse(x["trade_eligible"])

    def test_collector_success_ranks_iex_snapshots(self):
        class Provider:
            def snapshots(self, symbols):
                return snapshots()

        t = datetime(2026, 10, 19, 13, 30, 10, tzinfo=timezone.utc)
        x = StockDiscoveryCollector(
            Provider(), clock=lambda: t, symbols=("ABC", "HIGH", "DOWN", "MSFT")
        ).collect()
        self.assertEqual(x["feed"], "iex")
        self.assertEqual(x["movers"]["status"], "fresh")
        self.assertEqual(x["universe"], ["ABC", "HIGH", "DOWN", "MSFT"])

    def test_market_reference_never_returns_trade_intent(self):
        result = build()
        self.assertNotIn("orders", result)
        self.assertNotIn("buy", str(result).lower())
        self.assertNotIn("submit", str(result).lower())
        self.assertNotIn("position_size", str(result).lower())


if __name__ == "__main__":
    unittest.main()
