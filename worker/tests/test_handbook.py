"""Handbook rules: structure, Derek-first order, code cross-checks and prompt summary."""
import copy
import json
from decimal import Decimal
from pathlib import Path
import unittest

from momentum.handbook import (HANDBOOK_PATH, HandbookError, audit_table, handbook_prompt_summary,
                               load_handbook, parse_handbook)

WORKER = Path(__file__).resolve().parents[1]
REPO = WORKER.parent


def raw():
    return json.loads(HANDBOOK_PATH.read_text(encoding="utf-8"))


class HandbookStructureTests(unittest.TestCase):
    def test_loads_and_has_both_origins(self):
        book = load_handbook()
        self.assertTrue(book.version)
        self.assertIn("Not financial advice", book.disclaimer)
        self.assertGreaterEqual(len(book.rules_for(origin="derek")), 20)
        self.assertGreaterEqual(len(book.rules_for(origin="research")), 10)
        self.assertEqual(len(book.digest), 64)

    def test_derek_rules_come_first_and_cite_derek_sources(self):
        book = load_handbook()
        origins = [r.origin for r in book.rules]
        first_research = origins.index("research")
        self.assertTrue(all(o == "derek" for o in origins[:first_research]))
        self.assertTrue(all(o == "research" for o in origins[first_research:]))
        for rule in book.rules_for(origin="derek"):
            self.assertTrue(any(book.sources[s]["kind"] == "derek" for s in rule.sources), rule.id)

    def test_every_research_rule_cites_an_external_or_lab_source(self):
        book = load_handbook()
        for rule in book.rules_for(origin="research"):
            self.assertTrue(any(book.sources[s]["kind"] != "derek" for s in rule.sources), rule.id)

    def test_every_source_is_used(self):
        book = load_handbook()
        used = {s for r in book.rules for s in r.sources}
        self.assertEqual(set(book.sources) - used, set())

    def test_code_locations_exist(self):
        book = load_handbook()
        for rule in book.rules:
            if rule.in_code:
                self.assertTrue(rule.enforced_in, rule.id)
            for location in rule.enforced_in:
                path, symbol = location.split(":", 1)
                source = REPO / path
                self.assertTrue(source.is_file(), f"{rule.id}: {path}")
                needle = symbol.split(".")[-1]
                self.assertIn(needle, source.read_text(encoding="utf-8"), f"{rule.id}: {location}")

    def test_audit_table_rows(self):
        rows = audit_table()
        self.assertEqual(len(rows), len(load_handbook().rules))
        self.assertEqual(rows[0]["id"], "DR-01")
        self.assertEqual({"id", "title", "origin", "market", "status", "default", "enforced_in", "note"}, set(rows[0]))


class HandbookMatchesCodeTests(unittest.TestCase):
    """If one of these fails, the code changed: update handbook_rules.json and the docs."""

    def test_stock_risk_defaults(self):
        from momentum.risk import RiskConfig
        cfg = RiskConfig()
        self.assertEqual(cfg.per_trade_risk, Decimal("25"))       # DR-11
        self.assertEqual(cfg.max_daily_loss, Decimal("75"))       # DR-12
        self.assertLessEqual(cfg.max_notional, Decimal("2500"))   # DR-13 enforced_stricter
        self.assertEqual(cfg.execution_allowance_per_share, Decimal("0.02"))  # DR-15 partial
        self.assertGreaterEqual(cfg.min_reward_r, Decimal("2"))  # DR-17

    def test_stock_strategy_defaults(self):
        from momentum.strategy import StrategyConfig
        cfg = StrategyConfig()
        self.assertEqual(cfg.target_r, Decimal("2"))              # DR-17
        self.assertEqual(cfg.trade_start_et, "09:35")             # DR-07 differs
        self.assertEqual(cfg.trade_end_et, "15:45")               # DR-05 partial, DR-08 not on main
        self.assertEqual(cfg.max_hold_seconds, 600)               # DR-18 differs
        self.assertFalse(cfg.requires_float_catalyst)             # DR-19
        self.assertEqual(cfg.min_rvol, Decimal("1.2"))            # DR-19 differs

    def test_extended_hours_flag_matches_note(self):
        text = (WORKER / "momentum" / "alpaca.py").read_text(encoding="utf-8")
        self.assertIn('"extended_hours": True', text)              # DR-06 partial

    def test_forex_and_crypto_limits(self):
        from momentum.forex_risk import ForexRiskConfig
        from momentum.paper_risk import RiskLimits
        self.assertEqual(ForexRiskConfig().max_effective_leverage, Decimal("2.0"))  # RG-11
        self.assertTrue(ForexRiskConfig().require_stop_loss)                         # RG-02
        self.assertEqual(RiskLimits().max_daily_loss, Decimal("50"))                 # RG-04

    def test_handbook_is_not_wired_into_trading_or_the_hash(self):
        for name in ("config.py", "risk.py", "strategy.py", "session.py", "paper_risk.py",
                     "forex_risk.py", "crypto_paper_strategy.py", "crypto_paper_loop.py", "presets.py"):
            text = (WORKER / "momentum" / name).read_text(encoding="utf-8")
            self.assertNotIn("handbook", text, name)


