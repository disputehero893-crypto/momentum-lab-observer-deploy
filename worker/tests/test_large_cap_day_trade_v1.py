"""large_cap_day_trade_v1: every BOT/v1 rule from the ChatGPT chat, on synthetic bars."""
from dataclasses import replace
from datetime import date, time, timedelta
from decimal import Decimal
import json
import unittest
from unittest import mock

from momentum import large_cap_day_trade_v1 as lc
from momentum.day_trade_core import SessionSeries
from momentum.large_cap_day_trade_v1 import (CHOP, DOWN, NOT_READY, UP, LargeCapDayTradeV1, LargeCapV1Params,
                                             OrbSetup, SymbolState, effective_caps, orb_window, regime_at,
                                             spy_aligned, step_orb, step_vwap, vwap_window, window_label)
from momentum.risk import RiskConfig
from day_trade_fixtures import DAY, at, bar, falling, flat, orb_tail, rising, vwap_tail

P = LargeCapV1Params()


def engine(**kw):
    e = LargeCapDayTradeV1(RiskConfig(), **kw)
    e.start_session(DAY)
    return e


def feed(e, bars):
    out = []
    for b in bars:
        out.extend(e.on_bar(b))
    return out


def accepted(decisions):
    return [x for x in decisions if x.accepted and x.intent and x.intent.get("side") == "buy"]


def series(bars):
    s = SessionSeries(bars[0].symbol, DAY)
    for b in bars:
        s.add(b)
    return s


class ParamsAndCapsTests(unittest.TestCase):
    def test_spec_numbers(self):
        self.assertEqual((P.warmup_bars, P.atr_period, P.limit_ttl_seconds), (15, 14, 60))
        self.assertEqual((P.target_r, P.no_progress_r, P.max_hold_seconds), (Decimal("2"), Decimal("0.5"), 2700))
        self.assertEqual((P.max_campaigns_per_symbol, P.max_campaigns_per_day, P.disable_symbol_after_losses), (3, 6, 2))
        self.assertEqual((P.cooldown_after_exit_seconds, P.cooldown_after_loss_seconds), (600, 900))
        self.assertEqual(P.universe, ("AAPL", "MSFT", "SPY"))
        self.assertEqual(len(P.config_id), 64)

    def test_disabled_features_cannot_be_switched_on(self):
        for name in ("partials_enabled", "breakeven_enabled", "trailing_enabled", "scale_in_enabled"):
            with self.assertRaises(ValueError):
                LargeCapV1Params(**{name: True})
        with self.assertRaises(ValueError):
            LargeCapV1Params(regular_session_only=False)
        with self.assertRaises(ValueError):
            LargeCapV1Params(universe=("TSLA",))

    def test_caps_never_exceed_global_risk_config(self):
        caps = effective_caps(P, RiskConfig(), Decimal("100000"))
        self.assertEqual(caps.per_campaign_risk, Decimal("25"))
        self.assertEqual(caps.daily_loss, Decimal("75"))
        self.assertEqual(caps.symbol_notional, Decimal("1000"))   # RiskConfig.max_notional is stricter than $2,500
        self.assertEqual(caps.gross_notional, Decimal("1000"))
        self.assertEqual(caps.committed_risk, Decimal("25"))
        self.assertEqual(caps.max_simultaneous, 1)                 # main allows one open stock position
        self.assertEqual(caps.max_shares, 500)

    def test_spec_caps_win_when_global_is_looser(self):
        loose = RiskConfig(per_trade_risk=Decimal("100"), max_daily_loss=Decimal("500"), max_notional=Decimal("10000"))
        caps = effective_caps(P, loose, Decimal("250000"))
        self.assertEqual(caps.reference_equity, Decimal("100000"))  # E_ref capped at $100k
        self.assertEqual((caps.per_campaign_risk, caps.symbol_notional, caps.gross_notional),
                         (Decimal("25.00000"), Decimal("2500.000"), Decimal("5000.00")))
        self.assertEqual((caps.committed_risk, caps.daily_loss), (Decimal("50.0000"), Decimal("75.00000")))
        self.assertEqual(caps.max_simultaneous, 1)

    def test_smaller_equity_shrinks_caps(self):
        loose = RiskConfig(per_trade_risk=Decimal("100"), max_daily_loss=Decimal("500"), max_notional=Decimal("10000"))
        caps = effective_caps(P, loose, Decimal("40000"))
        self.assertEqual(caps.per_campaign_risk, Decimal("10.00000"))
        self.assertEqual(caps.daily_loss, Decimal("30.00000"))


