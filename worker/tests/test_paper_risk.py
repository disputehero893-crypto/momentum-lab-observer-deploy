"""Offline paper risk screens; no broker, no network, IEX feed label only."""
import unittest
from decimal import Decimal as D

from momentum.paper_risk import (
    DEFAULT_FEED,
    FOREX_PRACTICE_FEED,
    PaperIntent,
    RiskLimits,
    RiskState,
    check_paper_intent,
)


class PaperRiskTests(unittest.TestCase):
    def setUp(self):
        self.intent = PaperIntent("AAPL", D("1"), D("50"), "paper-001")
        self.state = RiskState(D("0"), D("0"), 0, 100, False, frozenset())

    def test_safe_paper_intent(self):
        self.assertEqual(
            check_paper_intent(self.intent, self.state),
            (True, "approved_for_paper_only"),
        )
        self.assertEqual(RiskLimits().feed, DEFAULT_FEED)
        self.assertEqual(DEFAULT_FEED, "iex")

    def test_kill_switch(self):
        reason = check_paper_intent(
            self.intent, RiskState(D("0"), D("0"), 0, 100, True, frozenset())
        )[1]
        self.assertEqual(reason, "kill_switch_or_invalid_state")

    def test_kill_switch_non_bool_fails_closed(self):
        reason = check_paper_intent(
            self.intent, RiskState(D("0"), D("0"), 0, 100, "yes", frozenset())  # type: ignore[arg-type]
        )[1]
        self.assertEqual(reason, "kill_switch_or_invalid_state")

    def test_duplicate(self):
        reason = check_paper_intent(
            self.intent,
            RiskState(D("0"), D("0"), 0, 100, False, frozenset({"paper-001"})),
        )[1]
        self.assertEqual(reason, "invalid_or_duplicate_order")

    def test_blank_symbol_or_order_id(self):
        blank_symbol = PaperIntent("  ", D("1"), D("50"), "paper-002")
        blank_id = PaperIntent("AAPL", D("1"), D("50"), "")
        self.assertEqual(
            check_paper_intent(blank_symbol, self.state)[1], "invalid_or_duplicate_order"
        )
        self.assertEqual(
            check_paper_intent(blank_id, self.state)[1], "invalid_or_duplicate_order"
        )

    def test_stale_feed(self):
        reason = check_paper_intent(
            self.intent, RiskState(D("0"), D("0"), 0, 3000, False, frozenset())
        )[1]
        self.assertEqual(reason, "stale_or_invalid_feed")

    def test_feed_age_at_limit_approved(self):
        state = RiskState(D("0"), D("0"), 0, 2000, False, frozenset())
        self.assertEqual(
            check_paper_intent(self.intent, state),
            (True, "approved_for_paper_only"),
        )

    def test_non_iex_feed_rejected(self):
        limits = RiskLimits(feed="sip")
        self.assertEqual(
            check_paper_intent(self.intent, self.state, limits)[1],
            "non_iex_feed_rejected",
        )

    def test_loss_limit(self):
        reason = check_paper_intent(
            self.intent, RiskState(D("0"), D("-50"), 0, 100, False, frozenset())
        )[1]
        self.assertEqual(reason, "daily_loss_limit")

    def test_notional_cap(self):
        reason = check_paper_intent(
            PaperIntent("AAPL", D("3"), D("50"), "paper-002"), self.state
        )[1]
        self.assertEqual(reason, "exposure_limit")

    def test_projected_exposure_cap(self):
        state = RiskState(D("950"), D("0"), 0, 100, False, frozenset())
        reason = check_paper_intent(
            PaperIntent("AAPL", D("2"), D("50"), "paper-003"), state
        )[1]
        self.assertEqual(reason, "exposure_limit")

    def test_position_cap(self):
        reason = check_paper_intent(
            self.intent, RiskState(D("0"), D("0"), 3, 100, False, frozenset())
        )[1]
        self.assertEqual(reason, "position_limit")

    def test_invalid_price_nan(self):
        reason = check_paper_intent(
            PaperIntent("AAPL", D("1"), D("NaN"), "paper-002"), self.state
        )[1]
        self.assertEqual(reason, "invalid_numeric_state")

    def test_zero_quantity(self):
        reason = check_paper_intent(
            PaperIntent("AAPL", D("0"), D("50"), "paper-002"), self.state
        )[1]
        self.assertEqual(reason, "invalid_quantity_or_price")

    def test_negative_exposure_state(self):
        reason = check_paper_intent(
            self.intent, RiskState(D("-1"), D("0"), 0, 100, False, frozenset())
        )[1]
        self.assertEqual(reason, "invalid_numeric_state")

    def test_invalid_position_type(self):
        reason = check_paper_intent(
            self.intent, RiskState(D("0"), D("0"), 1.5, 100, False, frozenset())  # type: ignore[arg-type]
        )[1]
        self.assertEqual(reason, "invalid_position_state")

    def test_seen_order_ids_must_be_set(self):
        reason = check_paper_intent(
            self.intent, RiskState(D("0"), D("0"), 0, 100, False, ["paper-x"])  # type: ignore[arg-type]
        )[1]
        self.assertEqual(reason, "invalid_or_duplicate_order")

    def test_forex_requires_practice_feed_and_keeps_equity_on_iex(self):
        fx = PaperIntent("EUR/USD", D("10"), D("1.08"), "fx-001")
        limits = RiskLimits(feed=FOREX_PRACTICE_FEED)
        self.assertEqual(
            check_paper_intent(fx, self.state, limits),
            (True, "approved_for_paper_only"),
        )
        self.assertEqual(
            check_paper_intent(fx, self.state)[1],
            "non_practice_forex_feed_rejected",
        )
        equity_on_fx_feed = check_paper_intent(self.intent, self.state, limits)[1]
        self.assertEqual(equity_on_fx_feed, "non_iex_feed_rejected")


if __name__ == "__main__":
    unittest.main()
