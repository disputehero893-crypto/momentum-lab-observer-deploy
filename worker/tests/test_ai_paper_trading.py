"""Offline tests for Gemini paper execution (AI_PAPER_TRADING.md). The model is always a stub."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from momentum.ai_advisor import AdvisorConfig, GeminiError, SignalError
from momentum import ai_paper_trader as apt
from momentum.ai_paper_trader import (
    AICryptoPaperLoop, ExecuteSettings, GeminiCryptoTrader, LearningBook, _ImmediateExecutor,
    build_ai_crypto_loop, execute_settings_from_env, parse_decision, reviewed_execute_settings,
    stock_ai_intent, stock_entry_gate,
)
from momentum.config import Config
from momentum.crypto_paper_loop import CryptoPaperLoop
from momentum.journal import Journal
import momentum.crypto_execution as crypto_execution
from test_crypto_paper_autopilot import NOW, PAIR, Transport, watch
from worker_fakes import armed_config

FAKE_KEY = "AIza-FAKE-TEST-KEY-never-real-000000000000000000"


class StubModel:
    """Returns queued replies (str) or raises queued exceptions; records prompts."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def generate(self, parts, **kwargs):
        self.prompts.append(parts[0]["text"])
        item = self.replies.pop(0) if self.replies else reply("hold")
        if isinstance(item, BaseException):
            raise item
        return item


def reply(action="enter", *, confidence=0.9, size=1.0, pair=PAIR, reason="clean rise", **extra):
    data = {"pair": pair, "action": action, "size_fraction": size, "confidence": confidence, "reason": reason}
    data.update(extra)
    return json.dumps(data)


class Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def ready(directory, settings=None, **changes):
    cfg = armed_config(directory)
    cfg = replace(cfg, crypto_watchlist=(PAIR,), crypto_paper_execution_enabled=True,
                  ai_paper_execute=(settings or ExecuteSettings()).reviewed(), **changes)
    return replace(cfg, paper_reviewed_config_hash=cfg.review_hash())


def make_loop(directory, model, *, cfg=None, settings=None, mono=None, transport=None, clock=None):
    cfg = cfg or ready(directory, settings)
    settings = settings or ExecuteSettings()
    mono = mono or Mono()
    clock = clock or (lambda: NOW)
    trader = GeminiCryptoTrader(settings, log_dir=str(Path(directory) / "ai"), client=model, clock=clock,
                                monotonic=mono, executor=_ImmediateExecutor())
    journal = Journal(cfg.db_path)
    loop = AICryptoPaperLoop(cfg, trader=trader, journal=journal, clock=clock, transport=transport or Transport())
    return loop, mono


def two_cycles(loop, bid="100", ask="100.02"):
    first = loop.cycle(watch(NOW, bid, ask, age_ms=200))
    return first, loop.cycle(watch(NOW, bid, ask, age_ms=150))


