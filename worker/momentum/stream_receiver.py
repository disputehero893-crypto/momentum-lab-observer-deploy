"""Read-only Alpaca WebSocket receiver for paper-market research.

PAPER / OBSERVE ONLY. Stocks use the free IEX stream
(`wss://stream.data.alpaca.markets/v2/iex`). SIP is intentionally absent.
No broker order APIs or credentials are stored here. This is an opt-in
diagnostic, not part of the deployed observation worker.
Requires the optional 'websockets' package at runtime.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from types import MappingProxyType
from collections.abc import Callable

from .stream_events import decode_message

# Free-feed endpoints only. Do not add /v2/sip without Derek's paid-data yes.
ENDPOINTS = MappingProxyType({
    "stocks": "wss://stream.data.alpaca.markets/v2/iex",
    "crypto": "wss://stream.data.alpaca.markets/v1beta3/crypto/us",
})
STOCKS_FEED = "iex"


def stocks_endpoint_is_iex(url: str) -> bool:
    return isinstance(url, str) and url == ENDPOINTS["stocks"]


def _safe_connect():
    """Load a connect factory that rejects redirects before authentication."""
    from websockets.asyncio.client import connect

    class RejectRedirectingConnect(connect):
        def process_redirect(self, exc):
            # websockets 15 calls this on its connect factory, not on
            # ClientConnection. Returning the original exception makes
            # __await_impl__ raise it instead of following a Location header.
            return exc

    return RejectRedirectingConnect


async def observe_stream(
    market: str,
    symbols: list[str],
    on_event: Callable,
    *,
    max_messages: int = 100,
    connect=None,
    clock_ns=time.monotonic_ns,
    endpoint: str | None = None,
) -> dict:
    """Connect, authenticate, subscribe and collect bounded market observations.

    Only returns timing of local receipt/decoding. It does not imply broker
    execution latency, and caller must separately validate exchange-clock age.
    """
    if market not in ENDPOINTS or not symbols or len(symbols) > 10:
        raise ValueError("invalid market or watchlist")
    if not all(isinstance(s, str) and 1 <= len(s) <= 32 for s in symbols):
        raise ValueError("invalid symbol")
    if not 1 <= max_messages <= 10000:
        raise ValueError("invalid message bound")
    approved_endpoint = ENDPOINTS[market]
    if endpoint is not None and endpoint != approved_endpoint:
        # Never send server credentials to a caller-selected host or path.
        if market == "stocks":
            raise ValueError("stocks stream must use the approved free IEX endpoint")
        raise ValueError("crypto stream must use the approved endpoint")
    url = approved_endpoint
    if market == "stocks" and not stocks_endpoint_is_iex(url):
        raise ValueError("stocks stream must use free IEX endpoint")
    if "/v2/sip" in url.lower():
        raise ValueError("SIP stream rejected; free IEX only")
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("missing server-side Alpaca credentials")
    if connect is None:
        connect = _safe_connect()
    collected = 0
    decoded = 0
    connect_options = {"open_timeout": 10, "close_timeout": 5, "max_size": 1_000_000}
    async with connect(url, **connect_options) as ws:
        await ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
        auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if not isinstance(auth, list) or not any(
            isinstance(m, dict) and m.get("T") == "success"
            and m.get("msg") == "authenticated" for m in auth
        ):
            raise RuntimeError("market stream authentication not confirmed")
        await ws.send(json.dumps({"action": "subscribe", "trades": symbols, "quotes": symbols}))
        while collected < max_messages:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            received_ns = clock_ns()
            events = decode_message(raw, market)
            parsed_ns = clock_ns()
            for event in events:
                if event.symbol not in symbols:
                    continue
                on_event(event, {
                    "received_ns": received_ns,
                    "parsed_ns": parsed_ns,
                    "decode_ms": (parsed_ns - received_ns) / 1_000_000,
                })
                decoded += 1
            collected += 1
    return {
        "frames": collected,
        "events": decoded,
        "market": market,
        "feed": STOCKS_FEED if market == "stocks" else "crypto",
        "endpoint": url,
    }

# Deliberately no order submission, account access, or automatic startup.
