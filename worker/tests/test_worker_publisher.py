import base64
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from email.message import Message
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from momentum.alpaca import NoRedirect
from momentum.config import Config
from momentum.journal import Journal
from momentum.publisher import MAX_RESPONSE_BYTES, Publisher, PublisherError


OWNER = "9d59c43f-1d43-494e-afd9-1f73edabb444"
OTHER = "baaaaedd-82bd-4d00-baa1-2d5bc6c0c111"
NOW = datetime(2026, 10, 16, 13, 30, tzinfo=timezone.utc)
SECRET = "sb_secret_FAKE_TEST_ONLY"
CONFIG = Config(supabase_url="https://project.supabase.co", supabase_service_role_key=SECRET,
                owner_user_id=OWNER)
DEMO = {"mode": "demo", "provenance": "synthetic", "run_key": "demo-v1", "strategy_version": "experimental_v1"}
COMMAND = {"id": "905644e3-d607-44c4-a46b-ed3cd5b60ab3", "owner_id": OWNER,
           "kind": "halt_new_entries", "nonce": "06976a0e-2b12-4e38-9408-82a538aa3eb3", "status": "queued",
           "created_at": "2026-10-16T13:29:59Z", "acknowledged_at": None, "error": None}


def jwt_for_role(role):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return encode({"alg": "HS256", "typ": "JWT"}) + "." + encode({"role": role}) + ".FAKE_SIGNATURE"


