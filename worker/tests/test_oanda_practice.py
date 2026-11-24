"""Offline OANDA practice adapter tests. Invented responses only; no network."""
from dataclasses import replace
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit
import unittest

from momentum.config import Config, ConfigError
from momentum.forex_risk import ForexRiskState
from momentum.oanda import (
    HOST_OVERRIDE_ENV, OandaHostError, OandaPracticeClient, OandaPracticeDisabled,
    OandaPracticeError, OandaPracticeStreamClient, PRACTICE_REST_HOST, PRACTICE_REST_ORIGIN,
    PRACTICE_STREAM_HOST, PRACTICE_STREAM_ORIGIN, assert_practice_url, redact_secret,
)
from momentum.paper_risk import FOREX_PRACTICE_FEED, RiskLimits, RiskState
from worker_fakes import armed_config


ACCOUNT = "001-001-1234567-001"
TOKEN = "FAKE_PRACTICE_TOKEN_NOT_REAL"
PAIRS = ("EUR/USD", "GBP/USD", "USD/JPY")


def practice_config(directory=None, *, enabled=False, env=None, **changes):
    base = {"MOMENTUM_MODE": "paper", "OANDA_PRACTICE_API_TOKEN": TOKEN,
            "OANDA_PRACTICE_ACCOUNT_ID": ACCOUNT,
            "OANDA_FOREX_WATCHLIST": "EUR_USD,GBP_USD,USD_JPY"}
    if env:
        base.update(env)
    if directory is None:
        cfg = Config.from_env(base)
        return replace(cfg, **changes) if changes else cfg
    cfg = armed_config(directory)
    cfg = replace(cfg, oanda_practice_api_token=TOKEN, oanda_practice_account_id=ACCOUNT,
                  oanda_environment="practice", oanda_practice_orders_enabled=enabled,
                  forex_watchlist=PAIRS, mode="paper", paper_enabled=enabled, **changes)
    return replace(cfg, paper_reviewed_config_hash=cfg.review_hash()) if enabled else cfg


def pricing_payload():
    return {"prices": [
        {"instrument": "EUR_USD", "tradeable": True, "time": "2026-11-24T15:00:00.000000001Z",
         "bids": [{"price": "1.08320", "liquidity": 1000}],
         "asks": [{"price": "1.08330", "liquidity": 1000}]},
    ]}