class RegimeTests(unittest.TestCase):
    def test_up_down_chop_not_ready(self):
        up = series(rising("SPY", 20))
        self.assertEqual(regime_at(up, 13, P), NOT_READY)   # 14 bars < 15 warmup
        self.assertEqual(regime_at(up, 19, P), UP)
        self.assertEqual(regime_at(series(falling("SPY", 20)), 19, P), DOWN)
        self.assertEqual(regime_at(series(flat("SPY", 20)), 19, P), CHOP)   # zero path = chop
        zig = [bar("SPY", 9, 30 + k, 100, 100.3, 99.9, 100.2 if k % 2 else 100.0) for k in range(20)]
        self.assertEqual(regime_at(series(zig), 19, P), CHOP)
        self.assertEqual(regime_at(up, -1, P), NOT_READY)

    def test_up_needs_eight_of_ten_closes_above_vwap(self):
        bars = rising("SPY", 20)
        s = series(bars)
        self.assertEqual(regime_at(s, 19, P), UP)
        strict = replace(P, closes_vs_vwap_needed=11)
        self.assertEqual(regime_at(s, 19, strict), CHOP)
        steep = replace(P, vwap_slope_atr=Decimal("5"))
        self.assertEqual(regime_at(s, 19, steep), CHOP)


class SpyTests(unittest.TestCase):
    def test_spy_alignment_rules(self):
        spy = series(rising("SPY", 20))
        self.assertEqual(spy_aligned(spy, at(9, 49), P), (True, "spy_aligned"))
        self.assertEqual(spy_aligned(None, at(9, 49), P)[1], "spy_missing")
        self.assertEqual(spy_aligned(spy, at(9, 55), P)[1], "spy_same_minute_bar_missing")
        self.assertEqual(spy_aligned(spy, at(9, 33), P)[1], "spy_vwap_history_missing")
        self.assertEqual(spy_aligned(series(falling("SPY", 20)), at(9, 49), P)[1], "spy_below_vwap")
        stale = series(rising("SPY", 19) + [bar("SPY", 9, 49, 100.6, 100.8, 100.5, 100.7, lag=30)])
        self.assertEqual(spy_aligned(stale, at(9, 49), P)[1], "spy_bar_stale")
        # Close above VWAP but VWAP lower than 5 minutes earlier.
        drop = falling("SPY", 19, base=101) + [bar("SPY", 9, 49, 100.0, 103.0, 100.0, 102.9, v=1, vwap=100)]
        self.assertEqual(spy_aligned(series(drop), at(9, 49), P)[1], "spy_vwap_falling")


class WindowTests(unittest.TestCase):
    def test_window_table(self):
        cases = {time(9, 44): "none", time(9, 45): "orb_only", time(9, 50): "orb_or_vwap",
                 time(10, 44): "orb_or_vwap", time(10, 45): "vwap_only", time(11, 29): "vwap_only",
                 time(11, 30): "none", time(13, 29): "none", time(13, 30): "vwap_only",
                 time(14, 59): "vwap_only", time(15, 0): "none"}
        for t, label in cases.items():
            self.assertEqual(window_label(t), label, t)
        self.assertTrue(orb_window(time(10, 44)) and not orb_window(time(10, 45)))
        self.assertTrue(vwap_window(time(13, 30)) and not vwap_window(time(12, 0)))


