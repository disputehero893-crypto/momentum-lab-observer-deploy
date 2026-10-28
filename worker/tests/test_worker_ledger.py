"""Invented accounting fixtures. No credentials, sockets, or broker calls."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, Inexact, ROUND_DOWN, ROUND_UP, localcontext
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from momentum.journal import Journal
from momentum.ledger import AccountingLedger, LedgerConflict, LedgerError


NOW = "2026-10-16T13:35:05.000000000Z"
DAY = "2026-10-16"
START = "2026-10-16T04:00:00Z"
ACCOUNT = "fixture-account-a"


def fill(source_id, side="buy", quantity="10", price="10", at="2026-10-16T13:30:00Z", **extra):
    return {"source_id": source_id, "revision": 1, "kind": "fill", "status": "active",
            "effective_at": at, "symbol": "XYZ", "side": side, "quantity": quantity, "price": price, **extra}


def fee(source_id, amount="0.25", execution_id=None, **extra):
    row = {"source_id": source_id, "revision": 1, "kind": "fee", "status": "active",
           "effective_date": DAY, "date_basis": "provider_activity_or_settlement_date", "amount": amount, **extra}
    if execution_id is not None:
        row["execution_id"] = execution_id
    return row


def covered(**extra):
    return {"account_id": ACCOUNT, "environment": "paper", "source": "alpaca_account_activities",
            "window_start": START, "window_end": NOW, "as_of": NOW,
            "executions_complete": True, "fees_complete": True, "opening_lots_complete": True,
            "opening_lots": [], "opening_evidence": "invented reviewed flat anchor at exact window start",
            "evidence": ["invented complete execution and cost evidence"], "blockers": [], **extra}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "journal.sqlite3")
        self.ledger = AccountingLedger(self.path, account_id=ACCOUNT)
        self.batch = 0

    def ingest(self, records, coverage=None, **extra):
        self.batch += 1
        return self.ledger.ingest_batch(records, batch_id=f"batch-{self.batch}", coverage=coverage or covered(), **extra)

    def snapshot(self, **extra):
        return self.ledger.snapshot(session_date=DAY, now=NOW, **extra).to_dict()

    def roundtrip(self):
        return [fill("buy"), fill("sell", side="sell", price="11", at="2026-10-16T13:31:00Z")]

    def test_empty_database_has_no_invented_balances_or_fee_total(self):
        snapshot = self.snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertIsNone(snapshot["realized_pnl"])
        self.assertIsNone(snapshot["fees"])
        self.assertIsNone(snapshot["as_of"])
        self.assertIn("coverage_missing", snapshot["blockers"])
        self.assertFalse(snapshot["execution_armed"])

    def test_fifo_partial_exits_preserve_exact_lot_basis(self):
        self.ingest([fill("b1", quantity="10", price="8"),
                     fill("b2", quantity="10", price="10", at="2026-10-16T13:30:01Z"),
                     fill("s1", side="sell", quantity="15", price="11", at="2026-10-16T13:31:00Z")])
        snapshot = self.snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["realized_pnl"], "35")
        self.assertEqual(snapshot["fees"], "0")
        self.assertEqual([row["quantity"] for row in snapshot["matches"]], ["10", "5"])
        self.assertEqual(snapshot["open_lots"][0]["quantity"], "5")
        self.assertEqual(snapshot["open_lots"][0]["cost_basis"], "50")
        self.assertEqual(snapshot["open_lots"][0]["opened_at"], "2026-10-16T13:30:01.000000000Z")

    def test_duplicate_and_reordered_records_have_same_projection(self):
        records = self.roundtrip()
        first = self.ingest([records[1], records[0], records[0]])
        before = self.snapshot()
        second = self.ingest(list(reversed(records)))
        after = self.snapshot()
        self.assertEqual(first["inserted"], 2)
        self.assertEqual(first["duplicates"], 1)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(before["projection_id"], after["projection_id"])
        self.assertEqual(len(self.ledger.revisions()), 2)

    def test_same_batch_retry_is_idempotent_and_different_body_is_durably_blocked(self):
        records = self.roundtrip()
        self.ledger.ingest_batch(records, batch_id="receipt", coverage=covered())
        duplicate = self.ledger.ingest_batch(records, batch_id="receipt", coverage=covered())
        self.assertTrue(duplicate["duplicate_batch"])
        with self.assertRaises(LedgerConflict):
            self.ledger.ingest_batch(records, batch_id="receipt", coverage=covered(evidence=["changed immutable receipt"]))
        reopened = AccountingLedger(self.path, account_id=ACCOUNT)
        self.assertIn("immutable_batch_identity_conflict", reopened.snapshot(session_date=DAY, now=NOW).blockers)

    def test_source_mutation_blocks_persistently_until_explicit_higher_revision(self):
        records = self.roundtrip()
        self.ingest(records)
        with self.assertRaises(LedgerConflict):
            self.ingest([{**records[0], "price": "9"}])
        self.assertIn("unresolved_source_revision_conflict", self.snapshot()["blockers"])
        self.ingest([{**records[0], "price": "9", "revision": 2}])
        self.assertTrue(self.snapshot()["complete"])
        self.assertEqual(self.snapshot()["realized_pnl"], "20")
        self.assertEqual(len(self.ledger.revisions()), 3)

    def test_conflict_inside_new_batch_is_durable_and_does_not_accept_other_rows(self):
        row = fill("same")
        with self.assertRaises(LedgerConflict):
            self.ingest([row, {**row, "price": "12"}, fill("unrelated")])
        self.assertEqual(self.ledger.revisions(), [])
        self.assertIn("unresolved_source_revision_conflict", self.snapshot()["blockers"])
        self.ingest([{**row, "revision": 2, "price": "11"}])
        self.assertTrue(self.snapshot()["complete"])

    def test_higher_revision_wins_when_older_revision_arrives_later(self):
        buy, sell = self.roundtrip()
        self.ingest([{**buy, "revision": 3, "price": "9"}, sell])
        projected = self.snapshot()["projection_id"]
        self.ingest([buy, {**buy, "revision": 2, "price": "8"}])
        self.assertEqual(self.snapshot()["realized_pnl"], "20")
        self.assertEqual(self.snapshot()["projection_id"], projected)
        self.assertEqual(len(self.ledger.revisions()), 4)

    def test_cancelled_sale_restores_lot_without_overwriting_source(self):
        self.ingest(self.roundtrip())
        self.ingest([{"source_id": "sell", "revision": 2, "kind": "fill", "status": "canceled",
                      "effective_at": "2026-10-16T13:31:00Z"}])
        snapshot = self.snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["realized_pnl"], "0")
        self.assertEqual(snapshot["open_lots"][0]["quantity"], "10")
        self.assertEqual(len(self.ledger.revisions()), 3)

    def test_cancelled_entry_cannot_leave_a_fabricated_profitable_exit(self):
        self.ingest(self.roundtrip())
        self.ingest([{"source_id": "buy", "revision": 2, "kind": "fill", "status": "canceled",
                      "effective_at": "2026-10-16T13:30:00Z"}])
        snapshot = self.snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertIsNone(snapshot["realized_pnl"])
        self.assertIn("sale_exceeds_known_long_inventory", snapshot["blockers"])

    def test_later_buy_does_not_repair_a_short_sale(self):
        self.ingest([fill("sell-first", side="sell"), fill("buy-later", at="2026-10-16T13:31:00Z")])
        snapshot = self.snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["matches"], [])
        self.assertIn("sale_exceeds_known_long_inventory", snapshot["blockers"])

    def test_nanoseconds_control_fifo_order_not_lexical_record_identity(self):
        self.ingest([fill("a-sale", side="sell", price="11", at="2026-10-16T13:30:00.000000002Z"),
                     fill("z-buy", at="2026-10-16T13:30:00.000000001Z")])
        snapshot = self.snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["matches"][0]["opened_at"], "2026-10-16T13:30:00.000000001Z")
        self.assertEqual(snapshot["realized_pnl"], "10")

    def test_exact_simultaneous_opposite_sides_are_explicitly_ambiguous(self):
        self.ingest([fill("a-buy"), fill("z-sell", side="sell", price="11")])
        self.assertIn("ambiguous_simultaneous_fill_order", self.snapshot()["blockers"])

    def test_equal_timestamp_buys_cannot_use_opaque_ids_as_cost_basis_order(self):
        self.ingest([fill("a-buy", quantity="1", price="10"),
                     fill("b-buy", quantity="1", price="20"),
                     fill("sell", side="sell", quantity="1", price="30", at="2026-10-16T13:30:00.000000001Z")])
        snapshot = self.snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertFalse(snapshot["execution_accounting_complete"])
        self.assertIsNone(snapshot["realized_pnl"])
        self.assertIn("ambiguous_simultaneous_buy_order", snapshot["blockers"])
        self.assertEqual(snapshot["matches"][0]["closed_at"], "2026-10-16T13:30:00.000000001Z")
        self.assertFalse(snapshot["matches"][0]["fee_allocation_complete"])
        self.assertIsNone(snapshot["matches"][0]["net_pnl"])

    def test_equal_price_tied_buys_with_different_fees_still_have_unknown_match_net(self):
        self.ingest([fill("a-buy", quantity="1", price="10"),
                     fill("b-buy", quantity="1", price="10"),
                     fill("sell", side="sell", quantity="1", price="11", at="2026-10-16T13:31:00Z"),
                     fee("a-cost", "0.1", "a-buy"), fee("b-cost", "0.9", "b-buy")])
        snapshot = self.snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertIn("ambiguous_simultaneous_buy_order", snapshot["blockers"])
        self.assertEqual(snapshot["observed_actual_fees"], "1")
        self.assertFalse(snapshot["matches"][0]["fee_allocation_complete"])
        self.assertIsNone(snapshot["matches"][0]["fees"])
        self.assertIsNone(snapshot["matches"][0]["net_pnl"])

    def test_same_time_opening_lots_do_not_prove_fifo_basis_for_partial_exit(self):
        baseline = [{"lot_id": "a-lot", "symbol": "XYZ", "quantity": "1", "price": "10",
                     "opened_at": "2026-10-15T13:30:00.123456789Z"},
                    {"lot_id": "b-lot", "symbol": "XYZ", "quantity": "1", "price": "20",
                     "opened_at": "2026-10-15T13:30:00.123456789Z"}]
        self.ingest([fill("sell", side="sell", quantity="1", price="30")], covered(opening_lots=baseline))
        snapshot = self.snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertFalse(snapshot["execution_accounting_complete"])
        self.assertIsNone(snapshot["realized_pnl"])
        self.assertIn("ambiguous_opening_lot_order", snapshot["blockers"])
        self.assertEqual(snapshot["matches"][0]["opened_at"], "2026-10-15T13:30:00.123456789Z")
        self.assertFalse(snapshot["matches"][0]["fee_allocation_complete"])

    def test_explicit_carried_lot_produces_current_day_gross_without_fake_entry_fee(self):
        baseline = [{"lot_id": "carry", "symbol": "XYZ", "quantity": "10", "price": "8",
                     "opened_at": "2026-10-15T13:31:00Z"}]
        self.ingest([fill("sell", side="sell", quantity="4", price="11")], covered(opening_lots=baseline))
        snapshot = self.snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["realized_pnl"], "12")
        self.assertEqual(snapshot["open_lots"][0]["quantity"], "6")
        self.assertIsNone(snapshot["matches"][0]["fees"])
        self.assertIsNone(snapshot["matches"][0]["net_pnl"])

    def test_session_roll_resets_realized_pnl_and_retains_fifo_inventory(self):
        self.ingest([fill("old-buy", quantity="20", price="8", at="2026-10-15T13:30:00Z"),
                     fill("old-sell", side="sell", quantity="5", price="10", at="2026-10-15T13:31:00Z"),
                     fill("new-sell", side="sell", quantity="10", price="11")],
                    covered(window_start="2026-10-15T04:00:00Z"))
        snapshot = self.snapshot()
        self.assertEqual(snapshot["realized_pnl"], "30")
        self.assertEqual(snapshot["open_lots"][0]["quantity"], "5")
        self.assertEqual(len(snapshot["matches"]), 2)

    def test_backdated_revision_one_is_not_silently_dropped_at_creation_time_bound(self):
        self.ingest([fill("backdated", at="2026-10-15T13:30:00Z")])
        self.assertIn("pre_window_execution_requires_rebuilt_history", self.snapshot()["blockers"])

    def test_baseline_must_match_backdated_execution_identity_and_basis(self):
        historical = fill("prior-buy", quantity="10", price="8", at="2026-10-15T13:30:00Z")
        baseline = [{"lot_id": "carry", "execution_id": "prior-buy", "symbol": "XYZ", "quantity": "6",
                     "price": "8", "opened_at": historical["effective_at"]}]
        self.ingest([historical], covered(opening_lots=baseline))
        self.assertTrue(self.snapshot()["complete"])
        self.ingest([], covered(opening_lots=[{**baseline[0], "price": "9"}]))
        self.assertFalse(self.snapshot()["complete"])

    def test_pre_window_correction_requires_rebuilt_history_even_with_old_anchor_id(self):
        historical = fill("prior-buy", revision=2, at="2026-10-15T13:30:00Z")
        baseline = [{"lot_id": "carry", "execution_id": "prior-buy", "symbol": "XYZ", "quantity": "10",
                     "price": "10", "opened_at": historical["effective_at"]}]
        self.ingest([historical], covered(opening_lots=baseline))
        self.assertIn("pre_window_correction_requires_rebuilt_history", self.snapshot()["blockers"])

    def test_missing_costs_remain_unknown_unless_simulation_selected(self):
        self.ingest(self.roundtrip() + [fee("observed", "0.2")], covered(fees_complete=False))
        actual = self.snapshot()
        simulated = self.snapshot(cost_policy="paper_zero_fee_simulation")
        self.assertIsNone(actual["fees"])
        self.assertIsNone(actual["net_realized_pnl"])
        self.assertEqual(actual["observed_actual_fees"], "0.2")
        self.assertFalse(actual["complete"])
        self.assertTrue(simulated["complete"])
        self.assertEqual(simulated["fees"], "0")
        self.assertEqual(simulated["observed_actual_fees"], "0.2")
        self.assertFalse(simulated["actual_costs_complete"])
        self.assertEqual(simulated["cost_basis"], "explicit_paper_zero_fee_simulation")

    def test_zero_fee_simulation_does_not_waive_execution_or_baseline_uncertainty(self):
        self.ingest([], covered(executions_complete=False, opening_lots_complete=False, opening_evidence=None, fees_complete=False))
        snapshot = self.snapshot(cost_policy="paper_zero_fee_simulation")
        self.assertFalse(snapshot["complete"])
        self.assertIn("execution_coverage_incomplete", snapshot["blockers"])
        self.assertIn("opening_inventory_unproven", snapshot["blockers"])

    def test_date_only_fee_keeps_date_basis_and_signed_credit(self):
        self.ingest(self.roundtrip() + [fee("charge", "0.3"), fee("refund", "-0.1")])
        snapshot = self.snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["fees"], "0.2")
        self.assertEqual(snapshot["net_realized_pnl"], "9.8")
        stored = next(row for row in self.ledger.revisions() if row["source_id"] == "charge")
        self.assertNotIn("effective_at", stored)
        self.assertEqual(stored["date_basis"], "provider_activity_or_settlement_date")
        self.assertIsNone(snapshot["matches"][0]["fees"])

    def test_negative_fee_total_is_preserved_but_cannot_satisfy_old_risk_contract(self):
        self.ingest(self.roundtrip() + [fee("refund", "-0.1")])
        snapshot = self.snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["observed_actual_fees"], "-0.1")
        self.assertIsNone(snapshot["fees"])
        self.assertIn("net_fee_credit_incompatible_with_session_gate", snapshot["blockers"])

    def test_late_fee_restates_projection_without_rewriting_trade_journal(self):
        journal = Journal(self.path)
        journal.append_trade("fixture", "original", {"gross_pnl": "10", "fees": None})
        self.ingest(self.roundtrip())
        before = self.snapshot()
        self.ingest([fee("late", "0.25")])
        after = self.snapshot()
        self.assertNotEqual(before["projection_id"], after["projection_id"])
        self.assertEqual(after["realized_pnl"], "10")
        self.assertEqual(after["net_realized_pnl"], "9.75")
        self.assertEqual(journal.trades()[0]["payload"], {"gross_pnl": "10", "fees": None})

    def test_explicit_fee_linkage_allocates_partial_fill_costs_and_retains_residual(self):
        self.ingest([fill("buy", quantity="10"),
                     fill("sell", side="sell", quantity="4", price="11", at="2026-10-16T13:31:00Z"),
                     fee("entry-charge", "1", "buy"), fee("exit-charge", "0.4", "sell")])
        snapshot = self.snapshot()
        self.assertEqual(snapshot["fees"], "1.4")
        self.assertEqual(snapshot["matches"][0]["fees"], "0.8")
        self.assertEqual(snapshot["matches"][0]["net_pnl"], "3.2")
        self.assertEqual(snapshot["open_lots"][0]["remaining_entry_fees"], "0.6")

    def test_fee_revision_and_cancellation_are_append_only_adjustments(self):
        self.ingest(self.roundtrip() + [fee("charge", "0.3")])
        self.ingest([fee("charge", "0.2", revision=2)])
        self.assertEqual(self.snapshot()["fees"], "0.2")
        canceled = {key: value for key, value in fee("charge", revision=3).items() if key != "amount"}
        canceled["status"] = "canceled"
        self.ingest([canceled])
        self.assertEqual(self.snapshot()["fees"], "0")
        self.assertEqual(len(self.ledger.revisions()), 5)

    def test_evaluation_does_not_refresh_old_coverage(self):
        self.ingest(self.roundtrip())
        before = self.snapshot()
        late = self.ledger.snapshot(session_date=DAY, now="2026-10-16T13:35:11Z").to_dict()
        self.assertIn("coverage_stale", late["blockers"])
        self.assertEqual(late["as_of"], before["as_of"])
        self.assertEqual(late["projection_id"], before["projection_id"])

    def test_freshness_gate_retains_nanoseconds(self):
        self.ingest([])
        exact = self.ledger.snapshot(session_date=DAY, now="2026-10-16T13:35:10.000000000Z")
        late = self.ledger.snapshot(session_date=DAY, now="2026-10-16T13:35:10.000000001Z")
        self.assertTrue(exact.complete)
        self.assertIn("coverage_stale", late.blockers)

    def test_future_partial_or_wrong_session_window_fails_closed(self):
        for change, code in [({"window_start": "2026-10-16T13:00:00Z"}, "current_session_window_not_covered"),
                             ({"window_end": "2026-10-16T13:36:00Z", "as_of": "2026-10-16T13:36:00Z"}, "coverage_from_future"),
                             ({"as_of": "2026-10-16T13:36:00Z"}, "import_watermark_from_future")]:
            with self.subTest(code=code):
                self.ingest([], covered(**change))
                self.assertIn(code, self.snapshot()["blockers"])
        wrong = self.ledger.snapshot(session_date="2026-10-15", now=NOW)
        self.assertIn("not_current_new_york_session", wrong.blockers)

    def test_incomplete_latest_import_does_not_fall_back_to_old_complete_coverage(self):
        self.ingest(self.roundtrip())
        self.ingest([], covered(executions_complete=False, blockers=["provider_correction_unresolved"]))
        snapshot = self.snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertIn("execution_coverage_incomplete", snapshot["blockers"])
        self.assertIn("provider_correction_unresolved", snapshot["blockers"])

    def test_account_environment_and_source_are_isolated(self):
        self.ingest(self.roundtrip())
        other = AccountingLedger(self.path, account_id="fixture-account-b")
        self.assertIn("coverage_missing", other.snapshot(session_date=DAY, now=NOW).blockers)
        with self.assertRaises(LedgerError):
            other.ingest_batch([], batch_id="wrong-account", coverage=covered())
        other_source = AccountingLedger(self.path, account_id=ACCOUNT, source="another_source")
        self.assertIn("coverage_missing", other_source.snapshot(session_date=DAY, now=NOW).blockers)
        with self.assertRaises(LedgerError):
            AccountingLedger(self.path, account_id=ACCOUNT, environment="live")

    def test_restart_and_concurrent_retry_reproduce_one_durable_import(self):
        def ingest(_):
            ledger = AccountingLedger(self.path, account_id=ACCOUNT)
            return ledger.ingest_batch(self.roundtrip(), batch_id="concurrent", coverage=covered())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(ingest, range(2)))
        self.assertEqual(sum(row["inserted"] for row in results), 2)
        before = self.snapshot()
        reopened = AccountingLedger(self.path, account_id=ACCOUNT)
        self.assertEqual(reopened.snapshot(session_date=DAY, now=NOW).to_dict(), before)

    def test_sql_append_only_triggers_protect_audit_evidence(self):
        self.ingest(self.roundtrip())
        with self.assertRaises(LedgerConflict):
            self.ingest([fill("buy", price="12")])
        with sqlite3.connect(self.path) as db:
            for table in ("accounting_revisions", "accounting_batches", "accounting_conflicts"):
                for operation in (f"DELETE FROM {table}", f"UPDATE {table} SET source=source"):
                    with self.subTest(operation=operation), self.assertRaises(sqlite3.IntegrityError):
                        db.execute(operation)

    def test_raw_fingerprint_mutation_conflicts_even_when_financial_fields_match(self):
        self.ingest([fill("buy", source_fingerprint="a" * 64)])
        with self.assertRaises(LedgerConflict):
            self.ingest([fill("buy", source_fingerprint="b" * 64)])
        self.assertFalse(self.snapshot()["complete"])

    def test_decimal_math_and_validation_ignore_ambient_precision(self):
        with localcontext() as context:
            context.prec = 6
            self.ingest([fill("buy", quantity="0.123456789", price="123.123456789"),
                         fill("sell", side="sell", quantity="0.123456789", price="124.123456789", at="2026-10-16T13:31:00Z"),
                         fee("cost", "0.000000001")])
            snapshot = self.snapshot()
            self.assertEqual(snapshot["realized_pnl"], "0.123456789")
            self.assertEqual(snapshot["net_realized_pnl"], "0.123456788")
            with self.assertRaises(LedgerError):
                self.ingest([fill("too-big", price="1000000000000000000.000000001")])

    def test_fee_allocation_rounding_and_traps_do_not_depend_on_caller_context(self):
        self.ingest([fill("buy", quantity="3"),
                     fill("sell", side="sell", quantity="1", price="11", at="2026-10-16T13:31:00Z"),
                     fee("entry-charge", "1", "buy")])
        with localcontext() as context:
            context.rounding = ROUND_DOWN
            down = self.snapshot()
        with localcontext() as context:
            context.rounding = ROUND_UP
            context.traps[Inexact] = True
            up = self.snapshot()
        self.assertEqual(down, up)
        with localcontext() as context:
            context.prec = 110
            allocated = Decimal(up["matches"][0]["fees"])
            residual = Decimal(up["open_lots"][0]["remaining_entry_fees"])
            self.assertEqual(allocated + residual, Decimal("1"))

    def test_invalid_numeric_time_and_coverage_evidence_rejected_atomically(self):
        malformed = [fill("bad", quantity=True), fill("bad", price=1.1), fill("bad", quantity="NaN"),
                     fill("bad", price="Infinity"), fill("bad", quantity="-1"),
                     fill("bad", at="2026-10-16T13:30:00"), fill("bad", at="2026-10-16T13:30:00.1234567890Z"),
                     fill("bad", revision=True), fill("bad", side="sell_short"),
                     fill("bad", side=[]), fill("bad", kind=[]), fill("bad", source_fingerprint="not-a-hash")]
        for record in malformed:
            with self.subTest(record=record), self.assertRaises(LedgerError):
                self.ingest([fill("otherwise-valid"), record])
        for change in ({"opening_evidence": None}, {"fees_complete": 1}, {"environment": "live"},
                       {"window_end": "2026-10-16T13:35:06Z"}, {"blockers": "not-a-list"}):
            with self.subTest(change=change), self.assertRaises(LedgerError):
                self.ingest([], covered(**change))
        self.assertEqual(self.ledger.revisions(), [])

    def test_snapshot_dict_is_a_copy_and_never_an_execution_instruction(self):
        self.ingest(self.roundtrip())
        snapshot = self.ledger.snapshot(session_date=DAY, now=NOW)
        changed = snapshot.to_dict()
        changed["coverage"]["opening_lots"].append({"made_up": True})
        changed["execution_armed"] = True
        snapshot.payload["complete"] = False
        self.assertEqual(snapshot.to_dict()["coverage"]["opening_lots"], [])
        self.assertFalse(snapshot.to_dict()["execution_armed"])
        self.assertTrue(snapshot.complete)


if __name__ == "__main__":
    unittest.main()