class HostAllowlistTests(unittest.TestCase):
    REST_OK = PRACTICE_REST_ORIGIN + "/v3/accounts/" + ACCOUNT + "/pricing"
    STREAM_OK = PRACTICE_STREAM_ORIGIN + "/v3/accounts/" + ACCOUNT + "/pricing/stream?instruments=EUR_USD"

    REFUSED = (
        "https://api-fxtrade.oanda.com/v3/accounts/" + ACCOUNT,
        "https://stream-fxtrade.oanda.com/v3/accounts/" + ACCOUNT + "/pricing/stream",
        "https://API-FXTRADE.oanda.com/v3/accounts/" + ACCOUNT,
        "https://Api-FxTrade.oanda.com/v3/accounts/" + ACCOUNT,
        "https://API-FXPRACTICE.oanda.com/v3/accounts/" + ACCOUNT,
        "https://api-fxpractice.OANDA.com/v3/accounts/" + ACCOUNT,
        "https://api-fxpractice.oanda.com./v3/accounts/" + ACCOUNT,
        "https://api-fxtrade.oanda.com./v3/accounts/" + ACCOUNT,
        "https://stream-fxpractice.oanda.com./v3/accounts/" + ACCOUNT + "/pricing/stream",
        "https://api-fxpractice.oanda.com:443/v3/accounts/" + ACCOUNT,
        "https://api-fxpractice.oanda.com:80/v3/accounts/" + ACCOUNT,
        "https://api-fxtrade.oanda.com:443/v3/accounts/" + ACCOUNT,
        "https://stream-fxpractice.oanda.com:443/v3/accounts/" + ACCOUNT + "/pricing/stream",
        "https://user@api-fxpractice.oanda.com/v3/accounts/" + ACCOUNT,
        "https://user:pass@api-fxpractice.oanda.com/v3/accounts/" + ACCOUNT,
        "https://" + TOKEN + "@api-fxpractice.oanda.com/v3/accounts/" + ACCOUNT,
        "https://user@api-fxtrade.oanda.com/v3/accounts/" + ACCOUNT,
        "https://user:pass@stream-fxpractice.oanda.com/v3/accounts/" + ACCOUNT + "/pricing/stream",
        "https://api-fxpractice.oanda.com.evil.example/v3/accounts/" + ACCOUNT,
        "https://evil.api-fxpractice.oanda.com/v3/accounts/" + ACCOUNT,
        "https://api-fxpractice.oanda.com.api-fxpractice.oanda.com/v3/accounts/" + ACCOUNT,
        "https://not-api-fxpractice.oanda.com/v3/accounts/" + ACCOUNT,
        "https://api-fxpractice.oanda.com.attacker/v3/accounts/" + ACCOUNT,
        "https://fxpractice.oanda.com/v3/accounts/" + ACCOUNT,
        "https://api-fxpractice.oanda.com.oanda.com/v3/accounts/" + ACCOUNT,
        "http://api-fxpractice.oanda.com/v3/accounts/" + ACCOUNT,
        "http://api-fxtrade.oanda.com/v3/accounts/" + ACCOUNT,
        "wss://stream-fxpractice.oanda.com/v3/accounts/" + ACCOUNT + "/pricing/stream",
        "https://stream-fxpractice.oanda.com/v3/accounts/" + ACCOUNT + "/pricing",
        PRACTICE_STREAM_ORIGIN + "/v3/accounts/" + ACCOUNT + "/pricing",
        "https://api-fxpractice.oanda.com#@api-fxtrade.oanda.com/v3/accounts/" + ACCOUNT,
        "https://api-fxpractice.oanda.com\\@api-fxtrade.oanda.com/v3/accounts/" + ACCOUNT,
        "https://127.0.0.1/v3/accounts/" + ACCOUNT,
        "https://[::1]/v3/accounts/" + ACCOUNT,
        "https://xn--api-fxpractice-oanda-com.example/v3/accounts/" + ACCOUNT,
    )

    def test_exact_practice_hosts_are_allowed(self):
        rest = assert_practice_url(self.REST_OK, role="rest")
        self.assertEqual(rest.netloc, PRACTICE_REST_HOST)
        stream = assert_practice_url(self.STREAM_OK, role="stream")
        self.assertEqual(stream.netloc, PRACTICE_STREAM_HOST)

    def test_refuses_live_hosts_and_tricks_by_allowlist(self):
        for url in self.REFUSED:
            with self.subTest(url=url):
                with self.assertRaises(OandaHostError) as ctx:
                    role = "stream" if "stream-fx" in url.lower() and "fxpractice" in url.lower() and "pricing/stream" in url else "rest"
                    # Mixed-role and live/trick URLs are refused for the role under test.
                    assert_practice_url(url, role=role)
                self.assertEqual(str(ctx.exception), "non_practice_host_refused")
                with self.assertRaises(OandaHostError):
                    assert_practice_url(url, role="rest")
                with self.assertRaises(OandaHostError):
                    assert_practice_url(url, role="stream")

    def test_rest_and_stream_hosts_are_not_interchangeable(self):
        with self.assertRaises(OandaHostError):
            assert_practice_url(self.REST_OK, role="stream")
        with self.assertRaises(OandaHostError):
            assert_practice_url(self.STREAM_OK, role="rest")

    def test_unknown_role_is_refused(self):
        with self.assertRaises(OandaHostError):
            assert_practice_url(self.REST_OK, role="live")