class HandbookPromptTests(unittest.TestCase):
    def test_summary_orders_derek_first_and_names_limits(self):
        text = handbook_prompt_summary(markets=("stocks",))
        self.assertIn("Not financial advice", text)
        self.assertIn("paper trading only", text)
        self.assertLess(text.index("[DR-01]"), text.index("[RG-01]"))
        self.assertIn("[DR-12]", text)
        self.assertNotIn("[RG-11]", text)   # forex-only
        self.assertNotIn("[RG-13]", text)   # crypto-only
        self.assertNotIn("omitted", text)
        self.assertLessEqual(len(text), 7000)

    def test_market_filters(self):
        crypto = handbook_prompt_summary(markets=("crypto",))
        self.assertIn("[RG-13]", crypto)
        self.assertNotIn("[DR-12]", crypto)
        self.assertIn("[DR-01]", crypto)    # market "all"
        forex = handbook_prompt_summary(markets=("forex",))
        self.assertIn("[RG-11]", forex)

    def test_summary_respects_max_chars(self):
        for limit in (800, 1200, 2000):
            text = handbook_prompt_summary(max_chars=limit)
            self.assertLessEqual(len(text), limit)
            self.assertIn("omitted for length", text)

    def test_bad_arguments(self):
        with self.assertRaises(HandbookError):
            handbook_prompt_summary(max_chars=500)
        with self.assertRaises(HandbookError):
            handbook_prompt_summary(max_chars="5000")
        with self.assertRaises(HandbookError):
            handbook_prompt_summary(markets=("options",))

    def test_non_prompt_rules_are_left_out(self):
        text = handbook_prompt_summary()
        self.assertNotIn("[DR-15]", text)
        self.assertNotIn("[RG-15]", text)


class DayTradeProfileRuleTests(unittest.TestCase):
    """Rules now implemented in the off-by-default day-trade profiles."""

    def test_profile_rules_are_located_but_not_counted_as_main(self):
        book = load_handbook()
        for rid in ("DR-05", "DR-07", "DR-08", "DR-09", "DR-10", "DR-16", "DR-18", "DR-20", "DR-21"):
            rule = book.rule(rid)
            self.assertEqual(rule.status, "in_profile_off", rid)
            self.assertEqual(rule.default, "off", rid)
            self.assertFalse(rule.in_code, rid)
            self.assertTrue(any("large_cap_day_trade_v1.py" in x or "day_trade_core.py" in x
                                for x in rule.enforced_in), rid)

    def test_ross_rules_are_gated(self):
        book = load_handbook()
        ross = [r for r in book.rules if r.id in {f"DR-{n}" for n in range(23, 31)}]
        self.assertEqual(len(ross), 8)
        for rule in ross:
            self.assertEqual((rule.status, rule.default, rule.origin), ("in_profile_off", "gated", "derek"))
            self.assertTrue(all("ross_cameron_small_cap_v1.py" in x for x in rule.enforced_in), rule.id)
            self.assertIn("D05", rule.sources)

    def test_in_profile_off_needs_a_location(self):
        data = raw()
        rule = next(r for r in data["rules"] if r["id"] == "DR-23")
        rule["enforced_in"] = []
        with self.assertRaises(HandbookError):
            parse_handbook(data)

    def test_prompt_label(self):
        self.assertIn("in code, profile off by default", handbook_prompt_summary(markets=("stocks",), max_chars=20000))


class HandbookValidationTests(unittest.TestCase):
    def test_rejects_duplicate_ids(self):
        data = raw()
        data["rules"].append(copy.deepcopy(data["rules"][-1]))
        with self.assertRaises(HandbookError):
            parse_handbook(data)

    def test_rejects_unknown_source(self):
        data = raw()
        data["rules"][0]["sources"] = ["NOPE"]
        with self.assertRaises(HandbookError):
            parse_handbook(data)

    def test_rejects_derek_rule_after_research(self):
        data = raw()
        derek = data["rules"].pop(0)
        data["rules"].append(derek)
        with self.assertRaises(HandbookError):
            parse_handbook(data)

    def test_rejects_derek_rule_without_derek_source(self):
        data = raw()
        data["rules"][0]["sources"] = ["S01"]
        with self.assertRaises(HandbookError):
            parse_handbook(data)

    def test_rejects_enforced_rule_without_location(self):
        data = raw()
        data["rules"][0]["enforced_in"] = []
        with self.assertRaises(HandbookError):
            parse_handbook(data)

    def test_rejects_plain_http_and_bad_enums(self):
        data = raw()
        data["sources"]["S01"]["url"] = "http://example.com"
        with self.assertRaises(HandbookError):
            parse_handbook(data)
        data = raw()
        data["rules"][0]["status"] = "maybe"
        with self.assertRaises(HandbookError):
            parse_handbook(data)
        with self.assertRaises(HandbookError):
            parse_handbook({"schema_version": 2})

    def test_missing_file_fails_closed(self):
        with self.assertRaises(HandbookError):
            load_handbook("/nonexistent/handbook_rules.json")


if __name__ == "__main__":
    unittest.main()
