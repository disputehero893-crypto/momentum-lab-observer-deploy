"""Offline coverage for PaperSession reconciliation, exit/cancel and pre-submit guard branches.

Paper only: every broker response comes from the in-memory FakeVenue. No network, no live mode.
Each test asserts the session fails closed (an error is reported, nothing extra is submitted).
"""
from datetime import datetime, timedelta
from decimal import Decimal
import tempfile
import unittest
from types import SimpleNamespace

from momentum.alpaca import PaperDisabled
from momentum.journal import Journal
from momentum.session import PaperSession
from worker_fakes import armed_config, FakeVenue


class SessionBranchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = armed_config(self.tmp.name)
        self.venue = FakeVenue(self.cfg)
        self.journal = Journal(self.cfg.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, **kwargs):
        return PaperSession(self.cfg, self.venue.client, journal=self.journal, clock=lambda: self.venue.now, **kwargs)

    def cycle(self, session, snapshots=None, ledger=None, **kw):
        return session.cycle(snapshots=snapshots or [self.venue.snapshot(**kw)],
                             session_ledger=ledger or self.venue.ledger(), execute=True)

    def posts(self):
        return sum(c["method"] == "POST" for c in self.venue.calls)

    def enter(self, session):
        first = self.cycle(session)
        self.assertTrue(first["actions"][0]["submitted"], first)
        self.venue.advance()
        return next(iter(self.venue.orders))

    def reconcile(self, session):
        return session._reconcile("paper-2026-10-16", self.venue.now)["errors"]

    # --- _reconcile evidence checks -------------------------------------------------------------
    def test_missing_broker_order_is_unresolved_until_marked_not_sent(self):
        with self.session() as session:
            cid = self.enter(session)
            del self.venue.orders[cid]
            self.assertIn("unresolved_order_submission", self.reconcile(session))
            self.journal.append("paper-2026-10-16", "paper_order_not_sent", {"client_order_id": cid},
                                key="not-sent:"+cid)
            self.assertNotIn("unresolved_order_submission", self.reconcile(session))

    def test_order_identity_status_and_fill_anomalies_fail_closed(self):
        cases = [
            ({"symbol": "DEMO-B"}, "broker_order_identity_mismatch"),
            ({"status": "mystery"}, "unrecognized_order_status"),
            ({"filled_qty": "500"}, "invalid_cumulative_fill_quantity"),
            ({"filled_qty": "5", "filled_avg_price": "0"}, "filled_price_unknown"),
        ]
        with self.session() as session:
            cid = self.enter(session)
            original = dict(self.venue.orders[cid])
            for change, error in cases:
                self.venue.orders[cid] = {**original, **change}
                self.assertIn(error, self.reconcile(session), change)
            self.assertEqual(self.posts(), 1)

    def test_regressed_cumulative_fill_is_flagged(self):
        with self.session() as session:
            cid = self.enter(session)
            self.venue.fill(cid, 10, "9.82")
            self.assertNotIn("cumulative_fill_regressed", self.reconcile(session))
            self.venue.orders[cid]["filled_qty"] = "4"
            self.assertIn("cumulative_fill_regressed", self.reconcile(session))

    def test_open_order_list_missing_our_active_order_is_a_race(self):
        with self.session() as session:
            self.enter(session)
            self.venue.client.open_orders = lambda: []
            self.assertIn("open_order_reconciliation_race", self.reconcile(session))

    def test_sell_fill_without_owned_position_is_unmatched(self):
        with self.session() as session:
            cid = self.enter(session)
            row = next(e for e in self.journal.events(event_type="paper_order_requested"))
            payload = row["payload"]
            sell_id = cid + "-sell"
            sell = {**payload, "client_order_id": sell_id,
                    "intent": {**payload["intent"], "side": "sell"}}
            self.journal.append(row["run_id"], "paper_order_requested", sell, key="test-sell:"+sell_id)
            self.venue.orders[sell_id] = {**self.venue.orders[cid], "client_order_id": sell_id, "id": "fake-sell",
                                          "side": "sell", "status": "filled",
                                          "filled_qty": self.venue.orders[cid]["qty"], "filled_avg_price": "9.90"}
            self.assertIn("unmatched_or_excess_sell_fill", self.reconcile(session))

    def test_cost_basis_mismatch_is_flagged(self):
        with self.session() as session:
            cid = self.enter(session)
            self.venue.fill(cid, 40, "9.82")
            self.venue.positions["DEMO-A"]["entry"] = Decimal("9.95")
            self.assertIn("position_cost_basis_mismatch", self.reconcile(session))

    # --- cycle-level checks ----------------------------------------------------------------------
    def test_open_position_without_quote_blocks_trading(self):
        with self.session() as session:
            cid = self.enter(session)
            self.venue.fill(cid, int(self.venue.orders[cid]["qty"]), "9.82")
            result = self.cycle(session, snapshots=[self.venue.snapshot(symbol="DEMO-B")])
        self.assertIn("quote_missing_for_open_position", result["errors"])
        self.assertFalse(result["reconciled"])
        self.assertEqual(self.posts(), 1)

    def test_session_date_change_mid_cycle_is_reported(self):
        snap = self.venue.snapshot()

        def builder():
            self.venue.advance(24*60*60)
            return [snap]

        with self.session(snapshot_builder=SimpleNamespace(build_watchlist=builder)) as session:
            result = session.cycle(snapshots=None, session_ledger=self.venue.ledger(), execute=True)
        self.assertIn("session_changed_during_cycle", result["errors"])
        self.assertIn("account_reconciliation_became_stale", result["errors"])
        self.assertEqual(self.posts(), 0)

    # --- exit and cancel branches ----------------------------------------------------------------
    def test_exit_cancels_opening_remainder_and_waits(self):
        with self.session() as session:
            cid = self.enter(session)
            self.venue.fill(cid, 40, "9.82")
            result = self.cycle(session, bid="9.70", ask="9.72")
        kinds = [a["kind"] for a in result["actions"]]
        self.assertIn("cancel_opening_remainder", kinds, result)
        self.assertIn("exit_waiting_for_opening_order_reconciliation", kinds)
        self.assertNotIn("paper_exit_intent", kinds)
        self.assertEqual(self.venue.orders[cid]["status"], "pending_cancel")
        self.assertEqual(self.posts(), 1)

    def test_pending_exit_waits_then_stale_exit_is_canceled(self):
        with self.session() as session:
            cid = self.enter(session)
            self.venue.fill(cid, int(self.venue.orders[cid]["qty"]), "9.82")
            submitted = self.cycle(session, bid="9.70", ask="9.72")
            self.assertTrue(any(a["kind"] == "paper_exit_intent" and a["submitted"] for a in submitted["actions"]))
            exit_id = next(k for k, o in self.venue.orders.items() if o["side"] == "sell")
            self.venue.advance()
            fresh = self.cycle(session, bid="9.70", ask="9.72")
            kinds = [a["kind"] for a in fresh["actions"]]
            self.assertIn("exit_pending_fill_or_cancel_confirmation", kinds, fresh)
            self.assertNotIn("cancel_stale_exit", kinds)
            self.venue.advance(int(self.cfg.order_ttl_seconds) + 1)
            stale = self.cycle(session, bid="9.70", ask="9.72")
        kinds = [a["kind"] for a in stale["actions"]]
        self.assertIn("cancel_stale_exit", kinds, stale)
        self.assertIn("exit_pending_fill_or_cancel_confirmation", kinds)
        self.assertEqual(self.venue.orders[exit_id]["status"], "pending_cancel")
        self.assertEqual(self.posts(), 2)  # one entry, one exit; no duplicate exit

    # --- guard() checks right before a paper submission -----------------------------------------
    def wrap_assess(self, session, after, call=2):
        """Run `after` right after the per-symbol risk decision (call 1 is the account watchdog)."""
        original = session.risk.assess
        calls = []

        def assess(intent, *args, **kwargs):
            out = original(intent, *args, **kwargs)
            calls.append(intent)
            if len(calls) == call:
                after()
            return out

        session.risk.assess = assess

    def test_entry_guard_rejects_lost_ownership(self):
        with self.session() as session:
            self.wrap_assess(session, lambda: setattr(session, "_owner", False))
            result = self.cycle(session)
        self.assertIn("PaperDisabled", result["errors"])
        self.assertEqual(self.posts(), 0)

    def test_entry_guard_rejects_ledger_that_went_stale(self):
        ledger = dict(self.venue.ledger(), as_of=(self.venue.now - timedelta(seconds=4)).isoformat())
        with self.session() as session:
            self.wrap_assess(session, lambda: self.venue.advance(2))
            result = self.cycle(session, ledger=ledger)
        self.assertIn("PaperDisabled", result["errors"])
        self.assertEqual(self.posts(), 0)

    def test_entry_guard_rejects_kill_switch_set_after_decision(self):
        with self.session() as session:
            self.wrap_assess(session, lambda: self.journal.set_kill_switch(True, "late halt", request_id="late"))
            result = self.cycle(session)
        self.assertIn("PaperDisabled", result["errors"])
        self.assertEqual(self.posts(), 0)

    def test_exit_guard_requires_reconciliation(self):
        with self.session() as session:
            cid = self.enter(session)
            self.venue.fill(cid, int(self.venue.orders[cid]["qty"]), "9.82")
            # Ledger missing => not reconciled; an exit decision must not be submitted.
            result = session.cycle(snapshots=[self.venue.snapshot(bid="9.70", ask="9.72")], execute=True)
        self.assertIn("PaperDisabled", result["errors"])
        self.assertEqual(self.posts(), 1)

    def test_exit_guard_rejects_session_rollover(self):
        # The exit decision is made at 23:59:58 New York time; the clock rolls past midnight before submit.
        with self.session() as session:
            # Seed a filled position through the normal entry path at an earlier, in-session time.
            self.venue.now = datetime.fromisoformat("2026-10-16T13:35:05+00:00")
            cid = self.enter(session)
            self.venue.fill(cid, int(self.venue.orders[cid]["qty"]), "9.82")
            self.venue.now = datetime.fromisoformat("2026-10-17T03:59:58+00:00")
            self.wrap_assess(session, lambda: self.venue.advance(3))
            result = self.cycle(session, bid="9.70", ask="9.72")
        self.assertIn("PaperDisabled", result["errors"], result)
        self.assertEqual(self.posts(), 1)


if __name__ == "__main__":
    unittest.main()
