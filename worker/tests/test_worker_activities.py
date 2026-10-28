"""Invented account-activity fixtures. No credentials or network are used."""
from copy import deepcopy
from datetime import datetime
from decimal import localcontext
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from momentum.activities import (ActivityImportError, activity_request_params,
                                 fetch_activities, normalize_activity, timestamp_parts,
                                 validate_activity_page)
from momentum.alpaca import BrokerError, PaperClient, PaperDisabled
from momentum.config import Config


ACCOUNT = "00000000-0000-4000-8000-000000000001"
START = "2026-10-01T00:00:00Z"
END = "2026-10-18T18:00:00Z"


def fill(number=1, **changes):
    row = {
        "id": f"20261016143000000::{number:08d}-0000-4000-8000-000000000001",
        "activity_type": "FILL", "type": "partial_fill", "symbol": "DEMO-A", "side": "buy",
        "qty": "2", "price": "10.25", "cum_qty": "2", "leaves_qty": "3",
        "transaction_time": "2026-10-16T14:30:00.000000001Z",
        "order_id": "00000000-0000-4000-8000-000000000010",
    }
    row.update(changes)
    return row


def fee(number=1, **changes):
    row = {"id": f"20261017000000000::{number:08d}-0000-4000-8000-000000000002",
           "activity_type": "FEE", "date": "2026-10-17", "net_amount": "-0.0231"}
    row.update(changes)
    return row


class Pages:
    def __init__(self, pages, now=END):
        self.pages = list(pages)
        self.now = datetime.fromisoformat(now.replace("Z", "+00:00"))
        self.calls = []
        self.client = PaperClient(Config(mode="paper", paper_enabled=True, api_key="FIXTURE-KEY",
                                         api_secret="FIXTURE-SECRET"),
                                  transport=self.request, clock=lambda: self.now)

    def request(self, method, url, headers, payload):
        self.calls.append({"method": method, "url": url, "payload": payload})
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return deepcopy(page)

    def fetch(self, **changes):
        return fetch_activities(self.client, **dict(account_id=ACCOUNT, after=START, until=END, **changes))


