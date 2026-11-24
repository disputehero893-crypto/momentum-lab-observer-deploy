"""Offline tests for OANDA practice leverage risk, order gating, and performance."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import tempfile
import unittest
from urllib.parse import urlsplit

from momentum.config import Config, ConfigError
from momentum.forex_execution import ForexPracticeTrader
from momentum.forex_risk import (
    ForexOrderIntent, ForexRiskConfig, ForexRiskState,
    check_forex_leverage_intent,
)
from momentum.oanda import (
    OandaHostError, OandaPracticeClient, OandaPracticeDisabled,
    OandaPracticeError, PRACTICE_REST_HOST, assert_practice_url,
)
from momentum.paper_risk import FOREX_PRACTICE_FEED, RiskLimits, RiskState
from worker_fakes import armed_config

ACCOUNT = "001-001-1234567-001"
TOKEN = "FAKE_PRACTICE_TOKEN_NOT_REAL"
NOW = datetime(2026, 11, 24, 18, 0, 0, tzinfo=timezone.utc)


def stamp(dt):
    return dt.isoformat().replace("+00:00", "Z")


def armed_practice_config(directory, *, orders_enabled=True, environment="practice",
                          paper_enabled=True, forex_risk_overrides=None, review=True):
    cfg = armed_config(directory)
    cfg = replace(
        cfg,
        mode="paper",
        paper_enabled=paper_enabled,
        oanda_practice_api_token=TOKEN,
        oanda_practice_account_id=ACCOUNT,
        oanda_environment=environment,
        oanda_practice_orders_enabled=orders_enabled,
        forex_watchlist=("EUR/USD", "GBP/USD", "USD/JPY"),
        forex_risk_overrides=forex_risk_overrides or {},
    )
    if review:
        cfg = replace(cfg, paper_reviewed_config_hash=cfg.review_hash())
    return cfg


def base_fx_state(**changes):
    defaults = dict(
        nav_usd=Decimal("5000"),
        margin_used_usd=Decimal("0"),
        margin_available_usd=Decimal("5000"),
        total_open_exposure_usd=Decimal("0"),
        realized_daily_pnl_usd=Decimal("0"),
        open_positions=0,
        feed_age_ms=100,
        kill_switch=False,
        seen_order_ids=frozenset(),
        pair_exposures_usd={},
        spread_bps=Decimal("1.5"),
    )
    defaults.update(changes)
    return ForexRiskState(**defaults)


def base_paper_state(**changes):
    defaults = dict(
        exposure=Decimal("0"),
        realized_daily_pnl=Decimal("0"),
        open_positions=0,
        feed_age_ms=100,
        kill_switch=False,
        seen_order_ids=frozenset(),
    )
    defaults.update(changes)
    return RiskState(**defaults)


class OrderFlagAndLockTests(unittest.TestCase):
    def test_orders_flag_defaults_false_and_watch_only_refuses_orders(self):
        cfg = Config.from_env({
            "MOMENTUM_MODE": "paper",
            "OANDA_ENVIRONMENT": "practice",
            "OANDA_PRACTICE_API_TOKEN": TOKEN,
            "OANDA_PRACTICE_ACCOUNT_ID": ACCOUNT,
            "OANDA_FOREX_WATCHLIST": "EUR_USD,GBP_USD",
        })
        self.assertFalse(cfg.oanda_practice_orders_enabled)
        self.assertFalse(cfg.public()["oanda_practice_orders_enabled"])
        self.assertEqual(cfg.public()["oanda_environment"], "practice")

        calls = []
        def transport(method, url, headers, payload):
            calls.append((method, url, payload))
            return {"prices": [
                {"instrument": "EUR_USD", "tradeable": True,
                 "time": stamp(NOW - timedelta(milliseconds=100)),
                 "bids": [{"price": "1.08320"}], "asks": [{"price": "1.08330"}]},
            ]}

        client = OandaPracticeClient(cfg, transport=transport, clock=lambda: NOW)
        # Watch-only GET pricing succeeds while orders flag is false.
        got = client.pricing(("EUR/USD",))
        self.assertEqual(got["prices"][0]["instrument"], "EUR_USD")
        self.assertEqual(len(calls), 1)

        with tempfile.TemporaryDirectory() as d:
            # Even with PAPER_TRADING_ENABLED=true and valid reviewed hash,
            # OANDA_PRACTICE_ORDERS_ENABLED=false blocks order placement before transport.
            armed_paper_only = armed_practice_config(d, orders_enabled=False)
            self.assertEqual(armed_paper_only.paper_blockers(), [])
            c2 = OandaPracticeClient(armed_paper_only, transport=transport, clock=lambda: NOW)
            trader = ForexPracticeTrader(armed_paper_only, client=c2, clock=lambda: NOW)
            with self.assertRaises(OandaPracticeDisabled):
                c2.submit_order(
                    {"instrument": "EUR_USD", "units": "20", "price": "1.0833",
                     "stop_loss_price": "1.0780", "client_order_id": "fx-off-1"},
                    risk_state=base_paper_state(),
                    forex_state=base_fx_state(),
                )
            with self.assertRaises(OandaPracticeDisabled):
                trader.place_order_from_live_price(
                    "EUR/USD", side="buy", units="20",
                    stop_loss_price="1.0780", client_order_id="fx-off-2",
                )
            with self.assertRaises(OandaPracticeDisabled):
                c2.cancel_order("100")
            with self.assertRaises(OandaPracticeDisabled):
                c2.close_trade("200")
        self.assertEqual(len(calls), 1)  # zero POST/PUT transport calls

    def test_oanda_environment_must_be_practice_or_orders_refuse(self):
        with tempfile.TemporaryDirectory() as d:
            no_env = armed_practice_config(d, environment="", orders_enabled=True)
            self.assertIn("oanda_environment_not_practice", no_env.paper_blockers())
            calls = []
            client = OandaPracticeClient(no_env, transport=lambda *a: calls.append(a))
            with self.assertRaises(OandaPracticeDisabled):
                client.submit_order(
                    {"instrument": "EUR_USD", "units": "20", "price": "1.0833",
                     "stop_loss_price": "1.0780", "client_order_id": "fx-noenv"},
                    risk_state=base_paper_state(),
                    forex_state=base_fx_state(),
                )
            self.assertEqual(calls, [])

        for bad_env in ("live", "fxtrade", "production"):
            with self.subTest(bad_env=bad_env), self.assertRaises(ConfigError):
                Config.from_env({"OANDA_ENVIRONMENT": bad_env})

    def test_orders_flag_cannot_bypass_existing_paper_lock_or_review_hash(self):
        with tempfile.TemporaryDirectory() as d:
            # 1) PAPER_TRADING_ENABLED=false -> refuses
            no_paper = armed_practice_config(d, orders_enabled=True, paper_enabled=False, review=False)
            client = OandaPracticeClient(no_paper, transport=lambda *a: None)
            with self.assertRaises(OandaPracticeDisabled):
                client.submit_order(
                    {"instrument": "EUR_USD", "units": "20", "price": "1.0833",
                     "stop_loss_price": "1.0780", "client_order_id": "fx-nopaper"},
                    risk_state=base_paper_state(),
                    forex_state=base_fx_state(),
                )
            # 2) Unreviewed / tampered forex risk override invalidates hash -> refuses
            armed = armed_practice_config(d, orders_enabled=True)
            tampered = replace(armed, forex_risk_overrides={"max_units_per_order": 500})
            self.assertIn("exact_paper_configuration_not_reviewed", tampered.paper_blockers())
            client2 = OandaPracticeClient(tampered, transport=lambda *a: None)
            with self.assertRaises(OandaPracticeDisabled):
                client2.submit_order(
                    {"instrument": "EUR_USD", "units": "20", "price": "1.0833",
                     "stop_loss_price": "1.0780", "client_order_id": "fx-hash"},
                    risk_state=base_paper_state(),
                    forex_state=base_fx_state(),
                )
            # 3) #8 check_paper_intent enforced: wrong feed and duplicate order id rejected
            c_ok = OandaPracticeClient(armed, transport=lambda *a: {})
            with self.assertRaises(OandaPracticeError) as ctx:
                c_ok.submit_order(
                    {"instrument": "EUR_USD", "units": "20", "price": "1.0833",
                     "stop_loss_price": "1.0780", "client_order_id": "fx-iex"},
                    risk_state=base_paper_state(),
                    limits=RiskLimits(feed="iex"),
                    forex_state=base_fx_state(),
                )
            self.assertIn("oanda_practice risk feed", str(ctx.exception))

    def test_live_host_still_refused_during_order_path(self):
        with self.assertRaises(OandaHostError) as ctx:
            assert_practice_url("https://api-fxtrade.oanda.com/v3/accounts/" + ACCOUNT + "/orders", role="rest")
        self.assertEqual(str(ctx.exception), "non_practice_host_refused")
        with self.assertRaises(ConfigError):
            Config.from_env({"OANDA_API_URL": "https://api-fxtrade.oanda.com"})


class LeverageRiskModelTests(unittest.TestCase):
    def test_conservative_defaults_and_config_bounds(self):
        cfg = ForexRiskConfig()
        self.assertLess(cfg.max_effective_leverage, Decimal("5"))
        self.assertEqual(cfg.max_effective_leverage, Decimal("2.0"))
        self.assertEqual(cfg.max_margin_utilization, Decimal("0.20"))
        self.assertTrue(cfg.require_stop_loss)
        for bad in (
            {"require_stop_loss": False},
            {"max_effective_leverage": Decimal("50")},
            {"max_effective_leverage": Decimal("5.1")},
            {"max_margin_utilization": Decimal("0.9")},
            {"max_units_per_order": 0},
            {"max_open_positions": 0},
            {"max_order_notional_usd": Decimal("500")},  # > max_notional_per_pair_usd
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ForexRiskConfig(**bad)

    def test_stop_loss_is_mandatory_and_directional(self):
        state = base_fx_state()
        # Missing stop loss
        ok, reason = check_forex_leverage_intent(
            ForexOrderIntent("EUR/USD", Decimal("50"), Decimal("1.08"), None, "sl-1"),
            state,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "stop_loss_required")

        # Wrong side for long / short
        for units, sl in ((Decimal("50"), Decimal("1.09")), (Decimal("-50"), Decimal("1.07"))):
            ok, reason = check_forex_leverage_intent(
                ForexOrderIntent("EUR/USD", units, Decimal("1.08"), sl, "sl-side"),
                state,
            )
            self.assertFalse(ok)
            self.assertEqual(reason, "stop_loss_wrong_side")

        # Too tight / too wide
        ok_tight, r_tight = check_forex_leverage_intent(
            ForexOrderIntent("EUR/USD", Decimal("50"), Decimal("1.08"), Decimal("1.0799"), "sl-tight"),
            state,
        )
        self.assertEqual((ok_tight, r_tight), (False, "stop_loss_too_tight"))

        ok_wide, r_wide = check_forex_leverage_intent(
            ForexOrderIntent("EUR/USD", Decimal("50"), Decimal("1.08"), Decimal("1.00"), "sl-wide"),
            state,
        )
        self.assertEqual((ok_wide, r_wide), (False, "stop_loss_too_wide"))

    def test_caps_reject_oversized_orders_and_margin_exhaustion(self):
        # 1) Order notional cap ($100 default)
        ok, reason = check_forex_leverage_intent(
            ForexOrderIntent("EUR/USD", Decimal("150"), Decimal("1.08"), Decimal("1.075"), "cap-1"),
            base_fx_state(),
        )
        self.assertEqual((ok, reason), (False, "order_notional_cap_exceeded"))

        # 2) Unit cap on USD/JPY (raise notional cap to hit unit cap)
        unit_cfg = ForexRiskConfig(max_units_per_order=100, max_order_notional_usd=Decimal("200"),
                                   max_notional_per_pair_usd=Decimal("250"))
        ok, reason = check_forex_leverage_intent(
            ForexOrderIntent("USD/JPY", Decimal("150"), Decimal("155.00"), Decimal("154.00"), "cap-2"),
            base_fx_state(),
            unit_cfg,
        )
        self.assertEqual((ok, reason), (False, "unit_cap_exceeded"))

        # 3) Per-pair notional cap
        ok, reason = check_forex_leverage_intent(
            ForexOrderIntent("EUR/USD", Decimal("50"), Decimal("1.08"), Decimal("1.075"), "cap-3"),
            base_fx_state(pair_exposures_usd={"EUR/USD": Decimal("210")}, total_open_exposure_usd=Decimal("210")),
        )
        self.assertEqual((ok, reason), (False, "pair_notional_cap_exceeded"))

        # 4) Total open exposure cap
        ok, reason = check_forex_leverage_intent(
            ForexOrderIntent("EUR/USD", Decimal("50"), Decimal("1.08"), Decimal("1.075"), "cap-4"),
            base_fx_state(total_open_exposure_usd=Decimal("960")),
        )
        self.assertEqual((ok, reason), (False, "total_exposure_cap_exceeded"))

        # 5) Effective leverage cap (2:1 default: NAV $100 -> max $200 total exposure)
        ok, reason = check_forex_leverage_intent(
            ForexOrderIntent("EUR/USD", Decimal("50"), Decimal("1.08"), Decimal("1.075"), "cap-5"),
            base_fx_state(nav_usd=Decimal("100"), margin_available_usd=Decimal("100"),
                          total_open_exposure_usd=Decimal("160")),
        )
        self.assertEqual((ok, reason), (False, "effective_leverage_cap_exceeded"))

        # 6) Margin utilization cap & insufficient free margin
        ok, reason = check_forex_leverage_intent(
            ForexOrderIntent("EUR/USD", Decimal("50"), Decimal("1.08"), Decimal("1.075"), "cap-6"),
            base_fx_state(nav_usd=Decimal("100"), margin_used_usd=Decimal("19"),
                          margin_available_usd=Decimal("81")),
        )
        self.assertEqual((ok, reason), (False, "margin_utilization_cap_exceeded"))

        ok, reason = check_forex_leverage_intent(
            ForexOrderIntent("EUR/USD", Decimal("50"), Decimal("1.08"), Decimal("1.075"), "cap-7"),
            base_fx_state(nav_usd=Decimal("1000"), margin_available_usd=Decimal("0.50")),
        )
        self.assertEqual((ok, reason), (False, "insufficient_margin_available"))

        # 7) Stop-loss dollar risk cap (1% NAV or $25)
        ok, reason = check_forex_leverage_intent(
            ForexOrderIntent("EUR/USD", Decimal("80"), Decimal("1.08"), Decimal("1.03"), "cap-8"),
            base_fx_state(nav_usd=Decimal("300")),  # 1% of 300 = $3; risk is 80 * 0.05 = $4
        )
        self.assertEqual((ok, reason), (False, "stop_loss_risk_cap_exceeded"))

        # 8) Daily loss cap, max open positions, spread, stale feed, cross missing rate
        self.assertEqual(
            check_forex_leverage_intent(
                ForexOrderIntent("EUR/USD", Decimal("20"), Decimal("1.08"), Decimal("1.075"), "cap-9"),
                base_fx_state(realized_daily_pnl_usd=Decimal("-50")),
            ),
            (False, "daily_loss_limit"),
        )
        self.assertEqual(
            check_forex_leverage_intent(
                ForexOrderIntent("EUR/USD", Decimal("20"), Decimal("1.08"), Decimal("1.075"), "cap-10"),
                base_fx_state(open_positions=3),
            ),
            (False, "position_limit"),
        )
        self.assertEqual(
            check_forex_leverage_intent(
                ForexOrderIntent("EUR/USD", Decimal("20"), Decimal("1.08"), Decimal("1.075"), "cap-11"),
                base_fx_state(spread_bps=Decimal("20")),
            ),
            (False, "spread_limit_exceeded"),
        )
        self.assertEqual(
            check_forex_leverage_intent(
                ForexOrderIntent("EUR/USD", Decimal("20"), Decimal("1.08"), Decimal("1.075"), "cap-12"),
                base_fx_state(feed_age_ms=2500),
            ),
            (False, "stale_or_invalid_feed"),
        )
        self.assertEqual(
            check_forex_leverage_intent(
                ForexOrderIntent("EUR/JPY", Decimal("20"), Decimal("165.00"), Decimal("164.00"), "cap-13"),
                base_fx_state(),
            ),
            (False, "cross_pair_usd_rate_missing"),
        )


class LivePriceOrderAndPerformanceTests(unittest.TestCase):
    def _mock_transport(self, calls, *, bid="1.08320", ask="1.08330", quote_age_ms=50, tradeable=True):
        def transport(method, url, headers, payload):
            calls.append((method, url, payload))
            parsed = urlsplit(url)
            self.assertEqual(parsed.netloc, PRACTICE_REST_HOST)
            if method == "GET" and parsed.path.endswith("/pricing"):
                return {"prices": [{
                    "instrument": "EUR_USD",
                    "tradeable": tradeable,
                    "time": stamp(NOW - timedelta(milliseconds=quote_age_ms)),
                    "bids": [{"price": bid, "liquidity": 1000}],
                    "asks": [{"price": ask, "liquidity": 1000}],
                }]}
            if method == "GET" and parsed.path.endswith("/summary"):
                return {"account": {
                    "id": ACCOUNT, "currency": "USD",
                    "NAV": "5000.00", "balance": "4990.00",
                    "unrealizedPL": "10.00", "pl": "42.50",
                    "marginUsed": "2.17", "marginAvailable": "4997.83",
                }}
            if method == "GET" and parsed.path.endswith("/openTrades"):
                return {"trades": [{
                    "id": "501", "instrument": "GBP_USD",
                    "currentUnits": "50", "price": "1.26000", "unrealizedPL": "10.00",
                }]}
            if method == "GET" and parsed.path.endswith("/trades"):
                return {"trades": [
                    {"id": "401", "instrument": "EUR_USD", "realizedPL": "12.50"},
                    {"id": "402", "instrument": "EUR_USD", "realizedPL": "-4.00"},
                ]}
            if method == "POST" and parsed.path.endswith("/orders"):
                return {"orderCreateTransaction": {"id": "9001", "type": payload["order"]["type"]},
                        "orderFillTransaction": {"id": "9002", "price": ask, "units": payload["order"]["units"]}}
            raise AssertionError(f"Unexpected call: {method} {url}")
        return transport

    def test_places_practice_market_order_from_live_quote_with_stop_loss_and_price_bound(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            cfg = armed_practice_config(d, orders_enabled=True)
            client = OandaPracticeClient(cfg, transport=self._mock_transport(calls), clock=lambda: NOW)
            trader = ForexPracticeTrader(cfg, client=client, clock=lambda: NOW)
            receipt = trader.place_order_from_live_price(
                "EUR/USD", side="buy", units="50",
                stop_loss_distance="0.00500",
                take_profit_price="1.09500",
                client_order_id="fx-live-1",
            )
            self.assertEqual(receipt["host"], PRACTICE_REST_HOST)
            self.assertEqual(receipt["environment"], "practice")
            self.assertEqual(receipt["quoted_entry_price"], "1.0833")
            self.assertEqual(receipt["stop_loss_price"], "1.07830")
            posts = [c for c in calls if c[0] == "POST"]
            self.assertEqual(len(posts), 1)
            order = posts[0][2]["order"]
            self.assertEqual(order["instrument"], "EUR_USD")
            self.assertEqual(order["type"], "MARKET")
            self.assertEqual(order["timeInForce"], "FOK")
            self.assertEqual(order["stopLossOnFill"]["price"], "1.07830")
            self.assertEqual(order["takeProfitOnFill"]["price"], "1.09500")
            self.assertIn("priceBound", order)
            self.assertNotIn("price", order)
            self.assertNotIn(TOKEN, json.dumps(receipt))

    def test_stale_or_wide_spread_live_quote_refuses_before_post(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = armed_practice_config(d, orders_enabled=True)
            stale_calls = []
            c1 = OandaPracticeClient(cfg, transport=self._mock_transport(stale_calls, quote_age_ms=3000), clock=lambda: NOW)
            t1 = ForexPracticeTrader(cfg, client=c1, clock=lambda: NOW)
            with self.assertRaises(OandaPracticeError):
                t1.place_order_from_live_price(
                    "EUR/USD", side="buy", units="50", stop_loss_distance="0.00500", client_order_id="fx-stale",
                )
            self.assertFalse(any(c[0] == "POST" for c in stale_calls))

            wide_calls = []
            c2 = OandaPracticeClient(cfg, transport=self._mock_transport(wide_calls, bid="1.08000", ask="1.08500"), clock=lambda: NOW)
            t2 = ForexPracticeTrader(cfg, client=c2, clock=lambda: NOW)
            with self.assertRaises(OandaPracticeError) as ctx:
                t2.place_order_from_live_price(
                    "EUR/USD", side="buy", units="50", stop_loss_distance="0.00500", client_order_id="fx-wide",
                )
            self.assertIn("spread_limit_exceeded", str(ctx.exception))
            self.assertFalse(any(c[0] == "POST" for c in wide_calls))

    def test_performance_report_summarizes_account_and_closed_trades(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            # Read-only performance summary works even with orders disabled.
            cfg = armed_practice_config(d, orders_enabled=False, paper_enabled=False, review=False)
            client = OandaPracticeClient(cfg, transport=self._mock_transport(calls), clock=lambda: NOW)
            trader = ForexPracticeTrader(cfg, client=client, clock=lambda: NOW)
            perf = trader.performance_report()
            self.assertEqual(perf["host"], PRACTICE_REST_HOST)
            self.assertEqual(perf["nav"], "5000.00")
            self.assertEqual(perf["unrealized_pl"], "10.00")
            self.assertEqual(perf["account_realized_pl"], "42.50")
            self.assertEqual(perf["recent_closed_realized_pl"], "8.50")
            self.assertEqual(perf["open_trade_count"], 1)
            self.assertEqual(perf["closed_wins"], 1)
            self.assertEqual(perf["closed_losses"], 1)
            self.assertNotIn(TOKEN, json.dumps(perf))


if __name__ == "__main__":
    unittest.main()
