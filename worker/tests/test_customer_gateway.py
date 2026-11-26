"""Customer HTTP -> actual paper controller, with invented Auth/DB/broker only."""
import base64
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from email.message import Message
import io
import json
import unittest
from unittest.mock import patch
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit, parse_qs

from momentum.customer_cloud import SupabaseCustomerCloud, CustomerCloudError, MAX_RESPONSE
from momentum.customer_gateway import CustomerGateway, CONSENT_VERSION
from momentum.customer_runtime import build_customer_paper_runtime
from momentum.customer_settings import SettingsBindingError
from momentum.server import Application, make_server
import test_customer_settings as settings_tests

OWNER, ACCOUNT, VERSION, OTHER = (settings_tests.OWNER, settings_tests.ACCOUNT,
                                  settings_tests.VERSION, settings_tests.OTHER)
ORIGIN = "https://portal.example.test"
PROJECT = "https://project-under-test.supabase.co"
PUBLIC = "sb_publishable_FAKE_ONLY"
SECRET = "sb_secret_FAKE_ONLY"
TOKEN = "test.user.signature"


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def legacy(role):
    segment = base64.urlsafe_b64encode(encoded({"role": role})).decode().rstrip("=")
    return "header." + segment + ".signature"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.f = settings_tests.CustomerSettingsTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.base = replace(self.f.base, cors_origin=ORIGIN, control_token="operator-test-only",
            supabase_url=PROJECT, supabase_service_role_key=SECRET)
        self.calls = []
        self.auth_user = {"id": OWNER, "aud": "authenticated", "role": "authenticated", "is_anonymous": False}
        self.auth_status = 200
        self.settings_status = 200
        self.custom_response = None
        self.cloud = self.new_cloud()

    def cloud_transport(self, url, headers):
        self.calls.append((url, deepcopy(headers)))
        if self.custom_response is not None:
            return self.custom_response
        if urlsplit(url).path == "/auth/v1/user":
            return self.auth_status, {"Content-Type": "application/json"}, encoded(self.auth_user)
        self.assertEqual(urlsplit(url).path, "/rest/v1/autopilot_settings")
        return self.settings_status, {"Content-Type": "application/json"}, encoded([self.f.row])

    def new_cloud(self, **overrides):
        args = dict(project_url=PROJECT, publishable_key=PUBLIC, server_key=SECRET,
                    owner_id=OWNER, clock=lambda: self.f.venue.now, transport=self.cloud_transport)
        args.update(overrides)
        return SupabaseCustomerCloud(**args)

    def start(self):
        self.worker = self.f.runner()
        self.worker.settings_loader = self.cloud.settings_for
        self.worker.__enter__()
        self.addCleanup(self.worker.__exit__)
        self.gateway = CustomerGateway(self.worker, self.cloud, origin=ORIGIN)
        self.worker.cycle()
        return self.worker

    def request(self, action="state", payload=None, *, headers=None, body=None, request_id=None, method=None, path=None):
        hdr = {"Authorization": "Bearer " + TOKEN, "Origin": ORIGIN,
               "x-tactirise-api-version": "v1", "Content-Type": "application/json",
               "x-tactirise-request-id": request_id or str(uuid.uuid4())}
        if headers:
            hdr.update(headers)
        method = method or ("GET" if action == "state" else "POST")
        data = body if body is not None else encoded(payload) if payload is not None else b""
        return self.gateway.handle(method, path or "/v1/bot/" + action, hdr, data)

    def consent(self, request_id=None):
        return self.request("consent", {"config_version": VERSION, "mode": "paper",
            "acknowledged": True, "disclosure_version": CONSENT_VERSION}, request_id=request_id)

    def activate(self, request_id=None):
        self.assertEqual(self.consent()[0], 200)
        return self.request("activate", {"config_version": VERSION, "mode": "paper"}, request_id=request_id)


