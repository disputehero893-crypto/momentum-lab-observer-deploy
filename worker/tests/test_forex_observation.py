"""Offline OANDA practice forex watch projection tests; invented quotes only."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import unittest

from momentum.forex_observation import (
    ForexWatchCollector, _pairs, _price, build_forex_watch, sanitize_forex_watch,
)
from momentum.oanda import OandaPracticeError

PAIRS = ("EUR/USD", "GBP/USD", "USD/JPY")
SOURCE = "2026-10-19T13:30:04.123456789Z"
RECEIVED = "2026-10-19T13:30:04.123456790Z"
COLLECTED = "2026-10-19T13:30:04.123456791Z"


def price(pair, bid="1.123450000000000001", ask="1.123460000000000001",
          at=SOURCE, tradeable=True):
    if pair == "USD/JPY":
        bid, ask = "155.11", "155.13"
    return {"instrument": pair.replace("/", "_"), "tradeable": tradeable, "time": at,
            "bids": [{"price": bid, "liquidity": 1000},
                     {"price": "1.0" if pair != "USD/JPY" else "155.1", "liquidity": 5000}],
            "asks": [{"price": ask, "liquidity": 1000},
                     {"price": "2.0" if pair != "USD/JPY" else "155.2", "liquidity": 5000}]}


def raw():
    return {"prices": [price(p) for p in PAIRS], "time": RECEIVED}


def build(response=None, pairs=PAIRS, *, receipt=RECEIVED, collected=COLLECTED):
    return build_forex_watch(raw() if response is None else response, pairs,
                             received_at=receipt, collected_at=collected)


class ForexProjectionTests(unittest.TestCase):
    def test_readonly_contract_with_decimal_nanos(self):
        result = build()
        self.assertEqual(result["schema_version"], "1.0")
        self.assertEqual(result["asset_class"], "forex")
        self.assertEqual(result["source"], "oanda_practice")
        self.assertTrue(result["observation_only"])
        self.assertFalse(result["execution_enabled"])
        self.assertEqual(result["requested_pairs"], list(PAIRS))
        self.assertEqual([r["status"] for r in result["rows"]], ["fresh"] * 3)
        self.assertEqual(result["rows"][0]["bid"], "1.123450000000000001")
        self.assertEqual(result["rows"][0]["source_at"], SOURCE)
        self.assertEqual(result["rows"][2]["ask"], "155.13")

    def test_rejects_equity_crypto_and_duplicate_pairs(self):
        for value in (("AAPL",), ("BTC/USD",), ("EUR/USD", "EUR/USD"), (),
                      ("eur/usd", "EUR/USD"), ("GBP/USD", "foo/bar"), ("USD/USDT",)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _pairs(value)

    def test_missing_and_nontradeable_are_explicit(self):
        result = build({"prices": [price("EUR/USD")]})
        self.assertEqual(result["rows"][0]["status"], "fresh")
        self.assertEqual(result["rows"][1]["status"], "missing")
        self.assertEqual(result["rows"][1]["error_code"], "pair_missing")
        r = build({"prices": [price("EUR/USD", tradeable=False)]}, pairs=("EUR/USD",))
        self.assertEqual(r["rows"][0]["status"], "missing")
        self.assertIsNone(r["rows"][0]["bid"])

    def test_crossed_duplicate_and_invalid_quotes(self):
        r = build({"prices": [price("EUR/USD"), price("EUR/USD")]}, pairs=("EUR/USD",))
        self.assertEqual(r["rows"][0]["error_code"], "quote_invalid")
        for row in (price("EUR/USD", bid="1.50", ask="1.00"),
                    {**price("EUR/USD"), "bids": []},
                    {**price("EUR/USD"), "asks": [{"price": "-1"}]},
                    {**price("EUR/USD"), "bids": [{"price": 1.12}]}):
            got = build({"prices": [row]}, pairs=("EUR/USD",))
            self.assertEqual(got["rows"][0]["error_code"], "quote_invalid")

    def test_stale_preserves_source_and_sanitize_does_not_promote(self):
        r = build({"prices": [price("EUR/USD", at="2026-10-19T13:25:00Z")]}, pairs=("EUR/USD",))
        self.assertEqual(r["rows"][0]["status"], "stale")
        self.assertEqual(r["rows"][0]["source_at"], "2026-10-19T13:25:00Z")
        later = "2026-10-19T13:31:05Z"
        checked = sanitize_forex_watch(build(), allowed_pairs=PAIRS, now=later)
        self.assertTrue(all(row["status"] == "stale" for row in checked["rows"]))
        self.assertFalse(checked["execution_enabled"])

    def test_envelope_errors_are_safe(self):
        for response in (None, {"raw_message": "SECRET"}, {"prices": "INVALID"}, {"prices": [None] * 51}):
            r = build_forex_watch(response, ("EUR/USD",), received_at=RECEIVED, collected_at=COLLECTED)
            self.assertEqual(r["rows"][0]["error_code"], "market_data_unavailable")
            self.assertNotIn("SECRET", json.dumps(r))
        self.assertEqual(_price(Decimal("1.000000000000000001")), "1.000000000000000001")

    def test_collector_good_and_failed_provider(self):
        class Provider:
            def pricing(self, pairs):
                return raw()
        t = datetime(2026, 10, 19, 13, 30, 4, 124000, tzinfo=timezone.utc)
        r = ForexWatchCollector(Provider(), pairs=PAIRS, clock=lambda: t).collect()
        self.assertEqual([row["status"] for row in r["rows"]], ["fresh"] * 3)
        class BadProvider:
            def pricing(self, pairs):
                raise OandaPracticeError("market_data_unavailable")
        r2 = ForexWatchCollector(BadProvider(), pairs=PAIRS, clock=lambda: t).collect()
        self.assertEqual([row["status"] for row in r2["rows"]], ["error"] * 3)
        self.assertFalse(r2["execution_enabled"])


if __name__ == "__main__":
    unittest.main()
