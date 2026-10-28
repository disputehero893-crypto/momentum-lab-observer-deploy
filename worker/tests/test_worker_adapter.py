from dataclasses import replace
import tempfile
import unittest
from urllib.parse import urlsplit

from momentum.alpaca import PaperClient, PaperDisabled, BrokerError, AmbiguousSubmission, NoRedirect
from momentum.journal import Journal
from momentum.risk import RiskManager
from momentum.strategy import evaluate_setup
from worker_fakes import armed_config, FakeVenue


def approved(venue):
    snapshot = venue.snapshot()
    account = {"equity": "10000", "buying_power": "10000", "realized_pnl": "0", "unrealized_pnl": "0",
               "fees": "0", "position_qty": 0, "open_order_qty": 0, "session_date": "2026-10-16", "as_of": snapshot["now"]}
    intent = RiskManager().assess(evaluate_setup(snapshot).intent, account, snapshot["quote"], snapshot["now"],
                                  connected=True, reconciled=True).intent
    return intent, snapshot["quote"]


class AdapterTests(unittest.TestCase):
    def test_fixed_paper_host_extended_hours_and_redirect_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            venue = FakeVenue(cfg)
            intent, quote = approved(venue)
            venue.client.submit(intent, Journal(cfg.db_path), "paper-test", quote=quote, guard=lambda: None)
            post = next(c for c in venue.calls if c["method"] == "POST")
            self.assertEqual(urlsplit(post["url"]).netloc, "paper-api.alpaca.markets")
            self.assertEqual(post["payload"]["type"], "limit")
            self.assertEqual(post["payload"]["time_in_force"], "day")
            self.assertIs(post["payload"]["extended_hours"], True)
            with self.assertRaises(BrokerError):
                venue.client._request("GET", "/v2/../account")
            with self.assertRaises(BrokerError):
                NoRedirect().redirect_request(None, None, 302, None, None, "https://api.alpaca.markets")

    def test_timeout_recovers_same_client_id_without_second_post(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            venue = FakeVenue(cfg)
            venue.ambiguous_post = True
            intent, quote = approved(venue)
            journal = Journal(cfg.db_path)
            with self.assertRaises(AmbiguousSubmission):
                venue.client.submit(intent, journal, "paper-test", quote=quote, guard=lambda: None)
            result = venue.client.submit(intent, Journal(cfg.db_path), "paper-test", quote=quote, guard=lambda: None)
            self.assertEqual(result["client_order_id"], PaperClient.client_order_id(intent))
            self.assertEqual(sum(c["method"] == "POST" for c in venue.calls), 1)

    def test_timeout_without_visible_order_never_blindly_resubmits(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            venue = FakeVenue(cfg)
            venue.ambiguous_post, venue.post_commits = True, False
            intent, quote = approved(venue)
            journal = Journal(cfg.db_path)
            for _ in range(2):
                with self.assertRaises(AmbiguousSubmission):
                    venue.client.submit(intent, journal, "paper-test", quote=quote, guard=lambda: None)
            self.assertEqual(sum(c["method"] == "POST" for c in venue.calls), 1)

    def test_stale_unreviewed_or_synthetic_input_never_posts(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            venue = FakeVenue(cfg)
            intent, quote = approved(venue)
            venue.advance(3)
            with self.assertRaises(PaperDisabled):
                venue.client.submit(intent, Journal(cfg.db_path), "paper-test", quote=quote, guard=lambda: None)
            venue.now = venue.now.replace(second=5)
            bad = dict(intent, feed="synthetic")
            with self.assertRaises(PaperDisabled):
                venue.client.submit(bad, Journal(cfg.db_path), "paper-test", quote=quote, guard=lambda: None)
            client = PaperClient(replace(cfg, paper_reviewed_config_hash=""), transport=venue.request)
            with self.assertRaises(PaperDisabled):
                client.submit(intent, Journal(cfg.db_path), "paper-test", quote=quote, guard=lambda: None)
            self.assertFalse(any(c["method"] == "POST" for c in venue.calls))

    def test_cancel_reconciles_and_retries_but_never_claims_flatness(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            venue = FakeVenue(cfg)
            intent, quote = approved(venue)
            journal = Journal(cfg.db_path)
            order = venue.client.submit(intent, journal, "paper-test", quote=quote, guard=lambda: None)
            venue.cancel_failure = True
            with self.assertRaises(BrokerError):
                venue.client.cancel(order, journal, "paper-test")
            venue.advance(3)
            venue.cancel_failure = False
            result = venue.client.cancel(order, journal, "paper-test")
            self.assertFalse(result["confirmed_canceled"])
            venue.client.cancel(order, journal, "paper-test")
            self.assertEqual(sum(c["method"] == "DELETE" for c in venue.calls), 2)

