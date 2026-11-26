"""small_cap_candle_patterns_iex_v1: free-data small-cap profile (IEX + movers/news + FMP float)."""
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import unittest

from momentum import small_cap_candle_patterns_iex_v1 as cv
from momentum.risk import RiskConfig
from momentum.ross_cameron_small_cap_v1 import Catalyst, ProviderSet, ScannerRow
from momentum.models import NY
from day_trade_fixtures import DAY, at
from test_candle_patterns import FLAG, seq

P = cv.CandleIexV1Params()
SYM = "SMCP"


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


def row(symbol=SYM, last="5.10", pct="15", rvol="1.0"):
    return ScannerRow(symbol=symbol, last=Decimal(last), prior_close=Decimal("4.40"), pct_change=Decimal(pct),
                      rvol=Decimal(rvol), premarket_high=None, rank=3)


def providers(rows=None, floats=None, news=None):
    return ProviderSet(float_provider=FakeFloat(floats if floats is not None else {SYM: 8_000_000}),
                       catalyst_provider=None if news is None else FakeNews(news),
                       gainers_scanner=FakeScanner(rows if rows is not None else [row()]))


def engine(**kw):
    e = cv.SmallCapCandlePatternsIexV1(RiskConfig(), kw.pop("providers", providers()), **kw)
    e.start_session(DAY)
    return e


class GateTests(unittest.TestCase):
    def test_blockers_allow_iex_and_need_float_and_scanner(self):
        self.assertEqual(cv.provider_blockers(providers(), "iex"), [])
        self.assertEqual(cv.provider_blockers(providers(), "sip"), [])
        self.assertEqual(cv.provider_blockers(None, "iex"), ["float_provider_missing", "gainers_scanner_missing"])
        self.assertIn("data_feed_must_be_iex_or_sip", cv.provider_blockers(providers(), "delayed_sip"))
        e = cv.SmallCapCandlePatternsIexV1(RiskConfig(), None)
        e.start_session(DAY)
        self.assertFalse(e.can_trade)
        self.assertEqual(e.on_bar(seq(FLAG)[0])[0].reasons, ("profile_blocked",))

    def test_candidate_gate_uses_iex_pace_rvol(self):
        ten = at(10, 0)  # 22% of a normal day traded
        ok, reasons, info = cv.candidate_gate(row(rvol="1.0"), 8_000_000, None, ten, P)
        self.assertTrue(ok, reasons)
        self.assertGreater(Decimal(info["iex_pace_rvol"]), 4)
        ok, reasons, _ = cv.candidate_gate(row(rvol="0.5"), 8_000_000, None, ten, P)
        self.assertEqual(reasons, ["iex_pace_rvol_below_3x"])
        ok, reasons, _ = cv.candidate_gate(row(last="25", pct="8"), 30_000_000, None, ten, P)
        self.assertEqual(set(reasons), {"price_outside_2_20", "float_not_under_20m", "not_up_10pct"})
        self.assertEqual(cv.candidate_gate(row(), None, None, ten, P)[1], ["float_unknown"])

    def test_catalyst_prefer_vs_require(self):
        ten = at(10, 0)
        news = Catalyst(SYM, "SMCP wins contract", ten - timedelta(hours=2), "synthetic")
        stale = Catalyst(SYM, "old", ten - timedelta(days=3), "synthetic")
        self.assertTrue(cv.candidate_gate(row(), 8_000_000, None, ten, P)[0])
        req = replace(P, catalyst_policy="require")
        self.assertEqual(cv.candidate_gate(row(), 8_000_000, None, ten, req)[1], ["no_fresh_news_catalyst"])
        self.assertEqual(cv.candidate_gate(row(), 8_000_000, stale, ten, req)[1], ["no_fresh_news_catalyst"])
        ok, _, info = cv.candidate_gate(row(), 8_000_000, news, ten, req)
        self.assertTrue(ok and info["fresh_catalyst"])

    def test_params_validate(self):
        with self.assertRaises(ValueError):
            cv.CandleIexV1Params(catalyst_policy="maybe")
        with self.assertRaises(ValueError):
            cv.CandleIexV1Params(regular_session_only=False)
        with self.assertRaises(ValueError):
            cv.CandleIexV1Params(enabled_patterns=("head_and_shoulders",))

    def test_caps_reuse_global_limits(self):
        caps = cv.candle_caps(P, RiskConfig())
        self.assertEqual((caps.per_trade_risk, caps.daily_loss, caps.max_notional, caps.max_simultaneous),
                         (Decimal("25"), Decimal("75"), Decimal("1000"), 1))
        tighter = cv.candle_caps(P, RiskConfig(per_trade_risk=Decimal("10"), max_daily_loss=Decimal("30")))
        self.assertEqual((tighter.per_trade_risk, tighter.daily_loss), (Decimal("10"), Decimal("30")))