class OrbTests(unittest.TestCase):
    def test_full_orb_retest_signal(self):
        e = engine()
        out = feed(e, rising("SPY", 15) + orb_tail("SPY"))
        intents = accepted(out)
        self.assertEqual(len(intents), 1)
        it = intents[0].intent
        self.assertEqual(it["setup_class"], "orb_retest")
        self.assertEqual((it["entry_limit"], it["stop_price"], it["target_price"]), ("100.93", "100.60", "101.59"))
        self.assertEqual(it["qty"], 9)                 # $1,000 notional cap binds
        self.assertEqual((it["mode"], it["side"], it["extended_hours"]), ("paper", "buy", False))
        self.assertEqual((it["limit_ttl_seconds"], it["profile"]), (60, "large_cap_day_trade_v1"))
        self.assertEqual(e.symbols["SPY"].orh, Decimal("100.62"))
        self.assertIn("SPY", e.pending)

    def _state(self, extra):
        st = SymbolState(series(rising("SPY", 15) + extra))
        return st

    def _run(self, st, start, regimes=None):
        events = []
        for i in range(start, len(st.series)):
            events.append(step_orb(st, i, P, (regimes or {}).get(i, UP))[1])
        return events

    def test_opening_range_needs_all_fifteen_bars(self):
        bars = rising("SPY", 15)
        del bars[3]
        st = SymbolState(series(bars + orb_tail("SPY")))
        self.assertEqual(step_orb(st, len(st.series) - 1, P, UP)[1], "opening_range_not_ready")

    def test_breakout_needs_fresh_cross_and_up_regime(self):
        st = self._state(orb_tail("SPY")[:1])
        self.assertEqual(step_orb(st, 14, P, UP)[1], "orb_idle")      # 09:44 bar ends 09:45: OR ready, no breakout
        self.assertEqual(step_orb(st, 15, P, CHOP)[1], "orb_idle")
        self.assertEqual(step_orb(st, 15, P, UP)[1], "orb_breakout")
        self.assertAlmostEqual(float(st.orb.atr0), 0.2071, places=3)

    def test_close_below_orh_minus_half_atr_cancels(self):
        st = self._state(orb_tail("SPY")[:1] + [bar("SPY", 9, 46, 100.6, 100.6, 100.40, 100.45)])
        self.assertEqual(self._run(st, 15), ["orb_breakout", "orb_cancel_close_below_orh"])

    def test_regime_down_cancels(self):
        st = self._state(orb_tail("SPY")[:2])
        self.assertEqual(self._run(st, 15, {16: DOWN}), ["orb_breakout", "orb_cancel_regime_down"])

    def test_no_retest_in_five_bars_expires(self):
        high = [bar("SPY", 9, 46 + k, 100.9, 101.0, 100.85, 100.95) for k in range(6)]
        st = self._state(orb_tail("SPY")[:1] + high)
        events = self._run(st, 15)
        self.assertEqual(events[0], "orb_breakout")
        self.assertEqual(events[-1], "orb_expired_no_retest")

    def test_confirmation_deadline_is_sixth_bar(self):
        wait = [bar("SPY", 9, 47 + k, 100.70, 100.76, 100.66, 100.70) for k in range(6)]
        st = self._state(orb_tail("SPY")[:2] + wait)
        events = self._run(st, 15)
        self.assertEqual(events[:2], ["orb_breakout", "orb_retest"])
        self.assertEqual(events[-1], "orb_expired_deadline")

    def test_retest_bar_cannot_confirm_itself(self):
        # One bar that both retests and closes high only counts as the retest.
        st = self._state(orb_tail("SPY")[:1] + [bar("SPY", 9, 46, 100.74, 101.20, 100.63, 101.15)])
        self.assertEqual(self._run(st, 15), ["orb_breakout", "orb_retest"])

    def test_window_close_cancels(self):
        st = SymbolState(series(rising("SPY", 15)))
        st.orh = Decimal("100.62")
        st.orb = OrbSetup(breakout_index=14, atr0=Decimal("0.2"), buf=Decimal("0.02"), orh=Decimal("100.62"))
        st.series.add(bar("SPY", 10, 45, 100.7, 100.8, 100.65, 100.75))
        self.assertEqual(step_orb(st, 15, P, UP)[1], "orb_cancel_window_closed")

    def test_max_one_filled_orb_per_symbol_per_day(self):
        e = engine()
        e.counters.orb_fills_by_symbol["SPY"] = 1
        out = feed(e, rising("SPY", 15) + orb_tail("SPY"))
        self.assertEqual(accepted(out), [])
        self.assertEqual(e.last_reasons["SPY"], "orb_already_filled_today")


