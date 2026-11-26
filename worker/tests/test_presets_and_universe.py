"""Preset, universe and paper-blocker alignment. No broker and no network."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from momentum.config import Config
from momentum.presets import (
    LIQUID_IEX_DEFAULTS,
    LIQUID_IEX_UNIVERSE,
    LIQUID_IEX_V1_DEFAULTS,
    PRESET_LIQUID_IEX,
    PRESET_LIQUID_IEX_V1,
    PRESET_LIQUID_IEX_V2,
    PRESET_SMALL_CAP_CATALYST,
    get_preset,
)
from momentum.strategy import StrategyConfig, evaluate_setup


def _quote(symbol, feed, now):
    stamp = now.isoformat().replace("+00:00", "Z")
    return {
        "symbol": symbol,
        "mode": "paper",
        "feed": feed,
        "now": stamp,
        "session": {"is_trading_day": True, "date": "2026-10-16"},
        "quote": {
            "symbol": symbol, "bid": "180.00", "ask": "180.02", "feed": feed,
            "timestamp": stamp, "received_at": stamp,
        },
        "last_trade": {
            "symbol": symbol, "price": "180.01", "feed": feed,
            "timestamp": stamp, "received_at": stamp,
        },
        "facts": {"previous_close": "178.00"},
        "rvol": {},
        "bars": [],
    }


class PresetDefaultTests(unittest.TestCase):
    def test_default_config_is_the_liquid_iex_v2_preset(self):
        cfg = StrategyConfig()
        self.assertEqual(cfg, StrategyConfig.liquid_iex())
        self.assertEqual(cfg, StrategyConfig.liquid_iex_v2())
        self.assertEqual(cfg.preset, PRESET_LIQUID_IEX_V2)
        self.assertEqual(cfg.preset, PRESET_LIQUID_IEX)
        self.assertFalse(cfg.requires_float_catalyst)
        self.assertFalse(cfg.enable_gated_presets)
        self.assertEqual(cfg.allowed_symbols, LIQUID_IEX_UNIVERSE)
        self.assertEqual(cfg.min_price, Decimal("5"))
        self.assertGreaterEqual(cfg.max_price, Decimal("100"))
        self.assertEqual(cfg.impulse_vol_vs_same_clock_median, Decimal("1.2"))
        self.assertFalse(cfg.require_strict_rising)
        for name, value in LIQUID_IEX_DEFAULTS.items():
            self.assertEqual(getattr(cfg, name), value, name)

    def test_v1_preset_remains_intact_and_is_not_default(self):
        cfg = StrategyConfig.liquid_iex_v1()
        self.assertEqual(cfg.preset, PRESET_LIQUID_IEX_V1)
        self.assertNotEqual(cfg.config_id, StrategyConfig().config_id)
        self.assertEqual(cfg.min_impulse_mean_volume, 50_000)
        self.assertIsNone(cfg.impulse_vol_vs_same_clock_median)
        self.assertTrue(cfg.require_strict_rising)
        self.assertTrue(cfg.require_target_within_impulse_peak)
        for name, value in LIQUID_IEX_V1_DEFAULTS.items():
            self.assertEqual(getattr(cfg, name), value, name)

    def test_small_cap_preset_is_gated_and_not_the_default(self):
        spec = get_preset(PRESET_SMALL_CAP_CATALYST)
        self.assertTrue(spec.gated)
        self.assertTrue(spec.requires_float_catalyst)
        self.assertEqual(spec.required_feed, "sip")
        cfg = StrategyConfig.small_cap_catalyst()
        self.assertEqual(cfg.preset, PRESET_SMALL_CAP_CATALYST)
        self.assertTrue(cfg.requires_float_catalyst)
        self.assertFalse(cfg.enable_gated_presets)
        self.assertEqual(cfg.allowed_symbols, ())
        self.assertNotEqual(cfg.config_id, StrategyConfig().config_id)

    def test_gated_preset_rejects_before_any_setup_check(self):
        decision = evaluate_setup({"symbol": "ABCD"}, StrategyConfig.small_cap_catalyst())
        self.assertEqual(decision.reasons, ("gated_preset_disabled",))
        self.assertEqual(decision.profile, "experimental_v1")

    def test_enabled_small_cap_still_refuses_free_iex(self):
        cfg = StrategyConfig.small_cap_catalyst(enable_gated_presets=True)
        now = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)
        decision = evaluate_setup(_quote("ABCD", "iex", now), cfg)
        self.assertEqual(decision.reasons[0], "small_cap_requires_sip_feed")

    def test_liquid_rejects_symbols_and_feeds_outside_the_free_universe(self):
        now = datetime(2026, 10, 16, 14, 40, tzinfo=timezone.utc)
        outside = evaluate_setup(_quote("TSLA", "iex", now))
        self.assertEqual(outside.reasons[0], "symbol_not_in_preset_universe")
        paid = evaluate_setup(_quote("AAPL", "sip", now))
        self.assertEqual(paid.reasons[0], "liquid_preset_requires_iex")
        early = evaluate_setup(_quote("AAPL", "iex", datetime(2026, 10, 16, 13, 0, tzinfo=timezone.utc)))
        self.assertEqual(early.reasons[0], "outside_entry_window")

    def test_liquid_does_not_require_float_or_a_catalyst(self):
        now = datetime(2026, 10, 16, 14, 40, tzinfo=timezone.utc)
        decision = evaluate_setup(_quote("AAPL", "iex", now))
        self.assertTrue(decision.reasons)
        self.assertNotIn(decision.reasons[0], {
            "float_unconfirmed", "float_above_threshold", "catalyst_unconfirmed",
            "gated_preset_disabled", "symbol_not_in_preset_universe",
        })
        self.assertEqual(decision.profile, PRESET_LIQUID_IEX_V2)


class WatchlistAgreementTests(unittest.TestCase):
    def test_empty_watchlist_env_defaults_to_the_liquid_universe(self):
        for env in ({}, {"WATCHLIST": ""}, {"WATCHLIST": " , "}):
            with self.subTest(env=env):
                cfg = Config.from_env(env)
                self.assertEqual(cfg.watchlist, LIQUID_IEX_UNIVERSE)
                self.assertEqual(cfg.public()["strategy_preset"], PRESET_LIQUID_IEX)
                blockers = cfg.paper_blockers()
                self.assertNotIn("watchlist_missing", blockers)
                self.assertNotIn("watchlist_outside_strategy_universe", blockers)
                self.assertNotIn("point_in_time_symbol_metadata_missing", blockers)
                self.assertNotIn("gated_preset_disabled", blockers)

    def test_watchlist_outside_the_active_universe_is_a_paper_blocker(self):
        cfg = Config.from_env({"WATCHLIST": "AAPL,TSLA", "ALPACA_DATA_FEED": "iex"})
        self.assertIn("watchlist_outside_strategy_universe", cfg.paper_blockers())
        inside = Config.from_env({"WATCHLIST": "SPY,AAPL,MSFT"})
        self.assertNotIn("watchlist_outside_strategy_universe", inside.paper_blockers())

    def test_paid_feed_blocks_the_liquid_preset(self):
        cfg = Config.from_env({"ALPACA_DATA_FEED": "sip"})
        self.assertIn("liquid_preset_requires_iex", cfg.paper_blockers())

    def test_small_cap_gate_metadata_and_sip_block_paper_without_enabling_it(self):
        cfg = Config.from_env({"STRATEGY_CONFIG_JSON": json.dumps({
            "preset": PRESET_SMALL_CAP_CATALYST,
            "enable_gated_presets": False,
        })})
        blockers = cfg.paper_blockers()
        self.assertIn("gated_preset_disabled", blockers)
        self.assertIn("small_cap_requires_sip_feed", blockers)
        self.assertIn("point_in_time_symbol_metadata_missing", blockers)
        self.assertNotIn("watchlist_outside_strategy_universe", blockers)
        self.assertFalse(cfg.paper_enabled)
        self.assertEqual(cfg.mode, "demo")


if __name__ == "__main__":
    unittest.main()


