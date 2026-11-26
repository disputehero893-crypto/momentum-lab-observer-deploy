"""Fail-closed completeness certification and crypto segregation.

Invented fixtures only: no credentials, network, broker orders or workflows.
"""
from datetime import datetime
import json
import logging
import os
import tempfile
import unittest

from momentum.activities import (NON_EQUITY_SEGREGATION_RULE, SEGREGATED_KIND, ActivityImportError,
                                 fetch_activities, normalize_activity)
from momentum.activity_completeness import apply_certification, certify_import
from momentum.autopilot import ExecutionAccounting
from momentum.config import Config
from momentum.ledger import AccountingLedger

ACCOUNT = "00000000-0000-4000-8000-000000000001"
START = "2026-10-01T00:00:00Z"
END = "2026-10-18T18:00:00Z"


def fill(number, symbol="DEMO-A", side="buy", qty="2", **changes):
    row = {"id": f"2026101614300000{number}::{number:08d}-0000-4000-8000-000000000001",
           "activity_type": "FILL", "type": "fill", "symbol": symbol, "side": side,
           "qty": qty, "price": "10.25", "cum_qty": qty, "leaves_qty": "0",
           "transaction_time": f"2026-10-16T14:30:0{number}.000000001Z",
           "order_id": f"00000000-0000-4000-8000-0000000000{number:02d}"}
    row.update(changes)
    return row


def crypto_fill(number, symbol="BTC/USD", **changes):
    return fill(number, symbol=symbol, qty="0.001", asset_class="crypto", price="60000", **changes)


def fee(number, **changes):
    row = {"id": f"2026101700000000{number}::{number:08d}-0000-4000-8000-000000000002",
           "activity_type": "FEE", "date": "2026-10-17", "net_amount": "-0.02"}
    row.update(changes)
    return row


def cfee(number, symbol="BTCUSD"):
    return fee(number, activity_type="CFEE", symbol=symbol, net_amount="-0.05")


class Provider:
    """GET-only fake: activity_page pages through rows; positions is fixed."""

    def __init__(self, rows, positions=(), *, chunk=100, rescan_rows=None, positions_error=None,
                 rescan_error=None, now=END):
        self.rows, self.rescan_rows, self.chunk = list(rows), rescan_rows, chunk
        self._positions, self.positions_error, self.rescan_error = list(positions), positions_error, rescan_error
        self.now = datetime.fromisoformat(now.replace("Z", "+00:00"))
        self.scans, self.page_calls, self.position_calls = 0, 0, 0

    def clock(self):
        return self.now

    def activity_page(self, *, after, until, page_token=None, page_size=100):
        self.page_calls += 1
        if page_token is None:
            self.scans += 1
        rows = self.rows if self.scans < 2 or self.rescan_rows is None else self.rescan_rows
        if self.scans >= 2 and self.rescan_error is not None:
            raise self.rescan_error
        ids = [row["id"] for row in rows]
        start = 0 if page_token is None else ids.index(page_token) + 1
        return [dict(row) for row in rows[start:start + min(self.chunk, page_size)]]

    def positions(self):
        self.position_calls += 1
        if self.positions_error is not None:
            raise self.positions_error
        return [dict(row) for row in self._positions]


def eq(symbol, qty):
    return {"symbol": symbol, "qty": qty, "asset_class": "us_equity"}


def certify(provider, **changes):
    batch = fetch_activities(provider, account_id=ACCOUNT, after=START, until=END)
    return batch, certify_import(provider, batch, account_id=ACCOUNT, after=START, until=END, **changes)


