from datetime import timedelta
from decimal import Decimal
import tempfile
import unittest

from momentum.journal import Journal
from momentum.risk import RiskManager
from momentum.session import PaperSession
from worker_fakes import armed_config, FakeVenue


class SessionTests(unittest.TestCase):
    def test_partial_fill_cancel_race_exit_and_confirmed_flatness(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            venue = FakeVenue(cfg)
            journal = Journal(cfg.db_path)
            with PaperSession(cfg, venue.client, journal=journal, clock=lambda: venue.now) as session:
                first = session.cycle(snapshots=[venue.snapshot()], session_ledger=venue.ledger(), execute=True)
                self.assertTrue(first["reconciled"], first)
                self.assertTrue(first["actions"][0]["submitted"], first)
                self.assertFalse(first["positions_confirmed_flat"])
                entry_id = next(iter(venue.orders))
                venue.advance()
                venue.fill(entry_id, 40, "9.82")
                partial = session.cycle(snapshots=[venue.snapshot()], session_ledger=venue.ledger(), execute=True)
                self.assertEqual(partial["account"]["position_qty"], 40, partial)
                self.assertEqual(partial["account"]["open_order_qty"], 61)
                opened_at = partial["positions"][0]["opened_at"]
                journal.set_kill_switch(True, "test halt", request_id="halt")
                venue.advance()
                halting = session.cycle(snapshots=[venue.snapshot()], session_ledger=venue.ledger(), execute=True)
                self.assertIn("cancel_opening_remainder", [a["kind"] for a in halting["actions"]], halting)
                self.assertEqual(halting["positions"][0]["qty"], 40)
                self.assertFalse(halting["positions_confirmed_flat"])
                # Ten more shares fill while the cancel is in flight.
                venue.advance()
                venue.fill(entry_id, 10, "9.82")
                venue.orders[entry_id]["status"] = "canceled"
                exit_pending = session.cycle(snapshots=[venue.snapshot(bid="9.70", ask="9.72")],
                                             session_ledger=venue.ledger(), execute=True)
                exits = [a for a in exit_pending["actions"] if a["kind"] == "paper_exit_intent"]
                self.assertEqual(len(exits), 1, exit_pending)
                self.assertEqual(exits[0]["intent"]["qty"], 50)
                self.assertTrue(exits[0]["submitted"])
                self.assertEqual(exit_pending["positions"][0]["opened_at"], opened_at)
                self.assertFalse(exit_pending["positions_confirmed_flat"])
                exit_id = next(key for key, o in venue.orders.items() if o["side"] == "sell")
                venue.advance()
                venue.fill(exit_id, 50, "9.70")
                closed = session.cycle(snapshots=[venue.snapshot(bid="9.70", ask="9.72")],
                                       session_ledger=venue.ledger(), execute=True)
                self.assertTrue(closed["positions_confirmed_flat"], closed)
                self.assertEqual(len(journal.trades()), 1)
                self.assertEqual(Decimal(journal.trades()[0]["payload"]["gross_pnl"]), Decimal("-6.00"))
                self.assertIsNone(journal.trades()[0]["payload"]["net_pnl"])
            with PaperSession(cfg, venue.client, journal=journal, clock=lambda: venue.now) as restarted:
                again = restarted.cycle(snapshots=[venue.snapshot()], session_ledger=venue.ledger(), execute=True)
                self.assertFalse(any(a.get("kind") == "paper_entry_intent" for a in again["actions"]))
                self.assertEqual(sum(c["method"] == "POST" for c in venue.calls), 2)
                # Recovery of yesterday's trade uses its original stable run identity.
                venue.advance(24*60*60)
                recovered = restarted._reconcile("paper-2026-10-17", venue.now)
                self.assertTrue(recovered["reconciled"])
                self.assertEqual(len(journal.trades()), 1)

    def test_unowned_position_and_missing_ledger_block_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            venue = FakeVenue(cfg)
            with PaperSession(cfg, venue.client, clock=lambda: venue.now) as session:
                no_ledger = session.cycle(snapshots=[venue.snapshot()], execute=True)
                self.assertIn("complete_current_session_pnl_and_fee_ledger_missing", no_ledger["errors"])
                venue.positions["UNOWNED"] = {"qty": 1, "entry": Decimal("9.81")}
                unowned = session.cycle(snapshots=[venue.snapshot()], session_ledger=venue.ledger(), execute=True)
                self.assertIn("position_quantity_mismatch_or_unowned_position", unowned["errors"])
                self.assertFalse(unowned["positions_confirmed_flat"])
                self.assertEqual(unowned["account"]["position_qty"], 1)
                self.assertFalse(any(c["method"] == "POST" for c in venue.calls))

    def test_no_quote_daily_loss_latches_and_restore_occurs_under_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            venue = FakeVenue(cfg)
            journal = Journal(cfg.db_path)
            constructed = PaperSession(cfg, venue.client, journal=journal, clock=lambda: venue.now)
            state = RiskManager().to_state()
            state["kill_latched"] = True
            journal.append("paper-state", "paper_risk_state", {"state": state}, key="newer-risk-state")
            with constructed as session:
                self.assertTrue(session.risk.locked)
                venue.realized = Decimal("-80")
                result = session.cycle(snapshots=[], session_ledger=venue.ledger(), execute=False)
                self.assertFalse(result["reconciled"])
                self.assertIn("no_current_snapshots", result["errors"])
                restored = journal.latest("paper_risk_state", "paper-state")["payload"]["state"]
                self.assertEqual(restored["daily_locks"]["2026-10-16"], "daily_loss_limit")

    def test_account_acquisition_delay_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            venue = FakeVenue(cfg)
            original = venue.client.positions
            def slow_positions():
                venue.advance(6)
                return original()
            venue.client.positions = slow_positions
            with PaperSession(cfg, venue.client, clock=lambda: venue.now) as session:
                result = session.cycle(snapshots=[venue.snapshot()], session_ledger=venue.ledger(), execute=True)
                self.assertIn("account_reconciliation_became_stale", result["errors"])
                self.assertFalse(any(c["method"] == "POST" for c in venue.calls))

    def test_risk_lock_checkpoint_precedes_external_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            venue = FakeVenue(cfg)
            journal = Journal(cfg.db_path)
            with PaperSession(cfg, venue.client, journal=journal, clock=lambda: venue.now) as session:
                session.cycle(snapshots=[venue.snapshot()], session_ledger=venue.ledger(), execute=True)
                venue.advance()
                venue.realized = Decimal("-80")
                def process_dies(*args, **kwargs):
                    raise SystemExit("injected process interruption")
                venue.client.cancel = process_dies
                with self.assertRaises(SystemExit):
                    session.cycle(snapshots=[venue.snapshot()], session_ledger=venue.ledger(), execute=True)
                saved = journal.latest("paper_risk_state", "paper-state")["payload"]["state"]
                self.assertEqual(saved["daily_locks"]["2026-10-16"], "daily_loss_limit")

    def test_late_order_lookup_cannot_refresh_old_account_or_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            venue = FakeVenue(cfg)
            positions = venue.client.positions
            lookup = venue.client.order_by_client_id
            def delayed_positions():
                venue.advance(4)
                return positions()
            def delayed_lookup(client_id):
                venue.advance(1.5)
                return lookup(client_id)
            venue.client.positions = delayed_positions
            venue.client.order_by_client_id = delayed_lookup
            class FreshBuilder:
                def build_watchlist(self):
                    return [venue.snapshot()]
            ledger = venue.ledger()
            with PaperSession(cfg, venue.client, snapshot_builder=FreshBuilder(), clock=lambda: venue.now) as session:
                result = session.cycle(session_ledger=ledger, execute=True)
                self.assertTrue(result["decisions"][0]["risk"]["accepted"], result)
                self.assertIn("PaperDisabled", result["errors"])
                self.assertFalse(any(c["method"] == "POST" for c in venue.calls))

