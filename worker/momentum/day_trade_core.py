"""Shared pieces for the opt-in stock day-trade profiles (paper only).

Pure functions and small state objects. Nothing here talks to a broker, reads
the environment or submits an order. The two profiles that use it
(``large_cap_day_trade_v1`` and ``ross_cameron_small_cap_v1``) are OFF by
default; see ``day_trade_profiles.py`` and ``DAY_TRADE_PROFILES.md``.

All prices are ``Decimal``. All clocks are America/New_York.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Mapping

from .models import NY

ZERO = Decimal("0")
CENT = Decimal("0.01")
BAR_SECONDS = 60
# Signals only from completed bars first received within 10 s of interval end.
MAX_BAR_RECEIPT_LAG_SECONDS = 10
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)

# NYSE scheduled 13:00 ET early closes this code knows about. The runtime should
# pass the broker calendar's close; this table is only a fail-safe fallback.
KNOWN_EARLY_CLOSES: dict[date, time] = {
    date(2026, 11, 27): time(13, 0),
    date(2026, 12, 24): time(13, 0),
}


def d(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def floor_int(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def cents_up(value: Decimal) -> Decimal:
    """Round a buy limit up to the next cent (never below the computed level)."""
    q = value.quantize(CENT, rounding="ROUND_CEILING")
    return q


def cents_down(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding="ROUND_FLOOR")


@dataclass(frozen=True)
class DTBar:
    """One completed 1-minute bar. ``start`` is the minute start (tz-aware)."""
    symbol: str
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    vwap: Decimal | None = None          # the bar's own volume-weighted price (IEX proxy)
    received_at: datetime | None = None  # when the worker first saw the completed bar

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=BAR_SECONDS)

    @property
    def green(self) -> bool:
        return self.close > self.open

    @property
    def red(self) -> bool:
        return self.close < self.open

    @property
    def price_for_vwap(self) -> Decimal:
        if self.vwap is not None and self.vwap > 0:
            return self.vwap
        return (self.high + self.low + self.close) / Decimal(3)

    @classmethod
    def from_mapping(cls, symbol: str, raw: Mapping[str, Any]) -> "DTBar":
        def pick(*names):
            for name in names:
                if raw.get(name) is not None:
                    return raw[name]
            raise ValueError(f"bar is missing {names[0]}")
        start = pick("start", "t", "timestamp")
        if isinstance(start, str):
            start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if start.tzinfo is None:
            raise ValueError("bar start must be timezone-aware")
        received = raw.get("received_at")
        if isinstance(received, str):
            received = datetime.fromisoformat(received.replace("Z", "+00:00"))
        vw = raw.get("vwap", raw.get("vw"))
        bar = cls(symbol=symbol, start=start, open=d(pick("open", "o")), high=d(pick("high", "h")),
                  low=d(pick("low", "l")), close=d(pick("close", "c")), volume=int(pick("volume", "v")),
                  vwap=None if vw is None else d(vw), received_at=received)
        if min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.volume < 0:
            raise ValueError("bar prices must be positive and volume non-negative")
        if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(bar.open, bar.close):
            raise ValueError("bar high/low are inconsistent")
        return bar


def local(ts: datetime) -> datetime:
    return ts.astimezone(NY)


def minus_minutes(t: time, minutes: int) -> time:
    base = datetime(2000, 1, 1, t.hour, t.minute)
    return (base - timedelta(minutes=minutes)).time()


@dataclass(frozen=True)
class SessionClock:
    """Regular-session clock with early-close-aware cutoffs.

    Normal day: no new buys after 15:30, forced flatten from 15:50.
    Early close: entry cutoff close-30 min, flatten close-10 min (13:00 -> 12:30/12:50).
    """
    session_date: date
    close: time = REGULAR_CLOSE

    @classmethod
    def for_day(cls, day: date, close: time | None = None) -> "SessionClock":
        return cls(day, close or KNOWN_EARLY_CLOSES.get(day, REGULAR_CLOSE))

    @property
    def early_close(self) -> bool:
        return self.close < REGULAR_CLOSE

    @property
    def entry_cutoff(self) -> time:
        return minus_minutes(self.close, 30)

    @property
    def flatten_at(self) -> time:
        return minus_minutes(self.close, 10)

    def in_regular_session(self, ts: datetime) -> bool:
        lt = local(ts)
        return lt.date() == self.session_date and REGULAR_OPEN <= lt.time() < self.close

    def entries_allowed(self, ts: datetime) -> bool:
        lt = local(ts)
        return lt.date() == self.session_date and REGULAR_OPEN <= lt.time() < self.entry_cutoff

    def must_flatten(self, ts: datetime) -> bool:
        lt = local(ts)
        return lt.date() != self.session_date or lt.time() >= self.flatten_at


def bar_is_fresh(bar: DTBar, max_lag_seconds: int = MAX_BAR_RECEIPT_LAG_SECONDS) -> bool:
    """A completed bar may produce a signal only if first received <=10 s after its end."""
    if bar.received_at is None:
        return False
    lag = (bar.received_at - bar.end).total_seconds()
    return 0 <= lag <= max_lag_seconds


class SessionSeries:
    """Per-symbol, per-session completed bars with running VWAP and ATR.

    Dedupe by symbol/minute, no forward fill, resets each session, regular
    session only (the caller filters extended-hours bars out).
    """

    def __init__(self, symbol: str, session_date: date, atr_period: int = 14) -> None:
        self.symbol = symbol
        self.session_date = session_date
        self.atr_period = atr_period
        self.bars: list[DTBar] = []
        self.vwaps: list[Decimal] = []
        self.true_ranges: list[Decimal] = []
        self._pv = ZERO
        self._v = ZERO
        self._seen: set[datetime] = set()

    def add(self, bar: DTBar) -> bool:
        """Append a bar. Returns False for a duplicate or out-of-order minute."""
        if bar.start in self._seen or (self.bars and bar.start <= self.bars[-1].start):
            return False
        self._seen.add(bar.start)
        prev = self.bars[-1] if self.bars else None
        tr = bar.high - bar.low if prev is None else max(
            bar.high - bar.low, abs(bar.high - prev.close), abs(bar.low - prev.close))
        self.true_ranges.append(tr)
        self._pv += bar.price_for_vwap * bar.volume
        self._v += Decimal(bar.volume)
        if self._v > 0:
            vwap = self._pv / self._v
        else:
            vwap = self.vwaps[-1] if self.vwaps else bar.price_for_vwap
        self.vwaps.append(vwap)
        self.bars.append(bar)
        return True

    def __len__(self) -> int:
        return len(self.bars)

    def atr(self, index: int = -1) -> Decimal | None:
        """SMA of the last ``atr_period`` true ranges ending at ``index``."""
        n = len(self.bars)
        i = index if index >= 0 else n + index
        if i < 0 or i >= n or i + 1 < self.atr_period:
            return None
        window = self.true_ranges[i + 1 - self.atr_period:i + 1]
        return sum(window) / Decimal(self.atr_period)

    def ema(self, period: int = 9, index: int = -1) -> Decimal | None:
        n = len(self.bars)
        i = index if index >= 0 else n + index
        if i + 1 < period:
            return None
        k = Decimal(2) / Decimal(period + 1)
        value = sum(b.close for b in self.bars[:period]) / Decimal(period)
        for bar in self.bars[period:i + 1]:
            value = bar.close * k + value * (1 - k)
        return value


def cost_allowance(price: Decimal, floor_cost: Decimal = Decimal("0.02"),
                   global_allowance: Decimal = ZERO) -> Decimal:
    """c = max($0.02, 0.0002 x P), never below the global RiskConfig allowance."""
    return max(floor_cost, Decimal("0.0002") * price, global_allowance)


def size_shares(*, price: Decimal, stop: Decimal, cost: Decimal, risk_left: Decimal,
                portfolio_risk_left: Decimal, symbol_notional_left: Decimal,
                gross_notional_left: Decimal, max_shares: int) -> int:
    """Q = floor(min(risk/(P-S+c), portfolio risk/(P-S+c), symbol notional/P, gross/P)); whole shares."""
    per_share = price - stop + cost
    if price <= 0 or per_share <= 0:
        return 0
    limits = [risk_left / per_share, portfolio_risk_left / per_share,
              symbol_notional_left / price, gross_notional_left / price, Decimal(max_shares)]
    q = floor_int(min(limits))
    return q if q >= 1 else 0


@dataclass
class DayCounters:
    """Counters that must survive a worker restart (persist with to_state)."""
    session_date: str = ""
    campaigns_total: int = 0
    campaigns_by_symbol: dict = field(default_factory=dict)
    orb_fills_by_symbol: dict = field(default_factory=dict)
    losses_by_symbol: dict = field(default_factory=dict)
    consecutive_losses: int = 0
    realized_pnl: str = "0"
    cooldown_until: dict = field(default_factory=dict)   # symbol -> iso timestamp
    disabled_symbols: list = field(default_factory=list)
    halted_reason: str = ""

    def roll(self, day: date) -> None:
        if self.session_date != day.isoformat():
            self.__init__(session_date=day.isoformat())

    def to_state(self) -> dict:
        return {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
                for k, v in self.__dict__.items()}

    @classmethod
    def from_state(cls, state: Mapping[str, Any] | None) -> "DayCounters":
        state = dict(state or {})
        known = {k: state[k] for k in cls().__dict__ if k in state}
        return cls(**known)

    def in_cooldown(self, symbol: str, now: datetime) -> bool:
        until = self.cooldown_until.get(symbol)
        return bool(until) and now < datetime.fromisoformat(until)