class ActivityRequestTests(unittest.TestCase):
    def test_fixed_get_host_unfiltered_query_and_cursor_encoding(self):
        venue = Pages([[]])
        cursor = fill()["id"]
        venue.client.activity_page(after="2026-10-01T01:00:00+01:00", until=END, page_token=cursor, page_size=17)
        request = venue.calls[0]
        parsed = urlsplit(request["url"])
        self.assertEqual(request["method"], "GET")
        self.assertIsNone(request["payload"])
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "paper-api.alpaca.markets")
        self.assertEqual(parsed.path, "/v2/account/activities")
        self.assertEqual(parse_qs(parsed.query), {"after": [START], "until": [END], "direction": ["asc"],
                                               "page_size": ["17"], "page_token": [cursor]})

    def test_activities_route_rejects_writes_data_host_and_hidden_filters(self):
        venue = Pages([])
        params = activity_request_params(after=START, until=END)
        bad_requests = [
            {"method": "POST"}, {"method": "DELETE"}, {"data": True}, {"payload": {}},
            {"path": "/v2/account/activities/FILL"}, {"path": "/v2/account/activities?activity_types=FILL"},
            {"params": {**params, "activity_types": "FILL"}}, {"params": {**params, "category": "trade_activity"}},
            {"params": {**params, "order_id": "abc"}}, {"params": {**params, "date": "2026-10-16"}},
            {"params": {**params, "direction": "desc"}}, {"params": {**params, "extra": "secret"}},
        ]
        for change in bad_requests:
            with self.subTest(change=list(change)):
                args = {"method": "GET", "path": "/v2/account/activities", "params": params, **change}
                with self.assertRaises(BrokerError):
                    venue.client._request(**args)
        self.assertEqual(venue.calls, [])

    def test_bad_bounds_sizes_and_cursor_rejected_before_transport(self):
        venue = Pages([])
        invalid = [{"after": "2026-10-01"}, {"after": "2026-10-01T00:00:00"},
                   {"after": "2026-10-01T00:00:00-00:00"}, {"after": END},
                   {"after": "2027-01-01T00:00:00Z"}, {"page_size": True}, {"page_size": 101},
                   {"page_size": 0}, {"page_token": ""}, {"page_token": "x&direction=desc"},
                   {"page_token": "https://api.alpaca.markets"}, {"page_token": "x" * 257},
                   {"until": "2026-99-01T00:00:00Z"}]
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    venue.client.activity_page(**{"after": START, "until": END, **change})
        self.assertEqual(venue.calls, [])

    def test_existing_paper_credential_gate_preserved(self):
        client = PaperClient(Config(), transport=lambda *_: self.fail("No request expected"))
        with self.assertRaises(PaperDisabled):
            client.activity_page(after=START, until=END)

    def test_http_payload_limit_and_timeout_are_enforced(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def read(self, limit):
                self.requested_limit = limit
                return b" " * limit

        class Opener:
            def __init__(self):
                self.response = Response()
            def open(self, request, timeout):
                self.timeout = timeout
                return self.response

        client = Pages([]).client
        client._opener = Opener()
        with self.assertRaisesRegex(BrokerError, "payload limit"):
            client._http("GET", "https://paper-api.alpaca.markets/v2/account/activities", {}, None)
        self.assertEqual(client._opener.timeout, 12)
        self.assertEqual(client._opener.response.requested_limit, 8_000_001)

    def test_page_schema_and_payload_limits(self):
        invalid = [None, {"activities": []}, [None], ["fill"], [fill(id="unsafe\nvalue")],
                   [fill()] * 101, [fill(description="x" * 16_384)], [fill(price=float("nan"))]]
        for page in invalid:
            with self.subTest(shape=type(page).__name__):
                with self.assertRaises(ActivityImportError):
                    validate_activity_page(page)
        # Individually bounded rows can still exceed the aggregate page bound.
        with self.assertRaisesRegex(ActivityImportError, "page_payload_limit"):
            validate_activity_page([fill(i, description="x" * 10_001) for i in range(100)])


class ActivityNormalizationTests(unittest.TestCase):
    def test_partial_fill_uses_execution_quantity_not_cumulative(self):
        observation = normalize_activity(fill(qty="2.000", cum_qty="17", leaves_qty="5", price="10.2500"))
        record = observation.record
        self.assertEqual((record["kind"], record["quantity"], record["price"]), ("fill", "2", "10.25"))
        self.assertEqual(record["effective_at"], "2026-10-16T14:30:00.000000001Z")
        self.assertEqual(record["revision"], 1)
        self.assertEqual(record["status"], "active")
        self.assertNotIn("cum_qty", record)
        self.assertEqual(len(record["source_fingerprint"]), 64)

    def test_decimal_precision_does_not_depend_on_global_context(self):
        exact = "12345678901234567.123456789012345678"
        with localcontext() as ctx:
            ctx.prec = 6
            record = normalize_activity(fill(price=exact)).record
            credit = normalize_activity(fee(net_amount=exact)).record
        self.assertEqual(record["price"], exact)
        self.assertEqual(credit["amount"], "-" + exact)

    def test_nanosecond_timestamp_offsets_and_calendar_boundary(self):
        stamp, nanos = timestamp_parts("2026-10-16T20:00:00.123456789-04:00")
        self.assertEqual(stamp, "2026-10-17T00:00:00.123456789Z")
        self.assertEqual(timestamp_parts(stamp)[1], nanos)
        self.assertEqual(timestamp_parts("2026-10-17T00:00:00.123456788Z")[1], nanos - 1)
        self.assertEqual(timestamp_parts("2026-10-17T00:00:00.100000000Z")[0], "2026-10-17T00:00:00.1Z")

    def test_fee_preserves_settlement_date_without_inventing_trade_time(self):
        observation = normalize_activity(fee(date="2026-10-17T23:30:00-04:00", description="regulatory fee"))
        record = observation.record
        self.assertEqual(record["effective_date"], "2026-10-17")
        self.assertEqual(record["date_basis"], "provider_activity_or_settlement_date")
        self.assertEqual(record["amount"], "0.0231")
        self.assertNotIn("effective_at", record)
        self.assertNotIn("execution_id", record)
        self.assertNotIn("description", observation.to_dict())

    def test_fee_credit_and_explicit_zero_are_not_clamped(self):
        self.assertEqual(normalize_activity(fee(net_amount="0.05")).record["amount"], "-0.05")
        self.assertEqual(normalize_activity(fee(net_amount="-0.00")).record["amount"], "0")

    def test_cash_movement_never_becomes_pnl_or_fee(self):
        for kind, amount in (("CSD", "10000"), ("CSW", "-12.30")):
            observation = normalize_activity(fee(activity_type=kind, net_amount=amount))
            self.assertEqual(observation.kind, "cash")
            self.assertIsNone(observation.record)
            self.assertEqual(observation.blockers, ())
        with self.assertRaisesRegex(ActivityImportError, "cash_activity_sign_mismatch"):
            normalize_activity(fee(activity_type="CSD", net_amount="-100"))

    def test_unknown_corporate_action_and_crypto_fees_are_visible_blockers(self):
        for kind in ("JNLS", "SSP", "CFEE", "FUTURE_TYPE"):
            observation = normalize_activity(fee(activity_type=kind))
            self.assertEqual(observation.kind, "unsupported")
            self.assertIsNone(observation.record)
            self.assertIn("unsupported_account_activity", observation.blockers)

    def test_correction_and_bust_markers_never_apply_as_ordinary_fills(self):
        changes = [{"type": "trade_correct"}, {"type": "trade_bust"}, {"previous_id": "prior"},
                   {"previous_execution_id": "prior"}, {"details": {"execution_type": "trade_bust"}},
                   {"event": "trade_correct"}]
        for change in changes:
            observation = normalize_activity(fill(**change))
            self.assertEqual(observation.kind, "correction")
            self.assertIsNone(observation.record)
            self.assertIn("activity_correction_requires_review", observation.blockers)

    def test_unsupported_asset_side_currency_and_type_do_not_enter_ledger(self):
        changes = [{"symbol": "BTC/USD"}, {"asset_class": "crypto"}, {"side": "sell_short"},
                   {"currency": "EUR"}, {"type": "replaced"}, {"type": []}, {"side": []}]
        for change in changes:
            observation = normalize_activity(fill(**change))
            self.assertIsNone(observation.record)
            self.assertTrue(observation.blockers)

    def test_invalid_money_and_execution_times_fail_closed(self):
        changes = [{"qty": "0"}, {"qty": "-1"}, {"qty": True}, {"price": 10.25}, {"price": "NaN"},
                   {"price": "1e100000"}, {"price": "1000000000000000001"},
                   {"price": "0.0000000000000000001"}, {"price": "0"},
                   {"cum_qty": "1"}, {"leaves_qty": "-1"}, {"order_id": ""},
                   {"transaction_time": "2026-10-16"}, {"transaction_time": "2026-10-16T14:30:00"},
                   {"transaction_time": "2026-10-16T14:30:00.1234567890Z"}]
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaises(ActivityImportError):
                    normalize_activity(fill(**change))

    def test_invalid_fee_date_is_not_replaced_with_observation_time(self):
        for value in (None, "not-a-date", "2026-02-30", "2026-10-17T00:00:00"):
            with self.assertRaises(ActivityImportError):
                normalize_activity(fee(date=value))

    def test_fingerprint_tracks_ignored_provider_field_changes(self):
        before = normalize_activity(fill(description="first provider detail"))
        after = normalize_activity(fill(description="corrected provider detail"))
        self.assertNotEqual(before.source_fingerprint, after.source_fingerprint)
        self.assertEqual(before.record["quantity"], after.record["quantity"])
        self.assertNotIn("first provider detail", json.dumps(before.to_dict()))


class ActivityScanTests(unittest.TestCase):
    def test_short_pages_are_followed_until_observed_empty_page(self):
        first, second, third = fill(1), fill(2), fee(3)
        venue = Pages([[first], [second, third], []])
        result = venue.fetch()
        self.assertEqual([record["source_id"] for record in result.records], [first["id"], second["id"], third["id"]])
        queries = [parse_qs(urlsplit(call["url"]).query) for call in venue.calls]
        self.assertNotIn("page_token", queries[0])
        self.assertEqual(queries[1]["page_token"], [first["id"]])
        self.assertEqual(queries[2]["page_token"], [third["id"]])
        self.assertEqual(result.coverage["page_count"], 3)
        self.assertTrue(result.coverage["activity_pages_exhausted"])

    def test_full_page_also_requires_following_empty_page(self):
        venue = Pages([[fill(i) for i in range(100)], []])
        result = venue.fetch(max_records=100)
        self.assertEqual(len(result.records), 100)
        self.assertEqual(len(venue.calls), 2)

    def test_empty_scan_is_not_financial_or_opening_lot_completeness(self):
        result = Pages([[]]).fetch()
        self.assertEqual(result.records, ())
        self.assertTrue(result.coverage["activity_pages_exhausted"])
        self.assertFalse(result.coverage["executions_complete"])
        self.assertFalse(result.coverage["fees_complete"])
        self.assertFalse(result.coverage["opening_lots_complete"])
        self.assertEqual(result.coverage["opening_lots"], [])

    def test_creation_window_does_not_discard_late_posted_execution(self):
        earlier_fill = fill(transaction_time="2026-09-30T14:30:00Z")
        result = Pages([[earlier_fill], []]).fetch()
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0]["effective_at"], "2026-09-30T14:30:00Z")
        self.assertEqual(result.coverage["query_time_basis"], "activity_creation_time")

    def test_repeated_ids_and_cursor_cycles_fail_without_partial_result(self):
        for pages in ([[fill()], [fill()]], [[fill(), fill()]], [[fill(1)], [fill(2)], [fill(1)]]):
            with self.subTest(page_count=len(pages)):
                with self.assertRaisesRegex(ActivityImportError, "repeated_activity_id_or_cursor"):
                    Pages(pages).fetch()

    def test_changed_duplicate_identity_fails_without_overwrite(self):
        with self.assertRaisesRegex(ActivityImportError, "activity_identity_content_conflict"):
            Pages([[fill()], [fill(price="11")]]).fetch()

    def test_page_and_record_caps_fail_closed(self):
        with self.assertRaisesRegex(ActivityImportError, "page_cap_reached"):
            Pages([[fill()]]).fetch(max_pages=1)
        with self.assertRaisesRegex(ActivityImportError, "record_cap_reached"):
            Pages([[fill(1), fill(2)]]).fetch(max_records=1)

    def test_page_failure_does_not_return_a_completed_first_page(self):
        venue = Pages([[fill()], BrokerError("Injected fixture timeout")])
        with self.assertRaisesRegex(BrokerError, "Injected fixture timeout"):
            venue.fetch()
        self.assertEqual(len(venue.calls), 2)

    def test_elapsed_budget_checked_before_and_after_request(self):
        for ticks, expected_calls in (([0, 31], 0), ([0, 0, 31], 1)):
            with self.subTest(expected_calls=expected_calls):
                clock = iter(ticks)
                venue = Pages([[]])
                with self.assertRaisesRegex(ActivityImportError, "deadline_exceeded"):
                    venue.fetch(monotonic=lambda: next(clock))
                self.assertEqual(len(venue.calls), expected_calls)

    def test_future_window_and_invalid_caps_do_not_call_provider(self):
        for changes in ({"max_pages": True}, {"max_pages": 1001}, {"max_records": 0},
                        {"max_elapsed_seconds": float("nan")}, {"max_elapsed_seconds": float("inf")},
                        {"max_elapsed_seconds": True}, {"max_elapsed_seconds": 0}):
            venue = Pages([])
            with self.assertRaises(ValueError):
                venue.fetch(**changes)
            self.assertEqual(venue.calls, [])
        with self.assertRaisesRegex(ValueError, "future"):
            fetch_activities(Pages([], now="2026-10-18T17:59:59Z").client,
                             account_id=ACCOUNT, after=START, until=END)

    def test_return_order_preserved_and_nanosecond_regression_flagged(self):
        later = fill(1, transaction_time="2026-10-16T14:30:00.000000002Z")
        earlier = fill(2, transaction_time="2026-10-16T14:30:00.000000001Z")
        result = Pages([[later], [earlier], []]).fetch()
        self.assertEqual([record["source_id"] for record in result.records], [later["id"], earlier["id"]])
        self.assertIn("activity_execution_order_regressed", result.coverage["blockers"])
        self.assertFalse(result.coverage["executions_complete"])

    def test_unknown_and_correction_rows_visible_after_successful_pagination(self):
        result = Pages([[fill(1), fee(2, activity_type="SSP"), fill(3, type="trade_bust")], []]).fetch()
        self.assertEqual(len(result.records), 1)
        self.assertEqual(len(result.observations), 3)
        self.assertEqual(result.coverage["activity_count"], 3)
        self.assertEqual(set(result.coverage["blockers"]),
                         {"unsupported_account_activity", "activity_correction_requires_review"})

    def test_account_identity_mismatch_fails_closed(self):
        with self.assertRaisesRegex(ActivityImportError, "account_identity_mismatch"):
            Pages([[fill(account_id="different-account")]]).fetch()

    def test_as_of_stays_frozen_upper_bound_and_batch_is_deterministic(self):
        result = Pages([[fill()], []], now="2026-10-18T18:00:03Z").fetch()
        same = Pages([[fill()], []], now="2026-10-18T18:00:03Z").fetch()
        self.assertEqual(result.coverage["as_of"], END)
        self.assertEqual(result.coverage["requested_at"], "2026-10-18T18:00:03Z")
        self.assertEqual(result.batch_id, same.batch_id)
        self.assertTrue(result.batch_id.startswith("alpaca-activities-"))
        self.assertNotIn("FIXTURE-SECRET", json.dumps(result.to_dict()))
        self.assertNotIn("FIXTURE-KEY", json.dumps(result.to_dict()))


if __name__ == "__main__":
    unittest.main()
