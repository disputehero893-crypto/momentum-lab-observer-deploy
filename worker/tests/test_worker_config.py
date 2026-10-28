from dataclasses import replace
import tempfile
import unittest

from momentum.config import Config, ConfigError
from momentum.alpaca import PaperClient, PaperDisabled
from worker_fakes import armed_config


class ConfigTests(unittest.TestCase):
    def test_demo_default_never_calls_transport(self):
        calls = []
        cfg = Config.from_env({})
        self.assertEqual(cfg.mode, "demo")
        self.assertFalse(cfg.paper_enabled)
        client = PaperClient(cfg, transport=lambda *args: calls.append(args))
        with self.assertRaises(PaperDisabled):
            client.account()
        self.assertEqual(calls, [])

    def test_live_mode_host_and_wildcard_rejected(self):
        for env in ({"MOMENTUM_MODE": "live"}, {"APCA_API_BASE_URL": "https://api.alpaca.markets"},
                    {"ALPACA_PAPER_TRADE": "false"}, {"CORS_ORIGIN": "*"},
                    {"STRATEGY_CONFIG_JSON": '{"mode":"live"}'},
                    {"RISK_CONFIG_JSON": '{"unknown_budget":12}'}):
            with self.subTest(env=env), self.assertRaises(ConfigError):
                Config.from_env(env)

    def test_exact_reviewed_configuration_required_and_secrets_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            self.assertEqual(cfg.paper_blockers(), [])
            changed = replace(cfg, risk_overrides={"per_trade_risk": "20"})
            self.assertIn("exact_paper_configuration_not_reviewed", changed.paper_blockers())
            self.assertNotIn("FAKE-SECRET", repr(cfg))
            self.assertNotIn("FAKE-KEY", str(cfg.public()))

