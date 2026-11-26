# Paper streaming receiver (draft)

**Branch:** `feature/paper-stream-latency` (draft PR #6). Not merged, not deployed.

## Feed

- Stocks: free Alpaca **IEX** WebSocket `wss://stream.data.alpaca.markets/v2/iex`
- Crypto: `wss://stream.data.alpaca.markets/v1beta3/crypto/us`
- **SIP is rejected.** Do not add `/v2/sip` without Derek's paid-data yes.

## Safety

- Read-only market data. No order routes, no account APIs, no auto-start.
- Opt-in CLI benchmark only (`python -m momentum.stream_benchmark`).
- Existing Render observer is unchanged by this branch.

## Tests

```bash
cd worker && python -m unittest tests.test_stream_events tests.test_stream_latency tests.test_stream_receiver -v
```

Offline fakes only; live benchmark job stays secrets-gated and manual.
