"""Exact bar HTTP decoding and bounded observation requests, entirely offline."""
from decimal import Decimal
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from momentum.alpaca import BrokerError, PaperDisabled
from momentum.config import Config
from momentum.observation import ReadOnlyPaperClient
from momentum.price_history import sanitize_price_history


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.body[:size]


class FixtureOpener:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.requests = []

    def open(self, request, timeout):
        if not self.bodies:
            raise AssertionError("Unexpected invented HTTP request")
        self.requests.append((request, timeout))
        return FakeResponse(self.bodies.pop(0))


class PriceHistoryAdapterTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError(
            "Real network is forbidden in bar observation tests"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def client(self, bodies):
        config = Config(mode="paper", paper_enabled=False, api_key="INVENTED-KEY",
                        api_secret="INVENTED-SECRET", watchlist=("DEMO-A",), data_feed="iex")
        client = ReadOnlyPaperClient(config)
        opener = FixtureOpener(bodies)
        client._opener = opener
        return client, opener

    @staticmethod
    def observe(client, **changes):
        args = {"timeframe": "1Min", "start": "2026-10-16T13:35:00.000000001Z",
                "end": "2026-10-16T13:45:00.000000001Z"}
        args.update(changes)
        return client.observation_bars(("DEMO-A",), **args)

    def test_bar_numbers_preserve_literals_and_other_routes_keep_their_existing_decoder(self):
        body = b'{"bars":{"DEMO-A":[{"c":6.310000000000000001,"v":100}]},"next_page_token":null}'
        client, opener = self.client([body, b'{"DEMO-A":{"latestTrade":{"p":6.31}}}'])
        value = self.observe(client)
        close = value["bars"]["DEMO-A"][0]["c"]
        self.assertIsInstance(close, Decimal)
        self.assertEqual(str(close), "6.310000000000000001")
        self.assertIs(type(value["bars"]["DEMO-A"][0]["v"]), int)
        snapshot = client.snapshots(("DEMO-A",))
        self.assertIs(type(snapshot["DEMO-A"]["latestTrade"]["p"]), float)
        self.assertTrue(all(request.get_method() == "GET" for request, _ in opener.requests))

    def test_bar_decoder_rejects_duplicate_keys_nonfinite_literals_and_invalid_json_safely(self):
        invalid = [b'{"bars":{},"bars":{}}', b'{"bars":{"DEMO-A":[{"c":1,"c":2}]}}',
                   b'{"bars":{"DEMO-A":[{"c":NaN}]}}', b'{"bars":{"DEMO-A":[{"c":Infinity}]}}',
                   b'{"bars":{"DEMO-A":[{"c":-Infinity}]}}', b'{"bars":', b'\xff']
        for body in invalid:
            with self.subTest(body=body):
                client, _ = self.client([body])
                with self.assertRaises(BrokerError) as caught:
                    self.observe(client)
                self.assertEqual(str(caught.exception), "Alpaca returned invalid stock bars JSON")

    def test_fixed_raw_feed_currency_mapping_and_page_limit_have_only_get_transport(self):
        client, opener = self.client([b'{"bars":{},"next_page_token":"opaque-more"}'])
        result = self.observe(client)
        self.assertEqual(result["next_page_token"], "opaque-more")
        self.assertEqual(len(opener.requests), 1, "Only the collector may classify this incomplete page")
        request, timeout = opener.requests[0]
        parsed = urlsplit(request.full_url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "data.alpaca.markets")
        self.assertEqual(parsed.path, "/v2/stocks/bars")
        self.assertEqual(parse_qs(parsed.query), {
            "symbols": ["DEMO-A"], "timeframe": ["1Min"],
            "start": ["2026-10-16T13:35:00.000000001Z"],
            "end": ["2026-10-16T13:45:00.000000001Z"],
            "limit": ["1000"], "adjustment": ["raw"], "feed": ["iex"],
            "sort": ["asc"], "asof": ["-"], "currency": ["USD"]})
        self.assertEqual(timeout, 12)
        self.assertIsNone(request.data)
        for operation in (lambda: client.submit({}), lambda: client.cancel({}),
                          lambda: client._request("POST", "/v2/stocks/bars", data=True)):
            with self.assertRaises(PaperDisabled):
                operation()
        self.assertEqual(len(opener.requests), 1)

    def test_invalid_symbol_timeframe_and_windows_never_reach_transport(self):
        client, opener = self.client([])
        for symbols in ([], ["DEMO-A", "DEMO-A"], ["DEMO-" + str(i) for i in range(11)],
                        ["lowercase"], [None], "DEMO-A"):
            with self.subTest(symbols=symbols), self.assertRaises(ValueError):
                client.observation_bars(symbols, timeframe="1Min", start="2026-10-16T13:35:00Z",
                                        end="2026-10-16T13:45:00Z")
        changes = [{"timeframe": "1Hour"}, {"timeframe": []},
                   {"end": "2026-10-16T13:45:00.000000002Z"},
                   {"end": "2026-10-16T13:34:59Z"},
                   {"timeframe": "1Day", "start": "2026-10-15T04:00:00Z",
                    "end": "2026-10-16T06:00:00.000000001Z"},
                   {"start": "2026-10-16T13:35:00"}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.observe(client, **change)
        self.assertEqual(opener.requests, [])

    def test_existing_historical_bar_pagination_keeps_decimal_precision(self):
        client, opener = self.client([
            b'{"bars":{"DEMO-A":[{"c":6.310000000000000001}]},"next_page_token":"fixture-page-2"}',
            b'{"bars":{"DEMO-A":[{"c":6.320000000000000002}]},"next_page_token":null}'])
        result = client.bars("DEMO-A", "2026-10-16T13:35:00Z", "2026-10-16T13:45:00Z")
        self.assertEqual([str(bar["c"]) for bar in result], ["6.310000000000000001", "6.320000000000000002"])
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(parse_qs(urlsplit(opener.requests[1][0].full_url).query)["page_token"], ["fixture-page-2"])

    def test_checked_in_fixture_is_explicitly_synthetic_and_roundtrips_without_precision_loss(self):
        path = Path(__file__).resolve().parent.parent / "examples" / "price-history.synthetic.json"
        fixture = json.loads(path.read_text())
        self.assertEqual(fixture["provenance"], "synthetic_contract_fixture")
        value = fixture["price_history"]
        self.assertTrue(all(symbol.startswith("DEMO-") for symbol in value["requested_symbols"]))
        normalized = sanitize_price_history(value, allowed_symbols=value["requested_symbols"],
                                            feed="iex", now=value["collected_at"])
        self.assertEqual(normalized, value)
        for row in value["rows"]:
            self.assertEqual(row["previous_close"]["status"], "observed")
            self.assertEqual(row["minute_bars"]["status"], "observed")
            self.assertEqual(len(row["minute_bars"]["bars"]), 9)
            self.assertEqual(len(row["minute_bars"]["missing_intervals"]), 1)
            self.assertEqual(row["minute_bars"]["bars"][0]["close"], "6.310000000000000001")


if __name__ == "__main__":
    unittest.main()
