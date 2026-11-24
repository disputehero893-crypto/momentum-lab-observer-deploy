"""Dependency-free PNG chart rendering for the optional AI advisor.

Draws price candles (or a close line), volume bars and 9/20-period simple
moving averages from bars the worker already holds. Only the Python standard
library is used (zlib + struct), so the worker image needs no new packages.
No text is drawn; the legend is described to the model in the prompt instead.
"""
from __future__ import annotations

from decimal import Decimal
import struct
import zlib
from typing import Any, Iterable, Mapping, Sequence

WIDTH, HEIGHT = 800, 480
BACKGROUND = (255, 255, 255)
GRID = (230, 230, 230)
UP = (22, 150, 70)        # green candle: close >= open
DOWN = (210, 45, 45)      # red candle: close < open
MA9 = (245, 140, 0)       # orange line
MA20 = (40, 90, 220)      # blue line
CLOSE_LINE = (60, 60, 60)
ENTRY = (22, 150, 70)
STOP = (210, 45, 45)
TARGET = (40, 90, 220)
VOLUME = (150, 150, 150)
LEGEND = ("Chart legend: green candles close up, red candles close down; orange line = 9-bar SMA; "
          "blue line = 20-bar SMA; grey bars at the bottom = volume; dashed horizontal lines, when present: "
          "green = planned entry, red = stop, blue = target. Time runs left to right, one bar per minute.")

MAX_BARS = 120


class ChartError(ValueError):
    pass


