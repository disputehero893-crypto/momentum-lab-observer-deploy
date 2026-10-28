"""Offline unit tests for Alpaca spot-crypto quotes; no broker access or orders."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit

from momentum.crypto_observation import (
    CryptoDataError, CryptoQuoteReader, CryptoWatchCollector,
    _pairs, _price, _timestamp, build_crypto_watch,
)

PAIRS = ("BTC/USD", "ETH/USD")
SOURCE = "2026-10-18T23:59:59.123456789Z"
RECEIPT = "2026-10-18T23:59:59.123456790Z"
COLLECTED = "2026-10-18T23:59:59.123456791Z"


def raw(btc=None, eth=None):
    btc = btc or {"bp": Decimal("67250.000000000000000001"),
                  "ap": Decimal("67250.000000000000000002"), "t": SOURCE}
    eth = eth or {"bp": Decimal("2660.11"), "ap": Decimal("2660.12"), "t": SOURCE}
    return {"quotes": {"BTC/USD": btc, "ETH/USD": eth}}


def build(data=None, *, pairs=PAIRS, receipt=RECEIPT, collected=COLLECTED, maximum=60):
    return build_crypto_watch(raw() if data is None else data, pairs,
                              received_at=receipt, collected_at=collected,
                              max_age_seconds=maximum)


class CryptoContractTests(unittest.TestCase):
    def test_contract_exact_decimals_and_nanos(self):
        x = build()
        self.assertEqual(x["asset_class"], "spot_crypto")
        self.assertEqual(x["source"], "alpaca_crypto")
        self.assertEqual(x["requested_pairs"], list(PAIRS))
        self.assertEqual([r["status"] for r in x["rows"]], ["fresh", "fresh"])
        self.assertEqual(x["rows"][0]["bid"], "67250.000000000000000001")
        self.assertEqual(x["rows"][0]["source_at"], SOURCE)
        self.assertTrue(x["observation_only"])
        self.assertFalse(x["execution_enabled"])
        self.assertLessEqual(Decimal(x["rows"][0]["bid"]), Decimal(x["rows"][0]["ask"]))

    def test_pair_gate_rejects_equities_and_duplicates(self):
        for bad in (("AAPL",), ("btC/USD",), ("BTC/USD", "BTC/USD"), (),
                    tuple("X%d/USD" % i for i in range(11)), ("BTC/USDT",), ("../USD",)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _pairs(bad)

    def test_explicit_missing_pair(self):
        x = build({"quotes": {"BTC/USD": raw()["quotes"]["BTC/USD"]}})
        self.assertEqual(x["rows"][1]["status"], "missing")
        self.assertEqual(x["rows"][1]["error_code"], "pair_missing")
        self.assertIsNone(x["rows"][1]["bid"])

    def test_provider_failure_does_not_leak_details(self):
        x = build({"unexpected": "BROKER-RAW-FAILURE-MESSAGE"})
        self.assertTrue(all(r["status"] == "error" for r in x["rows"]))
        self.assertTrue(all(r["error_code"] == "market_data_unavailable" for r in x["rows"]))
        self.assertNotIn("BROKER-RAW-FAILURE-MESSAGE", str(x))

    def test_one_bad_pair_does_not_poison_good_one(self):
        x = build(raw(eth={"bp": "700", "ap": "600", "t": SOURCE}))
        self.assertEqual(x["rows"][0]["status"], "fresh")
        self.assertEqual(x["rows"][1]["status"], "error")
        self.assertEqual(x["rows"][1]["error_code"], "quote_invalid")
        self.assertIsNone(x["rows"][1]["ask"])

    def test_future_quote_cannot_appear_current(self):
        x = build(raw(btc={"bp": "1", "ap": "2", "t": "2026-10-19T00:00:00Z"}))
        self.assertEqual(x["rows"][0]["error_code"], "quote_timestamp_invalid")
        self.assertIsNone(x["rows"][0]["source_at"])

    def test_stale_but_not_rebranded_fresh(self):
        x = build(raw(btc={"bp": "1", "ap": "2", "t": "2026-10-18T23:58:00Z"}))
        self.assertEqual(x["rows"][0]["status"], "stale")
        self.assertEqual(x["rows"][0]["error_code"], "quote_stale")
        self.assertEqual(x["rows"][0]["bid"], "1")

    def test_overprecision_rejected(self):
        data = raw(btc={"bp": Decimal("0.0000000000000000001"), "ap": Decimal("1"), "t": SOURCE})
        self.assertEqual(build(data)["rows"][0]["error_code"], "quote_invalid")

    def test_eighteen_decimal_places_allowed(self):
        self.assertEqual(_price(Decimal("0.000000000000000001")), "0.000000000000000001")
        self.assertEqual(_price(Decimal("1e18")), "1000000000000000000")

    def test_giant_exponents_do_not_expand(self):
        for value in (Decimal("1e-999999999"), Decimal("1e999999999")):
            with self.subTest(value=str(value)), self.assertRaises(ValueError):
                _price(value)

    def test_float_prices_refused_even_when_positive(self):
        x = build(raw(btc={"bp": 101.25, "ap": 101.26, "t": SOURCE}))
        self.assertEqual(x["rows"][0]["error_code"], "quote_invalid")

    def test_timezone_and_nano_order(self):
        self.assertLess(_timestamp(SOURCE)[1], _timestamp(RECEIPT)[1])
        self.assertEqual(_timestamp("2026-10-18T19:59:59.123456789-04:00")[0], SOURCE)
        for bad in ("2026-10-18", "2026-10-18T23:59:59",
                    "2026-10-18T23:59:59-00:00", "bad"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _timestamp(bad)

    def test_receipt_after_collection_fails(self):
        with self.assertRaises(ValueError):
            build(receipt="2026-10-19T00:00:00Z")

    def test_extra_unrequested_pair_is_not_published(self):
        data = raw()
        data["quotes"]["DOGE/USD"] = {"bp": "100", "ap": "101", "t": SOURCE}
        x = build(data)
        self.assertEqual(len(x["rows"]), 2)
        self.assertNotIn("DOGE/USD", str(x))


class CryptoHTTPTests(unittest.TestCase):
    def cfg(self, mode="paper", key="example-only", secret="test-secret"):
        return SimpleNamespace(mode=mode, api_key=key, api_secret=secret)

    def test_get_only_locked_host_and_pairs_encoded(self):
        got = []
        def transport(method, url, headers, payload):
            got.append((method, url, headers, payload))
            return raw()
        client = CryptoQuoteReader(self.cfg(), transport=transport)
        self.assertEqual(client.latest_quotes(PAIRS), raw())
        self.assertEqual(len(got), 1)
        method, url, headers, payload = got[0]
        self.assertEqual(method, "GET")
        self.assertEqual(urlsplit(url).netloc, "data.alpaca.markets")
        self.assertEqual(urlsplit(url).path, "/v1beta3/crypto/us/latest/quotes")
        self.assertEqual(parse_qs(urlsplit(url).query)["symbols"], ["BTC/USD,ETH/USD"])
        self.assertIsNone(payload)
        self.assertEqual(headers["APCA-API-KEY-ID"], "example-only")

    def test_paper_only(self):
        for cfg in (self.cfg(mode="demo"), self.cfg(mode="live"),
                    self.cfg(key=""), self.cfg(secret="")):
            with self.subTest(cfg=cfg), self.assertRaises(CryptoDataError):
                CryptoQuoteReader(cfg, transport=lambda *_: self.fail("transport called")).latest_quotes(PAIRS)

    def test_bad_symbol_rejected_before_transport(self):
        with self.assertRaises(ValueError):
            CryptoQuoteReader(self.cfg(), transport=lambda *_: self.fail("transport called")).latest_quotes(("AAPL",))

    def test_transport_error_does_not_echo_exception(self):
        def transport(*_):
            raise Exception("SECRET-EXAMPLE-DO-NOT-LOG")
        with self.assertRaisesRegex(CryptoDataError, "^market_data_unavailable$") as ctx:
            CryptoQuoteReader(self.cfg(), transport=transport).latest_quotes(PAIRS)
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_http_decimals_stay_exact_and_duplicate_fields_refused(self):
        class Response:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, *_): return self.body
        class Opener:
            def __init__(self, body): self.body = body
            def open(self, *_args, **_kwargs): return Response(self.body)
        client = CryptoQuoteReader(self.cfg())
        client._opener = Opener(b'{"quotes":{"BTC/USD":{"bp":0.000000000000000001,"ap":1,"t":"2026-10-18T23:59:59Z"}}}')
        data = client.latest_quotes(("BTC/USD",))
        self.assertEqual(data["quotes"]["BTC/USD"]["bp"], Decimal("0.000000000000000001"))
        client._opener = Opener(b'{"quotes":{},"quotes":{}}')
        with self.assertRaises(CryptoDataError):
            client.latest_quotes(("BTC/USD",))

    def test_collector_stays_readonly_on_provider_error(self):
        class Provider:
            def latest_quotes(self, pairs): raise CryptoDataError()
        t = datetime(2026, 10, 19, tzinfo=timezone.utc)
        result = CryptoWatchCollector(Provider(), clock=lambda: t).collect()
        self.assertEqual(result["requested_pairs"], ["BTC/USD", "ETH/USD"])
        self.assertTrue(all(r["error_code"] == "market_data_unavailable" for r in result["rows"]))
        self.assertFalse(result["execution_enabled"])

    def test_collector_passes_valid_market_data(self):
        class Provider:
            def latest_quotes(self, pairs): return raw()
        t = datetime(2026, 10, 18, 23, 59, 59, 124000, tzinfo=timezone.utc)
        result = CryptoWatchCollector(Provider(), clock=lambda: t).collect()
        self.assertTrue(all(r["status"] == "fresh" for r in result["rows"]))
        self.assertEqual(result["collected_at"], "2026-10-18T23:59:59.124Z")


if __name__ == "__main__":
    unittest.main()
