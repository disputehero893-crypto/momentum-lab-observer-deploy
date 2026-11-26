"""All day-trade profiles are OFF by default and the current reviewed hash is unchanged."""
from datetime import datetime, timezone
import unittest

from momentum import day_trade_profiles as dtp
from momentum.config import Config, ConfigError
from momentum.presets import APPROVED_PAPER_PROFILES
from momentum.risk import RiskConfig, RiskManager
from momentum.strategy import load_strategy

# Golden hashes computed on main d1f19207 before this change.
DEFAULT_HASH = "fa2211f50761a1eabf0cebc00c03f1fbc7e964d1bd58df9fcb166b5a43c4e42c"
EMPTY_ENV_HASH = "5e03eaff1ddfd826b6f92079fbce4de7eef562776f48b0accca5bbe2454308e2"
# Same shape as the reviewed hosted worker (stock watchlist + armed crypto paper), no secrets.
HOSTED_SHAPE_ENV = {"MOMENTUM_MODE": "paper", "WATCHLIST": "AAPL,MSFT,SPY",
                    "CRYPTO_WATCHLIST": "BTC/USD,ETH/USD,SOL/USD", "CRYPTO_PAPER_EXECUTION_ENABLED": "true"}
HOSTED_SHAPE_HASH = "2e443731843a50fe8b51cf520f5c4764cdafd6c2dafcf5f29131d1cfd0daaba5"
# The same hosted shape with DAY_TRADE_PROFILE=small_cap_candle_patterns_iex_v1 (NOT selected anywhere).
CANDLE_IEX_HOSTED_SHAPE_HASH = "2545caf816d02cbd839720d1790af9b91b5989c9caa0f6f4127809a107b2874c"


class OffByDefaultTests(unittest.TestCase):
    def test_profiles_off_by_default(self):
        self.assertEqual(dtp.DEFAULT_PROFILE, "")
        self.assertEqual(Config().day_trade_profile, "")
        self.assertEqual(Config.from_env({}).day_trade_profile, "")
        self.assertFalse(dtp.RUNTIME_WIRED)
        self.assertEqual(load_strategy().preset, "liquid_large_cap_iex_v2")

    def test_reviewed_hash_unchanged_for_current_default_config(self):
        self.assertEqual(Config().review_hash(), DEFAULT_HASH)
        self.assertEqual(Config.from_env({}).review_hash(), EMPTY_ENV_HASH)
        self.assertEqual(Config.from_env(HOSTED_SHAPE_ENV).review_hash(), HOSTED_SHAPE_HASH)
        for off in ("", "off", "none", " OFF "):
            self.assertEqual(Config.from_env({**HOSTED_SHAPE_ENV, "DAY_TRADE_PROFILE": off}).review_hash(),
                             HOSTED_SHAPE_HASH)

    def test_default_adds_no_blockers(self):
        cfg = Config.from_env(HOSTED_SHAPE_ENV)
        self.assertFalse([b for b in cfg.paper_blockers() if "day_trade" in b or "provider" in b])
        report = cfg.public()["day_trade_profile"]
        self.assertEqual(report["selected"], None)
        self.assertFalse(report["active"])
        self.assertEqual(report["available"], ["large_cap_day_trade_v1", "ross_cameron_small_cap_v1",
                                               "small_cap_candle_patterns_iex_v1"])

    def test_invalid_profile_rejected(self):
        with self.assertRaises(ConfigError):
            Config.from_env({"DAY_TRADE_PROFILE": "yolo"})
        with self.assertRaises(ConfigError):
            Config(day_trade_profile="Large_Cap_Day_Trade_V1")

    def test_risk_manager_refuses_profile_intents_until_approved(self):
        # Even if something handed it an intent, the unchanged RiskManager rejects the new profile ids.
        self.assertNotIn("large_cap_day_trade_v1", APPROVED_PAPER_PROFILES)
        self.assertNotIn("ross_cameron_small_cap_v1", APPROVED_PAPER_PROFILES)
        self.assertNotIn("small_cap_candle_patterns_iex_v1", APPROVED_PAPER_PROFILES)
        now = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
        account = {"session_date": "2026-11-23", "equity": "100000", "buying_power": "100000",
                   "realized_pnl": "0", "unrealized_pnl": "0", "fees": "0", "position_qty": 0,
                   "open_order_qty": 0, "as_of": now.isoformat()}
        quote = {"symbol": "SPY", "bid": "100.92", "ask": "100.93", "feed": "iex", "timestamp": now.isoformat(),
                 "received_at": now.isoformat(),
                 "bid_size": 100, "ask_size": 100}
        intent = {"symbol": "SPY", "side": "buy", "mode": "paper", "profile": "large_cap_day_trade_v1",
                  "entry_limit": "100.93", "stop_price": "100.60", "target_price": "101.59", "feed": "iex",
                  "observed_at": now.isoformat(), "liquidity_volume": 1000}
        decision = RiskManager(RiskConfig()).assess(intent, account, quote, now, connected=True, reconciled=True,
                                                    kill_switch=False, halt_new_entries=False)
        self.assertFalse(decision.accepted)
        self.assertIn("unapproved_intent_mode_or_profile", decision.reasons)