def _num(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise ChartError("invalid_bar_value")
    try:
        number = float(Decimal(str(value)))
    except Exception:
        raise ChartError("invalid_bar_value") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ChartError("invalid_bar_value")
    return number


def normalize_bars(bars: Iterable[Mapping[str, Any]], limit: int = MAX_BARS) -> list[dict[str, float | str]]:
    """Accept worker snapshot bars (open/high/...) or raw Alpaca bars (o/h/...)."""
    rows = []
    for bar in bars or ():
        if not isinstance(bar, Mapping):
            raise ChartError("invalid_bar")
        get = lambda long, short: bar.get(long, bar.get(short))  # noqa: E731
        o, h, l, c = (_num(get(k, k[0])) for k in ("open", "high", "low", "close"))
        v = _num(get("volume", "v"))
        if min(o, h, l, c) <= 0 or v < 0 or not l <= min(o, c) <= max(o, c) <= h:
            raise ChartError("invalid_ohlc")
        rows.append({"start": str(get("start", "t") or ""), "open": o, "high": h, "low": l, "close": c, "volume": v})
    rows.sort(key=lambda r: r["start"])
    return rows[-limit:]


def sma(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= period:
            total -= values[i - period]
        out.append(total / period if i >= period - 1 else None)
    return out


class _Canvas:
    def __init__(self, width: int, height: int):
        self.w, self.h = width, height
        self.px = bytearray(bytes(BACKGROUND) * (width * height))

    def dot(self, x: int, y: int, color) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i + 3] = bytes(color)

    def rect(self, x0: int, y0: int, x1: int, y1: int, color) -> None:
        x0, x1 = sorted((max(0, x0), min(self.w - 1, x1)))
        y0, y1 = sorted((max(0, y0), min(self.h - 1, y1)))
        row = bytes(color) * (x1 - x0 + 1)
        for y in range(y0, y1 + 1):
            i = (y * self.w + x0) * 3
            self.px[i:i + len(row)] = row

    def line(self, x0: int, y0: int, x1: int, y1: int, color, thick: int = 1) -> None:
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        while True:
            for ox in range(thick):
                for oy in range(thick):
                    self.dot(x0 + ox - thick // 2, y0 + oy - thick // 2, color)
            if x0 == x1 and y0 == y1:
                return
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def hline_dashed(self, y: int, x0: int, x1: int, color) -> None:
        for x in range(x0, x1):
            if (x // 8) % 2 == 0:
                self.dot(x, y, color)
                self.dot(x, y + 1, color)

    def png(self) -> bytes:
        raw = b"".join(b"\x00" + bytes(self.px[y * self.w * 3:(y + 1) * self.w * 3]) for y in range(self.h))

        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def render_chart(bars: Iterable[Mapping[str, Any]], *, style: str = "candles",
                 levels: Mapping[str, Any] | None = None,
                 width: int = WIDTH, height: int = HEIGHT) -> bytes:
    """Return PNG bytes. `levels` may carry entry/stop/target prices to mark."""
    if style not in {"candles", "line"}:
        raise ChartError("invalid_chart_style")
    rows = normalize_bars(bars)
    if len(rows) < 2:
        raise ChartError("not_enough_bars_for_chart")
    marks = {}
    for key in ("entry", "stop", "target"):
        if levels and levels.get(key) is not None:
            marks[key] = _num(levels[key])
    closes = [r["close"] for r in rows]
    ma9, ma20 = sma(closes, 9), sma(closes, 20)
    lo = min([r["low"] for r in rows] + list(marks.values()))
    hi = max([r["high"] for r in rows] + list(marks.values()))
    pad = (hi - lo) * 0.06 or hi * 0.001 or 1.0
    lo, hi = lo - pad, hi + pad
    c = _Canvas(width, height)
    left, right, top = 10, width - 10, 10
    price_bottom = int(height * 0.72)
    vol_top, vol_bottom = price_bottom + 12, height - 10
    for k in range(1, 5):
        y = top + (price_bottom - top) * k // 5
        c.rect(left, y, right, y, GRID)
    c.rect(left, vol_top - 6, right, vol_top - 6, GRID)
    n = len(rows)
    step = (right - left) / n
    body = max(1, int(step * 0.6))

    def x_at(i: int) -> int:
        return int(left + step * (i + 0.5))

    def y_at(price: float) -> int:
        return int(price_bottom - (price - lo) / (hi - lo) * (price_bottom - top))

    vmax = max(r["volume"] for r in rows) or 1.0
    for i, r in enumerate(rows):
        x = x_at(i)
        vh = int((vol_bottom - vol_top) * r["volume"] / vmax)
        c.rect(x - body // 2, vol_bottom - vh, x - body // 2 + body - 1, vol_bottom, VOLUME)
        if style == "candles":
            color = UP if r["close"] >= r["open"] else DOWN
            c.rect(x, y_at(r["high"]), x, y_at(r["low"]), color)
            c.rect(x - body // 2, y_at(max(r["open"], r["close"])), x - body // 2 + body - 1,
                   y_at(min(r["open"], r["close"])), color)
    if style == "line":
        for i in range(1, n):
            c.line(x_at(i - 1), y_at(closes[i - 1]), x_at(i), y_at(closes[i]), CLOSE_LINE, 2)
    for series, color in ((ma20, MA20), (ma9, MA9)):
        for i in range(1, n):
            if series[i - 1] is not None and series[i] is not None:
                c.line(x_at(i - 1), y_at(series[i - 1]), x_at(i), y_at(series[i]), color, 2)
    for key, color in (("entry", ENTRY), ("stop", STOP), ("target", TARGET)):
        if key in marks:
            c.hline_dashed(y_at(marks[key]), left, right, color)
    return c.png()


def summarize_bars(bars: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Small numeric facts that accompany a chart in prompts and notes."""
    rows = normalize_bars(bars)
    if not rows:
        return {"bars": 0}
    closes = [r["close"] for r in rows]
    ma9, ma20 = sma(closes, 9)[-1], sma(closes, 20)[-1]
    first, last = rows[0], rows[-1]
    out = {"bars": len(rows), "first_start": first["start"], "last_start": last["start"],
           "open": round(first["open"], 4), "last_close": round(last["close"], 4),
           "high": round(max(r["high"] for r in rows), 4), "low": round(min(r["low"] for r in rows), 4),
           "change_pct": round((last["close"] / first["open"] - 1) * 100, 3),
           "total_volume": int(sum(r["volume"] for r in rows)),
           "sma9": round(ma9, 4) if ma9 is not None else None,
           "sma20": round(ma20, 4) if ma20 is not None else None}
    if ma9 is not None and ma20 is not None:
        out["trend"] = "up" if last["close"] > ma9 > ma20 else "down" if last["close"] < ma9 < ma20 else "mixed"
    else:
        out["trend"] = "insufficient_bars"
    return out
