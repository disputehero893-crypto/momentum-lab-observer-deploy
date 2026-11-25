"""Offline automatic paper-crypto loop. Fake quotes and a fake order transport."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlsplit
import tempfile
import unittest

from momentum.crypto_observation import build_crypto_watch
from momentum.crypto_paper_loop import CryptoPaperLoop
from momentum.journal import Journal
from momentum.crypto_execution import PAPER_HOST, PAPER_ORIGIN
import momentum.crypto_execution as crypto_execution
from test_worker_observation import Fixture
from worker_fakes import armed_config


NOW = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)  # Saturday
PAIR = "BTC/USD"


def stamp(moment):
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def watch(now, bid, ask, *, age_ms=200, pair=PAIR):
    source = now - timedelta(milliseconds=age_ms)
    received = now - timedelta(milliseconds=max(age_ms - 50, 0))
    collected = now - timedelta(milliseconds=max(age_ms - 80, 0))
    if collected < received:
        collected = received
    raw = {"quotes": {pair: {"bp": str(bid), "ap": str(ask), "t": stamp(source)}}}
    return build_crypto_watch(
        raw, (pair,), received_at=stamp(received), collected_at=stamp(collected), max_age_seconds=60)


def rising_watches(now=NOW):
    steps = (("100", "100.02", 1500), ("100.30", "100.32", 1100),
             ("100.60", "100.62", 700), ("100.80", "100.82", 200))
    return [watch(now, bid, ask, age_ms=age) for bid, ask, age in steps]


class Transport:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, payload):
        host = urlsplit(url).hostname
        if host != PAPER_HOST:
            raise AssertionError("order host " + str(host))
        self.calls.append({"method": method, "url": url, "payload": dict(payload)})
        return {"id": "paper-1", "status": "accepted", "symbol": payload["symbol"],
                "qty": payload["qty"], "side": payload["side"], "type": payload["type"],
                "time_in_force": payload["time_in_force"], "client_order_id": payload["client_order_id"]}


def ready(directory, **changes):
    cfg = armed_config(directory)
    cfg = replace(cfg, crypto_watchlist=(PAIR,), crypto_paper_execution_enabled=True, **changes)
    return replace(cfg, paper_reviewed_config_hash=cfg.review_hash())


class AutopilotTests(unittest.TestCase):
    def test_flag_off_places_no_orders(self):
        transport = Transport()
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            self.assertFalse(cfg.crypto_paper_execution_enabled)
            loop = CryptoPaperLoop(cfg, journal=Journal(cfg.db_path), clock=lambda: NOW, transport=transport)
            for item in rising_watches():
                summary = loop.cycle(item)
            self.assertFalse(summary["enabled"])
            self.assertEqual(summary["orders_submitted"], 0)
            self.assertEqual(summary["skipped"], "crypto_paper_execution_disabled")
        self.assertEqual(transport.calls, [])

    def test_paper_lock_and_kill_switch_place_no_orders(self):
        cases = ("paper_off", "hash_mismatch", "kill_switch")
        for name in cases:
            with self.subTest(name=name):
                transport = Transport()
                with tempfile.TemporaryDirectory() as directory:
                    cfg = ready(directory)
                    journal = Journal(cfg.db_path)
                    if name == "paper_off":
                        cfg = replace(cfg, paper_enabled=False)
                    elif name == "hash_mismatch":
                        cfg = replace(cfg, paper_reviewed_config_hash="0" * 64)
                    else:
                        journal.set_kill_switch(True, "operator_halt", request_id="halt-auto")
                    loop = CryptoPaperLoop(cfg, journal=journal, clock=lambda: NOW, transport=transport)
                    summary = loop.cycle(rising_watches()[-1])
                    self.assertEqual(summary["orders_submitted"], 0)
                    self.assertIn(summary["skipped"], {"paper_lock", "kill_switch"})
                self.assertEqual(transport.calls, [])

    def test_limits_block_a_rising_market(self):
        transport = Transport()
        with tempfile.TemporaryDirectory() as directory:
            cfg = ready(directory)
            loop = CryptoPaperLoop(cfg, journal=Journal(cfg.db_path), clock=lambda: NOW, transport=transport)
            loop.realized = Decimal("-75")
            summary = None
            for item in rising_watches():
                summary = loop.cycle(item)
            self.assertEqual(summary["orders_submitted"], 0)
            self.assertEqual(summary["decisions"][-1]["reason"], "daily_loss_limit")
            loop.realized = Decimal("0")
            loop.positions = {
                "ETH/USD": {"qty": Decimal("1"), "entry": Decimal("1")},
                "SOL/USD": {"qty": Decimal("1"), "entry": Decimal("1")},
                "DOGE/USD": {"qty": Decimal("1"), "entry": Decimal("1")},
            }
            summary = loop.cycle(watch(NOW, "102", "102.02", age_ms=100))
            self.assertEqual(summary["orders_submitted"], 0)
            self.assertEqual(summary["decisions"][-1]["reason"], "position_limit")
        self.assertEqual(transport.calls, [])

    def test_stale_quote_and_live_host_place_no_orders(self):
        transport = Transport()
        with tempfile.TemporaryDirectory() as directory:
            cfg = ready(directory)
            journal = Journal(cfg.db_path)
            stale = CryptoPaperLoop(cfg, journal=journal, clock=lambda: NOW, transport=transport)
            summary = stale.cycle(watch(NOW, "100", "100.02", age_ms=10_000))
            self.assertEqual(summary["orders_submitted"], 0)
            self.assertEqual(summary["decisions"][-1]["reason"], "stale_quote")
            live = CryptoPaperLoop(cfg, journal=Journal(cfg.db_path + ".live"), clock=lambda: NOW, transport=transport)
            # A separate book so the refusal is the first order, not a later exit.
            for item in rising_watches()[:3]:
                self.assertEqual(live.cycle(item)["orders_submitted"], 0)
            original = crypto_execution.PAPER_API_HOST
            crypto_execution.PAPER_API_HOST = "https://api.alpaca.markets"
            try:
                summary = live.cycle(rising_watches()[-1])
            finally:
                crypto_execution.PAPER_API_HOST = original
            self.assertEqual(summary["orders_submitted"], 0)
            self.assertEqual(summary["decisions"][-1]["reason"], "live_trading_host_refused")
        self.assertEqual(transport.calls, [])

    def test_rising_saturday_quote_buys_on_the_paper_host_and_stop_sells(self):
        self.assertEqual(NOW.weekday(), 5)
        transport = Transport()
        waits = []
        with tempfile.TemporaryDirectory() as directory:
            cfg = ready(directory)
            journal = Journal(cfg.db_path)
            loop = CryptoPaperLoop(cfg, journal=journal, clock=lambda: NOW, transport=transport)
            queued = rising_watches()
            loop.run(lambda: queued.pop(0), max_cycles=4, wait=waits.append)
            self.assertEqual(len(transport.calls), 1)
            call = transport.calls[0]
            self.assertEqual(call["method"], "POST")
            self.assertEqual(call["url"], PAPER_ORIGIN + "/v2/orders")
            self.assertEqual(urlsplit(call["url"]).hostname, PAPER_HOST)
            payload = call["payload"]
            self.assertEqual(payload["symbol"], "BTC/USD")
            self.assertEqual(payload["side"], "buy")
            self.assertEqual(payload["type"], "limit")
            self.assertEqual(payload["time_in_force"], "gtc")
            self.assertEqual(payload["limit_price"], "100.82")
            qty = Decimal(payload["qty"])
            self.assertGreater(qty, 0)
            self.assertNotEqual(qty, qty.to_integral_value())
            self.assertLessEqual(qty * Decimal(payload["limit_price"]), Decimal("1000"))
            self.assertNotIn("extended_hours", payload)
            self.assertTrue(payload["client_order_id"].startswith("mlab_"))
            self.assertEqual(waits, [5.0, 5.0, 5.0])
            # The same rise must not open a second position.
            again = loop.cycle(watch(NOW, "101.20", "101.22", age_ms=150))
            self.assertEqual(again["orders_submitted"], 0)
            self.assertEqual(transport.calls[0]["payload"]["side"], "buy")
            # One percent under the entry ask (100.82) is a stop.
            stopped = loop.cycle(watch(NOW, "99.00", "99.02", age_ms=150))
            self.assertEqual(stopped["orders_submitted"], 1)
            self.assertEqual(stopped["decisions"][-1]["reason"], "stop_hit")
            sell = transport.calls[1]["payload"]
            self.assertEqual(sell["side"], "sell")
            self.assertEqual(sell["type"], "market")
            self.assertEqual(sell["time_in_force"], "ioc")
            self.assertEqual(sell["symbol"], "BTC/USD")
            self.assertEqual(urlsplit(transport.calls[1]["url"]).hostname, PAPER_HOST)
            restarted = CryptoPaperLoop(cfg, journal=journal, clock=lambda: NOW, transport=transport)
            self.assertEqual(restarted.positions, {})

    def test_observer_flag_off_does_not_add_a_crypto_order_step(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            with fixture.worker() as worker:
                result = worker.cycle()
            self.assertIsNone(result["error_code"], result)
            self.assertNotIn("crypto_paper", result)
            self.assertEqual(result["orders_submitted"], 0)
            self.assertFalse(result["execution_enabled"])
            self.assertTrue(all(row["method"] == "GET" for row in fixture.venue.calls))

    def test_observer_hook_places_one_paper_order_when_armed(self):
        transport = Transport()
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, armed=True)
            cfg = replace(fixture.config, crypto_watchlist=(PAIR,), crypto_paper_execution_enabled=True)
            fixture.config = replace(cfg, paper_reviewed_config_hash=cfg.review_hash())
            queued = rising_watches(fixture.venue.now)

            class Scripted:
                def collect(self):
                    return queued.pop(0)

            with fixture.worker(crypto_collector=Scripted()) as worker:
                worker.crypto_order_transport = transport
                summaries = []
                for _ in range(4):
                    result = worker.cycle()
                    self.assertIsNone(result["error_code"], result)
                    summaries.append(result["crypto_paper"])
            self.assertEqual(sum(item["orders_submitted"] for item in summaries), 1)
            self.assertEqual(transport.calls[0]["payload"]["side"], "buy")
            self.assertEqual(urlsplit(transport.calls[0]["url"]).hostname, PAPER_HOST)
            self.assertEqual(result["orders_submitted"], 0)
            self.assertFalse(result["execution_enabled"])
            self.assertTrue(all(row["method"] == "GET" for row in fixture.venue.calls))


if __name__ == "__main__":
    unittest.main()
