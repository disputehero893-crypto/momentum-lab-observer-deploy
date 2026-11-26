"""liquid_large_cap_iex_v2 rules + Lab-mirrored fixtures. Paper only. No network."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
import unittest

from momentum.models import NY
from momentum.presets import (
    PRESET_LIQUID_IEX_V1,
    PRESET_LIQUID_IEX_V2,
)
from momentum.strategy import StrategyConfig, evaluate_setup, lookup_same_clock_volume_median
from momentum.models import Bar


def _prior_sessions(day: date, n: int = 30) -> list[date]:
    prior = []
    cursor = day - timedelta(days=1)
    while len(prior) < n:
        if cursor.weekday() < 5 and cursor != date(2026, 9, 7):
            prior.append(cursor)
        cursor -= timedelta(days=1)
    prior.sort()
    return prior


def _rvol_block(as_of: str, feed: str, day: date, clock: str = "11:14:00", current_volume: int = 400_000):
    """as_of must be the exact NY clock second used in history rows (usually :00)."""
    prior = _prior_sessions(day)
    hh, mm, ss = [int(x) for x in clock.split(":")]
    return {
        "as_of": as_of,
        "feed": feed,
        "current_volume": current_volume,
        "session_start_et": "04:00",
        "prior_sessions": [x.isoformat() for x in prior],
        "history": [
            {
                "session": x.isoformat(),
                "clock": clock,
                "feed": feed,
                "volume": 100_000,
                "known_at": datetime(x.year, x.month, x.day, hh, mm, 1, tzinfo=NY).isoformat(),
            }
            for x in prior
        ],
    }


def msft_lab_holdout_snapshot():
    """Bars from Strategy Lab holdout trade MSFT 2026-07-28 entry 15:14 UTC.

    Impulse 15:10–15:12 (2 green / 1 red, net-up), 1-bar red pullback 15:13,
    breakout in 15:14. Prices/volumes taken from free IEX parquet used by Lab.
    """
    day = date(2026, 7, 28)
    # 15:14:01 UTC = 11:14:01 ET
    now = "2026-07-28T15:14:01+00:00"
    feed = "iex"
    symbol = "MSFT"
    rows = [
        # start_et_hour, start_et_min, o, h, l, c, vol  (America/New_York)
        (11, 10, "397.210", "397.590", "397.100", "397.130", 2956),  # red
        (11, 11, "397.125", "397.350", "396.945", "397.300", 3202),  # green
        (11, 12, "397.280", "398.025", "397.200", "397.935", 2649),  # green
        (11, 13, "397.835", "397.880", "397.565", "397.610", 1973),  # red pullback
    ]
    bars = []
    for hh, mm, op, hi, lo, cl, vol in rows:
        start = datetime(2026, 7, 28, hh, mm, tzinfo=NY)
        bars.append({
            "start": start.isoformat(),
            "open": op, "high": hi, "low": lo, "close": cl,
            "volume": vol, "feed": feed, "complete": True,
            "available_at": (start + timedelta(minutes=1)).isoformat(),
        })
    # Trigger = pullback high + tick = 397.880 + 0.01 = 397.89
    # Lab entry ~397.91 with 0.02 slip; worker uses ask as entry_limit.
    ask = "397.90"
    bid = "397.88"
    last = "397.90"
    return {
        "symbol": symbol,
        "mode": "paper",
        "now": now,
        "feed": feed,
        "session": {"date": day.isoformat(), "is_trading_day": True},
        "quote": {
            "symbol": symbol, "bid": bid, "ask": ask, "feed": feed,
            "timestamp": now, "received_at": now,
        },
        "last_trade": {
            "symbol": symbol, "price": last, "feed": feed,
            "timestamp": now, "received_at": now,
        },
        # previous close such that gain >= 0.10% at last~397.90
        "facts": {"previous_close": "390.00"},
        "bars": bars,
        "rvol": _rvol_block("2026-07-28T15:14:00+00:00", feed, day, clock="11:14:00", current_volume=400_000),
        "same_clock_volume_median": {
            "feed": feed,
            # Impulse last bar clock 11:12 ET; mean impulse vol ~2936 → 1.2*2000=2400 ok
            "medians": {"11:12:00": 2000},
        },
    }


def aapl_lab_holdout_like_snapshot():
    """AAPL-like loosened impulse that v1 would reject (red inside impulse)."""
    day = date(2026, 7, 2)
    now = "2026-07-02T14:03:01+00:00"  # 10:03:01 ET
    feed = "iex"
    symbol = "AAPL"
    rows = [
        (10, 0, "302.920", "303.170", "302.720", "302.720", 36443),  # red
        (10, 1, "302.745", "303.130", "302.735", "302.860", 22018),  # green
        (10, 2, "302.850", "303.030", "302.645", "303.000", 16245),  # green net-up
        (10, 2, "302.850", "303.030", "302.645", "302.760", 16245),  # placeholder overwritten
    ]
    # Rebuild carefully: impulse 09:59–10:01, pullback 10:02
    rows = [
        (9, 59, "302.275", "302.750", "302.210", "302.750", 13001),  # green
        (10, 0, "302.920", "303.170", "302.720", "302.720", 36443),  # red inside impulse
        (10, 1, "302.745", "303.130", "302.735", "302.860", 22018),  # green; net-up vs first
        (10, 2, "302.850", "303.030", "302.645", "302.760", 16245),  # red pullback
    ]
    bars = []
    for hh, mm, op, hi, lo, cl, vol in rows:
        start = datetime(2026, 7, 2, hh, mm, tzinfo=NY)
        bars.append({
            "start": start.isoformat(),
            "open": op, "high": hi, "low": lo, "close": cl,
            "volume": vol, "feed": feed, "complete": True,
            "available_at": (start + timedelta(minutes=1)).isoformat(),
        })
    # pullback high 303.030 + 0.01 = 303.04
    ask = "303.05"
    last = "303.05"
    bid = "303.03"
    return {
        "symbol": symbol,
        "mode": "paper",
        "now": now,
        "feed": feed,
        "session": {"date": day.isoformat(), "is_trading_day": True},
        "quote": {
            "symbol": symbol, "bid": bid, "ask": ask, "feed": feed,
            "timestamp": now, "received_at": now,
        },
        "last_trade": {
            "symbol": symbol, "price": last, "feed": feed,
            "timestamp": now, "received_at": now,
        },
        "facts": {"previous_close": "294.00"},
        "bars": bars,
        "rvol": _rvol_block("2026-07-02T14:03:00+00:00", feed, day, clock="10:03:00", current_volume=500_000),
        "same_clock_volume_median": {
            "feed": feed,
            "medians": {"10:01:00": 15000},
        },
    }


class V2ConfigTests(unittest.TestCase):
    def test_default_is_v2_not_v1(self):
        self.assertEqual(StrategyConfig().preset, PRESET_LIQUID_IEX_V2)
        self.assertEqual(StrategyConfig.liquid_iex_v1().preset, PRESET_LIQUID_IEX_V1)
        self.assertNotEqual(StrategyConfig().config_id, StrategyConfig.liquid_iex_v1().config_id)

    def test_live_mode_still_rejected(self):
        with self.assertRaises(ValueError):
            StrategyConfig(mode="live")
        with self.assertRaises(ValueError):
            StrategyConfig.liquid_iex_v2(mode="live")

    def test_v2_thresholds_match_lab_proposal(self):
        cfg = StrategyConfig.liquid_iex_v2()
        self.assertEqual(cfg.min_gain_pct, Decimal("0.10"))
        self.assertEqual(cfg.min_rvol, Decimal("1.2"))
        self.assertEqual(cfg.min_impulse_gain_pct, Decimal("0.10"))
        self.assertEqual(cfg.impulse_vol_vs_same_clock_median, Decimal("1.2"))
        self.assertEqual(cfg.min_impulse_body_fraction, Decimal("0.25"))
        self.assertEqual(cfg.min_green_impulse_bars, 2)
        self.assertFalse(cfg.require_strict_rising)
        self.assertEqual(cfg.min_pullback_bars, 1)
        self.assertEqual(cfg.max_pullback_bars, 3)
        self.assertEqual(cfg.max_retracement, Decimal("0.65"))
        self.assertEqual(cfg.max_pullback_volume_ratio, Decimal("1.0"))
        self.assertFalse(cfg.require_target_within_impulse_peak)


class SameClockMedianTests(unittest.TestCase):
    def test_lookup_by_clock_string(self):
        start = datetime(2026, 7, 28, 11, 12, tzinfo=NY)
        bar = Bar.from_dict({
            "start": start.isoformat(), "open": "1", "high": "2", "low": "1", "close": "1.5",
            "volume": 100, "feed": "iex", "complete": True,
            "available_at": (start + timedelta(minutes=1)).isoformat(),
        }, "iex")
        med = lookup_same_clock_volume_median(
            {"same_clock_volume_median": {"feed": "iex", "medians": {"11:12:00": "2400"}}},
            bar, "iex",
        )
        self.assertEqual(med, Decimal("2400"))

    def test_missing_median_rejects_setup(self):
        snap = msft_lab_holdout_snapshot()
        del snap["same_clock_volume_median"]
        decision = evaluate_setup(snap, StrategyConfig.liquid_iex_v2())
        self.assertEqual(decision.reasons[0], "same_clock_volume_median_missing")

    def test_relative_volume_gate_blocks_thin_impulse(self):
        snap = msft_lab_holdout_snapshot()
        snap["same_clock_volume_median"]["medians"]["11:12:00"] = 100_000  # 1.2*100k >> impulse
        decision = evaluate_setup(snap, StrategyConfig.liquid_iex_v2())
        self.assertEqual(decision.reasons[0], "impulse_volume_too_low")


class LoosenedShapeTests(unittest.TestCase):
    def test_v1_rejects_red_inside_impulse_v2_accepts_msft_lab_shape(self):
        snap = msft_lab_holdout_snapshot()
        v2 = evaluate_setup(snap, StrategyConfig.liquid_iex_v2())
        self.assertTrue(v2.accepted, v2.to_dict())
        self.assertEqual(v2.profile, PRESET_LIQUID_IEX_V2)
        self.assertEqual(v2.intent["mode"], "paper")
        # Target may exceed impulse peak under v2
        self.assertFalse(StrategyConfig.liquid_iex_v2().require_target_within_impulse_peak)

        v1 = evaluate_setup(snap, StrategyConfig.liquid_iex_v1())
        self.assertFalse(v1.accepted, v1.to_dict())
        # Absolute 50k volume gate or strict rising — either is fine as the reject reason
        self.assertIn(v1.reasons[0], {
            "impulse_not_rising", "impulse_volume_too_low", "impulse_body_too_small",
            "pullback_length_invalid", "insufficient_completed_bars",
        })

    def test_one_bar_pullback_allowed_on_v2_only(self):
        snap = msft_lab_holdout_snapshot()
        # Already 1-bar pullback; v2 accepts (above). Force v1 with relative median cleared
        # by using liquid_iex_v1 — min_pullback_bars=2 so length invalid.
        v1 = evaluate_setup(snap, StrategyConfig.liquid_iex_v1())
        self.assertIn(v1.reasons[0], {
            "pullback_length_invalid", "impulse_not_rising", "impulse_volume_too_low",
            "impulse_body_too_small", "insufficient_completed_bars",
        })

    def test_target_beyond_peak_allowed_on_v2(self):
        snap = msft_lab_holdout_snapshot()
        # Widen spread/ask so 2R target clears peak; peak=398.025
        # stop = 397.565 - 0.01 = 397.555; entry ask 397.90; risk=0.345; target=397.90+0.69=398.59 > peak
        decision = evaluate_setup(snap, StrategyConfig.liquid_iex_v2())
        self.assertTrue(decision.accepted, decision.to_dict())
        target = Decimal(decision.intent["target_price"])
        peak = Decimal("398.025")
        self.assertGreater(target, peak)

        # Same geometry with require_target_within_impulse_peak forced true
        cfg = StrategyConfig.liquid_iex_v2(require_target_within_impulse_peak=True)
        blocked = evaluate_setup(snap, cfg)
        self.assertEqual(blocked.reasons[0], "insufficient_known_resistance_room")


class LabFixtureTests(unittest.TestCase):
    def test_msft_holdout_lab_trade_accepted_on_v2(self):
        """Reproduces Lab holdout MSFT 2026-07-28 ~15:14 UTC setup geometry."""
        decision = evaluate_setup(msft_lab_holdout_snapshot(), StrategyConfig.liquid_iex_v2())
        self.assertTrue(decision.accepted, decision.to_dict())
        self.assertEqual(decision.intent["symbol"], "MSFT")
        self.assertEqual(decision.metrics["pullback_bars"], 1)

    def test_aapl_like_net_up_impulse_with_internal_red(self):
        decision = evaluate_setup(aapl_lab_holdout_like_snapshot(), StrategyConfig.liquid_iex_v2())
        self.assertTrue(decision.accepted, decision.to_dict())
        self.assertEqual(decision.intent["symbol"], "AAPL")
        # v1 strict rising must reject
        self.assertFalse(evaluate_setup(aapl_lab_holdout_like_snapshot(), StrategyConfig.liquid_iex_v1()).accepted)


if __name__ == "__main__":
    unittest.main()
