"""Tests for the read-only stream receiver using an in-memory socket.

The coroutines are driven without an asyncio event loop: a selector loop
needs socket.socketpair() for its self-pipe, which the kernel network-denial
runner (run_kernel_offline.py) blocks. FakeSocket never suspends, so each
coroutine runs to completion in one step; run_sync() fails loudly if a
coroutine ever tries to really suspend (i.e. would need real I/O).
"""
import os
import unittest
from unittest.mock import patch

from momentum.stream_receiver import (
    ENDPOINTS,
    STOCKS_FEED,
    observe_stream,
    stocks_endpoint_is_iex,
)


class FakeSocket:
    def __init__(self, authenticated=True, frames=None):
        self.sent = []
        self.authenticated = authenticated
        self.receives = 0
        self.frames = frames or [
            [{"T": "t", "S": "AAPL", "p": 100.5, "t": "2026-10-20T14:00:00Z"}]
        ]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, msg):
        import json
        self.sent.append(json.loads(msg))

    async def recv(self):
        import json
        self.receives += 1
        if self.receives == 1:
            return json.dumps([{
                "T": "success" if self.authenticated else "error",
                "msg": "authenticated" if self.authenticated else "denied",
            }])
        idx = self.receives - 2
        if idx >= len(self.frames):
            idx = len(self.frames) - 1
        return json.dumps(self.frames[idx])


async def _passthrough_wait_for(aw, timeout=None):
    return await aw


def run_sync(coro):
    """Run a never-suspending coroutine to completion without an event loop."""
    with patch("momentum.stream_receiver.asyncio.wait_for", _passthrough_wait_for):
        try:
            coro.send(None)
        except StopIteration as done:
            return done.value
        coro.close()
    raise AssertionError("coroutine suspended; tests must not perform real I/O")


class StreamReceiverTests(unittest.TestCase):
    def test_auth_subscribe_and_decode(self):
        sock = FakeSocket()
        seen = []
        with patch.dict(os.environ, {"ALPACA_API_KEY": "fake", "ALPACA_SECRET_KEY": "fake"}):
            result = run_sync(observe_stream(
                "stocks",
                ["AAPL"],
                lambda event, timings: seen.append((event, timings)),
                max_messages=1,
                connect=lambda *a, **k: sock,
            ))
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["feed"], STOCKS_FEED)
        self.assertTrue(stocks_endpoint_is_iex(result["endpoint"]))
        self.assertEqual(sock.sent[0]["action"], "auth")
        self.assertEqual(sock.sent[1]["action"], "subscribe")
        self.assertEqual(seen[0][0].symbol, "AAPL")
        self.assertGreaterEqual(seen[0][1]["decode_ms"], 0)

    def test_auth_failure_fails_closed(self):
        with patch.dict(os.environ, {"ALPACA_API_KEY": "fake", "ALPACA_SECRET_KEY": "fake"}):
            with self.assertRaises(RuntimeError):
                run_sync(observe_stream(
                    "stocks",
                    ["AAPL"],
                    lambda *_: None,
                    max_messages=1,
                    connect=lambda *a, **k: FakeSocket(False),
                ))

    def test_no_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                run_sync(observe_stream("crypto", ["BTC/USD"], lambda *_: None, max_messages=1))

    def test_default_stocks_endpoint_is_iex(self):
        self.assertTrue(stocks_endpoint_is_iex(ENDPOINTS["stocks"]))
        self.assertFalse(stocks_endpoint_is_iex("wss://stream.data.alpaca.markets.attacker.example/v2/iex"))
        self.assertEqual(STOCKS_FEED, "iex")
        self.assertNotIn("sip", ENDPOINTS["stocks"].lower())

    def test_sip_endpoint_rejected(self):
        with patch.dict(os.environ, {"ALPACA_API_KEY": "fake", "ALPACA_SECRET_KEY": "fake"}):
            with self.assertRaises(ValueError) as ctx:
                run_sync(observe_stream(
                    "stocks",
                    ["AAPL"],
                    lambda *_: None,
                    max_messages=1,
                    endpoint="wss://stream.data.alpaca.markets/v2/sip",
                ))
        self.assertIn("IEX", str(ctx.exception))

    def test_unapproved_hosts_and_paths_are_rejected_before_connecting(self):
        attempted = []

        def connect(url, **kwargs):
            attempted.append(url)
            return FakeSocket()

        cases = (
            ("stocks", ["AAPL"], "wss://stream.data.alpaca.markets.attacker.example/v2/iex"),
            ("crypto", ["BTC/USD"], "wss://attacker.example/v1beta3/crypto/us"),
        )
        with patch.dict(os.environ, {"ALPACA_API_KEY": "synthetic-key",
                                     "ALPACA_SECRET_KEY": "synthetic-secret"}):
            for market, symbols, endpoint in cases:
                with self.subTest(market=market, endpoint=endpoint):
                    with self.assertRaises(ValueError):
                        run_sync(observe_stream(
                            market, symbols, lambda *_: None, max_messages=1,
                            connect=connect, endpoint=endpoint,
                        ))
        self.assertEqual(attempted, [])

    def test_approved_endpoints_cannot_be_mutated(self):
        with self.assertRaises(TypeError):
            ENDPOINTS["stocks"] = "wss://attacker.example/v2/iex"

    def test_invalid_watchlist_bounds(self):
        with patch.dict(os.environ, {"ALPACA_API_KEY": "fake", "ALPACA_SECRET_KEY": "fake"}):
            with self.assertRaises(ValueError):
                run_sync(observe_stream("stocks", [], lambda *_: None, max_messages=1))
            with self.assertRaises(ValueError):
                run_sync(observe_stream(
                    "stocks",
                    [f"S{i}" for i in range(11)],
                    lambda *_: None,
                    max_messages=1,
                ))
            with self.assertRaises(ValueError):
                run_sync(observe_stream("forex", ["EURUSD"], lambda *_: None, max_messages=1))

    def test_foreign_symbol_in_frame_skipped(self):
        sock = FakeSocket(frames=[
            [{"T": "t", "S": "MSFT", "p": 100.5, "t": "2026-10-20T14:00:00Z"}]
        ])
        seen = []
        with patch.dict(os.environ, {"ALPACA_API_KEY": "fake", "ALPACA_SECRET_KEY": "fake"}):
            result = run_sync(observe_stream(
                "stocks",
                ["AAPL"],
                lambda event, timings: seen.append(event),
                max_messages=1,
                connect=lambda *a, **k: sock,
            ))
        self.assertEqual(result["events"], 0)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