class VwapPullbackTests(unittest.TestCase):
    def test_full_vwap_pullback_signal(self):
        e = engine()
        out = feed(e, rising("SPY", 25, step=0.04) + vwap_tail("SPY"))
        intents = accepted(out)
        self.assertEqual(len(intents), 1)
        it = intents[0].intent
        self.assertEqual(it["setup_class"], "vwap_pullback")
        self.assertEqual((it["entry_limit"], it["stop_price"]), ("100.83", "100.57"))

    def _events(self, tail, params=P):
        st = SymbolState(series(rising("SPY", 25, step=0.04) + tail))
        return [step_vwap(st, i, params, regime_at(st.series, i, P))[1] for i in range(25, len(st.series))]

    def test_entry_ceiling_skips_extended_confirmation(self):
        tail = vwap_tail("SPY")[:3] + [bar("SPY", 9, 58, 100.68, 101.0, 100.66, 100.98)]
        self.assertEqual(self._events(tail)[-1], "vwap_skip_entry_above_ceiling")

    def test_needs_a_vwap_touch(self):
        tail = [bar("SPY", 9, 55, 101.05, 101.08, 100.95, 101.00),
                bar("SPY", 9, 56, 101.0, 101.02, 100.92, 100.95),
                bar("SPY", 9, 57, 100.95, 100.97, 100.90, 100.93),
                bar("SPY", 9, 58, 100.95, 101.10, 100.94, 101.05)]
        self.assertNotIn("vwap_confirmed", self._events(tail))

    def test_close_below_vwap_band_cancels(self):
        tail = vwap_tail("SPY")[:1] + [bar("SPY", 9, 56, 100.75, 100.75, 100.30, 100.40)]
        self.assertEqual(self._events(tail)[-1], "vwap_cancel_close_below_band")

    def test_one_pullback_bar_is_not_enough(self):
        tail = [vwap_tail("SPY")[1].__class__(**{**vwap_tail("SPY")[1].__dict__, "start": at(9, 55),
                                                   "received_at": at(9, 56) + timedelta(seconds=2)}),
                bar("SPY", 9, 56, 100.68, 100.80, 100.66, 100.79)]
        self.assertNotIn("vwap_confirmed", self._events(tail))

    def test_max_lifetime_six_bars(self):
        hold = [bar("SPY", 9, 56 + k, 100.70, 100.74, 100.62, 100.68) for k in range(6)]
        events = self._events(vwap_tail("SPY")[:1] + hold)
        self.assertEqual(events[0], "vwap_pullback_start")
        self.assertIn("vwap_expired", events)
        self.assertEqual(events.index("vwap_expired"), 5)

    def test_needs_prior_extension_above_vwap(self):
        events = self._events(vwap_tail("SPY")[:1], params=replace(P, vwap_extension_atr=Decimal("10")))
        self.assertEqual(events, ["vwap_no_extension"])

    def test_regime_down_cancels_and_rearm_needs_fresh_trend(self):
        st = SymbolState(series(rising("SPY", 25, step=0.04) + vwap_tail("SPY")[:2]))
        self.assertEqual(step_vwap(st, 25, P, UP)[1], "vwap_pullback_start")
        self.assertEqual(step_vwap(st, 26, P, DOWN)[1], "vwap_cancel_regime_down")
        self.assertEqual(st.vwap_last_end, 26)
        st.series.add(bar("SPY", 9, 57, 100.66, 100.70, 100.60, 100.62))
        self.assertEqual(step_vwap(st, 27, P, UP)[1], "vwap_idle")   # bar before is the cancel bar

    def test_orb_has_priority_when_both_fire(self):
        e = engine()
        feed(e, rising("SPY", 15) + orb_tail("SPY")[:2])
        fake = {"kind": "vwap_pullback", "symbol": "SPY", "entry_limit": Decimal("100.90"),
                "stop_price": Decimal("100.60"), "atr0": Decimal("0.2"), "signal_close": Decimal("100.88")}
        with mock.patch.object(lc, "step_vwap", return_value=(fake, "vwap_confirmed")):
            out = e.on_bar(orb_tail("SPY")[2])
        self.assertEqual(accepted(out)[0].intent["setup_class"], "orb_retest")