class CloudTests(Fixture):
    def test_identity_is_verified_by_auth_not_token_claims(self):
        self.auth_user["id"] = OTHER
        forged_claim = legacy("authenticated")
        self.assertEqual(self.cloud.authenticate("Bearer " + forged_claim), OTHER)
        self.assertEqual(self.calls[0][0], PROJECT + "/auth/v1/user")
        self.assertEqual(self.calls[0][1]["apikey"], PUBLIC)
        self.assertEqual(self.calls[0][1]["Authorization"], "Bearer " + forged_claim)
        self.assertNotIn(SECRET, repr(self.calls))

    def test_invalid_bearers_never_reach_transport(self):
        for value in (None, "", "Bearer ", "Basic abc", "Bearer sb_secret_bad", "Bearer a.b.c\r\nx: y", "Bearer " + "a" * 9000):
            with self.subTest(value=str(value)[:30]), self.assertRaises(CustomerCloudError):
                self.cloud.authenticate(value)
        self.assertEqual(self.calls, [])

    def test_revoked_or_expired_auth_is_not_cached(self):
        self.assertEqual(self.cloud.authenticate("Bearer " + TOKEN), OWNER)
        self.auth_status = 401
        with self.assertRaises(CustomerCloudError) as err:
            self.cloud.authenticate("Bearer " + TOKEN)
        self.assertEqual(err.exception.status, 401)
        self.assertEqual(len(self.calls), 2)

    def test_anonymous_and_nonuser_roles_are_rejected(self):
        for change in ({"is_anonymous": True}, {"is_anonymous": "false"}, {"is_anonymous": None},
                       {"role": "service_role"}, {"aud": "anon"}, {"id": "bad"}):
            previous = deepcopy(self.auth_user)
            self.auth_user.update(change)
            with self.subTest(change=change), self.assertRaises(CustomerCloudError):
                self.cloud.authenticate("Bearer " + TOKEN)
            self.auth_user = previous

    def test_settings_are_exact_owner_scoped_and_uncached(self):
        self.assertEqual(self.cloud.settings_for(OWNER), self.f.row)
        url, headers = self.calls[-1]
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["owner_id"], ["eq." + OWNER])
        self.assertEqual(query["limit"], ["2"])
        self.assertEqual(query["select"], ["owner_id,version,request_id,configuration,updated_at"])
        self.assertEqual(headers["apikey"], SECRET)
        self.assertNotIn("Authorization", headers)
        self.f.row["version"] = OTHER
        self.assertEqual(self.cloud.settings_for(OWNER)["version"], OTHER)
        self.assertEqual(len(self.calls), 2)

    def test_cross_owner_settings_read_never_reaches_transport(self):
        with self.assertRaises(CustomerCloudError):
            self.cloud.settings_for(OTHER)
        self.assertEqual(self.calls, [])

    def test_missing_multiple_foreign_and_invalid_rows_fail_closed(self):
        wrong = deepcopy(self.f.row); wrong["owner_id"] = OTHER
        for rows in ([], [self.f.row, self.f.row], [wrong], {}, [{**self.f.row, "injected": True}]):
            self.custom_response = 200, {"content-type": "application/json"}, encoded(rows)
            with self.subTest(rows=str(rows)[:30]), self.assertRaises(SettingsBindingError):
                self.cloud.settings_for(OWNER)

    def test_invalid_remote_status_content_type_compression_and_size(self):
        for response in ((302, {}, b""), (500, {}, b"SECRET"),
                         (200, {"content-type": "text/html"}, b"{}"),
                         (200, {"content-type": "application/json", "content-encoding": "gzip"}, b"{}"),
                         (200, {"content-type": "application/json", "content-length": "999"}, b"{}"),
                         (200, {"content-type": "application/json"}, b"x" * (MAX_RESPONSE + 1))):
            self.custom_response = response
            with self.subTest(response=response[:2]), self.assertRaises(CustomerCloudError) as error:
                self.cloud.authenticate("Bearer " + TOKEN)
            self.assertNotIn("SECRET", str(error.exception))

    def test_duplicate_nonfinite_malformed_and_deep_json_rejected(self):
        for body in (b'{"id":"a","id":"b"}', b'{"value":NaN}', b'\xff', b'{broken', b'[' * 2000 + b']' * 2000):
            self.custom_response = 200, {"content-type": "application/json"}, body
            with self.subTest(body=body[:40]), self.assertRaises(CustomerCloudError):
                self.cloud.authenticate("Bearer " + TOKEN)

    def test_slow_response_is_rejected_after_receipt(self):
        times = iter([0.0, 4.0])
        cloud = self.new_cloud(monotonic=lambda: next(times))
        with self.assertRaises(CustomerCloudError):
            cloud.authenticate("Bearer " + TOKEN)

    def test_transport_errors_do_not_echo_private_messages(self):
        def bad(*args):
            raise RuntimeError("PRIVATE KEY: abc")
        with self.assertRaises(CustomerCloudError) as error:
            self.new_cloud(transport=bad).authenticate("Bearer " + TOKEN)
        self.assertEqual(str(error.exception), "customer_service_unavailable")

    def test_configuration_rejects_bad_urls_and_api_key_roles(self):
        for url in ("http://a.supabase.co", PROJECT + "/", PROJECT + "?x=1", PROJECT + ":443",
                    "https://a.supabase.co.evil.test", "https://u:p@a.supabase.co", "https://127.0.0.1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.new_cloud(project_url=url)
        for kwargs in ({"publishable_key": SECRET}, {"server_key": PUBLIC},
                       {"publishable_key": legacy("service_role")}, {"server_key": legacy("anon")},
                       {"owner_id": "uuid?evil"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.new_cloud(**kwargs)
        self.assertEqual(self.calls, [])

    def test_legacy_keys_keep_correct_header_separation(self):
        public, secret = legacy("anon"), legacy("service_role")
        cloud = self.new_cloud(publishable_key=public, server_key=secret)
        cloud.authenticate("Bearer " + TOKEN)
        self.assertEqual(self.calls[-1][1]["Authorization"], "Bearer " + TOKEN)
        cloud.settings_for(OWNER)
        self.assertEqual(self.calls[-1][1]["Authorization"], "Bearer " + secret)
        self.assertEqual(self.calls[-1][1]["apikey"], secret)

    def test_default_http_get_uses_fixed_get_no_redirect_and_bounded_read(self):
        class Response:
            status = 200
            headers = {"content-type": "application/json"}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, count):
                self.count = count
                return b"{}"
        response = Response()
        with patch("momentum.customer_cloud.build_opener") as opener:
            opener.return_value.open.return_value = response
            self.cloud._http_get(PROJECT + "/auth/v1/user", {"apikey": PUBLIC})
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(response.count, MAX_RESPONSE + 1)
            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 3.0)
            self.assertEqual(opener.call_args.args[0].proxies, {})
            self.assertEqual(type(opener.call_args.args[1]).__name__, "NoRedirect")

    def test_default_transport_does_not_read_upstream_error_body(self):
        error_body = io.BytesIO(b"UPSTREAM PRIVATE BODY")
        error = HTTPError(PROJECT, 302, "redirect", {}, error_body)
        with patch("momentum.customer_cloud.build_opener") as opener:
            opener.return_value.open.side_effect = error
            self.assertEqual(self.cloud._http_get(PROJECT, {}), (302, {}, b""))
        self.assertTrue(error_body.closed)


class GatewayTests(Fixture):
    def setUp(self):
        super().setUp()
        self.start()

    def test_state_is_owner_scoped_and_contains_no_keys_or_raw_worker_data(self):
        status, state = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(state["owner_id"], OWNER)
        self.assertEqual(state["status"], "paused")
        self.assertTrue(state["runtime_fresh"])
        for forbidden in (SECRET, PUBLIC, TOKEN, ACCOUNT, "FAKE-SECRET", "session", "api_key"):
            self.assertNotIn(forbidden, json.dumps(state))
        for absent in ("positions", "orders", "activity"):
            self.assertNotIn(absent, state)
        self.assertFalse(state["records_available"])

    def test_foreign_owner_cannot_read_or_command_this_worker(self):
        self.auth_user["id"] = OTHER
        count = len(self.f.venue.calls)
        for action in ("state", "consent", "activate", "pause", "emergency-stop"):
            result = self.request(action, {})
            self.assertEqual(result, (403, {"error": "owner_not_provisioned"}))
            self.assertNotIn(OWNER, str(result))
        self.assertEqual(len(self.f.venue.calls), count)
        self.assertIsNone(self.worker.journal.latest("autopilot_command", "autopilot"))

    def test_origin_version_and_path_checked_before_auth(self):
        count = len(self.calls)
        for kwargs in ({"headers": {"Origin": "https://evil.test"}},
                       {"headers": {"x-tactirise-api-version": "v2"}},
                       {"path": "/v1/bot/state?owner=" + OWNER},
                       {"path": "/v1/bot/%73tate"}, {"method": "DELETE"}):
            self.assertGreaterEqual(self.request(**kwargs)[0], 400)
        self.assertEqual(len(self.calls), count)

    def test_duplicate_authorization_and_version_headers_rejected(self):
        for duplicate in ("Authorization", "x-tactirise-api-version", "Origin"):
            headers = Message()
            for k, v in {"Authorization": "Bearer " + TOKEN, "x-tactirise-api-version": "v1", "Origin": ORIGIN}.items():
                headers[k] = v
            headers[duplicate] = headers[duplicate]
            self.assertEqual(self.gateway.handle("GET", "/v1/bot/state", headers)[0], 400)

    def test_activation_requires_separately_recorded_explicit_paper_consent(self):
        status, result = self.request("activate", {"config_version": VERSION, "mode": "paper"})
        self.assertEqual((status, result["error"]), (409, "current_paper_consent_required"))
        self.assertEqual(self.f.posts(), [])
        self.assertEqual(self.consent()[0], 200)
        self.assertEqual(self.worker._control()["action"], "pause")
        self.assertEqual(self.f.posts(), [])

    def test_consent_false_wrong_disclosure_or_extra_fields_never_persist(self):
        payload = {"config_version": VERSION, "mode": "paper", "acknowledged": True, "disclosure_version": CONSENT_VERSION}
        for change in ({"acknowledged": False}, {"acknowledged": "true"}, {"disclosure_version": "old"},
                       {"owner_id": OWNER}, {"broker_key": "BAD"}, {"mode": "live"}):
            self.assertGreaterEqual(self.request("consent", {**payload, **change})[0], 400)
        self.assertIsNone(self.worker.journal.latest("customer_paper_consent", "autopilot"))

    def test_consent_retry_does_not_refresh_timestamp(self):
        request = str(uuid.uuid4())
        first = self.consent(request)[1]
        self.f.venue.advance(10)
        second = self.consent(request)[1]
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["recorded_at"], second["recorded_at"])
        self.assertEqual(len(self.worker.journal.events("autopilot", event_type="customer_paper_consent")), 1)

    def test_expired_and_future_consent_rejected(self):
        self.consent()
        self.f.venue.advance(901)
        self.worker.cycle()
        self.assertEqual(self.request("activate", {"config_version": VERSION, "mode": "paper"})[0], 409)
        self.f.venue.advance(-902)
        self.assertEqual(self.request("activate", {"config_version": VERSION, "mode": "paper"})[0], 409)

    def test_changed_settings_or_worker_hash_cannot_reuse_consent(self):
        self.consent()
        self.f.row["version"] = OTHER
        self.assertEqual(self.request("activate", {"config_version": VERSION, "mode": "paper"})[0], 409)
        self.f.row["version"] = VERSION
        original = self.worker.worker_config_hash
        self.worker.worker_config_hash = "f" * 64
        try:
            self.assertEqual(self.request("activate", {"config_version": VERSION, "mode": "paper"})[0], 409)
        finally:
            self.worker.worker_config_hash = original
        self.assertEqual(self.f.posts(), [])

    def test_activation_receipt_is_not_active_until_a_real_worker_cycle(self):
        status, result = self.activate()
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "activating")
        self.assertTrue(result["command"]["acknowledged"])
        self.assertEqual(self.f.posts(), [])
        self.worker.cycle()
        state = self.request()[1]
        self.assertEqual(state["status"], "active")
        self.assertEqual(len(self.f.posts()), 1)

    def test_retry_old_activation_cannot_undo_newer_pause(self):
        request = str(uuid.uuid4())
        self.activate(request)
        self.worker.cycle()
        self.assertEqual(self.request("pause")[0], 200)
        result = self.request("activate", {"config_version": VERSION, "mode": "paper"}, request_id=request)
        self.assertTrue(result[1]["command"]["duplicate"])
        self.assertEqual(result[1]["status"], "paused")
        self.assertEqual(self.worker._control()["action"], "pause")

    def test_request_id_cannot_be_reused_for_different_actions_or_consent(self):
        request = str(uuid.uuid4())
        self.assertEqual(self.consent(request)[0], 200)
        self.assertEqual(self.request("pause", request_id=request)[0], 409)
        other = str(uuid.uuid4())
        self.assertEqual(self.request("pause", request_id=other)[0], 200)
        self.assertEqual(self.request("emergency-stop", request_id=other)[0], 409)
        self.assertFalse(self.worker.journal.kill_switch()["enabled"])

    def test_pause_stop_work_with_changed_or_unavailable_settings_and_stale_runtime(self):
        self.activate()
        self.worker.cycle()
        entry = next(iter(self.f.venue.orders))
        self.f.venue.fill(entry, 1, "9.82")
        self.worker.cycle()
        self.settings_status = 503
        self.f.venue.advance(31)
        self.assertEqual(self.request("pause")[0], 200)
        status, stopped = self.request("emergency-stop")
        self.assertEqual(status, 200)
        self.assertEqual(stopped["status"], "stopped")
        self.assertFalse(stopped["positions_confirmed_flat"])
        self.assertTrue(self.worker.journal.kill_switch()["flatten_requested"])

    def test_stale_future_or_foreign_cycle_never_becomes_active(self):
        self.activate(); self.worker.cycle()
        original = deepcopy(self.worker.latest)
        for change in ({"completed_at": (self.f.venue.now - timedelta(seconds=31)).isoformat()},
                       {"completed_at": (self.f.venue.now + timedelta(seconds=1)).isoformat()},
                       {"owner_id": OTHER}, {"worker_review_hash": "wrong"}, {"reconciled": False}):
            self.worker.latest = {**original, **change}
            self.assertNotEqual(self.request()[1]["status"], "active")
        self.worker.latest = original

    def test_new_gateway_observation_does_not_refresh_worker_timestamp(self):
        self.activate(); self.worker.cycle()
        stamp = self.worker.latest["completed_at"]
        self.f.venue.advance(31)
        result = self.request()[1]
        self.assertEqual(result["runtime_reported_at"], stamp)
        self.assertFalse(result["runtime_fresh"])
        self.assertEqual(result["status"], "error")

    def test_invalid_json_extra_identity_modes_and_body_sizes_fail_closed(self):
        payload = {"config_version": VERSION, "mode": "paper"}
        for body in (b'{"mode":"paper","mode":"live"}', b'{"x":NaN}', b'[' * 2000 + b']' * 2000,
                     encoded({**payload, "owner_id": OWNER}), encoded({**payload, "mode": "live"}),
                     b"x" * 8193):
            self.assertGreaterEqual(self.request("activate", body=body)[0], 400)
        self.assertEqual(self.f.posts(), [])

    def test_missing_request_id_content_type_and_unexpected_bodies_rejected(self):
        self.assertEqual(self.request("pause", headers={"x-tactirise-request-id": "bad"})[0], 400)
        self.assertEqual(self.request("pause", {})[0], 400)
        self.assertEqual(self.request("state", {})[0], 400)
        self.assertEqual(self.request("activate", {}, headers={"Content-Type": "text/plain"})[0], 415)

    def test_auth_outage_or_revocation_cannot_read_state_or_write_commands(self):
        for status in (401, 403, 500):
            self.auth_status = status
            for action in ("state", "pause", "emergency-stop"):
                result = self.request(action)
                self.assertIn(result[0], (401, 503))
                self.assertNotIn(OWNER, str(result))
        self.assertIsNone(self.worker.journal.latest("autopilot_command", "autopilot"))

    def test_invalid_database_settings_latch_pause_and_restore_cannot_resume(self):
        self.activate(); self.worker.cycle()
        self.f.row["configuration"]["market"] = "crypto"
        self.worker.cycle()
        self.assertEqual(self.worker._control()["action"], "pause")
        self.f.row["configuration"]["market"] = "stocks"
        self.worker.cycle()
        self.assertFalse(self.worker.state()["entries_enabled"])
        self.assertEqual(self.worker._control()["action"], "pause")

    def test_deleted_database_row_latches_pause_instead_of_temporary_outage(self):
        self.activate(); self.worker.cycle()
        original = self.cloud._transport
        self.cloud._transport = lambda url, headers: (
            (200, {"content-type": "application/json"}, b"[]")
            if "/rest/v1/" in url else original(url, headers))
        self.worker.cycle()
        self.assertEqual(self.worker._control()["action"], "pause")
        self.cloud._transport = original
        self.worker.cycle()
        self.assertFalse(self.worker.state()["entries_enabled"])

    def test_no_default_approval_or_missing_reconciliation_can_activate(self):
        self.consent()
        latest = self.worker.latest
        self.worker.latest = None
        self.assertEqual(self.request("activate", {"config_version": VERSION, "mode": "paper"})[0], 409)
        self.worker.latest = latest
        self.worker.config = replace(self.worker.config, paper_enabled=False)
        self.assertEqual(self.request("activate", {"config_version": VERSION, "mode": "paper"})[0], 409)
        self.assertEqual(self.f.posts(), [])

    def test_preflight_has_exact_origin_method_headers_and_no_auth_calls(self):
        before = len(self.calls)
        hdr = {"Origin": ORIGIN, "Access-Control-Request-Method": "POST",
               "Access-Control-Request-Headers": "Authorization, Content-Type, X-TactiRise-API-Version, X-TactiRise-Request-ID"}
        self.assertEqual(self.gateway.preflight("/v1/bot/pause", hdr), (204, None))
        for changes in ({"Origin": "null"}, {"Access-Control-Request-Method": "DELETE"},
                        {"Access-Control-Request-Headers": "x-owner-id"}):
            self.assertGreaterEqual(self.gateway.preflight("/v1/bot/pause", {**hdr, **changes})[0], 400)
        self.assertEqual(len(self.calls), before)

    def test_gateway_requires_explicit_customer_binding_and_exact_origin(self):
        for origin in ("*", "http://portal.test", ORIGIN + "/", ORIGIN + "?a=1", "https://x@y.test"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                CustomerGateway(self.worker, self.cloud, origin=origin)
        binding = self.worker.customer_binding
        self.worker.customer_binding = None
        try:
            with self.assertRaises(ValueError):
                CustomerGateway(self.worker, self.cloud, origin=ORIGIN)
        finally:
            self.worker.customer_binding = binding

    def test_recreated_gateway_preserves_durable_consent_and_command_idempotency(self):
        request = str(uuid.uuid4())
        self.activate(request)
        self.worker.cycle()
        self.gateway = CustomerGateway(self.worker, self.cloud, origin=ORIGIN)
        result = self.request("activate", {"config_version": VERSION, "mode": "paper"}, request_id=request)
        self.assertEqual(result[0], 200)
        self.assertTrue(result[1]["command"]["duplicate"])
        self.assertEqual(len(self.f.posts()), 1)

    def test_partial_fill_pause_and_stop_preserve_protective_exit_path(self):
        self.activate(); self.worker.cycle()
        entry = next(iter(self.f.venue.orders))
        self.f.venue.fill(entry, 4, "9.82")
        self.assertEqual(self.request("pause")[0], 200)
        self.f.venue.advance()
        result = self.worker.cycle()
        self.assertIn("cancel_opening_remainder", [a["kind"] for a in result["session"]["actions"]])
        self.f.venue.orders[entry]["status"] = "canceled"
        self.assertEqual(self.request("emergency-stop")[0], 200)
        self.f.venue.advance()
        self.f.bid, self.f.ask = "9.70", "9.72"
        result = self.worker.cycle()
        self.assertEqual(self.f.posts()[-1]["payload"]["side"], "sell")
        self.assertFalse(result["positions_confirmed_flat"])
        self.assertFalse(self.request()[1]["positions_confirmed_flat"])


class RuntimeTests(Fixture):
    def build(self, base=None, **extra):
        kwargs = dict(account_id=ACCOUNT, publishable_key=PUBLIC,
            expected_settings_version=VERSION, reviewed_strategy_ids=self.f.registry,
            clock=lambda: self.f.venue.now, cloud_factory=lambda **kwargs: self.cloud)
        kwargs.update(extra)
        return build_customer_paper_runtime(base or self.f.base, **kwargs)

    def test_runtime_wires_cloud_loader_and_gateway_without_starting_or_arming(self):
        cfg, worker, gateway = self.build()
        self.assertFalse(worker.active)
        self.assertFalse(cfg.paper_enabled)
        self.assertEqual(cfg.paper_reviewed_config_hash, "")
        self.assertEqual(worker.settings_loader, self.cloud.settings_for)
        self.assertIs(gateway.worker, worker)
        self.assertEqual(self.f.venue.calls, [])

    def test_only_existing_exact_operator_approval_is_preserved(self):
        compiled, _ = self.f.compile()
        base = replace(self.f.base, paper_enabled=True, paper_reviewed_config_hash=compiled.review_hash())
        cfg, _, _ = self.build(base)
        self.assertTrue(cfg.paper_enabled)
        for candidate in (replace(base, paper_enabled=False), replace(base, paper_reviewed_config_hash="wrong")):
            self.assertFalse(self.build(candidate)[0].paper_enabled)

    def test_runtime_rejects_wrong_settings_version_unapproved_strategy_and_bad_identity(self):
        for extra in ({"expected_settings_version": OTHER}, {"reviewed_strategy_ids": {}},
                      {"expected_settings_version": "bad"}, {"account_id": "bad"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.build(**extra)
        self.assertEqual(self.f.venue.calls, [])

    def test_runtime_requires_paper_operator_auth_and_https_origin_before_io(self):
        for change in ({"mode": "demo"}, {"control_token": ""}, {"cors_origin": "*"}):
            # Config itself refuses '*' as well.
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.build(replace(self.f.base, **change))
        self.assertEqual(self.calls, [])


class CustomerHTTPTests(Fixture):
    def setUp(self):
        super().setUp()
        self.start()
        class OfflineServer:
            def __init__(self, address, handler):
                self.handler = handler
                self.server_name, self.server_port = address
        self.server_type = OfflineServer
        self.server = make_server(self.worker.config,
            Application(self.worker.config, autopilot=self.worker, customer_gateway=self.gateway),
            server_class=OfflineServer)

    def http(self, method, path, payload=None, extra=(), body=None, server=None):
        body = body if body is not None else encoded(payload) if payload is not None else b""
        headers = [("Host", "localhost"), ("Authorization", "Bearer " + TOKEN), ("Origin", ORIGIN),
                   ("Content-Type", "application/json"), ("x-tactirise-api-version", "v1"),
                   ("x-tactirise-request-id", str(uuid.uuid4())), ("Content-Length", str(len(body)))]
        headers.extend(extra)
        raw = (f"{method} {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n").encode() + body
        class Socket:
            output = b""
            def makefile(self, *args): return io.BytesIO(raw)
            def sendall(self, data): self.output += data
            def settimeout(self, seconds): self.timeout = seconds
        conn = Socket()
        server = server or self.server
        server.handler(conn, ("127.0.0.1", 0), server)
        response, data = conn.output.split(b"\r\n\r\n", 1)
        return int(response.split(b" ", 2)[1]), json.loads(data) if data else None, response.decode()

    def test_http_consent_activation_cycle_pause_stop_end_to_end(self):
        body = {"config_version": VERSION, "mode": "paper", "acknowledged": True, "disclosure_version": CONSENT_VERSION}
        self.assertEqual(self.http("POST", "/v1/bot/consent", body)[0], 200)
        status, result, _ = self.http("POST", "/v1/bot/activate", {"config_version": VERSION, "mode": "paper"})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "activating")
        self.assertEqual(self.f.posts(), [])
        self.worker.cycle()
        self.assertEqual(self.http("GET", "/v1/bot/state")[1]["status"], "active")
        self.assertEqual(len(self.f.posts()), 1)
        self.assertEqual(self.http("POST", "/v1/bot/pause")[1]["status"], "paused")
        self.assertEqual(self.http("POST", "/v1/bot/emergency-stop")[1]["status"], "stopped")

    def test_default_server_does_not_enable_customer_routes(self):
        server = make_server(self.worker.config, Application(self.worker.config), server_class=self.server_type)
        self.assertEqual(self.http("GET", "/v1/bot/state", server=server)[0], 503)
        self.assertEqual(self.http("POST", "/v1/bot/activate", {}, server=server)[0], 503)
        self.assertEqual(self.f.posts(), [])

    def test_customer_jwt_does_not_authenticate_shared_operator_routes(self):
        self.assertEqual(self.http("GET", "/api/autopilot/state")[0], 401)
        self.assertEqual(self.http("POST", "/api/halt", {})[0], 401)

    def test_http_duplicate_length_authorization_and_transfer_encoding_rejected(self):
        for extra in (("Content-Length", "0"), ("Authorization", "Bearer other.user.token"),
                      ("Transfer-Encoding", "chunked"), ("x-tactirise-api-version", "v1")):
            self.assertEqual(self.http("POST", "/v1/bot/pause", extra=[extra])[0], 400)
        self.assertEqual(self.http("POST", "/v1/bot/activate", body=b"x" * 8193)[0], 413)

    def test_http_cors_preflight_and_private_cache_headers(self):
        status, _, headers = self.http("OPTIONS", "/v1/bot/pause", extra=[
            ("Access-Control-Request-Method", "POST"),
            ("Access-Control-Request-Headers", "Authorization, X-TactiRise-API-Version, X-TactiRise-Request-ID")])
        self.assertEqual(status, 204)
        self.assertIn("X-TactiRise-Request-ID", headers)
        _, _, state_headers = self.http("GET", "/v1/bot/state")
        self.assertIn("Cache-Control: no-store", state_headers)
        self.assertIn("X-Content-Type-Options: nosniff", state_headers)
        self.assertIn("Access-Control-Allow-Origin: " + ORIGIN, state_headers)

    def test_gateway_cannot_be_attached_to_different_worker_or_origin(self):
        with self.assertRaises(ValueError):
            Application(self.worker.config, customer_gateway=self.gateway)
        with self.assertRaises(ValueError):
            Application(replace(self.worker.config, cors_origin="https://other.test"),
                        autopilot=self.worker, customer_gateway=self.gateway)


if __name__ == "__main__":
    unittest.main()