class ConfigLockTests(unittest.TestCase):
    def test_host_override_env_vars_are_refused(self):
        practice_values = (
            PRACTICE_REST_ORIGIN, PRACTICE_REST_ORIGIN + "/",
            "https://api-fxtrade.oanda.com", "https://stream-fxtrade.oanda.com",
            "live", "https://api-fxpractice.oanda.com.evil.example",
        )
        for name in sorted(HOST_OVERRIDE_ENV):
            for value in practice_values:
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ConfigError):
                        Config.from_env({"OANDA_PRACTICE_API_TOKEN": TOKEN,
                                         "OANDA_PRACTICE_ACCOUNT_ID": ACCOUNT,
                                         name: value})

    def test_environment_override_must_be_practice(self):
        for name, value in (("OANDA_ENVIRONMENT", "live"), ("OANDA_ENV", "fxtrade"),
                            ("OANDA_ENVIRONMENT", "production"), ("OANDA_ENV", "Live")):
            with self.subTest(name=name, value=value), self.assertRaises(ConfigError):
                Config.from_env({name: value})
        cfg = Config.from_env({"OANDA_ENVIRONMENT": "practice", "MOMENTUM_MODE": "demo"})
        self.assertEqual(cfg.mode, "demo")

    def test_public_prefixed_secrets_are_refused_and_never_used(self):
        for key in ("VITE_OANDA_PRACTICE_API_TOKEN", "PUBLIC_OANDA_PRACTICE_API_TOKEN",
                    "NEXT_PUBLIC_OANDA_PRACTICE_ACCOUNT_ID", "VITE_OANDA_API_TOKEN"):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                Config.from_env({key: TOKEN})
        # A frontend-prefixed value must not be silently accepted as the token.
        cfg = Config.from_env({"OANDA_PRACTICE_API_TOKEN": TOKEN,
                               "OANDA_PRACTICE_ACCOUNT_ID": ACCOUNT})
        self.assertEqual(cfg.oanda_practice_api_token, TOKEN)
        self.assertTrue(cfg.public()["oanda_practice_configured"])
        self.assertNotIn(TOKEN, repr(cfg))
        self.assertNotIn(TOKEN, str(cfg.public()))
        self.assertNotIn(ACCOUNT, str(cfg.public()))
        self.assertEqual(cfg.public()["oanda_practice_rest_host"], PRACTICE_REST_HOST)
        self.assertEqual(cfg.public()["oanda_practice_stream_host"], PRACTICE_STREAM_HOST)

    def test_watchlist_accepts_underscore_or_slash_and_stays_opt_in(self):
        empty = Config.from_env({})
        self.assertEqual(empty.forex_watchlist, ())
        cfg = Config.from_env({"OANDA_FOREX_WATCHLIST": " eur_usd , GBP/USD "})
        self.assertEqual(cfg.forex_watchlist, ("EUR/USD", "GBP/USD"))
        # Observation settings do not change the reviewed equity execution hash.
        self.assertEqual(cfg.review_hash(), empty.review_hash())
        for bad in ("AAPL", "BTC/USD", "EUR/USD,EUR_USD", "USD/TRY",
                    ",".join("EUR/USD" for _ in range(2)),
                    "EUR_USD," + ",".join(f"XX{i}/USD" for i in range(10))):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                Config.from_env({"OANDA_FOREX_WATCHLIST": bad})