class AdmissionTests(unittest.TestCase):
    def test_stale_bar_blocks_entry(self):
        e = engine()
        tail = orb_tail("SPY")
        tail[-1] = bar("SPY", 9, 47, 100.70, 100.90, 100.68, 100.88, lag=11)
        out = feed(e, rising("SPY", 15) + tail)
        self.assertEqual(accepted(out), [])
        self.assertEqual(e.last_reasons["SPY"], "stale_bar_blocks_entry")

    def test_bar_gap_drops_setup_no_forward_fill(self):
        e = engine()
        tail = orb_tail("SPY")
        tail[-1] = bar("SPY", 9, 48, 100.70, 100.90, 100.68, 100.88)
        out = feed(e, rising("SPY", 15) + tail)
        self.assertEqual(accepted(out), [])
        self.assertFalse(e.symbols["SPY"].orb)

    def test_aapl_needs_spy_alignment(self):
        e = engine()
        out = feed(e, rising("AAPL", 15) + orb_tail("AAPL"))
        self.assertEqual(accepted(out), [])
        self.assertEqual(e.last_reasons["AAPL"], "spy_missing")
        e = engine()
        spy = rising("SPY", 18)
        aapl = rising("AAPL", 15) + orb_tail("AAPL")
        out = []
        for s_bar, a_bar in zip(spy, aapl):
            out += e.on_bar(s_bar) + e.on_bar(a_bar)
        self.assertEqual(len(accepted(out)), 1)

    def test_outside_universe_and_session(self):
        e = engine()
        self.assertEqual(e.on_bar(bar("TSLA", 10, 0, 1, 1, 1, 1))[0].reasons, ("symbol_outside_universe",))
        self.assertEqual(e.on_bar(bar("SPY", 9, 0, 1, 1, 1, 1))[0].reasons, ("outside_regular_session",))
        self.assertEqual(e.on_bar(bar("SPY", 16, 5, 1, 1, 1, 1))[0].reasons, ("outside_regular_session",))
        e.on_bar(bar("SPY", 10, 0, 1, 1, 1, 1))
        self.assertEqual(e.on_bar(bar("SPY", 10, 0, 1, 1, 1, 1))[0].reasons, ("duplicate_or_out_of_order_bar",))
        with self.assertRaises(RuntimeError):
            LargeCapDayTradeV1(RiskConfig()).on_bar(bar("SPY", 10, 0, 1, 1, 1, 1))

    def _signal_bar(self, **kw):
        e = engine(**kw)
        for b in rising("SPY", 15) + orb_tail("SPY")[:2]:
            e.on_bar(b)
        return e

    def test_caps_cooldowns_and_disable(self):
        cases = [
            ("halted_reason", "daily_loss_limit", "daily_loss_limit"),
            ("disabled_symbols", ["SPY"], "symbol_disabled_after_losses"),
            ("cooldown_until", {"SPY": at(10, 30).isoformat()}, "symbol_cooldown"),
            ("campaigns_by_symbol", {"SPY": 3}, "symbol_campaign_cap"),
            ("campaigns_total", 6, "daily_campaign_cap"),
        ]
        for field, value, reason in cases:
            e = self._signal_bar()
            setattr(e.counters, field, value)
            out = e.on_bar(orb_tail("SPY")[2])
            self.assertEqual(accepted(out), [], reason)
            self.assertEqual(e.last_reasons["SPY"], reason)

    def test_one_active_campaign_and_simultaneous_cap(self):
        e = self._signal_bar()
        e.pending["SPY"] = {"stop_price": Decimal("1"), "risk": Decimal("0"), "notional": Decimal("0")}
        e.on_bar(orb_tail("SPY")[2])
        self.assertEqual(e.last_reasons["SPY"], "symbol_has_active_campaign")
        e = self._signal_bar()
        e.pending["MSFT"] = {"stop_price": Decimal("1"), "risk": Decimal("0"), "notional": Decimal("0")}
        e.on_bar(orb_tail("SPY")[2])
        self.assertEqual(e.last_reasons["SPY"], "simultaneous_campaign_cap")

    def test_stop_distance_band(self):
        e = self._signal_bar(params=replace(P, stop_max_atr=Decimal("1.0")))
        e.on_bar(orb_tail("SPY")[2])
        self.assertEqual(e.last_reasons["SPY"], "stop_distance_outside_atr_band")
        e = self._signal_bar(params=replace(P, stop_min_atr=Decimal("2.0")))
        e.on_bar(orb_tail("SPY")[2])
        self.assertEqual(e.last_reasons["SPY"], "stop_distance_outside_atr_band")

    def test_round_trip_cost_admission(self):
        e = self._signal_bar(params=replace(P, cost_bps_per_side=Decimal("50")))
        e.on_bar(orb_tail("SPY")[2])
        self.assertEqual(e.last_reasons["SPY"], "round_trip_cost_too_large_for_stop")

    def test_after_entry_cutoff(self):
        e = LargeCapDayTradeV1(RiskConfig())
        e.start_session(DAY, close=time(9, 50))   # synthetic close so cutoff is 09:20
        for b in rising("SPY", 15) + orb_tail("SPY"):
            e.on_bar(b)
        self.assertEqual(e.last_reasons["SPY"], "after_entry_cutoff")

    def test_zero_quantity_when_daily_loss_used_up(self):
        e = self._signal_bar()
        e.counters.realized_pnl = "-74.95"
        e.on_bar(orb_tail("SPY")[2])
        self.assertEqual(e.last_reasons["SPY"], "zero_quantity_under_limits")

    def test_sizing_uses_cost_allowance(self):
        rc = RiskConfig(per_trade_risk=Decimal("2"), max_daily_loss=Decimal("75"))
        e = LargeCapDayTradeV1(rc)
        e.start_session(DAY)
        intents = accepted(feed(e, rising("SPY", 15) + orb_tail("SPY")))
        # 2 / (0.33 + max(0.02, 0.0002 x 100.93)) = 5.71 -> 5
        self.assertEqual(intents[0].intent["qty"], 5)
        self.assertEqual(intents[0].intent["cost_allowance_per_share"], "0.020186")


