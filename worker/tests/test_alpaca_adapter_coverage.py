"""Offline coverage for the paper-only Alpaca adapter (momentum.alpaca).

Paper only: every request goes to an in-memory fake transport or a patched opener.
Nothing opens a network connection, and the guard tests assert that no POST is sent.
"""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from momentum.alpaca import (AmbiguousSubmission, BrokerError, PaperClient, PaperDisabled,
                             decimal_string, parse_time)
from momentum.journal import Journal
from worker_fakes import armed_config, FakeVenue
from momentum.risk import RiskManager
from momentum.strategy import evaluate_setup, load_strategy


def approved(venue):
    snapshot = venue.snapshot()
    account = {"equity": "10000", "buying_power": "10000", "realized_pnl": "0", "unrealized_pnl": "0",
               "fees": "0", "position_qty": 0, "open_order_qty": 0, "session_date": "2026-10-16",
               "as_of": snapshot["now"]}
    # Paper-stack (#9/#19) default preset is the liquid IEX universe; the armed fake config
    # scopes the strategy to its synthetic DEMO-* symbols via strategy_overrides.
    setup = evaluate_setup(snapshot, load_strategy(venue.client.config.strategy_overrides))
    intent = RiskManager().assess(setup.intent, account, snapshot["quote"], snapshot["now"],
                                  connected=True, reconciled=True).intent
    return intent, snapshot["quote"]


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Recorder:
    def __init__(self, result=None, error=None):
        self.calls, self.result, self.error = [], result, error

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, payload))
        if self.error:
            raise self.error
        return self.result


class HelperTests(unittest.TestCase):
    def test_decimal_string_rejects_non_finite(self):
        self.assertEqual(decimal_string("1.50"), "1.50")
        for bad in ("NaN", "Infinity", float("inf")):
            with self.assertRaises(ValueError):
                decimal_string(bad)

    def test_parse_time_accepts_aware_datetime_and_rejects_naive(self):
        aware = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_time(aware), aware)
        self.assertEqual(parse_time("2026-10-16T13:00:00Z").tzinfo, timezone.utc)
        with self.assertRaises(ValueError):
            parse_time(datetime(2026, 10, 16, 8, 0))
        with self.assertRaises(ValueError):
            parse_time("2026-10-16T13:00:00")


class RequestLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = armed_config(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_credentials_or_disabled_mode_block_before_transport(self):
        transport = Recorder({})
        for cfg in (replace(self.cfg, api_secret=""), replace(self.cfg, api_key=""),
                    replace(self.cfg, paper_enabled=False)):
            with self.assertRaises(PaperDisabled):
                PaperClient(cfg, transport=transport).account()
        self.assertEqual(transport.calls, [])

    def test_data_route_is_get_only_and_methods_are_locked(self):
        transport = Recorder({})
        client = PaperClient(self.cfg, transport=transport)
        with self.assertRaises(BrokerError):
            client._request("POST", "/v2/stocks/snapshots", data=True)
        with self.assertRaises(BrokerError):
            client._request("PATCH", "/v2/orders")
        with self.assertRaises(BrokerError):
            client._request("GET", "/v2/account?x=1")
        with self.assertRaises(BrokerError):
            client._request("GET", "/v1/account")
        self.assertEqual(transport.calls, [])

    def test_credentials_go_only_to_fixed_paper_host(self):
        transport = Recorder({"status": "ACTIVE"})
        client = PaperClient(self.cfg, transport=transport)
        client.account()
        method, url, payload = transport.calls[0]
        self.assertEqual((method, payload), ("GET", None))
        self.assertTrue(url.startswith("https://paper-api.alpaca.markets/v2/account"))

    def test_open_orders_at_limit_fails_closed(self):
        client = PaperClient(self.cfg, transport=Recorder([{}] * 500))
        with self.assertRaises(BrokerError):
            client.open_orders()
        self.assertEqual(len(PaperClient(self.cfg, transport=Recorder([{}] * 3)).open_orders()), 3)

    def test_order_lookup_404_is_none_other_errors_propagate(self):
        self.assertIsNone(PaperClient(self.cfg, transport=Recorder(error=BrokerError("x", 404)))
                          .order_by_client_id("mlab_x"))
        with self.assertRaises(BrokerError) as caught:
            PaperClient(self.cfg, transport=Recorder(error=BrokerError("x", 500))).order_by_client_id("mlab_x")
        self.assertEqual(caught.exception.status, 500)

    def test_calendar_and_snapshots_validation(self):
        transport = Recorder({})
        client = PaperClient(self.cfg, transport=transport)
        client.calendar("2026-10-16", "2026-10-17")
        self.assertIn("/v2/calendar", transport.calls[0][1])
        for bad in ([], ["bad symbol!"]):
            with self.assertRaises(ValueError):
                client.snapshots(bad)
        client.snapshots(["DEMO-A"])
        self.assertTrue(transport.calls[-1][1].startswith("https://data.alpaca.markets/v2/stocks/snapshots"))

    def test_observation_bars_window_and_input_bounds(self):
        transport = Recorder({"bars": {}})
        client = PaperClient(self.cfg, transport=transport)
        start = "2026-10-16T13:30:00Z"
        with self.assertRaises(ValueError):
            client.observation_bars(["DEMO-A"], timeframe="1Min", start=start, end="2026-10-16T13:41:00Z")
        with self.assertRaises(ValueError):
            client.observation_bars(["DEMO-A"], timeframe="1Min", start=start, end="2026-10-16T13:29:00Z")
        with self.assertRaises(ValueError):
            client.observation_bars(["DEMO-A", "DEMO-A"], timeframe="1Min", start=start, end=start)
        with self.assertRaises(ValueError):
            client.observation_bars(["DEMO-A"], timeframe="5Min", start=start, end=start)
        self.assertEqual(transport.calls, [])
        client.observation_bars(["DEMO-A"], timeframe="1Min", start=start, end="2026-10-16T13:40:00Z")
        self.assertEqual(len(transport.calls), 1)

    def test_bars_pagination_repeat_token_and_cap(self):
        client = PaperClient(self.cfg, transport=Recorder({"bars": {"DEMO-A": [{"t": 1}]}, "next_page_token": "same"}))
        with self.assertRaises(BrokerError):
            client.bars("DEMO-A", "a", "b")
        counter = iter(range(100))

        def endless(method, url, headers, payload):
            return {"bars": {"DEMO-A": []}, "next_page_token": "t%d" % next(counter)}
        with self.assertRaises(BrokerError):
            PaperClient(self.cfg, transport=endless).bars("DEMO-A", "a", "b")
        with self.assertRaises(ValueError):
            client.bars("bad symbol!", "a", "b")

    def test_client_order_id_requires_setup_and_is_stable(self):
        with self.assertRaises(ValueError):
            PaperClient.client_order_id({"symbol": "DEMO-A", "side": "buy"})
        intent = {"symbol": "DEMO-A", "side": "buy", "setup_id": "s1"}
        self.assertEqual(PaperClient.client_order_id(intent), PaperClient.client_order_id(dict(intent)))
        self.assertTrue(PaperClient.client_order_id(intent).startswith("mlab_"))


class HttpErrorMappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = PaperClient(armed_config(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _open(self, effect):
        return patch.object(self.client._opener, "open", side_effect=effect)

    def test_http_error_status_kept_without_body(self):
        err = HTTPError("https://paper-api.alpaca.markets/v2/account", 403, "Forbidden", {},
                        io.BytesIO(b"secret body"))
        with self._open(err):
            with self.assertRaises(BrokerError) as caught:
                self.client._http("GET", "https://paper-api.alpaca.markets/v2/account", {}, None)
        self.assertEqual(caught.exception.status, 403)
        self.assertNotIn("secret", str(caught.exception))

    def test_connection_and_timeout_errors(self):
        for effect in (URLError("down"), TimeoutError(), OSError("reset")):
            with self._open(effect):
                with self.assertRaises(BrokerError) as caught:
                    self.client._http("GET", "https://paper-api.alpaca.markets/v2/account", {}, None)
            self.assertIsNone(caught.exception.status)

    def test_invalid_json_and_encoding(self):
        for body in (b"{not json", b"\xff\xfe\xfa"):
            with self._open(lambda *a, body=body, **k: _Response(body)):
                with self.assertRaises(BrokerError):
                    self.client._http("GET", "https://paper-api.alpaca.markets/v2/account", {}, None)

    def test_empty_body_is_none_and_payload_is_json(self):
        seen = {}

        def opener(request, timeout):
            seen["data"], seen["timeout"] = request.data, timeout
            return _Response(b"")
        with self._open(opener):
            self.assertIsNone(self.client._http("POST", "https://paper-api.alpaca.markets/v2/orders",
                                                {}, {"a": 1}))
        self.assertEqual(json.loads(seen["data"]), {"a": 1})
        self.assertEqual(seen["timeout"], 12)

    def test_stock_bars_route_keeps_decimal_literals(self):
        with self._open(lambda *a, **k: _Response(b'{"bars": {"A": [{"c": 1.10}]}}')):
            result = self.client._http("GET", "https://data.alpaca.markets/v2/stocks/bars", {}, None)
        self.assertEqual(result["bars"]["A"][0]["c"], Decimal("1.10"))


class SubmitGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = armed_config(self.tmp.name)
        self.venue = FakeVenue(self.cfg)
        self.intent, self.quote = approved(self.venue)
        self.journal = Journal(self.cfg.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _submit(self, intent=None, quote=None, guard=lambda: None):
        return self.venue.client.submit(intent or self.intent, self.journal, "paper-test",
                                        quote=quote or self.quote, guard=guard)

    def _no_post(self):
        self.assertFalse(any(c["method"] == "POST" for c in self.venue.calls))

    def test_intent_identity_and_scope_guards(self):
        cases = [
            (PaperDisabled, dict(self.intent, mode="live")),
            (PaperDisabled, dict(self.intent, profile="other")),
            (PaperDisabled, dict(self.intent, config_id="changed")),
            (PaperDisabled, dict(self.intent, risk_config_id="changed")),
            (ValueError, dict(self.intent, side="short")),
            (ValueError, dict(self.intent, symbol="bad symbol!")),
            (PaperDisabled, dict(self.intent, symbol="ZZZZ")),
        ]
        for error, intent in cases:
            with self.subTest(intent={k: intent[k] for k in ("mode", "profile", "side", "symbol")}):
                with self.assertRaises(error):
                    self._submit(intent)
        with self.assertRaises(PaperDisabled):
            self._submit(quote=dict(self.quote, symbol="DEMO-B"))
        with self.assertRaises(PaperDisabled):
            self._submit(quote=dict(self.quote, feed="sip"))
        self._no_post()

    def test_quantity_and_price_guards(self):
        for qty in ("0", "-1", "1.5", "NaN"):
            with self.subTest(qty=qty):
                with self.assertRaises(ValueError):
                    self._submit(dict(self.intent, qty=qty))
        for price in ("0", "-1", "Infinity", "9.815"):
            with self.subTest(price=price):
                with self.assertRaises(ValueError):
                    self._submit(dict(self.intent, entry_limit=price))
        self._no_post()

    def test_prior_attempt_without_visible_order_blocks_resubmission(self):
        client_id = PaperClient.client_order_id(self.intent)
        self.journal.append("paper-test", "paper_order_requested", {"client_order_id": client_id},
                            key="paper-request:" + client_id)
        with self.assertRaises(AmbiguousSubmission):
            self._submit()
        self._no_post()

    def test_stale_quote_receipt_and_risk_approval_block(self):
        with self.assertRaises(PaperDisabled):
            self._submit(quote=dict(self.quote, received_at="2026-10-16T13:00:00+00:00"))
        future = "2026-10-16T13:40:00+00:00"
        with self.assertRaises(PaperDisabled):
            self._submit(quote=dict(self.quote, timestamp=future))
        with self.assertRaises(PaperDisabled) as caught:
            self._submit(dict(self.intent, observed_at="2026-10-16T13:00:00+00:00"))
        self.assertIn("Intention stale", str(caught.exception))
        with self.assertRaises(PaperDisabled):
            self._submit(dict(self.intent, risk_checked_at="2026-10-16T13:00:00+00:00"))
        with self.assertRaises(PaperDisabled):
            self._submit(dict(self.intent, live_enabled=True))
        with self.assertRaises(PaperDisabled):
            self._submit(dict(self.intent, paper_only=False))
        self._no_post()

    def test_gate_change_after_request_journal_records_not_sent(self):
        calls = []

        def guard():
            calls.append(1)
            if len(calls) == 2:
                raise PaperDisabled("kill switch engaged")
        with self.assertRaises(PaperDisabled):
            self._submit(guard=guard)
        client_id = PaperClient.client_order_id(self.intent)
        self.assertTrue(self.journal.event_by_key("paper-request:" + client_id))
        self.assertTrue(self.journal.event_by_key("not-sent:" + client_id))
        self._no_post()

    def test_definite_4xx_rejection_is_recorded_and_reraised(self):
        original = self.venue.request

        def reject(method, url, headers, payload):
            if method == "POST":
                raise BrokerError("Alpaca HTTP 422", 422)
            return original(method, url, headers, payload)
        self.venue.client._transport = reject
        with self.assertRaises(BrokerError) as caught:
            self._submit()
        self.assertNotIsInstance(caught.exception, AmbiguousSubmission)
        client_id = PaperClient.client_order_id(self.intent)
        self.assertTrue(self.journal.event_by_key("paper_order_rejected:" + client_id))

    def test_429_is_treated_as_uncertain(self):
        original = self.venue.request

        def throttle(method, url, headers, payload):
            if method == "POST":
                raise BrokerError("Alpaca HTTP 429", 429)
            return original(method, url, headers, payload)
        self.venue.client._transport = throttle
        with self.assertRaises(AmbiguousSubmission):
            self._submit()


class CancelGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = armed_config(self.tmp.name)
        self.venue = FakeVenue(self.cfg)
        intent, quote = approved(self.venue)
        self.journal = Journal(self.cfg.db_path)
        self.order = self.venue.client.submit(intent, self.journal, "paper-test", quote=quote, guard=lambda: None)

    def tearDown(self):
        self.tmp.cleanup()

    def _deletes(self):
        return sum(c["method"] == "DELETE" for c in self.venue.calls)

    def test_foreign_or_unjournaled_orders_are_refused(self):
        for order in ({"id": "x", "client_order_id": "other_1"}, {"id": "bad id!", "client_order_id": "mlab_1"}):
            with self.assertRaises(PaperDisabled):
                self.venue.client.cancel(order, self.journal, "paper-test")
        with self.assertRaises(PaperDisabled):
            self.venue.client.cancel({"id": "fake-9", "client_order_id": "mlab_unknown"}, self.journal, "paper-test")
        self.assertEqual(self._deletes(), 0)

    def test_identity_mismatch_fails_closed(self):
        with self.assertRaises(BrokerError):
            self.venue.client.cancel(dict(self.order, id="fake-99"), self.journal, "paper-test")
        self.assertEqual(self._deletes(), 0)

    def test_terminal_status_is_resolved_without_delete(self):
        self.venue.orders[self.order["client_order_id"]]["status"] = "filled"
        result = self.venue.client.cancel(self.order, self.journal, "paper-test")
        self.assertEqual(result["status"], "filled")
        key = "cancel-resolved:paper-test:%s:filled" % self.order["id"]
        self.assertTrue(self.journal.event_by_key(key))
        self.assertEqual(self._deletes(), 0)

    def test_pending_cancel_is_observed_not_repeated(self):
        self.venue.orders[self.order["client_order_id"]]["status"] = "pending_cancel"
        self.venue.client.cancel(self.order, self.journal, "paper-test")
        self.assertEqual(self._deletes(), 0)

    def test_retry_spacing_and_three_attempt_limit(self):
        self.venue.cancel_failure = True
        with self.assertRaises(BrokerError):
            self.venue.client.cancel(self.order, self.journal, "paper-test")
        # Within 2 seconds of the last attempt: observe only, no new DELETE.
        self.venue.client.cancel(self.order, self.journal, "paper-test")
        self.assertEqual(self._deletes(), 1)
        for _ in range(2):
            self.venue.advance(3)
            with self.assertRaises(BrokerError):
                self.venue.client.cancel(self.order, self.journal, "paper-test")
        self.assertEqual(self._deletes(), 3)
        self.venue.advance(3)
        with self.assertRaises(BrokerError) as caught:
            self.venue.client.cancel(self.order, self.journal, "paper-test")
        self.assertIn("retry limit", str(caught.exception))
        self.assertEqual(self._deletes(), 3)


if __name__ == "__main__":
    unittest.main()