class EngineTests(unittest.TestCase):
    def feed(self, e, bars):
        out = []
        for b in bars:
            out.extend(e.on_bar(b))
        return out

    def test_flag_entry_then_half_at_2r_then_breakeven(self):
        e = engine()
        e.refresh_candidates(at(9, 31))
        decisions = self.feed(e, seq(FLAG))
        buys = [x for x in decisions if x.accepted]
        self.assertEqual(len(buys), 1, [x.reasons for x in decisions])
        intent = buys[0].intent
        self.assertEqual(intent["setup_class"], "first_pullback")
        self.assertEqual(intent["pattern"], "first_pullback")  # PR #69 paper_events field name
        self.assertEqual((intent["entry_limit"], intent["stop_price"], intent["target_price"]),
                         ("5.32", "5.19", "5.58"))
        self.assertEqual(intent["qty"], 166)  # $25 / (5.32 - 5.19 + 0.02)
        self.assertEqual(intent["data_feed"], "iex")
        self.assertTrue(intent["paper_only"])
        e.on_entry_filled(SYM, 166, "5.32", at(9, 37))
        up = seq([(5.32, 5.60, 5.30, 5.55, 3000)], start=(9, 37))[0]
        half = e.on_bar(up)[0]
        self.assertEqual((half.intent["reason"], half.intent["qty"]), ("first_target_half", 83))
        self.assertEqual(half.intent["pattern"], "first_pullback")
        self.assertIsNone(e.on_exit_filled(SYM, 83, "5.58", at(9, 38)))
        self.assertEqual(e.positions[SYM]["stop"], Decimal("5.32"))
        down = seq([(5.50, 5.51, 5.30, 5.35, 3000)], start=(9, 38))[0]
        out = e.on_bar(down)[0]
        self.assertEqual(out.intent["reason"], "breakeven_stop")
        self.assertEqual(e.on_exit_filled(SYM, 83, "5.32", at(9, 39)), Decimal("21.58"))
        self.assertEqual(e.counters.consecutive_losses, 0)

    def test_stop_and_three_loss_day_stop(self):
        e = engine()
        for k in range(3):
            e.pending[SYM] = {"stop": Decimal("4.90"), "entry": Decimal("5.00"), "qty": 10, "kind": "bull_flag",
                              "expires_at": at(10, 0)}
            e.on_entry_filled(SYM, 10, "5.00", at(9, 40 + k))
            e.on_exit_filled(SYM, 10, "4.90", at(9, 41 + k))
        self.assertEqual(e.counters.halted_reason, "three_consecutive_losses")
        e.refresh_candidates(at(9, 31))
        decisions = self.feed(e, seq(FLAG))
        self.assertFalse(any(x.accepted for x in decisions))
        self.assertIn(("three_consecutive_losses",), [x.reasons for x in decisions])

    def test_non_candidate_and_window_and_staleness(self):
        e = engine(providers=providers(rows=[row(rvol="0.1")]))
        e.refresh_candidates(at(9, 31))
        self.assertFalse(any(x.accepted for x in self.feed(e, seq(FLAG))))
        self.assertFalse(e.scan_log[SYM]["passed"])
        # Same flag after 11:30 ET: outside the morning entry window.
        e = engine(providers=providers(rows=[row(rvol="3.0")]))
        e.refresh_candidates(at(11, 31))
        late = seq([(5.0, 5.01, 4.99, 5.0, 100)] * 1, start=(9, 30)) + seq(FLAG, start=(11, 30))
        self.assertIn(("outside_entry_window",), [x.reasons for x in self.feed(e, late)])
        # A bar received late cannot open a trade.
        e = engine()
        e.refresh_candidates(at(9, 31))
        bars = seq(FLAG)
        bars[-1] = replace(bars[-1], received_at=bars[-1].end + timedelta(seconds=30))
        self.assertIn(("stale_bar_blocks_entry",), [x.reasons for x in self.feed(e, bars)])

    def test_thin_iex_minutes_block_entries(self):
        e = engine()
        e.refresh_candidates(at(9, 31))
        bars = seq(FLAG + FLAG[-1:])          # 8 bars, 09:30-09:37
        sparse = [bars[0], bars[1]] + [replace(b, start=b.start + timedelta(minutes=12),
                                               received_at=b.received_at + timedelta(minutes=12))
                                       for b in bars[2:7]]
        reasons = [x.reasons for x in self.feed(e, sparse)]
        self.assertIn(("iex_bar_coverage_too_thin",), reasons)

    def test_builds_five_minute_bars_from_one_minute(self):
        e = engine()
        e.refresh_candidates(at(9, 31))
        self.feed(e, seq([(5.0, 5.1, 4.9, 5.05, 100)] * 10))
        five = e.series[(SYM, "5m")].bars
        self.assertEqual([b.volume for b in five], [500, 500])
        self.assertEqual(five[1].start, at(9, 35))

    def test_five_minute_replay_mode(self):
        e = engine(params=replace(P, timeframes=("5m",)))
        e.refresh_candidates(at(9, 31))
        rows = [(5.00, 5.20, 4.95, 5.18, 10000), (5.18, 5.45, 5.15, 5.42, 14000), (5.42, 5.44, 5.30, 5.32, 5000),
                (5.32, 5.52, 5.31, 5.50, 16000)]
        decisions = []
        for b in seq(rows, minutes=5):
            decisions.extend(e.on_bar(b, timeframe="5m"))
        buys = [x for x in decisions if x.accepted]
        self.assertEqual(len(buys), 1, [x.reasons for x in decisions])
        self.assertEqual(buys[0].intent["timeframe"], "5m")

    def test_diagnostics_have_no_secrets_and_report_feed(self):
        e = engine()
        diag = e.diagnostics()
        self.assertEqual(diag["profile"], cv.PROFILE_ID)
        self.assertEqual(diag["data_feed"], "iex")
        self.assertTrue(diag["can_trade"])


if __name__ == "__main__":
    unittest.main()
