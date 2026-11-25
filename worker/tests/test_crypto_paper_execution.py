"""Offline Alpaca paper-crypto order tests. Fake transport only; no network."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit
import tempfile
import unittest

from momentum.config import PAPER_API_HOST, Config, ConfigError
from momentum.crypto_execution import (
    LIVE_HOST, PAPER_HOST, PAPER_ORIGIN, CryptoPaperClient, CryptoPaperDisabled,
    CryptoPaperError, CryptoPaperHostError, assert_paper_crypto_order_url, paper_order_url,
)
import momentum.crypto_execution as crypto_execution
from momentum.journal import Journal
from momentum.paper_risk import CRYPTO_PAPER_FEED, RiskLimits, RiskState
from momentum.risk import RiskManager
from worker_fakes import armed_config


SATURDAY = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
LIMIT = {
    "symbol": "BTC/USD",
    "qty": "0.01",
    "side": "buy",
    "type": "limit",
    "time_in_force": "gtc",
    "limit_price": "64000.50",
    "client_order_id": "mlab_btc0001ab",
}


def crypto_ready(directory, **changes):
    cfg = armed_config(directory)
    cfg = replace(
        cfg,
        crypto_watchlist=("BTC/USD", "ETH/USD", "SOL/USD"),
        crypto_paper_execution_enabled=True,
        **changes,
    )
    return replace(cfg, paper_reviewed_config_hash=cfg.review_hash())


def risk_state(**changes):
    values = dict(
        exposure=Decimal("0"), realized_daily_pnl=Decimal("0"), open_positions=0,
        feed_age_ms=100, kill_switch=False, seen_order_ids=frozenset(),
    )
    values.update(changes)
    return RiskState(**values)


def roomy_limits(**changes):
    values = dict(
        max_order_notional=Decimal("1000"),
        max_total_exposure=Decimal("5000"),
        max_daily_loss=Decimal("75"),
        max_open_positions=3,
        max_feed_age_ms=2000,
        feed=CRYPTO_PAPER_FEED,
    )
    values.update(changes)
    return RiskLimits(**values)


class RecordingTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, payload):
        host = urlsplit(url).hostname
        if host != PAPER_HOST:
            raise AssertionError("crypto order targeted " + str(host))
        if host == LIVE_HOST:
            raise AssertionError("live trading host was contacted")
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "payload": payload})
        echoed = dict(payload)
        echoed.update({
            "id": "paper-order-1", "status": "accepted", "asset_class": "crypto",
            "api_key": "SHOULD_NOT_LEAK", "secret": "nope",
        })
        return echoed


class HostLockTests(unittest.TestCase):
    PAPER_OK = PAPER_ORIGIN + "/v2/orders"
    LIVE = (
        "https://api.alpaca.markets/v2/orders",
        "https://API.ALPACA.MARKETS/v2/orders",
        "https://api.alpaca.markets./v2/orders",
        "https://api.alpaca.markets:443/v2/orders",
        "https://user@api.alpaca.markets/v2/orders",
        "https://user:secret@api.alpaca.markets/v2/orders",
        "http://api.alpaca.markets/v2/orders",
        "https://api.alpaca.markets/v2/account",
        "https://api.alpaca.markets/v2/orders?symbol=BTC/USD",
    )
    OTHER = (
        "https://paper-api.alpaca.markets./v2/orders",
        "https://paper-api.alpaca.markets:443/v2/orders",
        "https://user@paper-api.alpaca.markets/v2/orders",
        "http://paper-api.alpaca.markets/v2/orders",
        "https://data.alpaca.markets/v2/orders",
        "https://paper-api.alpaca.markets.evil.example/v2/orders",
        "https://evil.paper-api.alpaca.markets/v2/orders",
        "https://paper-api.alpaca.markets/v2/orders?side=buy",
        "https://paper-api.alpaca.markets/v2/orders/extra",
        "https://paper-api.alpaca.markets/v2/account",
    )

    def test_exact_paper_orders_url_is_allowed(self):
        parts = assert_paper_crypto_order_url(self.PAPER_OK)
        self.assertEqual(parts.hostname, PAPER_HOST)
        self.assertEqual(paper_order_url(), self.PAPER_OK)
        self.assertEqual(PAPER_API_HOST, PAPER_ORIGIN)

    def test_live_trading_host_is_refused(self):
        for url in self.LIVE:
            with self.subTest(url=url):
                with self.assertRaises(CryptoPaperHostError) as ctx:
                    assert_paper_crypto_order_url(url)
                self.assertEqual(str(ctx.exception), "live_trading_host_refused")

    def test_other_hosts_and_tricks_are_refused(self):
        for url in self.OTHER:
            with self.subTest(url=url):
                with self.assertRaises(CryptoPaperHostError) as ctx:
                    assert_paper_crypto_order_url(url)
                self.assertEqual(str(ctx.exception), "crypto_order_host_refused")

    def test_live_host_constant_drift_is_refused_before_transport(self):
        transport = RecordingTransport()
        with tempfile.TemporaryDirectory() as directory:
            client = CryptoPaperClient(crypto_ready(directory), transport=transport, clock=lambda: SATURDAY)
            original = crypto_execution.PAPER_API_HOST
            crypto_execution.PAPER_API_HOST = "https://api.alpaca.markets"
            try:
                with self.assertRaises(CryptoPaperHostError) as ctx:
                    client.submit_order(LIMIT, risk_state=risk_state(), limits=roomy_limits())
                self.assertEqual(str(ctx.exception), "live_trading_host_refused")
            finally:
                crypto_execution.PAPER_API_HOST = original
        self.assertEqual(transport.calls, [])

    def test_submit_to_live_url_does_not_call_transport(self):
        transport = RecordingTransport()
        with tempfile.TemporaryDirectory() as directory:
            client = CryptoPaperClient(crypto_ready(directory), transport=transport, clock=lambda: SATURDAY)
            with self.assertRaises(CryptoPaperHostError) as ctx:
                client.submit_to(
                    "https://api.alpaca.markets/v2/orders", LIMIT,
                    risk_state=risk_state(), limits=roomy_limits(),
                )
            self.assertEqual(str(ctx.exception), "live_trading_host_refused")
        self.assertEqual(transport.calls, [])


class FlagAndLockTests(unittest.TestCase):
    def test_flag_defaults_off_and_does_not_change_the_equity_review_hash(self):
        plain = Config.from_env({"WATCHLIST": "AAPL,MSFT"})
        watched = Config.from_env({"WATCHLIST": "AAPL,MSFT", "CRYPTO_WATCHLIST": "BTC/USD"})
        explicit_off = Config.from_env({
            "WATCHLIST": "AAPL,MSFT", "CRYPTO_WATCHLIST": "BTC/USD",
            "CRYPTO_PAPER_EXECUTION_ENABLED": "false",
        })
        self.assertFalse(plain.crypto_paper_execution_enabled)
        self.assertFalse(plain.public()["crypto_paper_execution_enabled"])
        self.assertEqual(plain.public()["crypto_paper_order_host"], PAPER_ORIGIN)
        self.assertEqual(watched.review_hash(), plain.review_hash())
        self.assertEqual(explicit_off.review_hash(), plain.review_hash())
        self.assertNotIn("FAKE-KEY", str(plain.public()))
        armed = Config.from_env({
            "WATCHLIST": "AAPL,MSFT", "CRYPTO_WATCHLIST": "BTC/USD",
            "CRYPTO_PAPER_EXECUTION_ENABLED": "true",
        })
        self.assertNotEqual(armed.review_hash(), plain.review_hash())
        self.assertIn("crypto_watchlist_missing", Config.from_env({
            "WATCHLIST": "AAPL,MSFT", "CRYPTO_PAPER_EXECUTION_ENABLED": "true",
        }).paper_blockers())
        with self.assertRaises(ConfigError):
            Config.from_env({"CRYPTO_PAPER_EXECUTION_ENABLED": "maybe"})

    def test_flag_off_places_no_order(self):
        transport = RecordingTransport()
        with tempfile.TemporaryDirectory() as directory:
            cfg = armed_config(directory)
            self.assertFalse(cfg.crypto_paper_execution_enabled)
            self.assertEqual(cfg.paper_blockers(), [])
            client = CryptoPaperClient(cfg, transport=transport, clock=lambda: SATURDAY)
            with self.assertRaises(CryptoPaperDisabled) as ctx:
                client.submit_order(LIMIT, risk_state=risk_state(), limits=roomy_limits())
            self.assertIn("observation-only", str(ctx.exception))
            self.assertNotIn("FAKE-KEY", repr(client))
            self.assertNotIn("FAKE-SECRET", repr(client))
        self.assertEqual(transport.calls, [])

    def test_paper_blockers_block_crypto_orders(self):
        cases = (
            {"mode": "demo"},
            {"paper_enabled": False},
            {"api_key": "", "api_secret": ""},
            {"paper_reviewed_config_hash": "0" * 64},
            {"crypto_watchlist": ()},
        )
        for changes in cases:
            with self.subTest(changes=tuple(changes)):
                transport = RecordingTransport()
                with tempfile.TemporaryDirectory() as directory:
                    cfg = crypto_ready(directory)
                    if "paper_reviewed_config_hash" not in changes and "crypto_watchlist" not in changes:
                        cfg = replace(cfg, **changes)
                    elif "crypto_watchlist" in changes:
                        cfg = replace(cfg, crypto_watchlist=())
                        cfg = replace(cfg, paper_reviewed_config_hash=cfg.review_hash())
                    else:
                        cfg = replace(cfg, **changes)
                    self.assertTrue(cfg.paper_blockers())
                    client = CryptoPaperClient(cfg, transport=transport, clock=lambda: SATURDAY)
                    with self.assertRaises(CryptoPaperDisabled) as ctx:
                        client.submit_order(LIMIT, risk_state=risk_state(), limits=roomy_limits())
                    self.assertIn("crypto paper execution blocked", str(ctx.exception))
                    self.assertNotIn("FAKE-KEY", str(ctx.exception))
                    self.assertNotIn("FAKE-SECRET", str(ctx.exception))
                self.assertEqual(transport.calls, [])


class RiskGateTests(unittest.TestCase):
    def test_kill_switch_and_risk_limits_block_before_http(self):
        orders = (
            ("kill_switch", risk_state(kill_switch=True), roomy_limits(), LIMIT),
            ("daily_loss", risk_state(realized_daily_pnl=Decimal("-75")), roomy_limits(), LIMIT),
            ("positions", risk_state(open_positions=3), roomy_limits(), LIMIT),
            ("stale_feed", risk_state(feed_age_ms=9000), roomy_limits(), LIMIT),
            ("notional", risk_state(), roomy_limits(max_order_notional=Decimal("100")), LIMIT),
            ("duplicate", risk_state(seen_order_ids=frozenset({"mlab_btc0001ab"})), roomy_limits(), LIMIT),
        )
        for name, state, limits, order in orders:
            with self.subTest(name=name):
                transport = RecordingTransport()
                with tempfile.TemporaryDirectory() as directory:
                    client = CryptoPaperClient(
                        crypto_ready(directory), transport=transport, clock=lambda: SATURDAY,
                    )
                    with self.assertRaises(CryptoPaperError) as ctx:
                        client.submit_order(order, risk_state=state, limits=limits)
                    self.assertTrue(str(ctx.exception).startswith("paper_risk_rejected:"))
                self.assertEqual(transport.calls, [])

    def test_risk_config_notional_cap_blocks_even_if_the_screen_limit_is_wider(self):
        transport = RecordingTransport()
        with tempfile.TemporaryDirectory() as directory:
            cfg = crypto_ready(directory, risk_overrides={"max_notional": "100"})
            client = CryptoPaperClient(cfg, transport=transport, clock=lambda: SATURDAY)
            with self.assertRaises(CryptoPaperError) as ctx:
                client.submit_order(LIMIT, risk_state=risk_state(), limits=roomy_limits())
            self.assertEqual(str(ctx.exception), "paper_risk_rejected:exposure_limit")
        self.assertEqual(transport.calls, [])

    def test_risk_config_daily_loss_blocks_when_tighter_than_the_screen(self):
        transport = RecordingTransport()
        with tempfile.TemporaryDirectory() as directory:
            cfg = crypto_ready(directory, risk_overrides={"per_trade_risk": "10", "max_daily_loss": "20"})
            client = CryptoPaperClient(cfg, transport=transport, clock=lambda: SATURDAY)
            state = risk_state(realized_daily_pnl=Decimal("-20"))
            with self.assertRaises(CryptoPaperError) as ctx:
                client.submit_order(LIMIT, risk_state=state, limits=roomy_limits(max_daily_loss=Decimal("75")))
            self.assertEqual(str(ctx.exception), "paper_risk_rejected:daily_loss_limit")
        self.assertEqual(transport.calls, [])

    def test_max_shares_and_per_trade_risk_block(self):
        transport = RecordingTransport()
        with tempfile.TemporaryDirectory() as directory:
            cfg = crypto_ready(directory, risk_overrides={"max_shares": 1})
            client = CryptoPaperClient(cfg, transport=transport, clock=lambda: SATURDAY)
            wide = dict(LIMIT)
            wide.update(qty="1.5", limit_price="10", client_order_id="mlab_eth0001ab", symbol="ETH/USD")
            with self.assertRaises(CryptoPaperError) as ctx:
                client.submit_order(wide, risk_state=risk_state(), limits=roomy_limits())
            self.assertEqual(str(ctx.exception), "paper_risk_rejected:max_shares")
            sized = dict(LIMIT)
            sized.update(qty="1", limit_price="100", stop_price="50", symbol="ETH/USD",
                         client_order_id="mlab_eth0002ab")
            with self.assertRaises(CryptoPaperError) as ctx:
                client.submit_order(sized, risk_state=risk_state(), limits=roomy_limits())
            self.assertEqual(str(ctx.exception), "paper_risk_rejected:per_trade_risk")
        self.assertEqual(transport.calls, [])

    def test_journal_kill_switch_blocks(self):
        transport = RecordingTransport()
        with tempfile.TemporaryDirectory() as directory:
            cfg = crypto_ready(directory)
            journal = Journal(cfg.db_path)
            journal.set_kill_switch(True, "operator_halt", request_id="halt-crypto")
            client = CryptoPaperClient(cfg, transport=transport, clock=lambda: SATURDAY)
            with self.assertRaises(CryptoPaperError) as ctx:
                client.submit_order(
                    LIMIT, risk_state=risk_state(kill_switch=False), limits=roomy_limits(), journal=journal,
                )
            self.assertEqual(str(ctx.exception), "paper_risk_rejected:kill_switch_or_invalid_state")
            self.assertEqual(journal.events(event_type="crypto_paper_order_requested"), [])
        self.assertEqual(transport.calls, [])

    def test_latched_risk_manager_blocks(self):
        transport = RecordingTransport()
        with tempfile.TemporaryDirectory() as directory:
            client = CryptoPaperClient(crypto_ready(directory), transport=transport, clock=lambda: SATURDAY)
            manager = RiskManager()
            manager._kill_latched = True
            self.assertTrue(manager.locked)
            with self.assertRaises(CryptoPaperError) as ctx:
                client.submit_order(
                    LIMIT, risk_state=risk_state(), limits=roomy_limits(), risk_manager=manager,
                )
            self.assertEqual(str(ctx.exception), "paper_risk_rejected:kill_switch_or_invalid_state")
        self.assertEqual(transport.calls, [])


class HappyPathTests(unittest.TestCase):
    def test_limit_order_shape_is_paper_crypto(self):
        self.assertEqual(SATURDAY.weekday(), 5)
        transport = RecordingTransport()
        with tempfile.TemporaryDirectory() as directory:
            cfg = crypto_ready(directory)
            self.assertEqual(cfg.paper_blockers(), [])
            journal = Journal(cfg.db_path)
            client = CryptoPaperClient(cfg, transport=transport, clock=lambda: SATURDAY)
            result = client.submit_order(LIMIT, risk_state=risk_state(), limits=roomy_limits(), journal=journal)
            self.assertEqual(len(transport.calls), 1)
            call = transport.calls[0]
            self.assertEqual(call["method"], "POST")
            self.assertEqual(call["url"], PAPER_ORIGIN + "/v2/orders")
            self.assertEqual(urlsplit(call["url"]).hostname, PAPER_HOST)
            self.assertEqual(call["payload"], {
                "symbol": "BTC/USD",
                "qty": "0.01",
                "side": "buy",
                "type": "limit",
                "time_in_force": "gtc",
                "limit_price": "64000.5",
                "client_order_id": "mlab_btc0001ab",
            })
            self.assertNotIn("extended_hours", call["payload"])
            self.assertEqual(call["headers"]["APCA-API-KEY-ID"], "FAKE-KEY")
            self.assertEqual(call["headers"]["APCA-API-SECRET-KEY"], "FAKE-SECRET")
            self.assertEqual(result["environment"], "paper")
            self.assertEqual(result["host"], PAPER_HOST)
            self.assertFalse(result["live_execution_supported"])
            self.assertEqual(result["request"], call["payload"])
            self.assertNotIn("SHOULD_NOT_LEAK", str(result))
            self.assertNotIn("FAKE-SECRET", str(result))
            self.assertNotIn("FAKE-KEY", str(result))
            requested = journal.events(event_type="crypto_paper_order_requested")
            self.assertEqual(len(requested), 1)
            self.assertEqual(requested[0]["payload"]["observed_at"], "2026-11-21T18:00:00Z")
            self.assertNotIn("FAKE-KEY", str(requested[0]["payload"]))
            self.assertEqual(
                journal.events(event_type="crypto_paper_order_acknowledged")[0]["payload"]["order"]["status"],
                "accepted",
            )
            with self.assertRaises(CryptoPaperError) as ctx:
                client.submit_order(LIMIT, risk_state=risk_state(), limits=roomy_limits(), journal=journal)
            self.assertEqual(str(ctx.exception), "crypto_order_already_requested")
            self.assertEqual(len(transport.calls), 1)

    def test_market_ioc_omits_limit_price_and_day_tif_is_refused(self):
        transport = RecordingTransport()
        with tempfile.TemporaryDirectory() as directory:
            client = CryptoPaperClient(crypto_ready(directory), transport=transport, clock=lambda: SATURDAY)
            market = {
                "symbol": "ETH/USD", "qty": "0.25", "side": "sell", "type": "market",
                "time_in_force": "ioc", "price": "2000", "client_order_id": "mlab_eth0003ab",
            }
            result = client.submit_order(market, risk_state=risk_state(), limits=roomy_limits())
            self.assertEqual(result["request"], {
                "symbol": "ETH/USD", "qty": "0.25", "side": "sell", "type": "market",
                "time_in_force": "ioc", "client_order_id": "mlab_eth0003ab",
            })
            day = dict(LIMIT)
            day["time_in_force"] = "day"
            day["client_order_id"] = "mlab_btc0002ab"
            with self.assertRaises(CryptoPaperError) as ctx:
                client.submit_order(day, risk_state=risk_state(), limits=roomy_limits())
            self.assertEqual(str(ctx.exception), "crypto_time_in_force_must_be_gtc_or_ioc")
            gtc_market = dict(market)
            gtc_market["time_in_force"] = "gtc"
            gtc_market["client_order_id"] = "mlab_eth0004ab"
            with self.assertRaises(CryptoPaperError) as ctx:
                client.submit_order(gtc_market, risk_state=risk_state(), limits=roomy_limits())
            self.assertEqual(str(ctx.exception), "crypto_market_orders_require_ioc")
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(all(item["url"].startswith(PAPER_ORIGIN) for item in transport.calls))

    def test_fractional_qty_is_not_rounded_to_a_whole_share(self):
        transport = RecordingTransport()
        with tempfile.TemporaryDirectory() as directory:
            client = CryptoPaperClient(crypto_ready(directory), transport=transport, clock=lambda: SATURDAY)
            order = dict(LIMIT)
            order.update(symbol="SOL/USD", qty="0.000000001", limit_price="150", client_order_id="mlab_sol0001ab")
            result = client.submit_order(order, risk_state=risk_state(), limits=roomy_limits())
        self.assertEqual(result["request"]["qty"], "0.000000001")
        self.assertIn(".", result["request"]["qty"])


if __name__ == "__main__":
    unittest.main()
