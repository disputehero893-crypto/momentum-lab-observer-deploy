"""Offline regression for redirect handling in websockets 15.0.1.

The real connect factory's handshake loop is exercised with an in-memory
connection. No socket, provider endpoint, or non-synthetic credential is used.
"""
import json
import os
import unittest
from unittest.mock import patch

from websockets.asyncio.client import connect as BaseConnect
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from momentum.stream_receiver import ENDPOINTS, _safe_connect, observe_stream


class FakeTransport:
    def __init__(self):
        self.aborted = False

    def abort(self):
        self.aborted = True


class FakeConnection:
    def __init__(self, redirect, auth_frames):
        self.redirect = redirect
        self.auth_frames = auth_frames
        self.transport = FakeTransport()
        self.recv_count = 0

    async def handshake(self, *_args):
        if self.redirect is not None:
            raise self.redirect

    def start_keepalive(self):
        pass

    async def send(self, message):
        self.auth_frames.append(json.loads(message))

    async def recv(self):
        self.recv_count += 1
        if self.recv_count == 1:
            return json.dumps([{"T": "success", "msg": "authenticated"}])
        return "[]"

    async def close(self):
        pass


class RedirectHandshakeSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_connect_factory_rejects_redirect_before_auth(self):
        attacker_url = "wss://redirect.invalid/credential-capture"
        headers = Headers()
        headers["Location"] = attacker_url
        redirect = InvalidStatus(Response(302, "Found", headers, b""))

        # Prove this is a redirect the pinned websockets implementation would
        # follow by default, rather than an arbitrary synthetic exception.
        self.assertEqual(
            BaseConnect(ENDPOINTS["stocks"]).process_redirect(redirect),
            attacker_url,
        )

        attempted_urls = []
        auth_frames = []
        safe_connect = _safe_connect()

        async def fake_create_connection(factory):
            url = str(factory.uri)
            attempted_urls.append(url)
            # If the factory follows Location, the second handshake succeeds.
            # The observer would then send its auth frame to this fake target.
            return FakeConnection(
                redirect if len(attempted_urls) == 1 else None,
                auth_frames,
            )

        with patch.object(safe_connect, "create_connection", fake_create_connection), \
             patch("momentum.stream_receiver._safe_connect", return_value=safe_connect), \
             patch.dict(os.environ, {
                 "ALPACA_API_KEY": "synthetic-key",
                 "ALPACA_SECRET_KEY": "synthetic-secret",
             }):
            with self.assertRaises(InvalidStatus) as caught:
                await observe_stream(
                    "stocks",
                    ["AAPL"],
                    lambda *_: None,
                    max_messages=1,
                )

        self.assertIs(caught.exception, redirect)
        self.assertEqual(attempted_urls, [ENDPOINTS["stocks"]])
        self.assertEqual(auth_frames, [])


if __name__ == "__main__":
    unittest.main()