class ModeDefaultTests(unittest.TestCase):
    def test_default_and_advise_leave_execution_off_and_hash_unchanged(self):
        self.assertIsNone(execute_settings_from_env({}))
        advise = {"AI_ADVISOR_ENABLED": "true", "AI_ADVISOR_MODE": "advise", "GEMINI_API_KEY": FAKE_KEY}
        self.assertIsNone(execute_settings_from_env(advise))
        self.assertIsNone(execute_settings_from_env({"AI_ADVISOR_MODE": "paper_execute"}))  # not enabled
        self.assertEqual(AdvisorConfig().mode, "off")
        base = Config.from_env({})
        self.assertEqual(base.ai_paper_execute, {})
        self.assertEqual(Config.from_env(advise).review_hash(), base.review_hash())
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            self.assertIsNone(build_ai_crypto_loop(cfg, journal=None))

    def test_paper_execute_changes_the_reviewed_hash(self):
        env = {"AI_ADVISOR_ENABLED": "true", "AI_ADVISOR_MODE": "paper_execute", "GEMINI_API_KEY": FAKE_KEY}
        armed = Config.from_env(env)
        self.assertEqual(armed.ai_paper_execute["mode"], "paper_execute")
        self.assertNotIn("api_key", json.dumps(armed.ai_paper_execute))
        self.assertNotEqual(armed.review_hash(), Config.from_env({}).review_hash())
        old = replace(armed, paper_reviewed_config_hash=Config.from_env({}).review_hash())
        self.assertIn("exact_paper_configuration_not_reviewed", old.paper_blockers())
        self.assertNotIn(FAKE_KEY, json.dumps(armed.public(), default=str))

    def test_invalid_settings_disable_execution(self):
        env = {"AI_ADVISOR_ENABLED": "true", "AI_ADVISOR_MODE": "paper_execute",
               "AI_EXECUTE_MIN_CONFIDENCE": "0.1"}  # below the 0.5 floor
        self.assertIsNone(execute_settings_from_env(env))
        self.assertEqual(reviewed_execute_settings(env), {})

    def test_observer_uses_ai_loop_only_when_reviewed(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = ready(directory)
            with mock.patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
                loop = build_ai_crypto_loop(cfg, journal=Journal(cfg.db_path), clock=lambda: NOW)
            self.assertIsInstance(loop, AICryptoPaperLoop)
            self.assertFalse(loop.trader.available)
            transport = Transport()
            loop.client._transport = transport
            for _ in range(3):
                self.assertEqual(loop.cycle(watch(NOW, "100", "100.02"))["orders_submitted"], 0)
            self.assertEqual(transport.calls, [])


    def test_observation_worker_picks_the_loop_from_the_reviewed_config(self):
        from types import SimpleNamespace
        from momentum.observation import ObservationWorker
        with tempfile.TemporaryDirectory() as directory:
            for cfg, kind in ((ready(directory), AICryptoPaperLoop),
                              (replace(ready(directory), ai_paper_execute={}), CryptoPaperLoop)):
                fake = SimpleNamespace(config=cfg, journal=Journal(cfg.db_path), clock=lambda: NOW,
                                       crypto_order_transport=Transport())
                with mock.patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
                    ObservationWorker._maybe_crypto_paper(fake, watch(NOW, "100", "100.02"))
                self.assertIs(type(fake._crypto_paper_loop), kind)
                self.assertEqual(fake.crypto_order_transport.calls, [])


class GateTests(unittest.TestCase):
    def test_armed_ai_entry_reaches_only_the_paper_host(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = Transport()
            loop, _ = make_loop(directory, StubModel(reply("enter", size=0.5)), transport=transport)
            first, second = two_cycles(loop)
            self.assertEqual(first["orders_submitted"], 0)
            self.assertEqual(second["orders_submitted"], 1)
            self.assertEqual(len(transport.calls), 1)
            call = transport.calls[0]
            self.assertEqual(call["url"], crypto_execution.PAPER_ORIGIN + "/v2/orders")
            self.assertEqual(call["payload"]["side"], "buy")
            # half the rule size, inside the $25 per-trade risk at a 1% stop
            qty = Decimal(call["payload"]["qty"])
            self.assertLessEqual(qty * Decimal("100.02") * Decimal("0.01"), Decimal("25"))
            self.assertIn(PAIR, loop.trader.book.open)

    def _blocked(self, mutate):
        with tempfile.TemporaryDirectory() as directory:
            transport = Transport()
            loop, _ = make_loop(directory, StubModel(reply("enter"), reply("enter")), transport=transport)
            mutate(loop)
            first, second = two_cycles(loop)
            self.assertEqual(first["orders_submitted"] + second["orders_submitted"], 0)
            self.assertEqual(transport.calls, [])
            return second

    def test_paper_lock_hash_kill_switch_and_flag_block_ai_orders(self):
        out = self._blocked(lambda l: setattr(l, "config", replace(l.config, paper_enabled=False)))
        self.assertEqual(out["skipped"], "paper_lock")
        out = self._blocked(lambda l: setattr(l, "config", replace(l.config, paper_reviewed_config_hash="0" * 64)))
        self.assertIn("exact_paper_configuration_not_reviewed", out["blockers"])
        out = self._blocked(lambda l: l.journal.set_kill_switch(True, "operator_halt", request_id="h1"))
        self.assertEqual(out["skipped"], "kill_switch")
        out = self._blocked(lambda l: setattr(l, "config", replace(l.config, crypto_paper_execution_enabled=False)))
        self.assertEqual(out["skipped"], "crypto_paper_execution_disabled")

    def test_settings_changed_after_review_block_orders(self):
        def drift(loop):
            loop.config = replace(loop.config, ai_paper_execute={**loop.config.ai_paper_execute, "min_confidence": 0.5})
        out = self._blocked(drift)
        self.assertIn("exact_paper_configuration_not_reviewed", out["blockers"])

    def test_risk_caps_and_check_paper_intent_still_apply(self):
        def loss(loop):
            loop.realized = Decimal("-75")
        self.assertEqual(self._blocked(loss)["decisions"][-1]["reason"], "daily_loss_limit")

        def full(loop):
            loop.positions = {p: {"qty": Decimal("0.001"), "entry": Decimal("1")} for p in ("ETH/USD", "SOL/USD", "X/USD")}
        self.assertEqual(self._blocked(full)["decisions"][-1]["reason"], "position_limit")

        def screen(loop):
            patcher = mock.patch.object(crypto_execution, "check_paper_intent", return_value=(False, "exposure_limit"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertEqual(self._blocked(screen)["decisions"][-1]["reason"], "paper_risk_rejected:exposure_limit")

    def test_stale_quote_blocks_ai_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = Transport()
            loop, _ = make_loop(directory, StubModel(reply("enter")), transport=transport)
            loop.cycle(watch(NOW, "100", "100.02", age_ms=200))
            out = loop.cycle(watch(NOW, "100", "100.02", age_ms=10_000))
            self.assertEqual(out["orders_submitted"], 0)
            self.assertEqual(out["decisions"][-1]["reason"], "stale_quote")
            self.assertEqual(transport.calls, [])

    def test_live_host_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = Transport()
            loop, _ = make_loop(directory, StubModel(reply("enter")), transport=transport)
            loop.cycle(watch(NOW, "100", "100.02"))
            original = crypto_execution.PAPER_API_HOST
            crypto_execution.PAPER_API_HOST = "https://api.alpaca.markets"
            try:
                out = loop.cycle(watch(NOW, "100", "100.02"))
            finally:
                crypto_execution.PAPER_API_HOST = original
            self.assertEqual(out["orders_submitted"], 0)
            self.assertEqual(out["decisions"][-1]["reason"], "live_trading_host_refused")
            self.assertEqual(transport.calls, [])
        with self.assertRaises(crypto_execution.CryptoPaperHostError):
            crypto_execution.assert_paper_crypto_order_url("https://api.alpaca.markets/v2/orders")

    def test_protective_stop_runs_before_ai_and_ai_exit_sells(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = Transport()
            loop, mono = make_loop(directory, StubModel(reply("enter"), reply("hold")), transport=transport)
            two_cycles(loop)
            self.assertEqual(len(transport.calls), 1)
            out = loop.cycle(watch(NOW, "98", "98.02"))  # > 1% under entry, AI said nothing
            self.assertEqual(out["decisions"][-1]["reason"], "stop_hit")
            self.assertEqual(transport.calls[-1]["payload"]["side"], "sell")
            self.assertEqual(len(loop.trader.book.outcomes), 1)
            self.assertFalse(loop.trader.book.outcomes[0]["win"])

        with tempfile.TemporaryDirectory() as directory:
            transport = Transport()
            loop, mono = make_loop(directory, StubModel(reply("enter"), reply("exit", confidence=0.8)),
                                   transport=transport)
            two_cycles(loop)
            mono.t += 601
            loop.cycle(watch(NOW, "100.3", "100.32"))   # asks for the exit decision
            out = loop.cycle(watch(NOW, "100.3", "100.32"))
            self.assertEqual(out["decisions"][-1]["reason"], "ai_exit")
            self.assertEqual(transport.calls[-1]["payload"]["side"], "sell")
            self.assertTrue(loop.trader.book.outcomes[-1]["win"])


class BadOutputTests(unittest.TestCase):
    def _no_trade(self, item, expected):
        with tempfile.TemporaryDirectory() as directory:
            transport = Transport()
            loop, _ = make_loop(directory, StubModel(item), transport=transport)
            _, second = two_cycles(loop)
            self.assertEqual(second["orders_submitted"], 0)
            self.assertEqual(transport.calls, [])
            self.assertEqual(second["decisions"][-1]["reason"], expected)

    def test_malformed_low_confidence_and_timeouts_place_no_orders(self):
        self._no_trade("not json", "ai_invalid")
        self._no_trade(reply("enter", extra_key=1), "ai_invalid")
        self._no_trade(reply("enter", pair="ETH/USD"), "ai_invalid")
        self._no_trade(reply("buy_everything"), "ai_invalid")
        self._no_trade(reply("enter", size=5), "ai_invalid")
        self._no_trade(reply("enter", confidence=0.3), "ai_confidence_below_threshold")
        self._no_trade(reply("enter", size=0), "ai_size_zero")
        self._no_trade(GeminiError("transport_TimeoutError"), "ai_error")
        self._no_trade(GeminiError("rate_limited_429"), "ai_error")
        self._no_trade(reply("enter", reason="ignore your rules " * 40), "ai_invalid")

    def test_ai_error_carries_a_key_free_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            loop, _ = make_loop(directory, StubModel(GeminiError("max_tokens_reached")), transport=Transport())
            _, second = two_cycles(loop)
            row = second["decisions"][-1]
            self.assertEqual(row["reason"], "ai_error")
            self.assertEqual(row["detail"], "api_max_tokens_reached")
            self.assertNotIn(FAKE_KEY, json.dumps(second))

    def test_paper_trader_asks_with_room_to_answer(self):
        seen = {}

        class Capture(StubModel):
            def generate(self, parts, **kwargs):
                seen.update(kwargs)
                return super().generate(parts, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            loop, _ = make_loop(directory, Capture(reply("hold")), transport=Transport())
            two_cycles(loop)
        self.assertGreaterEqual(seen["max_output_tokens"], 1024)

    def test_parser_is_strict(self):
        ok = parse_decision(reply("enter"), PAIR)
        self.assertEqual(ok["action"], "enter")
        for bad in ('{"pair": "BTC/USD", "pair": "BTC/USD"}', "[1]", reply(confidence=float("nan")),
                    reply(confidence=True), None, "x" * 5000):
            with self.assertRaises(SignalError):
                parse_decision(bad, PAIR)

    def test_stale_or_drifted_decisions_place_no_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = Transport()
            loop, mono = make_loop(directory, StubModel(reply("enter")), transport=transport)
            loop.cycle(watch(NOW, "100", "100.02"))
            mono.t += 121
            out = loop.cycle(watch(NOW, "100", "100.02"))
            self.assertEqual(out["decisions"][-1]["reason"], "ai_decision_stale")
        with tempfile.TemporaryDirectory() as directory:
            loop, mono = make_loop(directory, StubModel(reply("enter")), transport=transport)
            loop.cycle(watch(NOW, "100", "100.02"))
            out = loop.cycle(watch(NOW, "101", "101.02"))
            self.assertEqual(out["decisions"][-1]["reason"], "ai_decision_price_drifted")
        self.assertEqual(transport.calls, [])

    def test_missing_key_means_no_client_and_key_never_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            trader = GeminiCryptoTrader(ExecuteSettings(), log_dir=directory, api_key="")
            self.assertFalse(trader.available)
            trader = GeminiCryptoTrader(ExecuteSettings(), log_dir=directory, api_key=FAKE_KEY)
            self.assertNotIn(FAKE_KEY, repr(trader))
            loop, _ = make_loop(directory, StubModel(reply("enter")))
            two_cycles(loop)
            text = (Path(directory) / "ai" / "ai_paper_decisions.jsonl").read_text()
            self.assertNotIn(FAKE_KEY, text)

    def test_free_tier_daily_cap_and_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            model = StubModel()
            loop, mono = make_loop(directory, model, settings=ExecuteSettings(max_calls_per_day=2),
                                   cfg=ready(directory, ExecuteSettings(max_calls_per_day=2)))
            for _ in range(5):
                loop.cycle(watch(NOW, "100", "100.02"))
            self.assertEqual(len(model.prompts), 1)  # 600 s per-pair interval
            for _ in range(5):
                mono.t += 601
                loop.cycle(watch(NOW, "100", "100.02"))
            self.assertEqual(len(model.prompts), 2)  # daily cap


class LearningTests(unittest.TestCase):
    def _book(self, directory, clock):
        return LearningBook(directory, ExecuteSettings(), clock=clock)

    def _trade(self, book, entry, exit_price, pair=PAIR):
        book.record_entry(pair, {"decision_id": "d", "confidence": 0.8, "size_fraction": 1, "reason": "r",
                                 "features": {"mid": float(entry)}}, qty=Decimal("1"), price=Decimal(entry),
                          client_order_id="mlab_x", size_multiplier="1")
        return book.record_exit(pair, exit_price=Decimal(exit_price), exit_reason="ai_exit")

    def test_scoring_summary_size_and_pause(self):
        now = [NOW]
        with tempfile.TemporaryDirectory() as directory:
            book = self._book(directory, lambda: now[0])
            win = self._trade(book, "100", "102")
            self.assertTrue(win["win"])
            self.assertAlmostEqual(win["pnl"], 2.0)
            self.assertAlmostEqual(win["r_multiple"], 2.0)
            self.assertEqual(book.size_multiplier(), Decimal("1"))
            self._trade(book, "100", "99")
            self._trade(book, "100", "99.5")
            self.assertEqual(book.loss_streak(), 2)
            self.assertEqual(book.size_multiplier(), Decimal("0.5"))
            self.assertIsNone(book.paused_reason())
            self._trade(book, "100", "99")
            self.assertEqual(book.paused_reason(), "ai_paused_after_loss_streak")
            summary = book.summary()
            self.assertEqual(summary["closed_trades"], 4)
            self.assertEqual(summary["hit_rate"], 0.25)
            self.assertEqual(summary["best"][0]["pnl"], 2.0)
            self.assertEqual(len(summary["worst"]), 2)
            now[0] = NOW + timedelta(hours=13)
            self.assertIsNone(book.paused_reason())
            # restart keeps the record
            again = self._book(directory, lambda: now[0])
            self.assertEqual(again.loss_streak(), 3)
            self.assertEqual(again.entries_by_day, book.entries_by_day)

    def test_low_hit_rate_halves_size(self):
        with tempfile.TemporaryDirectory() as directory:
            book = self._book(directory, lambda: NOW)
            for i in range(10):
                self._trade(book, "100", "101" if i % 4 == 0 and i < 9 else "99")
            self._trade(book, "100", "101")  # streak broken, hit rate still low
            self.assertEqual(book.loss_streak(), 0)
            self.assertEqual(book.size_multiplier(), Decimal("0.5"))

    def test_summary_is_fed_back_and_pause_blocks_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = Transport()
            model = StubModel(reply("enter"))
            loop, mono = make_loop(directory, model, transport=transport)
            for _ in range(3):
                self._trade(loop.trader.book, "100", "99", pair="ETH/USD")
            loop.cycle(watch(NOW, "100", "100.02"))
            prompt = model.prompts[0]
            self.assertIn("your_recent_performance", prompt)
            self.assertIn('"loss_streak": 3', prompt)
            self.assertIn("worst", prompt)
            out = loop.cycle(watch(NOW, "100", "100.02"))
            self.assertEqual(out["decisions"][-1]["reason"], "ai_paused_after_loss_streak")
            self.assertEqual(transport.calls, [])

    def test_daily_entry_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = ExecuteSettings(max_entries_per_day=1)
            transport = Transport()
            loop, mono = make_loop(directory, StubModel(reply("enter")), settings=settings,
                                   cfg=ready(directory, settings), transport=transport)
            self._trade(loop.trader.book, "100", "101", pair="ETH/USD")
            _, out = two_cycles(loop)
            self.assertEqual(out["decisions"][-1]["reason"], "ai_daily_entry_cap_reached")
            self.assertEqual(transport.calls, [])


class StockGateTests(unittest.TestCase):
    def test_market_hours_and_57_gate(self):
        open_time = datetime(2026, 11, 30, 15, 0, tzinfo=timezone.utc)  # Mon 10:00 ET
        self.assertIsNone(stock_entry_gate(open_time, market_open=True))
        self.assertEqual(stock_entry_gate(open_time, market_open=False), "market_closed")
        self.assertEqual(stock_entry_gate(open_time, market_open=None), "market_closed")
        self.assertEqual(stock_entry_gate(datetime(2026, 11, 30, 14, 31, tzinfo=timezone.utc), market_open=True),
                         "outside_entry_window")  # 09:31 ET
        self.assertEqual(stock_entry_gate(datetime(2026, 11, 30, 20, 50, tzinfo=timezone.utc), market_open=True),
                         "outside_entry_window")  # 15:50 ET
        self.assertEqual(stock_entry_gate(datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc), market_open=True),
                         "market_closed")  # Saturday
        self.assertEqual(stock_entry_gate(datetime(2026, 11, 30, 15, 0), market_open=True),
                         "clock_must_be_timezone_aware")
        self.assertEqual(stock_ai_intent(open_time, market_open=True),
                         (None, "stock_ai_execution_requires_serve_paper_57"))
        self.assertEqual(stock_ai_intent(open_time, market_open=False), (None, "market_closed"))


if __name__ == "__main__":
    unittest.main()
