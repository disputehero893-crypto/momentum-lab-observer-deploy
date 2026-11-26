"""ross_cameron_small_cap_v1: Ross Cameron small-cap rules, hard-gated on data we do not have."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import unittest

from momentum.day_trade_core import SessionSeries
from momentum.risk import RiskConfig
from momentum.ross_cameron_small_cap_v1 import (Catalyst, ProviderSet, RossCameronSmallCapV1, RossV1Params,
                                                ScannerRow, detect_bull_flag, detect_gap_and_go,
                                                five_factor_gate, provider_blockers, ross_caps)
from day_trade_fixtures import DAY, at, bar

P = RossV1Params()
NOW = at(9, 29)


class FakeFloat:
    name = "synthetic-float"

    def __init__(self, shares):
        self.shares = shares

    def float_shares(self, symbol, as_of):
        return self.shares.get(symbol)


class FakeNews:
    name = "synthetic-news"

    def __init__(self, items):
        self.items = items

    def latest_catalyst(self, symbol, as_of):
        return self.items.get(symbol)


class FakeScanner:
    name = "synthetic-scanner"

    def __init__(self, rows):
        self.rows = rows

    def scan(self, as_of):
        return list(self.rows)


def row(symbol="ABCD", last="5.50", prior="5.00", pct="10", rvol="6", pmh="5.80", rank=1):
    return ScannerRow(symbol=symbol, last=Decimal(last), prior_close=Decimal(prior), pct_change=Decimal(pct),
                      rvol=Decimal(rvol), premarket_high=None if pmh is None else Decimal(pmh), rank=rank)


def news(symbol="ABCD", hours=2):
    return Catalyst(symbol, "Synthetic FDA headline", NOW - timedelta(hours=hours), "synthetic")


def providers(rows=None, floats=None, items=None):
    return ProviderSet(FakeFloat(floats or {"ABCD": 8_000_000}),
                       FakeNews(items if items is not None else {"ABCD": news()}),
                       FakeScanner(rows or [row()]))


def armed(rows=None, **kw):
    e = RossCameronSmallCapV1(RiskConfig(), providers(rows), data_feed="sip", **kw)
    e.start_session(DAY)
    e.refresh_candidates(NOW)
    return e


def series(bars):
    s = SessionSeries(bars[0].symbol, DAY)
    for b in bars:
        s.add(b)
    return s


def doji(n, start=(9, 30), price=5.01):
    return [bar("ABCD", start[0], start[1] + k, price, price + 0.04, price - 0.03, price) for k in range(n)]


FLAG = [bar("ABCD", 10, 10, 5.05, 5.25, 5.04, 5.24), bar("ABCD", 10, 11, 5.24, 5.45, 5.23, 5.44),
        bar("ABCD", 10, 12, 5.44, 5.60, 5.43, 5.58), bar("ABCD", 10, 13, 5.58, 5.59, 5.50, 5.52),
        bar("ABCD", 10, 14, 5.52, 5.54, 5.45, 5.47), bar("ABCD", 10, 15, 5.47, 5.62, 5.46, 5.60)]


class GateTests(unittest.TestCase):
    def test_hard_gate_without_providers(self):
        self.assertEqual(provider_blockers(None, "iex"), ["float_provider_missing", "catalyst_provider_missing",
                                                          "gainers_scanner_missing", "requires_sip_or_full_tape_feed"])
        self.assertEqual(provider_blockers(providers(), "sip"), [])
        self.assertEqual(provider_blockers(ProviderSet(float_provider=object()), "sip")[0], "float_provider_missing")

    def test_engine_refuses_to_trade_and_says_why(self):
        e = RossCameronSmallCapV1(RiskConfig())
        e.start_session(DAY)
        self.assertFalse(e.can_trade)
        self.assertEqual(e.refresh_candidates(NOW), {})
        out = e.on_bar(bar("ABCD", 9, 31, 5, 5.9, 5, 5.85))
        self.assertFalse(out[0].accepted)
        self.assertEqual(out[0].reasons, ("profile_blocked",))
        self.assertIn("float_provider_missing", out[0].metrics["blockers"])
        self.assertFalse(e.diagnostics()["can_trade"])
        iex = RossCameronSmallCapV1(RiskConfig(), providers(), data_feed="iex")
        self.assertEqual(iex.blockers, ["requires_sip_or_full_tape_feed"])

    def test_five_factor_gate(self):
        ok, reasons, info = five_factor_gate(row(), 8_000_000, news(), NOW, P)
        self.assertTrue(ok)
        self.assertTrue(info["top_gainer_preferred"])
        cases = [(row(last="1.50"), 8_000_000, news(), "price_outside_2_20"),
                 (row(last="25"), 8_000_000, news(), "price_outside_2_20"),
                 (row(), 20_000_000, news(), "float_not_under_20m"),
                 (row(), None, news(), "float_unknown"),
                 (row(rvol="4.9"), 8_000_000, news(), "rvol_below_5x"),
                 (row(pct="9.9"), 8_000_000, news(), "not_up_10pct"),
                 (row(), 8_000_000, None, "no_news_catalyst"),
                 (row(), 8_000_000, news(hours=30), "catalyst_not_fresh"),
                 (row(), 8_000_000, news(hours=-1), "catalyst_not_fresh")]
        for r, flt, cat, reason in cases:
            ok, reasons, _ = five_factor_gate(r, flt, cat, NOW, P)
            self.assertFalse(ok, reason)
            self.assertIn(reason, reasons)
        self.assertFalse(five_factor_gate(row(rank=9), 1, news(), NOW, P)[2]["top_gainer_preferred"])

    def test_refresh_candidates_applies_gate(self):
        e = RossCameronSmallCapV1(RiskConfig(), providers(rows=[row(), row("WXYZ")]), data_feed="sip")
        e.start_session(DAY)
        log = e.refresh_candidates(NOW)
        self.assertTrue(log["ABCD"]["passed"])
        self.assertFalse(log["WXYZ"]["passed"])     # no float, no news
        self.assertEqual(sorted(e.candidates), ["ABCD"])

    def test_caps_clamped_by_global_risk(self):
        caps = ross_caps(P, RiskConfig())
        self.assertEqual((caps.per_trade_risk, caps.daily_loss, caps.max_notional, caps.max_simultaneous),
                         (Decimal("25"), Decimal("75"), Decimal("1000"), 1))
        loose = ross_caps(replace(P, per_trade_risk=Decimal("500"), daily_max_loss=Decimal("1000")), RiskConfig())
        self.assertEqual((loose.per_trade_risk, loose.daily_loss), (Decimal("25"), Decimal("75")))

    def test_params_validation(self):
        with self.assertRaises(ValueError):
            RossV1Params(regular_session_only=False)
        with self.assertRaises(ValueError):
            RossV1Params(min_price=Decimal("20"), max_price=Decimal("2"))


class GapAndGoTests(unittest.TestCase):
    def test_premarket_high_break(self):
        bars = [bar("ABCD", 9, 30, 5.50, 5.75, 5.45, 5.70), bar("ABCD", 9, 31, 5.70, 5.90, 5.68, 5.85)]
        sig = detect_gap_and_go(series(bars), 1, row(), P)
        self.assertEqual(sig["kind"], "gap_and_go_pmh_break")
        self.assertEqual((sig["trigger"], sig["stop_low"]), (Decimal("5.81"), Decimal("5.68")))

    def test_one_minute_orb_stop_at_first_candle_low(self):
        bars = [bar("ABCD", 9, 30, 5.50, 5.75, 5.45, 5.70), bar("ABCD", 9, 31, 5.70, 5.80, 5.68, 5.78)]
        sig = detect_gap_and_go(series(bars), 1, row(pmh=None), P)
        self.assertEqual(sig["kind"], "gap_and_go_1m_orb")
        self.assertEqual(sig["stop_low"], Decimal("5.45"))

    def test_gap_window_and_session_start(self):
        bars = [bar("ABCD", 9, 30, 5.10, 5.75, 5.05, 5.70), bar("ABCD", 9, 31, 5.70, 5.90, 5.68, 5.85)]
        self.assertIsNone(detect_gap_and_go(series(bars), 1, row(), P))      # gap 2% <= 4%
        late = doji(30) + [bar("ABCD", 10, 0, 5.70, 5.95, 5.68, 5.90)]
        self.assertIsNone(detect_gap_and_go(series(late), 30, row(pmh="5.80", prior="4.50"), P))  # after 10:00
        no_open = [bar("ABCD", 9, 31, 5.50, 5.75, 5.45, 5.70)]
        self.assertIsNone(detect_gap_and_go(series(no_open), 0, row(), P))
        flat = [bar("ABCD", 9, 30, 5.50, 5.75, 5.45, 5.70), bar("ABCD", 9, 31, 5.70, 5.74, 5.60, 5.72)]
        self.assertIsNone(detect_gap_and_go(series(flat), 1, row(), P))


class BullFlagTests(unittest.TestCase):
    def test_bull_flag_break(self):
        s = series(doji(40) + FLAG)
        sig, why = detect_bull_flag(s, len(s) - 1, P)
        self.assertEqual(why, "bull_flag_break")
        self.assertEqual((sig["trigger"], sig["stop_low"], sig["red_candles"]), (Decimal("5.55"), Decimal("5.45"), 2))
        self.assertLessEqual(sig["retrace"], Decimal("0.5"))

    def test_bull_flag_rejections(self):
        def why(bars):
            s = series(bars)
            return detect_bull_flag(s, len(s) - 1, P)[1]
        self.assertEqual(why(doji(40) + FLAG[:4] + [FLAG[5]]), "bull_flag_no_pullback")
        extra = [bar("ABCD", 10, 14, 5.52, 5.53, 5.49, 5.50), bar("ABCD", 10, 15, 5.50, 5.51, 5.47, 5.48),
                 bar("ABCD", 10, 16, 5.48, 5.49, 5.46, 5.47), bar("ABCD", 10, 17, 5.47, 5.62, 5.46, 5.60)]
        self.assertEqual(why(doji(40) + FLAG[:4] + extra), "bull_flag_too_many_red_candles")
        deep = FLAG[:3] + [bar("ABCD", 10, 13, 5.58, 5.59, 5.30, 5.32), bar("ABCD", 10, 14, 5.32, 5.33, 5.20, 5.22),
                           bar("ABCD", 10, 15, 5.22, 5.40, 5.21, 5.38)]
        self.assertEqual(why(doji(40) + deep), "bull_flag_retrace_over_50pct")
        self.assertEqual(why(doji(40) + [FLAG[0]] + [bar("ABCD", 10, 11, 5.24, 5.25, 5.20, 5.21),
                                                        bar("ABCD", 10, 12, 5.21, 5.22, 5.19, 5.20),
                                                        bar("ABCD", 10, 13, 5.20, 5.30, 5.19, 5.28)]), "bull_flag_no_impulse")
        small = [bar("ABCD", 10, 10, 5.01, 5.03, 5.00, 5.02), bar("ABCD", 10, 11, 5.02, 5.04, 5.01, 5.03),
                 bar("ABCD", 10, 12, 5.03, 5.04, 5.02, 5.025), bar("ABCD", 10, 13, 5.025, 5.03, 5.02, 5.022),
                 bar("ABCD", 10, 14, 5.022, 5.05, 5.02, 5.04)]
        self.assertEqual(why(doji(40) + small), "bull_flag_impulse_too_small")
        self.assertEqual(why(doji(40) + FLAG[:5] + [bar("ABCD", 10, 15, 5.47, 5.53, 5.46, 5.50)]), "bull_flag_waiting_break")
        early = [bar("ABCD", 9, 30, 5.05, 5.25, 5.04, 5.24)]
        self.assertEqual(detect_bull_flag(series(early), 0, replace(P, bull_flag_start="09:45"))[1], "bull_flag_outside_window")

    def test_pullback_breaking_vwap_rejected(self):
        high = [bar("ABCD", 9, 30 + k, 6.0, 6.04, 5.97, 6.0, v=100000) for k in range(40)]
        self.assertEqual(detect_bull_flag(series(high + FLAG), 45, P)[1], "bull_flag_broke_vwap")

    def test_ema9_break_is_only_a_warning(self):
        s = series(doji(40) + FLAG)
        sig, _ = detect_bull_flag(s, len(s) - 1, P)
        self.assertIn("ema9_break_warning", sig)


class EngineTests(unittest.TestCase):
    def _flag_engine(self):
        e = armed()
        out = []
        for b in doji(40) + FLAG:
            out = e.on_bar(b)
        return e, out

    def test_bull_flag_entry_intent(self):
        e, out = self._flag_engine()
        self.assertTrue(out[0].accepted, out[0].reasons)
        it = out[0].intent
        self.assertEqual((it["entry_limit"], it["stop_price"], it["target_price"]), ("5.60", "5.44", "5.92"))
        self.assertEqual(it["qty"], 138)        # 25 / (0.16 + 0.02)
        self.assertEqual((it["mode"], it["extended_hours"], it["profile"]), ("paper", False, "ross_cameron_small_cap_v1"))

    def test_gap_and_go_entry(self):
        e = armed()
        e.on_bar(bar("ABCD", 9, 30, 5.50, 5.75, 5.45, 5.70))
        out = e.on_bar(bar("ABCD", 9, 31, 5.70, 5.90, 5.68, 5.85))
        self.assertEqual(out[0].intent["setup_class"], "gap_and_go_pmh_break")
        self.assertEqual(out[0].intent["qty"], 125)   # 25 / (5.85 - 5.67 + 0.02)

    def test_rejections(self):
        e = armed()
        self.assertEqual(e.on_bar(bar("WXYZ", 9, 30, 5, 5.1, 4.9, 5))[0].reasons, ("not_a_five_factor_candidate",))
        self.assertEqual(e.on_bar(bar("ABCD", 9, 0, 5, 5.1, 4.9, 5))[0].reasons, ("outside_regular_session",))
        e.on_bar(bar("ABCD", 9, 30, 5.50, 5.75, 5.45, 5.70))
        self.assertEqual(e.on_bar(bar("ABCD", 9, 30, 5.50, 5.75, 5.45, 5.70))[0].reasons, ("duplicate_or_out_of_order_bar",))
        with self.assertRaises(RuntimeError):
            RossCameronSmallCapV1(RiskConfig(), providers(), data_feed="sip").on_bar(bar("ABCD", 9, 30, 5, 5, 5, 5))

    def _admit(self, signal, b=None, **counters):
        e = armed()
        for k, v in counters.items():
            setattr(e.counters, k, v)
        return e, e.admit(signal, b or bar("ABCD", 10, 15, 5.47, 5.62, 5.46, 5.60))

    def test_admission_rules(self):
        sig = {"kind": "bull_flag", "trigger": Decimal("5.55"), "stop_low": Decimal("5.45")}
        self.assertEqual(self._admit(sig, halted_reason="three_consecutive_losses")[1].reasons, ("three_consecutive_losses",))
        self.assertEqual(self._admit(sig, b=bar("ABCD", 10, 15, 5.47, 5.62, 5.46, 5.60, lag=20))[1].reasons,
                         ("stale_bar_blocks_entry",))
        self.assertEqual(self._admit(sig, b=bar("ABCD", 15, 31, 5.47, 5.62, 5.46, 5.60))[1].reasons, ("after_entry_cutoff",))
        self.assertEqual(self._admit(sig, campaigns_total=5)[1].reasons, ("daily_trade_cap",))
        wide = {"kind": "bull_flag", "trigger": Decimal("5.55"), "stop_low": Decimal("5.20")}
        self.assertEqual(self._admit(wide)[1].reasons, ("stop_wider_than_max",))
        bad = {"kind": "bull_flag", "trigger": Decimal("5.00"), "stop_low": Decimal("5.45")}
        self.assertEqual(self._admit(bad)[1].reasons, ("invalid_stop",))
        chase = {"kind": "bull_flag", "trigger": Decimal("5.50"), "stop_low": Decimal("5.45")}
        self.assertEqual(self._admit(chase)[1].reasons, ("entry_extended_past_trigger",))
        pricey = {"kind": "bull_flag", "trigger": Decimal("20.05"), "stop_low": Decimal("19.95")}
        self.assertEqual(self._admit(pricey, b=bar("ABCD", 10, 15, 20, 20.1, 19.9, 20.05))[1].reasons, ("price_outside_2_20",))
        self.assertEqual(self._admit(sig, realized_pnl="-75")[1].reasons, ("zero_quantity_under_limits",))
        e, first = self._admit(sig)
        self.assertTrue(first.accepted)
        self.assertEqual(e.admit(sig, bar("ABCD", 10, 16, 5.47, 5.62, 5.46, 5.60)).reasons, ("one_position_at_a_time",))

    def test_half_at_first_target_then_stop_to_entry_then_first_red(self):
        e, _ = self._flag_engine()
        e.on_entry_filled("ABCD", 138, "5.60", at(10, 16))
        out = e.on_bar(bar("ABCD", 10, 16, 5.60, 5.95, 5.58, 5.93))
        self.assertEqual((out[0].intent["reason"], out[0].intent["qty"]), ("first_target_half", 69))
        self.assertIsNone(e.on_exit_filled("ABCD", 69, "5.92", at(10, 17)))
        self.assertEqual(e.positions["ABCD"]["stop"], Decimal("5.60"))
        out = e.on_bar(bar("ABCD", 10, 17, 5.93, 5.96, 5.80, 5.85))
        self.assertEqual((out[0].intent["reason"], out[0].intent["qty"]), ("first_red_candle", 69))
        pnl = e.on_exit_filled("ABCD", 69, "5.85", at(10, 18))
        self.assertGreater(pnl, 0)
        self.assertEqual(e.counters.consecutive_losses, 0)

    def test_first_red_candle_before_target_and_stops(self):
        e, _ = self._flag_engine()
        e.on_entry_filled("ABCD", 138, "5.60", at(10, 16))
        self.assertEqual(e.on_bar(bar("ABCD", 10, 16, 5.62, 5.63, 5.55, 5.57))[0].intent["reason"], "first_red_candle")
        e, _ = self._flag_engine()
        e.on_entry_filled("ABCD", 138, "5.60", at(10, 16))
        self.assertEqual(e.on_bar(bar("ABCD", 10, 16, 5.60, 5.95, 5.40, 5.90))[0].intent["reason"], "stop")
        e, _ = self._flag_engine()
        e.on_entry_filled("ABCD", 1, "5.60", at(10, 16))
        self.assertEqual(e.on_bar(bar("ABCD", 10, 16, 5.60, 5.95, 5.58, 5.93))[0].intent["reason"], "first_target_full")
        e, _ = self._flag_engine()
        e.on_entry_filled("ABCD", 138, "5.60", at(10, 16))
        e.on_exit_filled("ABCD", 69, "5.92", at(10, 17))
        self.assertEqual(e.on_bar(bar("ABCD", 10, 17, 5.70, 5.72, 5.59, 5.71))[0].intent["reason"], "breakeven_stop")
        e, _ = self._flag_engine()
        e.on_entry_filled("ABCD", 138, "5.60", at(10, 16))
        held = e.on_bar(bar("ABCD", 10, 16, 5.60, 5.70, 5.59, 5.65))   # green, above stop: keep holding
        self.assertFalse(held[0].accepted)
        self.assertIn("ABCD", e.positions)

    def test_three_consecutive_losses_stop_the_day(self):
        e = armed()
        for k in range(3):
            e.pending["ABCD"] = {"stop": Decimal("5.44"), "entry": Decimal("5.60"), "qty": 10,
                                 "expires_at": at(10, 20)}
            e.on_entry_filled("ABCD", 10, "5.60", at(10, 16 + k))
            e.on_exit_filled("ABCD", 10, "5.50", at(10, 17 + k))
        self.assertEqual(e.counters.consecutive_losses, 3)
        self.assertEqual(e.counters.halted_reason, "three_consecutive_losses")

    def test_daily_loss_and_session_flatten(self):
        e = armed()
        e.pending["ABCD"] = {"stop": Decimal("5.44"), "entry": Decimal("5.60"), "qty": 100, "expires_at": at(10, 20)}
        e.on_entry_filled("ABCD", 100, "5.60", at(10, 16))
        self.assertEqual(e.manage(at(15, 50), {"ABCD": "5.61"})[0].intent["reason"], "session_flatten")
        self.assertEqual(e.manage(at(10, 30), {"ABCD": "4.80"})[0].intent["reason"], "daily_loss_limit")
        e2 = armed()
        e2.pending["ABCD"] = {"stop": Decimal("5.44"), "entry": Decimal("5.60"), "qty": 100, "expires_at": at(10, 20)}
        e2.on_entry_filled("ABCD", 100, "5.60", at(10, 16))
        e2.on_exit_filled("ABCD", 100, "4.80", at(10, 17))
        self.assertEqual(e2.counters.halted_reason, "daily_loss_limit")

    def test_pending_ttl(self):
        e, _ = self._flag_engine()
        self.assertEqual(e.pending_actions(at(10, 16, ) + timedelta(seconds=30)), [])
        self.assertEqual(e.pending_actions(at(10, 17))[0].intent["action"], "cancel_entry")
        self.assertEqual(e.diagnostics()["candidates"], ["ABCD"])


if __name__ == "__main__":
    unittest.main()
