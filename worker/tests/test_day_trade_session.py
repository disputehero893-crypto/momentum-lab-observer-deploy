"""Separate day-trade equity session for small_cap_candle_patterns_iex_v1. Offline only."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlsplit, parse_qs
import json
import tempfile
import unittest

from momentum.alpaca import BrokerError, PaperClient, PaperDisabled
from momentum.candle_patterns import PatternSignal
from momentum.config import Config
from momentum.day_trade_session import (REQUESTED, DayTradeEquitySession, DayTradeScope, day_trade_claims,
                                        foreign_filter)
from momentum.journal import Journal
from momentum.models import Decision
from momentum.presets import APPROVED_PAPER_PROFILES
from momentum.ross_cameron_small_cap_v1 import ProviderSet, ScannerRow
from momentum.session import PaperSession
from momentum.small_cap_candle_patterns_iex_v1 import PROFILE_ID, SmallCapCandlePatternsIexV1
from worker_fakes import armed_config

NOW = datetime(2026, 11, 24, 15, 0, 5, tzinfo=timezone.utc)   # Tue 10:00:05 ET
SYM = "ABCD"


class FloatStub:
    name = "fmp_stub"

    def float_shares(self, symbol, as_of):
        return 5_000_000


class ScannerStub:
    name = "movers_stub"

    def scan(self, as_of):
        return [ScannerRow(SYM, Decimal("10.08"), Decimal("8"), Decimal("26"), Decimal("40"), rank=1)]


PROVIDERS = ProviderSet(float_provider=FloatStub(), gainers_scanner=ScannerStub())


class Broker:
    """Paper-api fake: orders, positions, quotes and bars. No network."""

    def __init__(self):
        self.now = NOW
        self.orders = {}
        self.positions = []
        self.posts = []
        self.deletes = []
        self.bid, self.ask = "10.09", "10.10"

    def request(self, method, url, headers, payload):
        parts = urlsplit(url)
        assert parts.hostname in {"paper-api.alpaca.markets", "data.alpaca.markets"}, url
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        path = parts.path
        if path == "/v2/account":
            return {"id": "acct-1", "status": "ACTIVE", "trading_blocked": False, "account_blocked": False}
        if path == "/v2/positions":
            return [dict(p) for p in self.positions]
        if path == "/v2/orders" and method == "GET":
            return [o for o in self.orders.values() if o["status"] not in {"filled", "canceled", "expired"}]
        if path == "/v2/orders:by_client_order_id":
            if q["client_order_id"] not in self.orders:
                raise BrokerError("Alpaca HTTP 404", 404)
            return dict(self.orders[q["client_order_id"]])
        if path == "/v2/orders" and method == "POST":
            self.posts.append(payload)
            order = {"id": "o%d" % len(self.posts), "client_order_id": payload["client_order_id"],
                     "symbol": payload["symbol"], "side": payload["side"], "qty": payload["qty"],
                     "limit_price": payload["limit_price"], "status": "new", "filled_qty": "0",
                     "filled_avg_price": None, "submitted_at": self.now.isoformat()}
            self.orders[payload["client_order_id"]] = order
            return dict(order)
        if path.startswith("/v2/orders/") and method == "DELETE":
            self.deletes.append(path)
            return None
        if path == "/v2/calendar":
            return [{"date": q["start"], "open": "09:30", "close": "16:00"}]
        if path == "/v2/stocks/snapshots":
            t = (self.now - timedelta(seconds=1)).isoformat()
            return {s: {"latestQuote": {"bp": self.bid, "ap": self.ask, "t": t}} for s in q["symbols"].split(",")}
        if path == "/v2/stocks/bars":
            return {"bars": {s: [{"t": "2026-11-24T14:59:00Z", "o": "10.00", "h": "10.09", "l": "9.98",
                                  "c": "10.08", "v": 5000, "vw": "10.04"}] for s in q["symbols"].split(",")}}
        raise AssertionError("unexpected " + method + " " + path)

    def fill(self, client_id, qty=None, price="10.10"):
        order = self.orders[client_id]
        order.update(status="filled", filled_qty=str(qty or order["qty"]), filled_avg_price=price,
                     filled_at=self.now.isoformat())
        held = next((p for p in self.positions if p["symbol"] == order["symbol"]), None)
        n = int(order["filled_qty"]) * (1 if order["side"] == "buy" else -1)
        if held is None:
            self.positions.append({"symbol": order["symbol"], "side": "long", "qty": str(n),
                                   "avg_entry_price": price, "asset_class": "us_equity"})
        else:
            held["qty"] = str(int(held["qty"]) + n)
            if held["qty"] == "0":
                self.positions.remove(held)


class PatternEngine(SmallCapCandlePatternsIexV1):
    """Real engine (caps, admit, exits); the pattern scan is replaced by one fixed signal."""

    def on_bar(self, bar, timeframe="1m"):
        if bar.symbol not in self.candidates:
            return []
        signal = PatternSignal("bull_flag", "1m", 0, Decimal("10.10"), Decimal("9.95"), ("green_close",))
        return [self.admit(signal, bar)]


def config(directory, **changes):
    cfg = armed_config(directory)
    cfg = replace(cfg, day_trade_profile=PROFILE_ID, **changes)
    return replace(cfg, paper_reviewed_config_hash=cfg.review_hash())


LEDGER = {"complete": True, "realized_pnl": "0", "fees": "0", "environment": "paper"}


class DayTradeSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config(self.tmp.name)
        self.broker = Broker()
        self.client = PaperClient(self.cfg, transport=self.broker.request, clock=lambda: self.broker.now)
        self.journal = Journal(self.cfg.db_path, sample_seconds=0, min_free_bytes=0)

    def tearDown(self):
        self.journal.close() if hasattr(self.journal, "close") else None
        self.tmp.cleanup()

    def session(self, providers=PROVIDERS):
        from momentum.risk import RiskConfig
        engine = PatternEngine(RiskConfig(), providers, data_feed="iex")
        return DayTradeEquitySession(self.cfg, self.client, self.journal, providers=providers, engine=engine,
                                     clock=lambda: self.broker.now)

    def test_missing_providers_block_only_this_session(self):
        self.assertEqual(self.cfg.paper_blockers(), [])          # large-cap and crypto stay open
        session = DayTradeEquitySession(self.cfg, self.client, self.journal, env={}, clock=lambda: NOW)
        out = session.cycle(execute=True, session_ledger=LEDGER)
        self.assertFalse(out["active"])
        self.assertEqual(out["blockers"], ["float_provider_missing", "gainers_scanner_missing"])
        self.assertEqual(self.broker.posts, [])
        flag_only = DayTradeEquitySession(self.cfg, self.client, self.journal,
                                          env={"DAY_TRADE_DATA_PROVIDERS_ENABLED": "true"}, clock=lambda: NOW)
        self.assertIn("float_provider_missing", flag_only.blockers)   # no FMP key -> still blocked, quietly

    def test_entry_goes_through_check_paper_intent_and_its_own_journal_events(self):
        out = self.session().cycle(execute=True, session_ledger=LEDGER)
        self.assertTrue(out["active"], out)
        self.assertEqual(out["orders_submitted"], 1, out)
        post = self.broker.posts[0]
        self.assertEqual((post["symbol"], post["side"], post["type"], post["time_in_force"]),
                         (SYM, "buy", "limit", "day"))
        self.assertFalse(post["extended_hours"])
        self.assertLessEqual(int(post["qty"]) * Decimal(post["limit_price"]), Decimal("1000"))
        self.assertEqual(out["actions"][0]["paper_intent_reason"], "approved_for_paper_only")
        self.assertEqual(len(self.journal.events(event_type=REQUESTED)), 1)
        self.assertEqual(self.journal.events(event_type="paper_order_requested"), [])  # not a large-cap order
        self.assertIn(PROFILE_ID, APPROVED_PAPER_PROFILES)

    def test_account_wide_exposure_blocks_the_entry(self):
        # A large-cap position already uses most of the $1,000 exposure cap.
        self.broker.positions = [{"symbol": "DEMO-A", "side": "long", "qty": "90", "avg_entry_price": "10",
                                  "asset_class": "us_equity"}]
        out = self.session().cycle(execute=True, session_ledger=LEDGER)
        self.assertEqual(self.broker.posts, [])
        self.assertEqual(out["actions"][0]["paper_intent_reason"], "exposure_limit")

    def test_daily_loss_kill_switch_and_unarmed_place_nothing(self):
        out = self.session().cycle(execute=True, session_ledger={**LEDGER, "realized_pnl": "-75"})
        self.assertEqual(out["actions"][0]["paper_intent_reason"], "daily_loss_limit")
        s = self.session()
        out = s.cycle(execute=False, session_ledger=LEDGER)
        self.assertEqual(out["actions"][0]["skipped"], "entries_not_armed_or_not_reconciled")
        self.assertEqual(s.engine.pending, {})                      # no phantom slot
        self.journal.set_kill_switch(True, "test", request_id="t")
        out = self.session().cycle(execute=True, session_ledger=LEDGER)
        self.assertEqual(self.broker.posts, [])
        out = self.session().cycle(execute=True, session_ledger=None)   # ledger incomplete
        self.assertEqual(self.broker.posts, [])

    def test_fill_then_protective_exit_at_bid_even_when_halted(self):
        s = self.session()
        s.cycle(execute=True, session_ledger=LEDGER)
        cid = self.broker.posts[0]["client_order_id"]
        self.broker.fill(cid)
        self.broker.now += timedelta(seconds=30)
        self.broker.bid, self.broker.ask = "9.90", "9.91"              # under the 9.94 stop
        self.journal.set_kill_switch(True, "halt entries", request_id="h")
        out = s.cycle(execute=True, session_ledger=LEDGER)
        self.assertIn(SYM, s.engine.positions)
        sells = [p for p in self.broker.posts if p["side"] == "sell"]
        self.assertEqual(len(sells), 1, out)
        self.assertEqual((sells[0]["limit_price"], sells[0]["qty"]), ("9.90", self.broker.posts[0]["qty"]))
        self.assertFalse(sells[0]["extended_hours"])

    def test_restart_rebuilds_the_position_from_journal_and_broker(self):
        self.session().cycle(execute=True, session_ledger=LEDGER)
        self.broker.fill(self.broker.posts[0]["client_order_id"])
        fresh = self.session()
        out = fresh.cycle(execute=False, session_ledger=LEDGER)
        self.assertIn(SYM, fresh.engine.positions)
        self.assertEqual(out["errors"], [])
        self.assertEqual(fresh.exposure()[1], 1)

    def test_unowned_position_outside_the_watchlist_blocks_entries_only_here(self):
        self.broker.positions = [{"symbol": "ZZZZ", "side": "long", "qty": "5", "avg_entry_price": "3",
                                  "asset_class": "us_equity"},
                                 {"symbol": "BTCUSD", "side": "long", "qty": "0.01", "avg_entry_price": "90000",
                                  "asset_class": "crypto"}]
        out = self.session().cycle(execute=True, session_ledger=LEDGER)
        self.assertIn("day_trade_position_mismatch_or_unowned_position", out["errors"])
        self.assertEqual(self.broker.posts, [])

    def test_scope_refuses_watchlist_and_crypto_symbols(self):
        scope = DayTradeScope(PROFILE_ID, "cfg", frozenset(self.cfg.watchlist))
        self.assertFalse(scope.allows("DEMO-A"))
        self.assertFalse(scope.allows("BTC/USD"))
        self.assertTrue(scope.allows(SYM))
        intent = {"symbol": "DEMO-A", "side": "buy", "mode": "paper", "profile": PROFILE_ID, "config_id": "cfg"}
        with self.assertRaises(PaperDisabled):
            self.client.submit(intent, self.journal, "r", quote={}, guard=lambda: True, scope=scope)
        # The large-cap path refuses a candle intent (config id / watchlist).
        intent = dict(intent, symbol=SYM)
        with self.assertRaises(PaperDisabled):
            self.client.submit(intent, self.journal, "r", quote={}, guard=lambda: True)

    def test_large_cap_reconcile_ignores_crypto_and_day_trade_rows(self):
        s = self.session()
        s.cycle(execute=True, session_ledger=LEDGER)
        cid = self.broker.posts[0]["client_order_id"]
        self.broker.positions.append({"symbol": "BTCUSD", "side": "long", "qty": "0.01",
                                      "avg_entry_price": "90000", "asset_class": "crypto"})
        claims = day_trade_claims(self.journal)
        self.assertIn(cid, claims[0])
        position, order = foreign_filter(self.cfg.watchlist, claims)
        self.assertTrue(order(self.broker.orders[cid]))
        self.assertTrue(position({"symbol": "BTCUSD", "asset_class": "crypto"}))
        self.assertFalse(position({"symbol": "DEMO-A", "asset_class": "us_equity"}))
        self.broker.fill(cid)
        large = PaperSession(self.cfg, self.client, journal=self.journal, clock=lambda: self.broker.now)
        evidence = large._reconcile("paper-2026-11-24", self.broker.now)
        self.assertNotIn("unowned_open_order", evidence["errors"])
        self.assertNotIn("position_quantity_mismatch_or_unowned_position", evidence["errors"])
        self.assertEqual(evidence["actual_positions"], [])
        # ...but a truly unknown equity position still blocks the large-cap session.
        self.broker.positions.append({"symbol": "QQQQ", "side": "long", "qty": "1", "avg_entry_price": "5",
                                      "asset_class": "us_equity"})
        evidence = large._reconcile("paper-2026-11-24", self.broker.now)
        self.assertIn("position_quantity_mismatch_or_unowned_position", evidence["errors"])

    def test_large_cap_screen_counts_day_trade_exposure(self):
        large = PaperSession(self.cfg, self.client, journal=self.journal, clock=lambda: self.broker.now)
        large.external_exposure = lambda: (Decimal("995"), 1)
        quote = {"timestamp": self.broker.now.isoformat(), "received_at": self.broker.now.isoformat()}
        ok, reason = large._screen_equity_entry(
            {"symbol": "DEMO-A", "side": "buy", "qty": 1, "entry_limit": "10", "setup_id": "s"},
            {"quote": quote}, {"positions": [], "opening": []}, {"realized_pnl": "0"})
        self.assertEqual((ok, reason), (False, "exposure_limit"))


class WiringTests(unittest.TestCase):
    def test_serve_paper_builds_the_session_and_serve_observe_does_not(self):
        from momentum.observation import ObservationWorker
        with tempfile.TemporaryDirectory() as directory:
            cfg = config(directory)
            journal = Journal(cfg.db_path, sample_seconds=0, min_free_bytes=0)
            broker = Broker()
            client = PaperClient(cfg, transport=broker.request, clock=lambda: broker.now)
            paper = ObservationWorker(cfg, provider=client, journal=journal, clock=lambda: broker.now,
                                      stock_execution=True, publisher=False)
            self.assertIsNotNone(paper.day_trade)
            self.assertIs(paper.session.external_exposure.__self__, paper.day_trade)
            observe = ObservationWorker(cfg, provider=client, journal=journal, clock=lambda: broker.now,
                                        publisher=False)
            self.assertIsNone(observe.day_trade)
            other = ObservationWorker(replace(cfg, day_trade_profile=""), provider=client, journal=journal,
                                      clock=lambda: broker.now, stock_execution=True, publisher=False)
            self.assertIsNone(other.day_trade)

    def test_hash_and_blockers_for_the_hosted_shape(self):
        env = {"MOMENTUM_MODE": "paper", "WATCHLIST": "AAPL,MSFT,SPY", "CRYPTO_WATCHLIST": "BTC/USD,ETH/USD,SOL/USD",
               "CRYPTO_PAPER_EXECUTION_ENABLED": "true", "DAY_TRADE_PROFILE": PROFILE_ID}
        cfg = Config.from_env(env)
        self.assertFalse([b for b in cfg.paper_blockers() if "day_trade" in b or "provider" in b or "scanner" in b])


if __name__ == "__main__":
    unittest.main()