class OrderAndExitTests(unittest.TestCase):
    def _filled(self, price="100.93", qty=9):
        e = engine()
        feed(e, rising("SPY", 15) + orb_tail("SPY"))
        e.on_entry_filled("SPY", qty, price, at(9, 48, ) + timedelta(seconds=10))
        return e

    def test_pending_cancels_after_60s_and_on_invalidation(self):
        e = engine()
        feed(e, rising("SPY", 15) + orb_tail("SPY"))
        self.assertEqual(e.pending_actions(at(9, 48, ) + timedelta(seconds=59)), [])
        acts = e.pending_actions(at(9, 49))
        self.assertEqual(acts[0].intent["reason"], "limit_ttl_expired")
        e = engine()
        feed(e, rising("SPY", 15) + orb_tail("SPY"))
        out = e.on_bar(bar("SPY", 9, 48, 100.7, 100.7, 100.5, 100.55))
        self.assertEqual(out[0].intent["reason"], "entry_invalidated_close_at_stop")
        e = engine()
        feed(e, rising("SPY", 15) + orb_tail("SPY"))
        e.counters.halted_reason = "daily_loss_limit"
        self.assertEqual(e.pending_actions(at(9, 48))[0].intent["reason"], "daily_loss_limit")
        e = engine()
        feed(e, rising("SPY", 15) + orb_tail("SPY"))
        e.pending["SPY"]["expires_at"] = at(15, 45)
        self.assertEqual(e.pending_actions(at(15, 31))[0].intent["reason"], "entry_window_closed")

    def test_partial_fills_cover_actual_shares(self):
        e = engine()
        feed(e, rising("SPY", 15) + orb_tail("SPY"))
        e.on_entry_filled("SPY", 4, "100.90", at(9, 48, ))
        self.assertIn("SPY", e.pending)
        e.on_entry_filled("SPY", 5, "100.93", at(9, 48, ))
        self.assertNotIn("SPY", e.pending)
        pos = e.positions["SPY"]
        self.assertEqual(pos["qty"], 9)
        exits = e.manage(at(9, 50), {"SPY": "100.0"})
        self.assertEqual(exits[0].intent["qty"], 9)
        self.assertEqual(e.counters.campaigns_total, 1)
        self.assertEqual(e.counters.orb_fills_by_symbol["SPY"], 1)
        with self.assertRaises(ValueError):
            e.on_entry_filled("MSFT", 1, "1", at(9, 48))

    def test_exit_rules(self):
        e = self._filled()
        pos = e.positions["SPY"]
        self.assertEqual(pos["r"], Decimal("0.33"))
        self.assertEqual(pos["target"], Decimal("101.59"))
        reason = lambda e, t, m: [x.intent["reason"] for x in e.manage(t, {"SPY": m}) if x.intent.get("action") == "exit"]
        self.assertEqual(reason(e, at(9, 50), "100.95"), [])
        self.assertEqual(reason(e, at(9, 50), "100.60"), ["stop"])
        self.assertEqual(reason(self._filled(), at(9, 50), "101.60"), ["target_2r"])
        self.assertEqual(reason(self._filled(), at(9, 58, ) + timedelta(seconds=10), "101.00"), ["no_progress_10m"])
        e = self._filled()
        e.manage(at(9, 52), {"SPY": "101.20"})                 # reached +0.5R once
        self.assertEqual(reason(e, at(9, 59), "101.00"), [])
        self.assertEqual(reason(e, at(10, 33, ) + timedelta(seconds=10), "101.00"), ["max_hold_45m"])
        self.assertEqual(reason(self._filled(), at(15, 50), "101.00"), ["session_flatten"])
        e = self._filled()
        e.positions["SPY"]["session_date"] = (DAY - timedelta(days=1)).isoformat()
        self.assertEqual(reason(e, at(9, 50), "101.00"), ["prior_day_liquidation"])

    def test_bar_high_counts_toward_progress(self):
        e = self._filled()
        e.on_bar(bar("SPY", 9, 49, 100.9, 101.15, 100.9, 101.0))
        self.assertEqual(e.positions["SPY"]["max_high"], Decimal("101.15"))

    def test_daily_loss_flattens_and_latches(self):
        e = self._filled()
        e.counters.realized_pnl = "-70"
        exits = e.manage(at(9, 50), {"SPY": "100.30"})         # -5.67 unrealized -> net <= -75
        self.assertEqual(exits[0].intent["reason"], "daily_loss_limit")
        self.assertEqual(e.counters.halted_reason, "daily_loss_limit")

    def test_loss_cooldown_disable_and_win_cooldown(self):
        e = self._filled()
        pnl = e.on_exit_filled("SPY", "100.60", at(9, 50))
        self.assertLess(pnl, 0)
        self.assertEqual(e.counters.cooldown_until["SPY"], at(10, 5).isoformat())
        self.assertEqual(e.counters.losses_by_symbol["SPY"], 1)
        e.pending["SPY"] = {"stop_price": Decimal("100"), "kind": "vwap_pullback", "qty": 9,
                            "risk": Decimal("1"), "notional": Decimal("1")}
        e.on_entry_filled("SPY", 9, "100.93", at(10, 10))
        e.on_exit_filled("SPY", "100.50", at(10, 12))
        self.assertIn("SPY", e.counters.disabled_symbols)
        w = self._filled()
        w.on_exit_filled("SPY", "101.59", at(9, 55))
        self.assertEqual(w.counters.cooldown_until["SPY"], at(10, 5).isoformat())
        self.assertEqual(w.counters.consecutive_losses, 0)

    def test_realized_daily_loss_latches(self):
        e = self._filled(qty=9)
        e.counters.realized_pnl = "-74"
        e.on_exit_filled("SPY", "100.60", at(9, 50))
        self.assertEqual(e.counters.halted_reason, "daily_loss_limit")

    def test_counters_survive_restart(self):
        e = self._filled()
        state = json.loads(json.dumps(e.to_state()))
        again = LargeCapDayTradeV1(RiskConfig(), state=state)
        again.start_session(DAY)
        self.assertEqual(again.counters.campaigns_total, 1)
        self.assertEqual(again.positions["SPY"]["entry"], Decimal("100.93"))
        self.assertEqual(again.manage(at(15, 50), {"SPY": "101"})[0].intent["reason"], "session_flatten")
        e2 = engine()
        feed(e2, rising("SPY", 15) + orb_tail("SPY"))
        again = LargeCapDayTradeV1(RiskConfig(), state=json.loads(json.dumps(e2.to_state())))
        self.assertEqual(again.pending["SPY"]["qty"], 9)
        nextday = LargeCapDayTradeV1(RiskConfig(), state=state)
        nextday.start_session(DAY + timedelta(days=1))
        self.assertEqual(nextday.counters.campaigns_total, 0)

    def test_diagnostics(self):
        e = self._filled()
        diag = e.diagnostics()
        self.assertEqual(diag["profile"], "large_cap_day_trade_v1")
        self.assertEqual(diag["open_positions"], ["SPY"])
        self.assertEqual(diag["session"]["flatten_at"], "15:50:00")
        self.assertIsNone(LargeCapDayTradeV1(RiskConfig()).diagnostics()["session"])
        early = LargeCapDayTradeV1(RiskConfig())
        early.start_session(date(2026, 11, 27))
        self.assertEqual(early.diagnostics()["session"]["flatten_at"], "12:50:00")


if __name__ == "__main__":
    unittest.main()