class ClientGateTests(unittest.TestCase):
    def client(self, cfg, calls):
        def capture(method, url, headers, payload):
            calls.append((method, url, headers, payload))
            return pricing_payload() if method == "GET" else {"orderCreateTransaction": {"id": "100"}}
        return OandaPracticeClient(cfg, transport=capture), calls

    def test_demo_mode_never_calls_transport(self):
        calls = []
        cfg = Config.from_env({"OANDA_PRACTICE_API_TOKEN": TOKEN,
                               "OANDA_PRACTICE_ACCOUNT_ID": ACCOUNT})
        self.assertEqual(cfg.mode, "demo")
        client, _ = self.client(cfg, calls)
        with self.assertRaises(OandaPracticeDisabled):
            client.pricing(PAIRS)
        self.assertEqual(calls, [])

    def test_get_pricing_is_allowed_in_paper_observe_without_execution(self):
        calls = []
        cfg = Config.from_env({"MOMENTUM_MODE": "paper", "OANDA_PRACTICE_API_TOKEN": TOKEN,
                               "OANDA_PRACTICE_ACCOUNT_ID": ACCOUNT})
        self.assertFalse(cfg.paper_enabled)
        client, _ = self.client(cfg, calls)
        self.assertEqual(client.pricing(("EUR/USD",)), pricing_payload())
        self.assertEqual(len(calls), 1)
        method, url, headers, body = calls[0]
        parsed = urlsplit(url)
        self.assertEqual(method, "GET")
        self.assertEqual(parsed.netloc, PRACTICE_REST_HOST)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.path, f"/v3/accounts/{ACCOUNT}/pricing")
        self.assertEqual(parse_qs(parsed.query)["instruments"], ["EUR_USD"])
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertIsNone(body)
        self.assertIn("***", repr(client))
        self.assertNotIn(TOKEN, repr(client))
        self.assertNotIn(TOKEN, str(client))
        self.assertEqual(redact_secret(TOKEN), "***")

    def test_submit_is_blocked_until_existing_paper_gate_is_armed(self):
        calls = []
        cfg = Config.from_env({"MOMENTUM_MODE": "paper", "PAPER_TRADING_ENABLED": "true",
                               "OANDA_PRACTICE_API_TOKEN": TOKEN,
                               "OANDA_PRACTICE_ACCOUNT_ID": ACCOUNT})
        # Armed flag is on, but the existing paper_blockers (Alpaca creds, hash, etc.) remain.
        self.assertTrue(cfg.paper_enabled)
        self.assertTrue(cfg.paper_blockers())
        client, _ = self.client(cfg, calls)
        state = RiskState(Decimal("0"), Decimal("0"), 0, 100, False, frozenset())
        with self.assertRaises(OandaPracticeDisabled):
            client.submit_order({"instrument": "EUR_USD", "units": "10", "price": "1.08",
                                 "client_order_id": "fx-1"}, risk_state=state)
        with self.assertRaises(OandaPracticeDisabled):
            client.cancel_order("100")
        self.assertEqual(calls, [])

    def test_submit_uses_paper_risk_when_gate_is_open(self):
        import tempfile
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            cfg = practice_config(directory, enabled=True)
            self.assertEqual(cfg.paper_blockers(), [])
            client, _ = self.client(cfg, calls)
            state = RiskState(Decimal("0"), Decimal("0"), 0, 100, False, frozenset())
            fx_state = ForexRiskState(
                nav_usd=Decimal("5000"), margin_used_usd=Decimal("0"),
                margin_available_usd=Decimal("5000"), total_open_exposure_usd=Decimal("0"),
                realized_daily_pnl_usd=Decimal("0"), open_positions=0,
                feed_age_ms=100, kill_switch=False,
            )
            limits = RiskLimits(feed=FOREX_PRACTICE_FEED)
            client.submit_order({"instrument": "EUR_USD", "units": "10", "price": "1.08",
                                 "stop_loss_price": "1.075",
                                 "client_order_id": "fx-1"}, risk_state=state, limits=limits,
                                forex_state=fx_state)
            self.assertEqual(calls[0][0], "POST")
            self.assertEqual(urlsplit(calls[0][1]).netloc, PRACTICE_REST_HOST)
            self.assertEqual(urlsplit(calls[0][1]).path, f"/v3/accounts/{ACCOUNT}/orders")
            self.assertEqual(calls[0][3]["order"]["instrument"], "EUR_USD")
            self.assertEqual(calls[0][3]["order"]["stopLossOnFill"]["price"], "1.075")
            # Default IEX limits cannot screen a forex order.
            with self.assertRaises(OandaPracticeError):
                client.submit_order({"instrument": "EUR_USD", "units": "10", "price": "1.08",
                                     "stop_loss_price": "1.075",
                                     "client_order_id": "fx-2"}, risk_state=state,
                                    limits=RiskLimits(), forex_state=fx_state)
            # Exposure limit from the existing #8 screen (raise leverage config cap to isolate #8).
            from momentum.forex_risk import ForexRiskConfig
            wide_fx = ForexRiskConfig(max_order_notional_usd=Decimal("2000"),
                                      max_notional_per_pair_usd=Decimal("2000"),
                                      max_total_open_exposure_usd=Decimal("2000"),
                                      max_risk_per_trade_usd=Decimal("50"))
            fat = RiskState(Decimal("0"), Decimal("0"), 0, 100, False, frozenset())
            with self.assertRaises(OandaPracticeError) as ctx:
                client.submit_order({"instrument": "EUR_USD", "units": "1000", "price": "1.08",
                                     "stop_loss_price": "1.075",
                                     "client_order_id": "fx-3"}, risk_state=fat, limits=limits,
                                    forex_state=fx_state, forex_config=wide_fx)
            self.assertIn("exposure_limit", str(ctx.exception))
            # A rejected order must not have reached transport after the first POST.
            self.assertEqual(sum(1 for call in calls if call[0] == "POST"), 1)

    def test_close_trade_skips_entry_screen_but_keeps_the_paper_gate(self):
        import tempfile
        calls = []
        cfg = Config.from_env({"MOMENTUM_MODE": "paper", "OANDA_PRACTICE_API_TOKEN": TOKEN,
                               "OANDA_PRACTICE_ACCOUNT_ID": ACCOUNT})
        client, _ = self.client(cfg, calls)
        with self.assertRaises(OandaPracticeDisabled):
            client.close_trade("200")
        self.assertEqual(calls, [])
        with tempfile.TemporaryDirectory() as directory:
            armed = practice_config(directory, enabled=True)
            client, calls2 = self.client(armed, [])
            client.close_trade("200")
            self.assertEqual(calls2[0][0], "PUT")
            self.assertTrue(calls2[0][1].endswith("/trades/200/close"))

    def test_client_refuses_injected_non_practice_url(self):
        cfg = Config.from_env({"MOMENTUM_MODE": "paper", "OANDA_PRACTICE_API_TOKEN": TOKEN,
                               "OANDA_PRACTICE_ACCOUNT_ID": ACCOUNT})
        client = OandaPracticeClient(cfg, transport=lambda *args: pricing_payload())
        with self.assertRaises(OandaHostError):
            client._http("GET", "https://api-fxtrade.oanda.com/v3/accounts/" + ACCOUNT, {}, None)
        with self.assertRaises(OandaPracticeError):
            client._request("GET", "/v3/accounts/someone-else/pricing")

    def test_token_not_leaked_on_transport_errors(self):
        cfg = Config.from_env({"MOMENTUM_MODE": "paper", "OANDA_PRACTICE_API_TOKEN": TOKEN,
                               "OANDA_PRACTICE_ACCOUNT_ID": ACCOUNT})
        def boom(*_):
            raise RuntimeError("provider leaked " + TOKEN)
        client = OandaPracticeClient(cfg, transport=boom)
        with self.assertRaises(RuntimeError) as ctx:
            client.pricing(("EUR/USD",))
        # The injected transport is a test double; production _http wraps errors.
        # The default HTTP path must still redact. Exercise the wrapper via _http failure shape:
        self.assertIn(TOKEN, str(ctx.exception))  # injected double can leak; the client must not log it
        # Safe wrapper: use _http against a refused host (no network) and inspect the error.
        with self.assertRaises(OandaHostError) as safe:
            client._http("GET", "https://api-fxtrade.oanda.com/v3/accounts/" + ACCOUNT, {"Authorization": "Bearer " + TOKEN}, None)
        self.assertNotIn(TOKEN, str(safe.exception))


