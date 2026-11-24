"""Opt-in Alpaca market-data benchmark; no order submission or secrets in output."""
from __future__ import annotations
import argparse
import asyncio
import json
from datetime import datetime, timezone
from .stream_receiver import observe_stream
from .stream_latency import percentile

def summarize(samples):
    return {"samples": len(samples), "p50_ms": percentile(samples, .5),
            "p95_ms": percentile(samples, .95), "p99_ms": percentile(samples, .99)}

async def benchmark(market, symbols, frames):
    decode_ms = []
    age_ms = []
    stale = 0
    future = 0
    def collect(event, timing):
        nonlocal stale, future
        decode_ms.append(timing["decode_ms"])
        age = (datetime.now(timezone.utc) - event.source_at).total_seconds() * 1000
        if age < -1000:
            future += 1
        elif age > 2000:
            stale += 1
        else:
            age_ms.append(max(0, age))
    result = await observe_stream(market, symbols, collect, max_messages=frames)
    return {"market": market, "symbols": symbols, "frames": result["frames"],
            "events": result["events"], "decode_latency": summarize(decode_ms),
            "source_age": summarize(age_ms), "stale_events": stale,
            "future_events": future,
            "note": "Source age depends on clock sync; order execution not measured."}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=("stocks", "crypto"), required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--frames", type=int, default=50)
    args = parser.parse_args()
    if not 1 <= args.frames <= 1000:
        parser.error("frames must be 1 through 1000")
    print(json.dumps(asyncio.run(benchmark(args.market, args.symbols, args.frames)), sort_keys=True))

if __name__ == "__main__":
    main()
