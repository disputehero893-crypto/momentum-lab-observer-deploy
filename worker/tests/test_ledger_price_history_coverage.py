"""Offline fail-closed coverage for the FIFO ledger and raw price history.

Every account, price, calendar and timestamp here is invented. Ledgers live in
fresh temporary SQLite files, providers are in-memory fakes, and urllib is
blocked so no broker or market-data request can leave the process. Paper only.
"""
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from momentum import ledger as ledger_module
from momentum import price_history as ph
from momentum.ledger import PAPER_ZERO_FEES, AccountingLedger, LedgerError
from momentum.models import NY


# --------------------------------------------------------------------------
# Ledger fixtures (invented account, paper environment only)
# --------------------------------------------------------------------------
L_NOW = "2026-10-16T13:35:05.000000000Z"
L_DAY = "2026-10-16"
L_START = "2026-10-16T04:00:00Z"
L_ACCOUNT = "fixture-coverage-account"


def fill(source_id, side="buy", quantity="10", price="10", at="2026-10-16T13:30:00Z", revision=1, **extra):
    return {"source_id": source_id, "revision": revision, "kind": "fill", "status": "active",
            "effective_at": at, "symbol": "XYZ", "side": side, "quantity": quantity, "price": price, **extra}


def fee(source_id, amount="0.25", revision=1, **extra):
    row = {"source_id": source_id, "revision": revision, "kind": "fee", "status": "active",
           "effective_date": L_DAY, "date_basis": "provider_activity_or_settlement_date", "amount": amount}
    row.update(extra)
    return row


def covered(**extra):
    return {"account_id": L_ACCOUNT, "environment": "paper", "source": "alpaca_account_activities",
            "window_start": L_START, "window_end": L_NOW, "as_of": L_NOW,
            "executions_complete": True, "fees_complete": True, "opening_lots_complete": True,
            "opening_lots": [], "opening_evidence": "invented reviewed flat anchor",
            "evidence": ["invented complete execution and cost evidence"], "blockers": [], **extra}


class LedgerCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "nested" / "journal.sqlite3")
        self.ledger = AccountingLedger(self.path, account_id=L_ACCOUNT)
        self.batch = 0

    def ingest(self, records, coverage=None):
        self.batch += 1
        return self.ledger.ingest_batch(records, batch_id=f"batch-{self.batch}", coverage=coverage or covered())

    def snapshot(self, **extra):
        return self.ledger.snapshot(session_date=L_DAY, now=L_NOW, **extra).to_dict()

    def assert_rejected_and_nothing_stored(self, records, coverage=None, *, batch_id="rejected-batch"):
        with self.assertRaises(LedgerError):
            self.ledger.ingest_batch(records, batch_id=batch_id, coverage=coverage or covered())
        self.assertEqual(self.ledger.revisions(), [])
        self.assertIn("coverage_missing", self.snapshot()["blockers"])

    # -- construction -----------------------------------------------------
    def test_in_memory_database_is_refused_because_evidence_must_be_durable(self):
        with self.assertRaises(LedgerError):
            AccountingLedger(":memory:", account_id=L_ACCOUNT)
        with self.assertRaises(LedgerError):
            AccountingLedger(str(Path(self.temp.name) / "x.sqlite3"), account_id=L_ACCOUNT, environment="live")
        self.assertTrue(Path(self.path).exists(), "parent directories are created for a durable path")

    # -- record normalization fails closed ----------------------------------
    def test_malformed_source_records_are_rejected_before_any_write(self):
        cases = {
            "unknown field": fill("b1", raw_provider_blob="x"),
            "fill with fee date fields": fill("b1", effective_date=L_DAY),
            "cancellation carrying economics": {"source_id": "b1", "revision": 2, "kind": "fill",
                                                "status": "canceled", "effective_at": "2026-10-16T13:30:00Z",
                                                "symbol": "XYZ"},
            "fee carrying fill fields": fee("f1", symbol="XYZ"),
            "fee with two time bases": fee("f1", effective_at="2026-10-16T13:30:00Z"),
            "unsupported fee date basis": fee("f1", date_basis="trade_date_guess"),
            "malformed fee date": fee("f1", effective_date="2026/10/16"),
            "impossible fee date": fee("f1", effective_date="2026-02-30"),
            "canceled fee with amount": {"source_id": "f1", "revision": 2, "kind": "fee", "status": "canceled",
                                         "effective_date": L_DAY,
                                         "date_basis": "provider_activity_or_settlement_date", "amount": "1"},
            "invalid source identity": fill(" leading-space"),
        }
        for name, record in cases.items():
            with self.subTest(name):
                self.assert_rejected_and_nothing_stored([record])

    def test_batch_shape_identity_and_coverage_evidence_fail_closed(self):
        self.assert_rejected_and_nothing_stored({"not": "a list"})
        self.assert_rejected_and_nothing_stored([fill("b1")], batch_id="")
        lot = {"lot_id": "lot-1", "symbol": "XYZ", "quantity": "1", "price": "5",
               "opened_at": "2026-10-15T15:00:00Z"}
        bad_coverages = {
            "opening lots not a list": covered(opening_lots={"lot-1": lot}),
            "lowercase lot symbol": covered(opening_lots=[{**lot, "symbol": "xyz"}]),
            "duplicate lot identity": covered(opening_lots=[lot, {**lot, "quantity": "2"}]),
            "lot opened inside window": covered(opening_lots=[{**lot, "opened_at": L_START}]),
            "blank opening evidence": covered(opening_evidence="   "),
            "control character in evidence": covered(evidence=["invented\x00evidence"]),
            "empty evidence list": covered(evidence=[]),
        }
        for name, coverage in bad_coverages.items():
            with self.subTest(name):
                self.assert_rejected_and_nothing_stored([fill("b1")], coverage)

    def test_failure_inside_transaction_rolls_back_every_revision_in_the_batch(self):
        # An invented storage fault while recording the batch row must not leave
        # orphaned revisions that a later snapshot could mistake for evidence.
        with closing(sqlite3.connect(self.path)) as raw, raw:
            raw.execute("CREATE TRIGGER fixture_fault BEFORE INSERT ON accounting_batches "
                        "BEGIN SELECT RAISE(ABORT, 'invented storage fault'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            self.ingest([fill("b1"), fill("s1", side="sell", price="11", at="2026-10-16T13:31:00Z")])
        self.assertEqual(self.ledger.revisions(), [])
        self.assertIn("coverage_missing", self.snapshot()["blockers"])
        with closing(sqlite3.connect(self.path)) as raw, raw:
            raw.execute("DROP TRIGGER fixture_fault")
        result = self.ingest([fill("b1"), fill("s1", side="sell", price="11", at="2026-10-16T13:31:00Z")])
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(self.snapshot()["realized_pnl"], "10")

    # -- fee effective_at basis ----------------------------------------------
    def test_fee_with_exact_timestamp_counts_for_its_new_york_session(self):
        self.ingest([fill("b1"), fill("s1", side="sell", price="11", at="2026-10-16T13:31:00Z"),
                     {"source_id": "f1", "revision": 1, "kind": "fee", "status": "active",
                      "effective_at": "2026-10-16T13:31:00.5Z", "amount": "0.40", "execution_id": "s1"}])
        stored = [row for row in self.ledger.revisions() if row["source_id"] == "f1"][0]
        self.assertEqual(stored["effective_at"], "2026-10-16T13:31:00.500000000Z")
        self.assertNotIn("effective_date", stored)
        snapshot = self.snapshot()
        self.assertTrue(snapshot["complete"], snapshot["blockers"])
        self.assertEqual(snapshot["fees"], "0.4")
        self.assertEqual(snapshot["net_realized_pnl"], "9.6")
        self.assertEqual(snapshot["observed_execution_fees"], {"s1": "0.4"})

    def test_fee_after_coverage_window_blocks_actual_costs(self):
        self.ingest([fill("b1"),
                     {"source_id": "f-late", "revision": 1, "kind": "fee", "status": "active",
                      "effective_at": "2026-10-16T13:40:00Z", "amount": "0.10"}])
        snapshot = self.snapshot()
        self.assertIn("fee_after_coverage_window", snapshot["blockers"])
        self.assertFalse(snapshot["actual_costs_complete"])
        self.assertIsNone(snapshot["fees"])
        self.assertIsNone(snapshot["net_realized_pnl"])
        self.assertFalse(snapshot["complete"])

    # -- execution evidence blockers ----------------------------------------
    def test_execution_after_coverage_window_is_never_silently_dropped(self):
        self.ingest([fill("b1"), fill("s-late", side="sell", price="12", at="2026-10-16T13:40:00Z")])
        snapshot = self.snapshot()
        self.assertIn("execution_after_coverage_window", snapshot["blockers"])
        self.assertFalse(snapshot["execution_accounting_complete"])
        self.assertIsNone(snapshot["realized_pnl"])
        self.assertIsNone(snapshot["net_realized_pnl"])
        self.assertFalse(snapshot["execution_armed"])

    def test_opening_lot_execution_repeated_inside_window_is_not_double_counted(self):
        lot = {"lot_id": "lot-1", "symbol": "XYZ", "quantity": "10", "price": "8",
               "opened_at": "2026-10-15T15:00:00Z", "execution_id": "b-open"}
        self.ingest([fill("b-open", price="8", at="2026-10-16T13:30:00Z")],
                    coverage=covered(opening_lots=[lot]))
        snapshot = self.snapshot()
        self.assertIn("opening_execution_repeated_in_window", snapshot["blockers"])
        self.assertIsNone(snapshot["realized_pnl"])
        self.assertEqual([row["quantity"] for row in snapshot["open_lots"]], ["10"],
                         "the repeated execution must not add a second lot")

    def test_execution_identity_changing_kind_across_revisions_blocks_accounting(self):
        self.ingest([fill("x1")])
        self.ingest([fee("x1", revision=2)])
        snapshot = self.snapshot()
        self.assertIn("execution_identity_kind_changed", snapshot["blockers"])
        self.assertFalse(snapshot["execution_accounting_complete"])
        self.assertFalse(snapshot["actual_costs_complete"])
        self.assertIsNone(snapshot["realized_pnl"])
        self.assertIsNone(snapshot["fees"])

    def test_projection_limit_blocks_snapshot_and_audit_export(self):
        self.ingest([fill("b1"), fill("s1", side="sell", price="11", at="2026-10-16T13:31:00Z")])
        with patch.object(ledger_module, "_MAX_PROJECTION", 1):
            with self.assertRaises(LedgerError):
                self.ledger.revisions()
            snapshot = self.snapshot()
        self.assertIn("accounting_projection_limit_exceeded", snapshot["blockers"])
        self.assertFalse(snapshot["execution_accounting_complete"])
        self.assertFalse(snapshot["actual_costs_complete"])
        self.assertIsNone(snapshot["realized_pnl"])
        self.assertEqual(len(self.ledger.revisions()), 2, "the unpatched limit still exports the audit")

    # -- snapshot argument gates ---------------------------------------------
    def test_snapshot_arguments_are_validated_not_coerced(self):
        self.ingest([fill("b1")])
        with self.assertRaises(LedgerError):
            self.ledger.snapshot(session_date=L_DAY, now=L_NOW, cost_policy="assume_zero")
        with self.assertRaises(LedgerError):
            self.ledger.snapshot(session_date=L_DAY, now=L_NOW, max_age_seconds=6)
        with self.assertRaises(LedgerError):
            self.ledger.snapshot(session_date=L_DAY, now=datetime(2026, 10, 16, 13, 35, 5))
        with self.assertRaises(LedgerError):
            self.ledger.snapshot(session_date="2026-13-01", now=L_NOW)
        aware = self.ledger.snapshot(session_date=L_DAY, now=datetime(2026, 10, 16, 13, 35, 5, tzinfo=timezone.utc))
        self.assertTrue(aware.complete)

    def test_projection_id_is_deterministic_and_bound_to_cost_policy(self):
        self.ingest([fill("b1"), fill("s1", side="sell", price="11", at="2026-10-16T13:31:00Z")])
        first = self.ledger.snapshot(session_date=L_DAY, now=L_NOW)
        again = self.ledger.snapshot(session_date=L_DAY, now=L_NOW)
        simulated = self.ledger.snapshot(session_date=L_DAY, now=L_NOW, cost_policy=PAPER_ZERO_FEES)
        self.assertRegex(first.projection_id, r"\A[0-9a-f]{64}\Z")
        self.assertEqual(first.projection_id, again.projection_id)
        self.assertNotEqual(first.projection_id, simulated.projection_id)
        self.assertEqual(simulated.payload["cost_basis"], "explicit_paper_zero_fee_simulation")
        self.ingest([fill("b2", at="2026-10-16T13:32:00Z")])
        self.assertNotEqual(self.ledger.snapshot(session_date=L_DAY, now=L_NOW).projection_id, first.projection_id)


# --------------------------------------------------------------------------
# Price history fixtures (invented calendar and bars; no network)
# --------------------------------------------------------------------------
SYMBOLS = ("DEMO-A", "DEMO-B")
NOW = datetime(2026, 10, 16, 13, 45, 30, tzinfo=timezone.utc)  # 09:45:30 New York
PRICE = "42.125"


def utc_stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def ny_midnight(day):
    return datetime.combine(date.fromisoformat(day), datetime.min.time(), NY)


def provider_bar(start, close=PRICE):
    return {"t": utc_stamp(start), "o": PRICE, "h": "43", "l": "41", "c": close, "v": 100}


class Clock:
    def __init__(self, value=NOW):
        self.value = value
        self.queued = []

    def __call__(self):
        if self.queued:
            self.value = self.queued.pop(0)
        return self.value


class FakeHistoryProvider:
    def __init__(self, clock):
        self.clock = clock
        self.records = [{"date": "2026-10-15", "open": "09:30", "close": "16:00"},
                        {"date": "2026-10-16", "open": "09:30", "close": "16:00"}]
        self.daily = {"bars": {s: [provider_bar(ny_midnight("2026-10-15"))] for s in SYMBOLS},
                      "next_page_token": None}
        start = datetime(2026, 10, 16, 9, 35, tzinfo=NY)
        self.minutes = {"bars": {s: [provider_bar(start + timedelta(minutes=i)) for i in range(10)]
                                 for s in SYMBOLS}, "next_page_token": None}
        self.after_daily = None

    def calendar(self, start, end):
        return deepcopy(self.records)

    def observation_bars(self, symbols, *, timeframe, start, end):
        if timeframe == "1Day":
            if self.after_daily:
                self.after_daily()
            return deepcopy(self.daily)
        if timeframe == "1Min":
            return deepcopy(self.minutes)
        raise AssertionError("Unexpected timeframe")


def collector():
    clock = Clock()
    provider = FakeHistoryProvider(clock)
    cfg = SimpleNamespace(watchlist=SYMBOLS, data_feed="iex")
    return ph.PriceHistoryCollector(provider, cfg, clock=clock), provider


def sanitize(value, now=NOW):
    return ph.sanitize_price_history(value, allowed_symbols=SYMBOLS, feed="iex", now=now)


class PriceHistoryCoverageTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError(
            "Network access is forbidden in offline coverage tests"))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.base = collector()[0].collect()
        # Fixture sanity: the untouched envelope is fully observed.
        for row in self.base["rows"]:
            assert row["previous_close"]["status"] == "observed", row
            assert row["minute_bars"]["status"] == "observed", row

    def mutated(self, change):
        value = deepcopy(self.base)
        change(value)
        return sanitize(value)

    # -- primitive validators -------------------------------------------------
    def test_primitive_parsers_reject_ambiguous_or_oversized_inputs(self):
        for bad in (1760622330, None, "2026-10-16T13:45:30" + "0" * 60 + "Z"):
            with self.subTest(instant=bad), self.assertRaises(ValueError):
                ph._instant(bad)
        for bad in (None, 20261016, "2026-10-1", "2026-W42-5"):
            with self.subTest(day=bad), self.assertRaises(ValueError):
                ph._day(bad)
        for bad in ("", "1" * 65, "abc", "1e", True, 1.5):
            with self.subTest(number=bad), self.assertRaises(ValueError):
                ph._number(bad)
        for bad in ([], "bar", None):
            with self.subTest(shape=bad), self.assertRaises(ValueError):
                ph._ohlcv(bad)
            with self.subTest(session=bad), self.assertRaises(ValueError):
                ph._session(bad)
        self.assertEqual(ph._number("42.1250"), "42.125")

    # -- calendar projection --------------------------------------------------
    def assert_calendar_unknown(self, result, code):
        calendar = result["calendar"]
        self.assertIsNone(calendar["is_trading_day"])
        self.assertIsNone(calendar["previous_session"])
        self.assertIsNone(calendar["target_session"])
        self.assertEqual(calendar["error_code"], code)
        for row in result["rows"]:
            self.assertEqual(row["previous_close"]["status"], "error")
            self.assertIsNone(row["previous_close"]["close"])
            self.assertEqual(row["previous_close"]["error_code"], code)
            self.assertEqual(row["minute_bars"]["bars"], [])
            self.assertEqual(row["minute_bars"]["error_code"], code)

    def test_unknown_trading_day_keeps_only_known_calendar_error_codes(self):
        def unknown(code):
            def change(value):
                value["calendar"].update(is_trading_day=None, error_code=code)
            return change
        self.assert_calendar_unknown(self.mutated(unknown("calendar_stale")), "calendar_stale")
        self.assert_calendar_unknown(self.mutated(unknown("invented_free_text")), "calendar_invalid")

    def test_calendar_error_with_boolean_day_is_not_trusted(self):
        result = self.mutated(lambda v: v["calendar"].update(error_code="calendar_unavailable"))
        self.assert_calendar_unknown(result, "calendar_unavailable")
        result = self.mutated(lambda v: v["calendar"].update(error_code=["calendar_unavailable"]))
        self.assert_calendar_unknown(result, "calendar_invalid")

    def test_inconsistent_calendar_sessions_become_calendar_invalid(self):
        cases = {
            "previous session not before anchor": lambda v: v["calendar"].update(
                previous_session=deepcopy(v["calendar"]["target_session"])),
            "trading day targeting another date": lambda v: v["calendar"].update(
                target_session=deepcopy(v["calendar"]["previous_session"])),
            "closed day with a different target": lambda v: v["calendar"].update(is_trading_day=False),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.assert_calendar_unknown(self.mutated(change), "calendar_invalid")

    def test_calendar_with_wrong_identity_or_nonboolean_day_is_invalid(self):
        cases = {
            "not an object": lambda v: v.update(calendar="open"),
            "foreign source": lambda v: v["calendar"].update(source="invented_calendar"),
            "widened max age": lambda v: v["calendar"].update(max_age_seconds=3600),
            "string trading-day flag": lambda v: v["calendar"].update(is_trading_day="true"),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.assert_calendar_unknown(self.mutated(change), "calendar_invalid")

    # -- previous close ---------------------------------------------------------
    def close_of(self, result, symbol="DEMO-A"):
        return {row["symbol"]: row for row in result["rows"]}[symbol]["previous_close"]

    def test_close_request_before_calendar_check_is_a_timestamp_error(self):
        earlier = utc_stamp(NOW - timedelta(seconds=1))
        result = self.mutated(lambda v: v["rows"][0]["previous_close"].update(requested_at=earlier))
        close = self.close_of(result)
        self.assertEqual(close["error_code"], "history_timestamp_invalid")
        self.assertIsNone(close["close"])
        self.assertEqual(self.close_of(result, "DEMO-B")["status"], "observed")

    def test_close_receipt_without_request_or_before_request_is_a_timestamp_error(self):
        earlier = utc_stamp(NOW - timedelta(seconds=1))
        for name, change in {
            "receipt without request": {"requested_at": None},
            "receipt before request": {"received_at": earlier},
        }.items():
            with self.subTest(name):
                close = self.close_of(self.mutated(lambda v: v["rows"][0]["previous_close"].update(change)))
                self.assertEqual(close["error_code"], "history_timestamp_invalid")
                self.assertIsNone(close["close"])

    def test_observed_close_carrying_an_error_code_or_wrong_session_is_invalid(self):
        for name, change in {
            "observed with error code": {"error_code": "history_stale"},
            "other session date": {"session_date": "2026-10-14"},
            "unknown status": {"status": "final"},
        }.items():
            with self.subTest(name):
                result = self.mutated(lambda v: v["rows"][0]["previous_close"].update(change))
                close = self.close_of(result)
                self.assertEqual((close["status"], close["error_code"], close["close"]),
                                 ("error", "daily_bar_invalid", None))
                self.assertEqual(self.close_of(result, "DEMO-B")["status"], "observed")

    def test_close_requested_before_the_session_closed_is_not_a_closing_price(self):
        # A (stale) calendar checked before yesterday's close lets the request
        # timestamp pass _attempt; the close ordering check must still refuse it.
        checked = "2026-10-15T19:00:00Z"
        early = "2026-10-15T19:30:00Z"

        def change(v):
            v["calendar"]["checked_at"] = checked
            v["rows"][0]["previous_close"].update(requested_at=early, received_at=early)
        result = self.mutated(change)
        self.assertEqual(result["calendar"]["error_code"], "calendar_stale")
        close = self.close_of(result)
        self.assertEqual((close["status"], close["error_code"], close["close"]), ("error", "daily_bar_invalid", None))

    def test_observed_close_without_any_request_receipt_is_a_timestamp_error(self):
        close = self.close_of(self.mutated(lambda v: v["rows"][0]["previous_close"].update(
            requested_at=None, received_at=None)))
        self.assertEqual(close["error_code"], "history_timestamp_invalid")
        self.assertIsNone(close["close"])

    def test_absent_or_non_object_rows_are_invalid_not_fabricated(self):
        dropped = self.mutated(lambda v: v["rows"].pop(0))
        self.assertEqual([row["symbol"] for row in dropped["rows"]], list(SYMBOLS))
        close, candles = self.close_of(dropped), self.minutes_of(dropped)
        self.assertEqual((close["status"], close["error_code"], close["close"]), ("error", "daily_bar_invalid", None))
        self.assertEqual((candles["status"], candles["error_code"], candles["bars"]), ("error", "minute_bar_invalid", []))
        replaced = self.mutated(lambda v: v["rows"][0].update(previous_close="42.125", minute_bars=[]))
        self.assertEqual(self.close_of(replaced)["error_code"], "daily_bar_invalid")
        self.assertEqual(self.minutes_of(replaced)["error_code"], "minute_bar_invalid")
        self.assertEqual(self.close_of(replaced, "DEMO-B")["status"], "observed")

    def test_missing_close_keeps_known_codes_and_replaces_unknown_ones(self):
        def missing(code):
            return lambda v: v["rows"][0].update(previous_close={
                "status": "missing", "session_date": "2026-10-15", "close": None, "source_at": None,
                "requested_at": None, "received_at": None, "error_code": code})
        known = self.close_of(self.mutated(missing("symbol_missing")))
        self.assertEqual((known["status"], known["error_code"], known["close"]), ("missing", "symbol_missing", None))
        unknown = self.close_of(self.mutated(missing("provider said so")))
        self.assertEqual((unknown["status"], unknown["error_code"]), ("missing", "daily_bar_invalid"))

    def test_close_not_stamped_at_new_york_midnight_is_a_session_mismatch(self):
        utc_midnight = "2026-10-15T00:00:00Z"
        close = self.close_of(self.mutated(lambda v: v["rows"][0]["previous_close"].update(source_at=utc_midnight)))
        self.assertEqual(close["error_code"], "daily_bar_session_mismatch")
        self.assertIsNone(close["close"])

    def test_nonpositive_close_is_invalid_not_rounded(self):
        for bad in ("0", "-1", "NaN"):
            with self.subTest(close=bad):
                close = self.close_of(self.mutated(lambda v: v["rows"][0]["previous_close"].update(close=bad)))
                self.assertEqual((close["status"], close["error_code"], close["close"]),
                                 ("error", "daily_bar_invalid", None))

    # -- minute bars ------------------------------------------------------------
    def minutes_of(self, result, symbol="DEMO-A"):
        return {row["symbol"]: row for row in result["rows"]}[symbol]["minute_bars"]

    def test_malformed_minute_projections_fail_closed_without_bars(self):
        def bars_of(v):
            return v["rows"][0]["minute_bars"]

        def not_started_during_session(v):
            bars_of(v).update(status="not_started", expected_intervals=0, missing_intervals=[])

        def swap_first_two(v):
            bars = bars_of(v)["bars"]
            bars[0], bars[1] = bars[1], bars[0]

        cases = {
            "not_started while a window has elapsed": not_started_during_session,
            "unknown status": lambda v: bars_of(v).update(status="partial"),
            "shifted window start": lambda v: bars_of(v).update(window_start_at="2026-10-16T13:36:00Z"),
            "bars not a list": lambda v: bars_of(v).update(bars={"0": bars_of(v)["bars"][0]}),
            "non-object bar": lambda v: bars_of(v)["bars"].__setitem__(0, "bar"),
            "out-of-order bars": swap_first_two,
            "empty status with bars": lambda v: bars_of(v).update(status="empty"),
            "observed status without bars": lambda v: bars_of(v).update(
                bars=[], missing_intervals=list(bars_of(v)["missing_intervals"])),
            "observed status with an error code": lambda v: bars_of(v).update(error_code="history_stale"),
        }
        for name, change in cases.items():
            with self.subTest(name):
                result = self.mutated(change)
                candles = self.minutes_of(result)
                self.assertEqual(candles["error_code"], "minute_bar_invalid")
                self.assertEqual(candles["status"], "error")
                self.assertEqual(candles["bars"], [])
                self.assertEqual(self.minutes_of(result, "DEMO-B")["status"], "observed")

    # -- collector ---------------------------------------------------------------
    def test_collector_refuses_unsupported_feed(self):
        with self.assertRaises(ValueError):
            ph.PriceHistoryCollector(object(), SimpleNamespace(watchlist=SYMBOLS, data_feed="otc"))

    def test_invalid_provider_calendars_never_become_known_sessions(self):
        cases = {
            "not a list": {"date": "2026-10-16"},
            "non-object record": ["2026-10-16"],
            "no prior session": [{"date": "2026-10-16", "open": "09:30", "close": "16:00"}],
        }
        for name, records in cases.items():
            with self.subTest(name):
                c, provider = collector()
                provider.records = records
                result = c.collect()
                self.assertEqual(result["calendar"]["error_code"], "calendar_invalid")
                self.assertIsNone(result["calendar"]["is_trading_day"])
                for row in result["rows"]:
                    self.assertIsNone(row["previous_close"]["close"])
                    self.assertEqual(row["minute_bars"]["bars"], [])

    def test_out_of_range_duplicate_or_unordered_calendar_days_are_invalid(self):
        good = {"open": "09:30", "close": "16:00"}
        cases = {
            "future day": [{"date": "2026-10-15", **good}, {"date": "2026-10-19", **good}],
            "older than lookback": [{"date": "2026-09-01", **good}, {"date": "2026-10-15", **good}],
            "duplicate day": [{"date": "2026-10-15", **good}, {"date": "2026-10-15", **good}],
            "descending days": [{"date": "2026-10-16", **good}, {"date": "2026-10-15", **good}],
            "unpadded open clock": [{"date": "2026-10-15", "open": "9:30", "close": "16:00"},
                                    {"date": "2026-10-16", **good}],
            "missing close clock": [{"date": "2026-10-15", "open": "09:30"}, {"date": "2026-10-16", **good}],
        }
        for name, records in cases.items():
            with self.subTest(name):
                c, provider = collector()
                provider.records = records
                result = c.collect()
                self.assertEqual(result["calendar"]["error_code"], "calendar_invalid")
                self.assertIsNone(result["calendar"]["previous_session"])

    def test_calendar_received_after_new_york_midnight_is_stale_and_not_cached(self):
        before = datetime(2026, 10, 16, 3, 59, 59, tzinfo=timezone.utc)  # 23:59:59 on Oct 15 NY
        after = datetime(2026, 10, 16, 4, 0, 1, tzinfo=timezone.utc)    # 00:00:01 on Oct 16 NY
        c, provider = collector()
        provider.records = [{"date": "2026-10-14", "open": "09:30", "close": "16:00"},
                            {"date": "2026-10-15", "open": "09:30", "close": "16:00"}]
        provider.clock.queued.extend([before, after])
        result = c.collect()
        self.assertEqual(result["calendar"]["error_code"], "calendar_stale")
        self.assertIsNone(c._calendar_cache, "a calendar that rolled over mid-fetch must not be cached")
        for row in result["rows"]:
            self.assertIsNone(row["previous_close"]["close"])
            self.assertEqual(row["previous_close"]["error_code"], "calendar_stale")
            self.assertEqual(row["minute_bars"]["bars"], [])

    def test_provider_daily_record_shapes_are_isolated_per_symbol(self):
        c, provider = collector()
        provider.daily["bars"]["DEMO-A"] = provider.daily["bars"]["DEMO-A"] * 2
        result = c.collect()
        self.assertEqual(self.close_of(result)["error_code"], "daily_bar_invalid")
        self.assertEqual(self.close_of(result, "DEMO-B")["status"], "observed")

        c, provider = collector()
        provider.daily["bars"]["DEMO-A"] = []
        close = self.close_of(c.collect())
        self.assertEqual((close["status"], close["error_code"], close["close"]),
                         ("missing", "daily_bar_missing", None))

    def test_provider_minute_records_that_are_not_a_list_are_invalid(self):
        c, provider = collector()
        provider.minutes["bars"]["DEMO-A"] = {"t": "2026-10-16T13:35:00Z"}
        result = c.collect()
        self.assertEqual(self.minutes_of(result)["error_code"], "minute_bar_invalid")
        self.assertEqual(self.minutes_of(result)["bars"], [])
        self.assertEqual(self.minutes_of(result, "DEMO-B")["status"], "observed")

    def test_minute_window_crossing_a_boundary_before_the_request_is_rejected(self):
        c, provider = collector()
        later = NOW + timedelta(seconds=60)
        # The frozen window is captured at NOW; the request's own start stamp
        # lands one minute later. Recomputing after I/O is forbidden.
        provider.after_daily = lambda: provider.clock.queued.extend([NOW, NOW, later])
        result = c.collect()
        for symbol in SYMBOLS:
            candles = self.minutes_of(result, symbol)
            self.assertEqual(candles["error_code"], "history_timestamp_invalid")
            self.assertEqual(candles["bars"], [])
            self.assertEqual(self.close_of(result, symbol)["status"], "observed")


if __name__ == "__main__":
    unittest.main()
