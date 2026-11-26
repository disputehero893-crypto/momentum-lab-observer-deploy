"""Unit tests for paper_runs / paper_events logging (mocked Supabase transport)."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import warnings

from momentum.alpaca import NoRedirect
from momentum.paper_run_logging import (
    EVENT_KINDS,
    MAX_RESPONSE_BYTES,
    PaperRunLogger,
    PaperRunLoggingError,
    create_paper_run_logger,
)


OWNER = "9d59c43f-1d43-494e-afd9-1f73edabb444"
NOW = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
SECRET = "sb_secret_FAKE_TEST_ONLY"


@dataclass
class FakeConfig:
    mode: str = "paper"
    supabase_url: str = "https://project.supabase.co"
    supabase_service_role_key: str = SECRET
    owner_user_id: str = OWNER
    api_base_url: str = ""
    alpaca_base_url: str = ""


CONFIG = FakeConfig()


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


def _run_row(run_id, run_key="paper-2026-10-28"):
    return {
        "id": run_id,
        "owner_id": OWNER,
        "run_key": run_key,
        "mode": "paper",
        "strategy_version": "liquid_large_cap_iex_v1",
        "summary": {"status": "running", "watchlist": ["AAPL", "MSFT", "SPY"], "mode": "paper"},
        "started_at": "2026-10-28T15:00:00.000000Z",
        "completed_at": None,
        "created_at": "2026-10-28T15:00:00.000000Z",
    }


class PaperRunLoggingTests(unittest.TestCase):
    def logger(self, transport, config=CONFIG):
        return PaperRunLogger(config, transport=transport, clock=lambda: NOW)

    def test_create_degrades_to_warning_when_credentials_missing(self):
        for cfg in (
            FakeConfig(supabase_url="", supabase_service_role_key=""),
            FakeConfig(supabase_url="https://project.supabase.co", supabase_service_role_key=""),
            FakeConfig(supabase_url="", supabase_service_role_key=SECRET),
            FakeConfig(owner_user_id=""),
        ):
            with self.subTest(cfg=cfg), warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                logger = create_paper_run_logger(cfg, warn_fn=warnings.warn)
                self.assertFalse(logger.enabled)
                self.assertIsNone(logger.start_run(run_key="x", strategy_version="v1"))
                self.assertFalse(logger.append_event(event_key="e", kind="heartbeat", payload={}))
                self.assertFalse(logger.close_run())
                self.assertTrue(any(issubclass(w.category, RuntimeWarning) for w in caught))

    def test_rejects_non_paper_mode_in_factory_and_constructor(self):
        with self.assertRaises(PaperRunLoggingError) as caught:
            create_paper_run_logger(FakeConfig(mode="demo"))
        self.assertEqual(caught.exception.code, "mode_rejected")
        with self.assertRaises(PaperRunLoggingError) as caught:
            PaperRunLogger(FakeConfig(mode="live"))
        self.assertEqual(caught.exception.code, "mode_rejected")

    def test_refuses_live_alpaca_host(self):
        with self.assertRaises(PaperRunLoggingError) as caught:
            PaperRunLogger(FakeConfig(api_base_url="https://api.alpaca.markets"))
        self.assertEqual(caught.exception.code, "live_host_rejected")

    def test_start_run_posts_paper_only_row_and_session_started_event(self):
        calls = []

        def transport(method, url, headers, payload):
            calls.append((method, url, headers, deepcopy(payload)))
            table = urlsplit(url).path.rsplit("/", 1)[-1]
            if table == "paper_runs":
                return [_run_row(payload["id"], payload["run_key"])]
            return [{
                "id": payload["id"], "owner_id": OWNER, "run_id": payload["run_id"],
                "event_key": payload["event_key"], "occurred_at": payload["occurred_at"],
                "received_at": payload["received_at"], "kind": payload["kind"],
                "payload": payload["payload"],
            }]

        logger = self.logger(transport)
        run_id = logger.start_run(
            run_key="paper-2026-10-28",
            strategy_version="liquid_large_cap_iex_v1",
            watchlist=("AAPL", "MSFT", "SPY"),
            config_hash="abc123",
        )
        self.assertEqual(run_id, logger.run_id)
        self.assertEqual(len(calls), 2)
        method, url, headers, body = calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("paper_runs", url)
        self.assertEqual(body["mode"], "paper")
        self.assertEqual(body["owner_id"], OWNER)
        self.assertEqual(body["strategy_version"], "liquid_large_cap_iex_v1")
        self.assertEqual(body["summary"]["watchlist"], ["AAPL", "MSFT", "SPY"])
        self.assertEqual(body["summary"]["config_hash"], "abc123")
        self.assertIs(body["summary"]["execution_enabled"], False)
        self.assertIs(body["summary"]["live_execution_supported"], False)
        self.assertIn("resolution=ignore-duplicates", headers["Prefer"])
        self.assertEqual(parse_qs(urlsplit(url).query)["on_conflict"], ["owner_id,run_key"])
        self.assertNotIn(SECRET, json.dumps(body))
        event_body = calls[1][3]
        self.assertEqual(event_body["kind"], "session_started")
        self.assertEqual(event_body["payload"]["mode"], "paper")
        self.assertEqual(event_body["run_id"], run_id)

    def test_append_heartbeat_signal_blocker_and_paper_order(self):
        calls = []

        def transport(method, url, headers, payload):
            calls.append((method, urlsplit(url).path, deepcopy(payload)))
            if payload and "run_key" in (payload or {}) and "strategy_version" in payload:
                return [_run_row(payload["id"])]
            return [payload]

        logger = self.logger(transport)
        logger.start_run(run_key="paper-day", strategy_version="liquid_large_cap_iex_v1", watchlist=("AAPL",))
        calls.clear()
        self.assertTrue(logger.log_heartbeat(status="observing", event_key="hb-1"))
        self.assertTrue(logger.log_signal_evaluated(
            symbol="AAPL", accepted=False, reasons=("gain_below_threshold",), event_key="sig-1"))
        self.assertTrue(logger.log_blocker(
            "watchlist_outside_strategy_universe", detail="TSLA", event_key="blk-1"))
        self.assertTrue(logger.log_blocker("gated_preset_disabled", event_key="blk-2"))
        self.assertTrue(logger.log_blocker("paper_trading_disabled", event_key="blk-3"))
        self.assertTrue(logger.log_paper_order(
            client_order_id="mlab_test", side="buy", symbol="AAPL", qty=1, event_key="ord-1"))
        kinds = [c[2]["kind"] for c in calls]
        self.assertEqual(kinds, ["heartbeat", "signal_evaluated", "blocker", "blocker", "blocker", "paper_order"])
        self.assertTrue(all(c[2]["payload"].get("mode") == "paper" for c in calls))
        self.assertIs(calls[-1][2]["payload"]["placed_by_logger"], False)
        self.assertNotIn("place_order", json.dumps(calls))

    def test_close_run_patches_completed_at_and_status(self):
        calls = []

        def transport(method, url, headers, payload):
            calls.append((method, url, deepcopy(payload)))
            table = urlsplit(url).path.rsplit("/", 1)[-1]
            if method == "POST" and table == "paper_runs":
                return [_run_row(payload["id"])]
            if method == "POST" and table == "paper_events":
                return [payload]
            if method == "PATCH":
                row = _run_row(parse_qs(urlsplit(url).query)["id"][0].removeprefix("eq."))
                row["completed_at"] = payload["completed_at"]
                row["summary"] = payload["summary"]
                return [row]
            raise AssertionError(method + " " + url)

        logger = self.logger(transport)
        run_id = logger.start_run(run_key="paper-day", strategy_version="liquid_large_cap_iex_v1")
        closed = logger.close_run(status="halted", summary={"blockers": ["paper_trading_disabled"]})
        self.assertEqual(closed, run_id)
        self.assertIsNone(logger.run_id)
        patch_calls = [c for c in calls if c[0] == "PATCH"]
        self.assertEqual(len(patch_calls), 1)
        body = patch_calls[0][2]
        self.assertEqual(body["mode"], "paper")
        self.assertEqual(body["summary"]["status"], "halted")
        self.assertIs(body["summary"]["execution_enabled"], False)
        self.assertIsNotNone(body["completed_at"])

    def test_rejects_non_paper_payload_mode_and_disallowed_tables(self):
        calls = []
        logger = self.logger(lambda *a: calls.append(a) or [_run_row("11111111-1111-1111-1111-111111111111")])
        logger.start_run(run_key="paper-day", strategy_version="v1")
        with self.assertRaises(PaperRunLoggingError) as caught:
            logger.append_event(event_key="bad", kind="heartbeat", payload={"mode": "live"})
        self.assertEqual(caught.exception.code, "mode_rejected")
        with self.assertRaises(PaperRunLoggingError):
            logger.request("POST", "demo_runs", payload={"owner_id": OWNER})
        with self.assertRaises(PaperRunLoggingError):
            logger.request("DELETE", "paper_runs", payload={"owner_id": OWNER})
        with self.assertRaises(ValueError):
            logger.append_event(event_key="x", kind="not_a_real_kind", payload={})

    def test_append_without_start_fails_closed(self):
        logger = self.logger(lambda *a: None)
        with self.assertRaises(PaperRunLoggingError) as caught:
            logger.log_heartbeat()
        self.assertEqual(caught.exception.code, "no_active_run")

    def test_transport_errors_are_safe_and_bounded(self):
        def fail(*args):
            raise RuntimeError("RAW_FAKE_SECRET " + SECRET)

        with self.assertRaises(PaperRunLoggingError) as caught:
            self.logger(fail).start_run(run_key="paper-day", strategy_version="v1")
        self.assertNotIn("RAW_FAKE_SECRET", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))

    def test_http_caps_and_rejects_redirects(self):
        response = Response(b"x" * (MAX_RESPONSE_BYTES + 1))
        with patch("momentum.paper_run_logging.build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(PaperRunLoggingError) as caught:
                PaperRunLogger(CONFIG, clock=lambda: NOW).start_run(run_key="paper-day", strategy_version="v1")
        self.assertEqual(caught.exception.code, "response_too_large")
        self.assertIsInstance(opener.call_args.args[0], NoRedirect)

    def test_event_kinds_cover_required_activity(self):
        required = {"heartbeat", "signal_evaluated", "blocker", "paper_order", "session_started", "session_ended"}
        self.assertTrue(required <= EVENT_KINDS)

    def test_modern_secret_is_apikey_only(self):
        calls = []

        def transport(method, url, headers, payload):
            calls.append(headers)
            if "paper_runs" in url and method == "POST" and payload and "strategy_version" in payload:
                return [_run_row(payload["id"])]
            return [payload]

        self.logger(transport).start_run(run_key="paper-day", strategy_version="v1")
        self.assertEqual(calls[0]["apikey"], SECRET)
        self.assertNotIn("Authorization", calls[0])


if __name__ == "__main__":
    unittest.main()