class CryptoSegregationTests(unittest.TestCase):
    def test_reviewed_pairs_are_segregated_visibly_not_dropped(self):
        for row in (crypto_fill(1), crypto_fill(1, symbol="BTCUSD"), crypto_fill(1, symbol="ETH/USD"),
                    fill(1, symbol="SOLUSD"), cfee(1), cfee(1, symbol="ETH/USD")):
            with self.subTest(symbol=row["symbol"], kind=row["activity_type"]):
                observation = normalize_activity(row)
                self.assertEqual(observation.kind, SEGREGATED_KIND)
                self.assertIsNone(observation.record)
                self.assertEqual(observation.blockers, ())
                self.assertEqual(observation.segregation["rule"], NON_EQUITY_SEGREGATION_RULE)
                self.assertIn(observation.segregation["pair"], {"BTC/USD", "ETH/USD", "SOL/USD"})
                self.assertEqual(observation.to_dict()["segregation"], observation.segregation)

    def test_slashless_crypto_symbol_never_enters_equity_ledger(self):
        # "BTCUSD" matches the equity symbol regex; the rule must catch it first.
        observation = normalize_activity(fill(1, symbol="BTCUSD"))
        self.assertEqual(observation.kind, SEGREGATED_KIND)
        self.assertEqual(observation.segregation["pair"], "BTC/USD")

    def test_unreviewed_or_ambiguous_non_equity_rows_still_block(self):
        cases = [(crypto_fill(1, symbol="DOGE/USD"), "unreviewed_non_equity_asset"),
                 (crypto_fill(1, symbol="DEMO-A"), "unreviewed_non_equity_asset"),
                 (fill(1, symbol="BTCUSD", asset_class="us_option"), "non_equity_asset_class_conflict"),
                 (crypto_fill(1, side="sell_short"), "unsupported_fill_asset_or_side"),
                 (cfee(1, symbol="DOGEUSD"), "unattributed_non_equity_fee"),
                 (fee(1, activity_type="CFEE"), "unattributed_non_equity_fee")]
        for row, blocker in cases:
            with self.subTest(blocker=blocker, symbol=row.get("symbol")):
                observation = normalize_activity(row)
                self.assertEqual(observation.kind, "unsupported")
                self.assertIn(blocker, observation.blockers)

    def test_crypto_correction_is_still_a_correction(self):
        observation = normalize_activity(crypto_fill(1, type="trade_bust"))
        self.assertEqual(observation.kind, "correction")

    def test_import_counts_logs_and_explains_segregation(self):
        provider = Provider([fill(1), crypto_fill(2), cfee(3), crypto_fill(4, symbol="ETHUSD")])
        with self.assertLogs("momentum.accounting", level="INFO") as logs:
            batch = fetch_activities(provider, account_id=ACCOUNT, after=START, until=END)
        self.assertEqual(len(batch.records), 1)
        self.assertEqual(batch.coverage["blockers"], [])
        self.assertEqual(batch.coverage["segregated_non_equity_count"], 3)
        self.assertEqual(batch.coverage["segregated_non_equity_by_pair"], {"BTC/USD": 2, "ETH/USD": 1})
        self.assertEqual(batch.coverage["segregation_rule"], NON_EQUITY_SEGREGATION_RULE)
        self.assertEqual(len(batch.coverage["segregated_non_equity_ids"]), 3)
        self.assertTrue(any(NON_EQUITY_SEGREGATION_RULE in line for line in batch.coverage["evidence"]))
        self.assertTrue(any("segregated 3 non-equity" in line for line in logs.output))
        self.assertEqual(len(batch.observations), 4)  # nothing silently dropped

    def test_import_flags_stay_false_without_certification(self):
        batch = fetch_activities(Provider([fill(1)]), account_id=ACCOUNT, after=START, until=END)
        self.assertFalse(batch.coverage["executions_complete"])
        self.assertFalse(batch.coverage["fees_complete"])


