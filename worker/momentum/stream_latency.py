"""Paper-only streaming latency instrumentation.

No broker credentials, network connections, or order submission are included.
Durations use monotonic clocks; exchange-to-receipt age requires synchronized
wall clocks and is reported separately by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Callable
import time

STAGES = ("received", "parsed", "signal", "risk_checked",
          "submitted", "acknowledged", "entry_filled",
          "exit_submitted", "exit_filled")


@dataclass
class TradeTrace:
    correlation_id: str
    clock_ns: Callable[[], int] = field(default=time.monotonic_ns, repr=False)
    stages_ns: dict[str, int] = field(default_factory=dict)

    def mark(self, stage: str) -> None:
        if stage not in STAGES:
            raise ValueError("unknown stage")
        if stage in self.stages_ns:
            raise ValueError("duplicate stage")
        now = self.clock_ns()
        if self.stages_ns and now < max(self.stages_ns.values()):
            raise ValueError("nonmonotonic timestamp")
        self.stages_ns[stage] = now

    def duration_ms(self, start: str, end: str) -> float | None:
        a, b = self.stages_ns.get(start), self.stages_ns.get(end)
        if a is None or b is None:
            return None
        return (b - a) / 1_000_000


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile, for nonempty nonnegative durations."""
    if not values:
        return None
    if not 0 <= fraction <= 1:
        raise ValueError("fraction out of range")
    if any(v < 0 for v in values):
        raise ValueError("negative duration")
    ordered = sorted(values)
    import math
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def latency_summary(traces: list[TradeTrace], start: str, end: str) -> dict:
    samples = [v for trace in traces
               if (v := trace.duration_ms(start, end)) is not None]
    return {"samples": len(samples), "missing": len(traces) - len(samples),
            "p50_ms": percentile(samples, .50),
            "p95_ms": percentile(samples, .95),
            "p99_ms": percentile(samples, .99)}
