"""Synthetic 1-minute bars for the day-trade profile tests. Every number is invented."""
from datetime import date, datetime, timedelta
from decimal import Decimal

from momentum.day_trade_core import DTBar
from momentum.models import NY

DAY = date(2026, 11, 23)  # a normal Monday session (16:00 close)


def at(hh, mm, day=DAY):
    return datetime(day.year, day.month, day.day, hh, 0, tzinfo=NY) + timedelta(minutes=mm)


def bar(symbol, hh, mm, o, h, l, c, v=1000, *, lag=2, day=DAY, vwap=None):
    start = at(hh, mm, day)
    o, h, l, c = (Decimal(str(x)) for x in (o, h, l, c))
    return DTBar(symbol=symbol, start=start, open=o, high=h, low=l, close=c, volume=v,
                 vwap=(h + l + c) / 3 if vwap is None else Decimal(str(vwap)),
                 received_at=None if lag is None else start + timedelta(seconds=60 + lag))


def rising(symbol, n, *, start=(9, 30), base=100.0, step=0.03, rng=0.20, day=DAY, lag=2):
    """n bars climbing ``step`` per minute with a fixed ``rng`` high-low range."""
    out = []
    t0 = datetime(day.year, day.month, day.day, *start, tzinfo=NY)
    for k in range(n):
        t = t0 + timedelta(minutes=k)
        low = round(base + step * k, 4)
        out.append(bar(symbol, t.hour, t.minute, low + 0.05, low + rng, low, low + 0.15, day=day, lag=lag))
    return out


def falling(symbol, n, *, start=(9, 30), base=100.0, step=0.03, rng=0.20, day=DAY):
    out = []
    t0 = datetime(day.year, day.month, day.day, *start, tzinfo=NY)
    for k in range(n):
        t = t0 + timedelta(minutes=k)
        high = round(base - step * k, 4)
        out.append(bar(symbol, t.hour, t.minute, high - 0.05, high, high - rng, high - 0.15, day=day))
    return out


def flat(symbol, n, *, start=(9, 30), price=100.0, rng=0.20, day=DAY):
    out = []
    t0 = datetime(day.year, day.month, day.day, *start, tzinfo=NY)
    for k in range(n):
        t = t0 + timedelta(minutes=k)
        out.append(bar(symbol, t.hour, t.minute, price + 0.1, price + rng, price, price + 0.1, day=day))
    return out


def orb_tail(symbol):
    """Breakout (09:45), retest (09:46) and confirmation (09:47) after ``rising(symbol, 15)``."""
    return [bar(symbol, 9, 45, 100.55, 100.80, 100.50, 100.75),
            bar(symbol, 9, 46, 100.74, 100.78, 100.63, 100.70),
            bar(symbol, 9, 47, 100.70, 100.90, 100.68, 100.88)]


def vwap_tail(symbol):
    """Pullback (09:55-09:57) to VWAP and confirmation (09:58) after ``rising(symbol, 25, step=0.04)``."""
    return [bar(symbol, 9, 55, 101.05, 101.08, 100.80, 100.85),
            bar(symbol, 9, 56, 100.75, 100.75, 100.62, 100.66),
            bar(symbol, 9, 57, 100.66, 100.74, 100.60, 100.68),
            bar(symbol, 9, 58, 100.68, 100.80, 100.66, 100.78)]