class Response:
    def __init__(self, body, *, content_type="application/json", status=200):
        self.body, self.status, self.read_limit = body, status, None
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        self.read_limit = limit
        return self.body[:limit]


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "publisher.sqlite3"
        self.journal = Journal(self.path)

    def publisher(self, transport, config=CONFIG):
        return Publisher(config, transport=transport, clock=lambda: NOW)

    def test_demo_publication_is_stable_append_only_and_explicitly_synthetic(self):
        calls = []
        publisher = self.publisher(lambda *args: calls.append(args))
        publisher.publish_demo(DEMO)
        publisher.publish_demo(DEMO)
        method, url, headers, body = calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(body, calls[1][3])
        self.assertIn("resolution=ignore-duplicates", headers["Prefer"])
        self.assertEqual(parse_qs(urlsplit(url).query)["on_conflict"], ["owner_id,run_key"])
        self.assertNotIn(SECRET, str(body))
        for payload in ({**DEMO, "mode": "paper"}, {**DEMO, "provenance": "provider"}, None):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                publisher.publish_demo(payload)
        for table, method in (("demo_runs", "PATCH"), ("demo_runs", "DELETE"), ("paper_runs", "POST")):
            with self.subTest(table=table, method=method), self.assertRaises(PublisherError):
                publisher.request(method, table, payload={"owner_id": OWNER})

    def test_modern_secret_is_apikey_only_and_legacy_service_jwt_uses_bearer(self):
        for secret, bearer in ((SECRET, False), (jwt_for_role("service_role"), True)):
            calls = []
            self.publisher(lambda *args: calls.append(args), replace(CONFIG, supabase_service_role_key=secret)).publish_demo(DEMO)
            headers = calls[0][2]
            self.assertEqual(headers["apikey"], secret)
            self.assertEqual("Authorization" in headers, bearer)
            if bearer:
                self.assertEqual(headers["Authorization"], "Bearer " + secret)

    def test_rejects_non_server_keys_and_non_project_urls_before_any_io(self):
        for secret in ("", "sb_publishable_FAKE", "opaque-unknown", jwt_for_role("authenticated"), "sb_secret_X\nInjected"):
            with self.subTest(secret_kind=secret[:12]), self.assertRaises(ValueError):
                Publisher(replace(CONFIG, supabase_service_role_key=secret))
        for url in ("http://project.supabase.co", "https://project.supabase.co.evil.example",
                    "https://project.supabase.co:443", "https://user@project.supabase.co",
                    "https://project.supabase.co/rest/v1", "https://project.supabase.co?key=bad",
                    "https://project.supabase.co#fragment", "https://extra.project.supabase.co"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Publisher(replace(CONFIG, supabase_url=url))

    def test_http_caps_read_at_limit_plus_one_and_rejects_overflow_without_redirects(self):
        response = Response(b"x" * (MAX_RESPONSE_BYTES + 1))
        with patch("momentum.publisher.build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(PublisherError) as caught:
                Publisher(CONFIG).publish_demo(DEMO)
        self.assertEqual(caught.exception.code, "response_too_large")
        self.assertEqual(response.read_limit, MAX_RESPONSE_BYTES + 1)
        self.assertIsInstance(opener.call_args.args[0], NoRedirect)

    def test_http_rejects_malformed_duplicate_non_json_and_non_success_responses(self):
        cases = ((b'{"x":1,"x":2}', "application/json", 200),
                 (b'{"x":NaN}', "application/json", 200), (b"oops", "application/json", 200),
                 (b"[]", "text/html", 200), (b"[]", "application/json", 302))
        for body, content_type, status in cases:
            with self.subTest(body=body, status=status), patch("momentum.publisher.build_opener") as opener:
                opener.return_value.open.return_value = Response(body, content_type=content_type, status=status)
                with self.assertRaises(PublisherError):
                    Publisher(CONFIG).publish_demo(DEMO)

    def test_transport_errors_and_oversized_payloads_are_safe_and_bounded(self):
        def fail(*args):
            raise RuntimeError("RAW_FAKE_SECRET " + SECRET)
        with self.assertRaises(PublisherError) as caught:
            self.publisher(fail).publish_demo(DEMO)
        self.assertNotIn("RAW_FAKE_SECRET", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        calls = []
        with self.assertRaises(PublisherError) as caught:
            self.publisher(lambda *args: calls.append(args)).publish_demo({**DEMO, "extra": "x" * 2_000_001})
        self.assertEqual(caught.exception.code, "request_too_large")
        self.assertEqual(calls, [])

    def test_empty_verified_queue_does_not_halt_or_ack(self):
        calls = []
        def transport(*args):
            calls.append(args)
            return []
        self.assertEqual(self.publisher(transport).acknowledge_halts(self.journal), [])
        self.assertFalse(self.journal.kill_switch()["enabled"])
        self.assertEqual([call[0] for call in calls], ["GET"])

    def test_halt_is_committed_before_ack_and_emergency_flatten_is_preserved(self):
        self.journal.set_kill_switch(True, "existing emergency", flatten=True, request_id="existing")
        calls = []
        def transport(method, url, headers, payload):
            calls.append((method, url, headers, payload))
            if method == "GET":
                return [deepcopy(COMMAND)]
            state = Journal(self.path).kill_switch()
            self.assertIs(state["enabled"], True)
            self.assertIs(state["flatten_requested"], True)
            self.assertIsNotNone(Journal(self.path).event_by_key("kill:supabase-command-" + COMMAND["id"]))
            return [{**COMMAND, **payload}]
        self.assertEqual(self.publisher(transport).acknowledge_halts(self.journal), [COMMAND["id"]])
        method, url, headers, body = calls[-1]
        self.assertEqual(headers["Prefer"], "return=representation")
        params = parse_qs(urlsplit(url).query)
        self.assertEqual(params["owner_id"], ["eq." + OWNER])
        self.assertEqual(params["nonce"], ["eq." + COMMAND["nonce"]])
        self.assertEqual(params["status"], ["eq.queued"])
        self.assertEqual(params["kind"], ["eq.halt_new_entries"])
        self.assertEqual(set(body), {"status", "acknowledged_at", "error"})
        self.assertIsNone(body["error"])

    def test_unconfirmed_ack_retains_durable_halt_and_retry_reuses_command_marker(self):
        calls = []
        fail_ack = [True]
        def transport(method, url, headers, payload):
            calls.append(method)
            if method == "GET":
                return [deepcopy(COMMAND)]
            if fail_ack[0]:
                raise TimeoutError("RAW_FAKE_SECRET")
            return [{**COMMAND, **payload}]
        publisher = self.publisher(transport)
        with self.assertRaises(PublisherError) as caught:
            publisher.acknowledge_halts(self.journal)
        self.assertEqual(caught.exception.code, "command_ack_unconfirmed")
        self.assertTrue(Journal(self.path).kill_switch()["enabled"])
        self.assertFalse(Journal(self.path).kill_switch()["flatten_requested"])
        fail_ack[0] = False
        self.assertEqual(publisher.acknowledge_halts(Journal(self.path)), [COMMAND["id"]])
        self.assertEqual(len(self.journal.events(event_type="kill_switch_changed")), 1)
        self.assertEqual(calls, ["GET", "PATCH", "GET", "PATCH"])

    def test_missing_or_mismatched_ack_is_not_claimed_as_completed(self):
        cases = (None, [], [{**COMMAND, "status": "acknowledged", "acknowledged_at": NOW.isoformat(), "owner_id": OTHER}])
        for ack in cases:
            with self.subTest(ack=ack):
                publisher = self.publisher(lambda method, *_: [deepcopy(COMMAND)] if method == "GET" else deepcopy(ack))
                with self.assertRaises(PublisherError) as caught:
                    publisher.acknowledge_halts(self.journal)
                self.assertEqual(caught.exception.code, "command_ack_unconfirmed")
                self.assertTrue(Journal(self.path).kill_switch()["enabled"])

    def test_invalid_entire_batch_halts_without_partial_ack_or_remote_error_echo(self):
        invalid = (None, {"error": "RAW_FAKE_SECRET"}, [{**COMMAND, "owner_id": OTHER}],
                   [{**COMMAND, "kind": "flatten"}], [{**COMMAND, "nonce": "bad"}],
                   [{**COMMAND, "error": "RAW_FAKE_SECRET"}],
                   [{**COMMAND, "created_at": "2026-10-17T13:30:00Z"}],
                   [deepcopy(COMMAND), deepcopy(COMMAND)],
                   [deepcopy(COMMAND), {**COMMAND, "id": OTHER, "nonce": "malformed"}])
        for response in invalid:
            calls = []
            def transport(method, *args):
                calls.append(method)
                return deepcopy(response)
            with self.subTest(response_type=type(response).__name__), self.assertRaises(PublisherError) as caught:
                self.publisher(transport).acknowledge_halts(self.journal)
            self.assertEqual(caught.exception.code, "command_invalid")
            self.assertNotIn("RAW_FAKE_SECRET", str(caught.exception))
            self.assertEqual(calls, ["GET"])
            self.assertTrue(Journal(self.path).kill_switch()["enabled"])
        self.assertNotIn("RAW_FAKE_SECRET", json.dumps(self.journal.events()))

    def test_unreadable_command_channel_halts_and_cannot_claim_ack(self):
        def transport(*args):
            raise OSError("RAW_FAKE_SECRET")
        with self.assertRaises(PublisherError) as caught:
            self.publisher(transport).acknowledge_halts(self.journal)
        self.assertEqual(caught.exception.code, "command_poll_failed")
        self.assertTrue(Journal(self.path).kill_switch()["enabled"])
        self.assertNotIn("RAW_FAKE_SECRET", json.dumps(self.journal.events()))

    def test_historical_halt_marker_does_not_override_later_clear(self):
        self.journal.set_kill_switch(True, "Supabase halt_new_entries command",
                                     request_id="supabase-command-" + COMMAND["id"])
        self.journal.set_kill_switch(False, "local test reset", request_id="later-clear")
        def transport(method, url, headers, payload):
            if method == "GET":
                return [deepcopy(COMMAND)]
            self.assertTrue(Journal(self.path).kill_switch()["enabled"])
            return [{**COMMAND, **payload}]
        self.assertEqual(self.publisher(transport).acknowledge_halts(self.journal), [COMMAND["id"]])
        self.assertEqual(len(self.journal.events(event_type="kill_switch_changed")), 3)

    def test_local_halt_failure_prevents_remote_ack(self):
        calls = []
        def transport(method, *args):
            calls.append(method)
            return [deepcopy(COMMAND)]
        with patch.object(self.journal, "set_kill_switch", side_effect=OSError("RAW_FAKE_SECRET")):
            with self.assertRaises(PublisherError) as caught:
                self.publisher(transport).acknowledge_halts(self.journal)
        self.assertEqual(caught.exception.code, "local_halt_unavailable")
        self.assertEqual(calls, ["GET"])