class CertificationTests(unittest.TestCase):
    def test_clean_paginated_scan_is_certified(self):
        provider = Provider([fill(1), fee(2), fill(3, side="sell", qty="1"), fill(4, symbol="DEMO-B")],
                            [eq("DEMO-A", "1"), eq("DEMO-B", "2")], chunk=2)
        with self.assertLogs("momentum.accounting", level="INFO"):
            batch, result = certify(provider)
        self.assertTrue(result.executions_complete and result.fees_complete and result.complete)
        self.assertEqual(result.reasons, ())
        self.assertTrue(result.rescan_matched and result.positions_matched)
        self.assertEqual(provider.scans, 2)
        self.assertGreater(batch.coverage["page_count"], 2)  # short pages were not treated as terminal

    def test_crypto_activity_and_positions_do_not_block_equity_certification(self):
        provider = Provider([fill(1), crypto_fill(2), cfee(3)],
                            [eq("DEMO-A", "2"), {"symbol": "BTCUSD", "qty": "0.001", "asset_class": "crypto"}])
        _, result = certify(provider)
        self.assertTrue(result.complete)
        self.assertEqual(result.segregated_non_equity_count, 2)

    def test_opening_lots_count_toward_reconciliation(self):
        lots = [{"lot_id": "lot-1", "symbol": "DEMO-A", "quantity": "3", "price": "9",
                 "opened_at": "2026-09-30T14:00:00Z"}]
        self.assertTrue(certify(Provider([fill(1)], [eq("DEMO-A", "5")]), opening_lots=lots)[1].complete)
        self.assertFalse(certify(Provider([fill(1)], [eq("DEMO-A", "5")]))[1].complete)

    def test_any_doubt_fails_closed(self):
        cases = {
            "activity_rescan_mismatch": Provider([fill(1)], [eq("DEMO-A", "2")], rescan_rows=[fill(1), fee(2)]),
            "equity_positions_do_not_reconcile": Provider([fill(1)], [eq("DEMO-A", "3")]),
            "certification_error": Provider([fill(1)], positions_error=OSError("down")),
            "certification_error:activities_response_not_array": Provider(
                [fill(1)], [eq("DEMO-A", "2")], rescan_error=ActivityImportError("activities_response_not_array")),
            "activity_import_has_blockers": Provider([fill(1), crypto_fill(2, symbol="DOGE/USD")], [eq("DEMO-A", "2")]),
            "unreviewed_non_equity_position": Provider([fill(1)], [eq("DEMO-A", "2"),
                                                       {"symbol": "DOGEUSD", "qty": "1", "asset_class": "crypto"}]),
            "unsupported_broker_position": Provider([fill(1)], [eq("DEMO-A", "2"),
                                                    {"symbol": "DEMO240119C00010000", "qty": "1",
                                                     "asset_class": "us_option"}]),
            "broker_position_quantity_invalid": Provider([fill(1)], [eq("DEMO-A", "NaN")]),
        }
        for reason, provider in cases.items():
            with self.subTest(reason=reason):
                _, result = certify(provider)
                self.assertFalse(result.executions_complete)
                self.assertFalse(result.fees_complete)
                self.assertIn(reason, result.reasons)

    def test_correction_rows_block_certification(self):
        _, result = certify(Provider([fill(1, type="trade_correct")], []))
        self.assertIn("activity_requires_review", result.reasons)
        self.assertFalse(result.complete)

    def test_broker_positions_not_a_list_fail_closed(self):
        provider = Provider([fill(1)])
        provider.positions = lambda: {"error": "unexpected"}
        self.assertIn("broker_positions_unreadable", certify(provider)[1].reasons)

    def test_coverage_doubts_skip_broker_calls(self):
        provider = Provider([fill(1)], [eq("DEMO-A", "2")])
        batch = fetch_activities(provider, account_id=ACCOUNT, after=START, until=END)
        for change, reason in (({"activity_pages_exhausted": False}, "activity_page_end_unverified"),
                               ({"terminal_observation": None}, "activity_page_end_unverified"),
                               ({"environment": "live"}, "certification_requires_paper_environment"),
                               ({"window_end": "2026-10-18T17:00:00Z"}, "activity_window_bounds_mismatch"),
                               ({"segregated_non_equity_count": 4}, "segregation_count_mismatch")):
            with self.subTest(reason=reason):
                tampered = type(batch)(batch.records, batch.observations, {**batch.coverage, **change})
                result = certify_import(provider, tampered, account_id=ACCOUNT, after=START, until=END)
                self.assertIn(reason, result.reasons)
                self.assertFalse(result.complete)
        self.assertEqual(provider.position_calls, 0)
        self.assertIn("certification_account_mismatch",
                      certify_import(provider, batch, account_id="00000000-0000-4000-8000-000000000009",
                                     after=START, until=END).reasons)

    def test_apply_certification_only_sets_flags_and_appends_evidence(self):
        batch, result = certify(Provider([fill(1)], [eq("DEMO-A", "2")]))
        coverage = apply_certification({"executions_complete": False, "fees_complete": False,
                                        "evidence": ["x"], "blockers": ["kept"]}, result)
        self.assertEqual((coverage["executions_complete"], coverage["fees_complete"]), (True, True))
        self.assertEqual(coverage["blockers"], ["kept"])
        self.assertEqual(coverage["evidence"][0], "x")
        self.assertGreater(len(coverage["evidence"]), 1)


class ExecutionAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "ledger.sqlite3")
        self.baseline = os.path.join(self.tmp.name, "baseline.json")
        with open(self.baseline, "w") as handle:
            json.dump({"schema_version": "1.0", "account_id": ACCOUNT, "environment": "paper",
                       "window_start": START, "evidence": "Invented fixture: account flat at window start.",
                       "opening_lots": []}, handle)

    def config(self, **changes):
        values = dict(mode="paper", paper_enabled=True, api_key="FIXTURE-KEY", api_secret="FIXTURE-SECRET",
                      db_path=self.db, accounting_history_start=START, accounting_baseline_path=self.baseline)
        values.update(changes)
        return Config(**values)

    def collect(self, provider, **changes):
        return ExecutionAccounting(self.config(**changes), provider, provider.clock).collect(ACCOUNT)

    def test_equity_ledger_complete_when_only_extra_activity_is_crypto(self):
        provider = Provider([fill(1), crypto_fill(2), cfee(3), crypto_fill(4, symbol="BTCUSD", side="sell")],
                            [eq("DEMO-A", "2"), {"symbol": "BTCUSD", "qty": "0", "asset_class": "crypto"}])
        snapshot = self.collect(provider)
        self.assertTrue(snapshot["complete"], snapshot.get("blockers"))
        self.assertEqual(snapshot["blockers"], [])
        self.assertTrue(snapshot["completeness_certification"]["executions_complete"])
        self.assertEqual(snapshot["completeness_certification"]["segregated_non_equity_count"], 3)
        rows = AccountingLedger(self.db, account_id=ACCOUNT).snapshot(
            session_date="2026-10-18", now=provider.now).to_dict()
        self.assertTrue(all("BTC" not in json.dumps(row) for row in rows.get("positions", [])))

    def test_uncertified_import_keeps_ledger_blockers(self):
        snapshot = self.collect(Provider([fill(1)], [eq("DEMO-A", "7")]))
        self.assertFalse(snapshot["complete"])
        self.assertIn("execution_coverage_incomplete", snapshot["blockers"])
        self.assertIn("actual_fee_coverage_incomplete", snapshot["blockers"])
        self.assertIn("equity_positions_do_not_reconcile", snapshot["completeness_certification"]["reasons"])

    def test_missing_baseline_still_blocks_opening_inventory(self):
        snapshot = self.collect(Provider([fill(1)], [eq("DEMO-A", "2")]), accounting_baseline_path="")
        self.assertFalse(snapshot["complete"])
        self.assertIn("opening_inventory_unproven", snapshot["blockers"])

    def test_missing_history_start_unchanged(self):
        provider = Provider([fill(1)])
        result = ExecutionAccounting(self.config(accounting_history_start="", accounting_baseline_path=""),
                                     provider, provider.clock).collect(ACCOUNT)
        self.assertEqual(result, {"complete": False, "blockers": ["accounting_history_start_missing"]})
        self.assertEqual(provider.page_calls, 0)


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