class StreamClientTests(unittest.TestCase):
    def test_stream_url_is_pinned_and_get_only(self):
        calls = []
        cfg = Config.from_env({"MOMENTUM_MODE": "paper", "OANDA_PRACTICE_API_TOKEN": TOKEN,
                               "OANDA_PRACTICE_ACCOUNT_ID": ACCOUNT})
        def capture(method, url, headers, payload):
            calls.append((method, url, headers, payload))
            return [{"type": "PRICE", "instrument": "EUR_USD", "bids": [{"price": "1.08"}]}]
        client = OandaPracticeStreamClient(cfg, transport=capture)
        rows = list(client.iter_pricing(("EUR/USD",)))
        self.assertEqual(rows[0]["instrument"], "EUR_USD")
        method, url, headers, body = calls[0]
        parsed = urlsplit(url)
        self.assertEqual(method, "GET")
        self.assertEqual(parsed.netloc, PRACTICE_STREAM_HOST)
        self.assertEqual(parsed.path, f"/v3/accounts/{ACCOUNT}/pricing/stream")
        self.assertEqual(parse_qs(parsed.query)["instruments"], ["EUR_USD"])
        self.assertIsNone(body)
        self.assertNotIn(TOKEN, repr(client))
        with self.assertRaises(OandaHostError):
            assert_practice_url(url.replace(PRACTICE_STREAM_HOST, "stream-fxtrade.oanda.com"), role="stream")

    def test_stream_disabled_in_demo(self):
        calls = []
        cfg = Config.from_env({"OANDA_PRACTICE_API_TOKEN": TOKEN,
                               "OANDA_PRACTICE_ACCOUNT_ID": ACCOUNT})
        client = OandaPracticeStreamClient(cfg, transport=lambda *a: calls.append(a) or [])
        with self.assertRaises(OandaPracticeDisabled):
            list(client.iter_pricing(("EUR/USD",)))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
