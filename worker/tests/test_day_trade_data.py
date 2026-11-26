"""day_trade_data adapters with mocked HTTP. Every key, symbol and number here is invented."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from momentum import day_trade_data as dd
from momentum.day_trade_profiles import blockers
from momentum.models import NY
from momentum.ross_cameron_small_cap_v1 import (CatalystProvider, FloatProvider, GainersScanner, ProviderSet,
                                                RossCameronSmallCapV1, RossV1Params, five_factor_gate,
                                                provider_blockers)

FAKE_KEY = "test-not-a-real-key"
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=NY)          # Monday 10:00 ET
NOW_UTC = NOW.astimezone(timezone.utc)


def iso(ts):
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Fake:
    """Routes by URL path. ``responses`` maps a path to (status, obj|bytes|Exception) or a callable."""

    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def __call__(self, url, headers, timeout):
        parts = urlsplit(url)
        self.calls.append((parts.path, parse_qs(parts.query), dict(headers), timeout))
        item = self.responses[parts.path]
        if callable(item):
            item = item(parse_qs(parts.query))
        if isinstance(item, Exception):
            raise item
        status, body = item
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        return status, body

    def count(self, path):
        return sum(1 for c in self.calls if c[0] == path)


def fmp_ok(shares=8_000_000, symbol="ABCD"):
    return (200, [{"symbol": symbol, "date": "2026-11-20", "freeFloat": 70.5, "floatShares": shares,
                   "outstandingShares": 11_000_000, "source": "x"}])


class FmpTests(unittest.TestCase):
    def make(self, response, **kw):
        self.clock = Clock()
        self.fake = Fake({"/stable/shares-float": response})
        return dd.FmpFloatProvider(FAKE_KEY, transport=self.fake, clock=self.clock, **kw)

    def test_float_parsed_and_cached_per_symbol_per_day(self):
        p = self.make(lambda q: fmp_ok(8_000_000, q["symbol"][0]))
        self.assertEqual(p.float_shares("abcd", NOW), 8_000_000)
        self.assertEqual(p.float_shares("ABCD", NOW + timedelta(hours=3)), 8_000_000)
        self.assertEqual(self.fake.count("/stable/shares-float"), 1)
        self.assertEqual(p.float_shares("WXYZ", NOW), 8_000_000)
        self.assertEqual(self.fake.count("/stable/shares-float"), 2)
        p.float_shares("ABCD", NOW + timedelta(days=1))          # next ET day refetches
        self.assertEqual(self.fake.count("/stable/shares-float"), 3)
        self.assertIsInstance(p, FloatProvider)

    def test_request_shape_and_timeout(self):
        p = self.make(fmp_ok())
        p.float_shares("ABCD", NOW)
        path, query, headers, timeout = self.fake.calls[0]
        self.assertEqual(query["symbol"], ["ABCD"])
        self.assertEqual(timeout, 5.0)

    def test_failures_are_unknown_never_raise(self):
        bad = [(500, b""), (429, b""), (401, b""), (200, b"not json"), (200, {"Error Message": "x"}), (200, []),
               (200, [{"symbol": "ABCD", "floatShares": None}]), (200, [{"symbol": "ABCD", "floatShares": 0}]),
               (200, [{"symbol": "ABCD", "floatShares": -5}]), (200, [{"symbol": "ABCD", "floatShares": 1.5}]),
               (200, [{"symbol": "ABCD", "floatShares": "abc"}]), (200, [{"symbol": "OTHER", "floatShares": 5}]),
               (200, [7, None]), (200, b"[NaN]"), TimeoutError("slow"), OSError("down")]
        for response in bad:
            p = self.make(response)
            self.assertIsNone(p.float_shares("ABCD", NOW), response)
        self.assertIsNone(self.make(fmp_ok()).float_shares("../etc", NOW))
        self.assertIsNone(self.make(fmp_ok()).float_shares("ABCD", datetime(2026, 11, 23, 10)))  # naive as_of

    def test_unknown_cached_briefly_then_retried(self):
        p = self.make((500, b""), cooldown=0.0)
        self.assertIsNone(p.float_shares("ABCD", NOW))
        self.assertIsNone(p.float_shares("ABCD", NOW))
        self.assertEqual(self.fake.count("/stable/shares-float"), 1)
        self.clock.t += 301
        p.float_shares("ABCD", NOW)
        self.assertEqual(self.fake.count("/stable/shares-float"), 2)

    def test_429_starts_cooldown_for_other_symbols(self):
        p = self.make((429, b""))
        self.assertIsNone(p.float_shares("AAAA", NOW))
        self.assertIsNone(p.float_shares("BBBB", NOW))
        self.assertEqual(self.fake.count("/stable/shares-float"), 1)
        self.assertEqual(p.status()["cooldown_skips"], 1)

    def test_rate_budget_enforced(self):
        p = self.make(lambda q: fmp_ok(5_000_000, q["symbol"][0]), calls_per_minute=3)
        got = [p.float_shares(s, NOW) for s in ("AAAA", "BBBB", "CCCC", "DDDD")]
        self.assertEqual(got, [5_000_000] * 3 + [None])
        self.assertEqual(p.status()["throttled"], 1)
        self.clock.t += 61
        self.assertEqual(p.float_shares("DDDD", NOW), 5_000_000)

    def test_key_never_in_repr_status_or_error(self):
        p = self.make(TimeoutError(f"boom {FAKE_KEY}"))
        p.float_shares("ABCD", NOW)
        blob = repr(p) + json.dumps(p.status())
        self.assertNotIn(FAKE_KEY, blob)
        self.assertEqual(p.status()["last_error"], "TimeoutError")
        with self.assertRaises(ValueError):
            dd.FmpFloatProvider("")


def news_item(sym="ABCD", hours=2, tags=None, headline="Synthetic FDA headline", source="fiscal"):
    return {"id": 1, "headline": headline, "created_at": iso(NOW_UTC - timedelta(hours=hours)),
            "symbols": tags or [sym], "source": source}


class NewsTests(unittest.TestCase):
    def make(self, response, **kw):
        self.clock = Clock()
        self.fake = Fake({"/v1beta1/news": response})
        return dd.AlpacaNewsCatalystProvider("k-id", "k-secret", transport=self.fake, clock=self.clock, **kw)

    def test_latest_item_wins_and_headers_sent(self):
        p = self.make((200, {"news": [news_item(hours=5), news_item(hours=1, headline="Newest"), news_item(hours=3)]}))
        cat = p.latest_catalyst("abcd", NOW)
        self.assertEqual((cat.symbol, cat.headline), ("ABCD", "Newest"))
        self.assertEqual(cat.published_at, (NOW_UTC - timedelta(hours=1)).replace(microsecond=123456))
        self.assertEqual(cat.source, "alpaca-news:fiscal")
        path, query, headers, timeout = self.fake.calls[0]
        self.assertEqual(headers["APCA-API-KEY-ID"], "k-id")
        self.assertEqual(query["symbols"], ["ABCD"])
        self.assertEqual(query["end"], [NOW_UTC.strftime("%Y-%m-%dT%H:%M:%SZ")])
        self.assertEqual(query["start"], [(NOW_UTC - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")])
        self.assertIsInstance(p, CatalystProvider)

    def test_future_roundup_and_foreign_items_ignored(self):
        items = [news_item(hours=-1), news_item(tags=["ABCD", "A", "B", "C"]), news_item(tags=["OTHER"]),
                 {"headline": "", "created_at": iso(NOW_UTC), "symbols": ["ABCD"]},
                 {"headline": "x", "created_at": "garbage", "symbols": ["ABCD"]}, 3]
        self.assertIsNone(self.make((200, {"news": items})).latest_catalyst("ABCD", NOW))

    def test_failures_return_none(self):
        for response in [(500, b""), (429, b""), (403, b""), (200, b"{"), (200, {"news": "x"}), (200, []),
                         TimeoutError(), OSError()]:
            self.assertIsNone(self.make(response).latest_catalyst("ABCD", NOW), response)
        self.assertIsNone(self.make((200, {"news": []})).latest_catalyst("bad symbol!", NOW))

    def test_cache_ttl_including_no_news_but_not_failures(self):
        p = self.make((200, {"news": [news_item()]}))
        p.latest_catalyst("ABCD", NOW)
        p.latest_catalyst("ABCD", NOW)
        self.assertEqual(self.fake.count("/v1beta1/news"), 1)
        self.clock.t += 301
        p.latest_catalyst("ABCD", NOW)
        self.assertEqual(self.fake.count("/v1beta1/news"), 2)
        q = self.make((200, {"news": []}))
        q.latest_catalyst("ABCD", NOW)
        q.latest_catalyst("ABCD", NOW)
        self.assertEqual(self.fake.count("/v1beta1/news"), 1)
        r = self.make((500, b""), cooldown=0.0)
        r.latest_catalyst("ABCD", NOW)
        r.latest_catalyst("ABCD", NOW)
        self.assertEqual(self.fake.count("/v1beta1/news"), 2)       # failures are not cached

    def test_cached_item_not_returned_for_earlier_as_of(self):
        p = self.make((200, {"news": [news_item(hours=1)]}))
        self.assertIsNotNone(p.latest_catalyst("ABCD", NOW))
        self.assertIsNone(p.latest_catalyst("ABCD", NOW - timedelta(hours=2)))   # published after that as_of

    def test_secret_not_in_status(self):
        p = self.make(TimeoutError("k-secret"))
        p.latest_catalyst("ABCD", NOW)
        self.assertNotIn("k-secret", repr(p) + json.dumps(p.status()))


def mover(sym, price, pct, change):
    return {"symbol": sym, "price": price, "percent_change": pct, "change": change}


def day_bar(day_offset, vol, base=NOW):
    ts = (base + timedelta(days=day_offset)).replace(hour=0, minute=0)
    return {"t": iso(ts), "o": 5, "h": 6, "l": 4, "c": 5, "v": vol}


class ScannerTests(unittest.TestCase):
    def make(self, movers=None, snaps=None, bars=None, **kw):
        self.clock = Clock()
        movers = movers or (200, {"gainers": [mover("ABCD", 5.5, 25.0, 1.1), mover("BIGP", 90.0, 30.0, 20.0),
                                              mover("PNNY", 0.5, 80.0, 0.2), mover("SMALL", 6.0, 4.0, 0.2),
                                              mover("TWOD", 3.0, 50.0, 1.0)],
                                   "losers": [], "market_type": "stocks", "last_updated": iso(NOW_UTC - timedelta(seconds=20))})
        snaps = snaps or (200, {"ABCD": {"dailyBar": day_bar(0, 600_000)}, "TWOD": {"dailyBar": day_bar(0, 10_000)}})
        history = [day_bar(-k, 100_000) for k in range(1, 21)]
        bars = bars or (200, {"bars": {"ABCD": history, "TWOD": history[:3]}, "next_page_token": None})
        self.fake = Fake({"/v1beta1/screener/stocks/movers": movers, "/v2/stocks/snapshots": snaps, "/v2/stocks/bars": bars})
        return dd.AlpacaMoversScanner("k-id", "k-secret", transport=self.fake, clock=self.clock, **kw)

    def test_rows_filtered_ranked_and_rvol_same_feed(self):
        rows = self.make().scan(NOW)
        self.assertEqual([r.symbol for r in rows], ["ABCD", "TWOD"])      # price window + 10% gate pre-filter
        abcd, twod = rows
        self.assertEqual((abcd.rank, abcd.last, abcd.pct_change), (1, Decimal("5.5"), Decimal("25.0")))
        self.assertEqual(abcd.prior_close, Decimal("4.4"))
        self.assertEqual(abcd.rvol, Decimal(6))
        self.assertIsNone(abcd.premarket_high)
        self.assertEqual(twod.rvol, Decimal(0))                             # short history -> unknown -> 0
        self.assertEqual(twod.rank, 5)
        for path in ("/v2/stocks/snapshots", "/v2/stocks/bars"):
            call = next(c for c in self.fake.calls if c[0] == path)
            self.assertEqual(call[1]["feed"], ["iex"])
            self.assertEqual(call[1]["symbols"], ["ABCD,TWOD"])
        self.assertIsInstance(self.make(), GainersScanner)

    def test_sip_feed_passed_through(self):
        s = self.make(feed="sip")
        s.scan(NOW)
        self.assertTrue(all(c[1].get("feed") == ["sip"] for c in self.fake.calls if "screener" not in c[0]))
        with self.assertRaises(ValueError):
            dd.AlpacaMoversScanner("a", "b", feed="otc")

    def test_premarket_or_stale_list_returns_nothing(self):
        for last_updated in (iso(NOW_UTC - timedelta(days=1)), iso(NOW_UTC - timedelta(minutes=11)),
                             iso(NOW_UTC + timedelta(minutes=5)), "garbage", None):
            movers = (200, {"gainers": [mover("ABCD", 5.5, 25.0, 1.1)], "losers": [], "last_updated": last_updated})
            s = self.make(movers=movers)
            self.assertEqual(s.scan(NOW), [])
            self.assertEqual(s.last_skip, "movers_stale_or_premarket")
            self.assertEqual(self.fake.count("/v2/stocks/bars"), 0)

    def test_failures_return_empty_never_raise(self):
        for movers in [(500, b""), (429, b""), (403, b""), (200, b"x"), (200, {"gainers": "x"}), (200, [])]:
            s = self.make(movers=movers)
            self.assertEqual(s.scan(NOW), [], movers)
        s = self.make(movers=TimeoutError())
        self.assertEqual(s.scan(NOW), [])
        self.assertEqual(s.status()["last_error"], "TimeoutError")
        self.assertEqual(self.make().scan(datetime(2026, 11, 23, 10)), [])    # naive as_of

    def test_rvol_failures_keep_rows_with_zero_rvol(self):
        for snaps, bars in [((500, b""), None), (None, (500, b"")), ((200, {}), None),
                            ((200, {"ABCD": {"dailyBar": day_bar(-1, 600_000)}}), None),   # yesterday's bar
                            (None, (200, {"bars": {}}))]:
            rows = self.make(snaps=snaps, bars=bars).scan(NOW)
            self.assertEqual([r.rvol for r in rows], [Decimal(0), Decimal(0)], (snaps, bars))

    def test_malformed_mover_rows_skipped(self):
        movers = (200, {"gainers": [None, {}, mover("ABCD", "x", 25, 1), mover("ABCD", 5.5, 25.0, 6.0),
                                    mover("bad sym", 5.5, 25.0, 1.1), mover("OKAY", 5.5, 25.0, 1.1)],
                        "last_updated": iso(NOW_UTC)})
        rows = self.make(movers=movers, snaps=(200, {}), bars=(200, {"bars": {}})).scan(NOW)
        self.assertEqual([r.symbol for r in rows], ["OKAY"])

    def test_ttl_cache_limits_calls(self):
        s = self.make()
        s.scan(NOW)
        s.scan(NOW)
        self.assertEqual(self.fake.count("/v1beta1/screener/stocks/movers"), 1)
        self.clock.t += 31
        s.scan(NOW)
        self.assertEqual(self.fake.count("/v1beta1/screener/stocks/movers"), 2)

    def test_top_is_clamped(self):
        s = self.make(top=500)
        s.scan(NOW)
        self.assertEqual(self.fake.calls[0][1]["top"], ["50"])


class EndToEndTests(unittest.TestCase):
    def test_adapters_feed_the_five_factor_gate(self):
        clock = Clock()
        scanner = ScannerTests().make()
        scanner._clock = clock
        float_p = dd.FmpFloatProvider(FAKE_KEY, clock=clock, transport=Fake({"/stable/shares-float": fmp_ok(8_000_000)}))
        news_p = dd.AlpacaNewsCatalystProvider("a", "b", clock=clock, transport=Fake({"/v1beta1/news": (200, {"news": [news_item()]})}))
        rows = scanner.scan(NOW)
        ok, reasons, info = five_factor_gate(rows[0], float_p.float_shares("ABCD", NOW), news_p.latest_catalyst("ABCD", NOW), NOW_UTC, RossV1Params())
        self.assertTrue(ok, reasons)
        ok2, reasons2, _ = five_factor_gate(rows[1], None, None, NOW_UTC, RossV1Params())
        self.assertFalse(ok2)
        self.assertIn("float_unknown", reasons2)
        self.assertIn("rvol_below_5x", reasons2)

    def test_profile_still_blocked_on_iex_even_with_all_adapters(self):
        ps = dd.build_provider_set({dd.ENABLE_FLAG: "true", "FMP_API_KEY": FAKE_KEY, "ALPACA_API_KEY": "a",
                                    "ALPACA_SECRET_KEY": "b"}, transport=Fake({}))
        self.assertEqual(provider_blockers(ps, "iex"), ["requires_sip_or_full_tape_feed"])
        self.assertEqual(provider_blockers(ps, "sip"), [])
        self.assertIn("day_trade_profile_runtime_not_wired", blockers("ross_cameron_small_cap_v1", data_feed="sip", providers=ps))

    def test_dead_providers_make_refresh_find_no_candidates_without_crashing(self):
        dead = Fake({"/stable/shares-float": (500, b""), "/v1beta1/news": (500, b""),
                     "/v1beta1/screener/stocks/movers": TimeoutError()})
        ps = dd.build_provider_set({dd.ENABLE_FLAG: "1", "FMP_API_KEY": FAKE_KEY, "ALPACA_API_KEY": "a",
                                    "ALPACA_SECRET_KEY": "b", "ALPACA_DATA_FEED": "sip"}, transport=dead)
        from momentum.risk import RiskConfig
        engine = RossCameronSmallCapV1(RiskConfig(), ps, data_feed="sip")
        self.assertTrue(engine.can_trade)
        self.assertEqual(engine.refresh_candidates(NOW), {})
        self.assertEqual(engine.candidates, {})


class BuildTests(unittest.TestCase):
    ENV = {"FMP_API_KEY": FAKE_KEY, "ALPACA_API_KEY": "a", "ALPACA_SECRET_KEY": "b"}

    def test_off_by_default_even_with_all_credentials(self):
        for flag in (None, "", "0", "false", "no", "off", "maybe"):
            env = dict(self.ENV, **({} if flag is None else {dd.ENABLE_FLAG: flag}))
            ps = dd.build_provider_set(env)
            self.assertEqual(ps, ProviderSet(), flag)
        self.assertEqual(dd.build_provider_set({}), ProviderSet())

    def test_flag_on_builds_only_what_has_credentials(self):
        full = dd.build_provider_set(dict(self.ENV, **{dd.ENABLE_FLAG: "TRUE"}), transport=Fake({}))
        self.assertIsInstance(full.float_provider, dd.FmpFloatProvider)
        self.assertIsInstance(full.catalyst_provider, dd.AlpacaNewsCatalystProvider)
        self.assertIsInstance(full.gainers_scanner, dd.AlpacaMoversScanner)
        no_fmp = dd.build_provider_set({dd.ENABLE_FLAG: "1", "ALPACA_API_KEY": "a", "ALPACA_SECRET_KEY": "b"})
        self.assertIsNone(no_fmp.float_provider)
        self.assertEqual(provider_blockers(no_fmp, "sip"), ["float_provider_missing"])
        no_alpaca = dd.build_provider_set({dd.ENABLE_FLAG: "1", "FMP_API_KEY": FAKE_KEY})
        self.assertEqual(provider_blockers(no_alpaca, "sip"), ["catalyst_provider_missing", "gainers_scanner_missing"])
        self.assertEqual(provider_blockers(dd.build_provider_set({dd.ENABLE_FLAG: "1"}), "sip"),
                         ["float_provider_missing", "catalyst_provider_missing", "gainers_scanner_missing"])

    def test_flag_and_keys_do_not_touch_profile_or_review_hash(self):
        from momentum.config import Config
        hosted = {"MOMENTUM_MODE": "paper", "WATCHLIST": "AAPL,MSFT,SPY",
                  "CRYPTO_WATCHLIST": "BTC/USD,ETH/USD,SOL/USD", "CRYPTO_PAPER_EXECUTION_ENABLED": "true"}
        pinned = "2e443731843a50fe8b51cf520f5c4764cdafd6c2dafcf5f29131d1cfd0daaba5"   # hosted shape on main
        with_flag = Config.from_env({**hosted, **self.ENV, dd.ENABLE_FLAG: "true", "FMP_API_KEY": FAKE_KEY})
        self.assertEqual(with_flag.day_trade_profile, "")
        self.assertEqual(with_flag.review_hash(), pinned)
        self.assertEqual(Config.from_env({"MOMENTUM_MODE": "demo", dd.ENABLE_FLAG: "1"}).review_hash(),
                         Config.from_env({"MOMENTUM_MODE": "demo"}).review_hash())
        self.assertNotIn(FAKE_KEY, json.dumps(with_flag.public(), default=str))


class TransportTests(unittest.TestCase):
    def test_default_transport_refuses_other_hosts_and_schemes(self):
        for url in ("https://evil.example/x", "http://financialmodelingprep.com/stable/x", "file:///etc/passwd",
                    "https://data.alpaca.markets.evil.example/x"):
            with self.assertRaises(ValueError):
                dd.default_transport(url, {}, 1.0)

    def test_allowed_hosts_are_exactly_two(self):
        self.assertEqual(dd.ALLOWED_HOSTS, {"https://financialmodelingprep.com", "https://data.alpaca.markets"})

    def test_rate_limiter_window(self):
        clock = Clock()
        limiter = dd.RateLimiter(2, 10.0, clock)
        self.assertEqual([limiter.allow() for _ in range(3)], [True, True, False])
        clock.t += 10
        self.assertTrue(limiter.allow())


if __name__ == "__main__":
    unittest.main()