class SelectedProfileTests(unittest.TestCase):
    def test_selecting_large_cap_changes_hash_and_blocks_until_wired(self):
        cfg = Config.from_env({**HOSTED_SHAPE_ENV, "DAY_TRADE_PROFILE": "large_cap_day_trade_v1"})
        self.assertNotEqual(cfg.review_hash(), HOSTED_SHAPE_HASH)
        self.assertIn("day_trade_profile_runtime_not_wired", cfg.paper_blockers())
        block = dtp.hash_settings("large_cap_day_trade_v1", {}, "iex")
        self.assertEqual(block["entry_extended_hours"], False)
        self.assertEqual(block["effective_caps"]["per_campaign_risk"], "25.00000")
        self.assertEqual(block["effective_caps"]["symbol_notional"], "1000")
        self.assertEqual(block["universe"], ["AAPL", "MSFT", "SPY"])
        report = cfg.public()["day_trade_profile"]
        self.assertEqual(report["selected"], "large_cap_day_trade_v1")
        self.assertFalse(report["active"])

    def test_large_cap_watchlist_and_feed_rules(self):
        self.assertIn("large_cap_v1_watchlist_must_be_aapl_msft_spy",
                      dtp.blockers("large_cap_day_trade_v1", watchlist=("AAPL", "TSLA"), data_feed="iex"))
        self.assertIn("large_cap_v1_needs_spy_for_aapl_msft",
                      dtp.blockers("large_cap_day_trade_v1", watchlist=("AAPL",), data_feed="iex"))
        self.assertIn("large_cap_v1_reviewed_on_iex_only",
                      dtp.blockers("large_cap_day_trade_v1", watchlist=("SPY",), data_feed="sip"))
        self.assertEqual(dtp.blockers("", watchlist=("SPY",)), [])

    def test_selecting_ross_reports_every_missing_data_source(self):
        cfg = Config.from_env({**HOSTED_SHAPE_ENV, "DAY_TRADE_PROFILE": "ross_cameron_small_cap_v1"})
        blockers = cfg.paper_blockers()
        for reason in ("day_trade_profile_runtime_not_wired", "float_provider_missing", "catalyst_provider_missing",
                       "gainers_scanner_missing", "requires_sip_or_full_tape_feed"):
            self.assertIn(reason, blockers)
        self.assertNotEqual(cfg.review_hash(), HOSTED_SHAPE_HASH)
        self.assertIn("float_provider_missing", cfg.public()["day_trade_profile"]["blockers"])

    def test_selecting_candle_iex_profile_allows_iex_and_needs_float_and_scanner(self):
        cfg = Config.from_env({**HOSTED_SHAPE_ENV, "DAY_TRADE_PROFILE": "small_cap_candle_patterns_iex_v1"})
        self.assertEqual(cfg.data_feed, "iex")
        blockers = cfg.paper_blockers()
        for reason in ("day_trade_profile_runtime_not_wired", "float_provider_missing", "gainers_scanner_missing"):
            self.assertIn(reason, blockers)
        for reason in ("catalyst_provider_missing", "requires_sip_or_full_tape_feed", "data_feed_must_be_iex_or_sip"):
            self.assertNotIn(reason, blockers)
        self.assertEqual(cfg.review_hash(), CANDLE_IEX_HOSTED_SHAPE_HASH)
        block = dtp.hash_settings("small_cap_candle_patterns_iex_v1", {}, "iex")
        self.assertEqual((block["universe"], block["feed"]), (["alpaca_movers_scanner"], "iex"))

    def test_profiles_hash_differently_and_follow_risk_config(self):
        large = Config.from_env({**HOSTED_SHAPE_ENV, "DAY_TRADE_PROFILE": "large_cap_day_trade_v1"}).review_hash()
        ross = Config.from_env({**HOSTED_SHAPE_ENV, "DAY_TRADE_PROFILE": "ross_cameron_small_cap_v1"}).review_hash()
        self.assertNotEqual(large, ross)
        a = dtp.hash_settings("large_cap_day_trade_v1", {}, "iex")
        b = dtp.hash_settings("large_cap_day_trade_v1", {"per_trade_risk": "10"}, "iex")
        self.assertNotEqual(a["risk_config_id"], b["risk_config_id"])
        self.assertEqual(b["effective_caps"]["per_campaign_risk"], "10")
        with self.assertRaises(ValueError):
            dtp.hash_settings("", {}, "iex")
        self.assertEqual(dtp.hash_settings("ross_cameron_small_cap_v1", {}, "sip")["universe"], ["full_market_scanner"])


if __name__ == "__main__":
    unittest.main()
