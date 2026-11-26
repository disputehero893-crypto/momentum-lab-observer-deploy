"""Pure candle / price-action pattern detectors for 1-minute and 5-minute bars.

Built for Derek's "see what we can do with what we have" ask: they read the
SHAPE of the candles, and use volume only as a ratio against the same symbol's
own bars on the same feed. That keeps them usable on Alpaca's free IEX feed,
where absolute volume is only ~2-3% of the tape (see docs/handbook chapter 11).

Nothing here talks to a broker, reads the environment or keeps state. Every
detector takes ``bars`` (completed bars of ONE timeframe for ONE session, oldest
first), the matching running session ``vwaps``, the index ``i`` of the bar that
just completed, and a :class:`PatternParams`. It returns ``(signal, reason)``;
``signal`` is ``None`` when the pattern is not there and ``reason`` says why.

Patterns (long only):

* ``bull_flag`` / ``first_pullback``: a sharp up-move, a short shallow pullback
  (<= 50% retrace, holds VWAP, lighter volume than the move), then a candle that
  closes above the last pullback candle's high. ``first_pullback`` is the same
  shape when it is the first dip after the opening run.
* ``micro_pullback`` (1-minute only): during a fast run, one small pause candle
  that does not undo the run, then a candle that closes over its high.
* ``opening_range_breakout``: the first 5 minutes close green, and a later
  candle is the first to close above the opening-range high (09:35-10:30 ET).
* ``vwap_reclaim``: a stock that was above VWAP dips under it for 2-8 candles,
  then a candle closes back above VWAP.
* ``higher_low_breakout``: a swing low, a bounce, a higher swing low that holds
  VWAP, then the first close above the bounce high.

Every entry also needs a candle confirmation on the trigger bar (strong close in
the top quarter of its range, or bullish engulfing) or a hammer at the pullback
low, plus an in-session volume surge (trigger bar volume vs the symbol's own
recent bars). Stops are the pattern low.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any, Sequence

from .day_trade_core import DTBar, ZERO, d, local

PATTERN_KINDS = ("bull_flag", "first_pullback", "micro_pullback", "opening_range_breakout",
                 "vwap_reclaim", "higher_low_breakout")
CONFIRMATIONS = ("strong_close", "bullish_engulfing", "hammer_at_pullback_low")


# ---- 5-minute bars ----------------------------------------------------------------
@dataclass(frozen=True)
class DTBar5(DTBar):
    """A completed 5-minute bar (same fields as ``DTBar``; only ``end`` differs)."""

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=5)


def bucket_start(ts: datetime, minutes: int = 5) -> datetime:
    """Start of the ``minutes`` bucket containing ``ts``, aligned to 09:30 ET."""
    lt = local(ts)
    anchor = lt.replace(hour=9, minute=30, second=0, microsecond=0)
    offset = int((lt - anchor).total_seconds() // 60)
    return anchor + timedelta(minutes=(offset // minutes) * minutes)


def aggregate_bars(bars: Sequence[DTBar], minutes: int = 5, *, include_partial: bool = False) -> list[DTBar5]:
    """Roll completed 1-minute bars into 5-minute bars aligned to 09:30 ET.

    A bucket is complete once a bar from its last minute (or any later bucket) has
    been seen; missing IEX minutes are simply absent (no forward fill). Volume is
    summed and the bar VWAP is volume weighted (falls back to typical price).
    """
    groups: dict[datetime, list[DTBar]] = {}
    for b in bars:
        groups.setdefault(bucket_start(b.start, minutes), []).append(b)
    out: list[DTBar5] = []
    keys = sorted(groups)
    for n, key in enumerate(keys):
        g = sorted(groups[key], key=lambda x: x.start)
        last_minute = key + timedelta(minutes=minutes - 1)
        complete = n < len(keys) - 1 or local(g[-1].start) >= last_minute
        if not complete and not include_partial:
            continue
        vol = sum(x.volume for x in g)
        pv = sum((x.price_for_vwap * x.volume for x in g), ZERO)
        received = g[-1].received_at
        out.append(DTBar5(symbol=g[0].symbol, start=key, open=g[0].open, high=max(x.high for x in g),
                          low=min(x.low for x in g), close=g[-1].close, volume=vol,
                          vwap=(pv / vol) if vol else None, received_at=received))
    return out


# ---- parameters ---------------------------------------------------------------------
@dataclass(frozen=True)
class PatternParams:
    timeframe: str = "1m"                          # "1m" or "5m"
    tick: Decimal = Decimal("0.01")
    # candle confirmations
    strong_close_top_fraction: Decimal = Decimal("0.25")   # close in the top 25% of the bar's range
    hammer_lower_wick_body_ratio: Decimal = Decimal("2")
    hammer_max_upper_wick_fraction: Decimal = Decimal("0.25")
    # IEX-relative volume: trigger bar vs the symbol's own prior session bars on this feed
    surge_lookback_bars: int = 10
    min_session_surge: Decimal = Decimal("1.5")
    min_prior_bars_for_surge: int = 3
    max_flag_volume_ratio: Decimal = Decimal("1.0")        # pullback avg vol <= impulse avg vol
    # bull flag / first pullback
    impulse_lookback_bars: int = 8
    min_impulse_pct: Decimal = Decimal("3")
    min_impulse_bars: int = 2
    min_pullback_bars: int = 2
    max_pullback_bars: int = 5
    max_retrace: Decimal = Decimal("0.50")
    # micro pullback (1m only)
    micro_run_bars: int = 3
    micro_min_run_pct: Decimal = Decimal("2")
    micro_max_retrace: Decimal = Decimal("0.30")
    # opening range breakout
    opening_range_minutes: int = 5
    orb_end: str = "10:30"                        # ET, inclusive bar end
    orb_base_bars: int = 3
    # VWAP reclaim
    reclaim_min_below_bars: int = 2
    reclaim_max_below_bars: int = 8
    reclaim_max_crosses: int = 4
    reclaim_cross_lookback: int = 20
    # higher low
    higher_low_lookback_bars: int = 20

    def __post_init__(self) -> None:
        if self.timeframe not in {"1m", "5m"}:
            raise ValueError("timeframe must be 1m or 5m")
        if self.min_pullback_bars < 1 or self.max_pullback_bars < self.min_pullback_bars:
            raise ValueError("invalid pullback bar range")
        if not ZERO < self.max_retrace < 1:
            raise ValueError("max_retrace must be between 0 and 1")

    @property
    def bar_minutes(self) -> int:
        return 1 if self.timeframe == "1m" else 5


ONE_MINUTE = PatternParams()
FIVE_MINUTE = PatternParams(timeframe="5m", min_impulse_pct=Decimal("5"), min_impulse_bars=2, min_pullback_bars=1,
                            max_pullback_bars=4, impulse_lookback_bars=6, surge_lookback_bars=6,
                            min_session_surge=Decimal("1.2"), min_prior_bars_for_surge=2,
                            reclaim_min_below_bars=1, reclaim_max_below_bars=4, reclaim_cross_lookback=12,
                            higher_low_lookback_bars=12)


@dataclass(frozen=True)
class PatternSignal:
    kind: str
    timeframe: str
    index: int
    trigger: Decimal              # breakout level (+1 tick); entry is never below it
    stop_low: Decimal             # pattern low; the engine puts the stop one tick under it
    confirmations: tuple[str, ...]
    metrics: dict = field(default_factory=dict)


# ---- single-candle helpers ------------------------------------------------------------
def candle_range(b: DTBar) -> Decimal:
    return b.high - b.low


def body(b: DTBar) -> Decimal:
    return abs(b.close - b.open)


def upper_wick(b: DTBar) -> Decimal:
    return b.high - max(b.open, b.close)


def lower_wick(b: DTBar) -> Decimal:
    return min(b.open, b.close) - b.low


def strong_close(b: DTBar, top_fraction: Decimal = Decimal("0.25")) -> bool:
    """Green candle closing in the top ``top_fraction`` of its range (buyers in control at the close)."""
    rng = candle_range(b)
    return rng > 0 and b.close > b.open and b.close >= b.high - top_fraction * rng


def bullish_engulfing(prev: DTBar, cur: DTBar) -> bool:
    """Red candle followed by a green candle whose body covers the red body (shadows ignored)."""
    return prev.red and cur.green and cur.open <= prev.close and cur.close >= prev.open and body(cur) > body(prev)


def hammer(b: DTBar, params: PatternParams = ONE_MINUTE) -> bool:
    """Long lower wick (>= 2x body and >= half the range), small upper wick."""
    rng = candle_range(b)
    if rng <= 0:
        return False
    lw = lower_wick(b)
    return (lw >= params.hammer_lower_wick_body_ratio * max(body(b), params.tick) and lw * 2 >= rng
            and upper_wick(b) <= params.hammer_max_upper_wick_fraction * rng)


def confirmations(bars: Sequence[DTBar], i: int, params: PatternParams, pullback_low_index: int | None = None) -> tuple[str, ...]:
    out = []
    cur = bars[i]
    if strong_close(cur, params.strong_close_top_fraction):
        out.append("strong_close")
    if i >= 1 and bullish_engulfing(bars[i - 1], cur):
        out.append("bullish_engulfing")
    if pullback_low_index is not None and 0 <= pullback_low_index < i and hammer(bars[pullback_low_index], params):
        out.append("hammer_at_pullback_low")
    return tuple(out)


# ---- IEX-relative volume ------------------------------------------------------------------
def _mean_volume(bars: Sequence[DTBar]) -> Decimal | None:
    if not bars:
        return None
    return Decimal(sum(b.volume for b in bars)) / Decimal(len(bars))


def session_surge(bars: Sequence[DTBar], i: int, params: PatternParams) -> Decimal | None:
    """Bar ``i`` volume / mean volume of up to ``surge_lookback_bars`` earlier bars of this session.

    A ratio of the symbol against itself on the same feed, so the IEX share of
    the tape cancels out. ``None`` when there is not enough history or no volume.
    """
    prior = bars[max(0, i - params.surge_lookback_bars):i]
    if len(prior) < params.min_prior_bars_for_surge:
        return None
    mean = _mean_volume(prior)
    if not mean:
        return None
    return Decimal(bars[i].volume) / mean


def cumulative_volume_fraction(ts: datetime) -> Decimal:
    """Rough share of a normal day's volume traded by ``ts`` (ET). Front-loaded on purpose.

    Momentum small caps trade most of their volume early, so this curve is front
    loaded; that keeps the time-of-day-adjusted RVOL conservative in the morning.
    Floored at 0.06 so the first minutes cannot blow the ratio up.
    """
    points = ((time(9, 30), "0.00"), (time(9, 35), "0.06"), (time(9, 45), "0.14"), (time(10, 0), "0.22"),
              (time(10, 30), "0.33"), (time(11, 0), "0.42"), (time(12, 0), "0.55"), (time(13, 0), "0.64"),
              (time(14, 0), "0.73"), (time(15, 0), "0.84"), (time(15, 30), "0.90"), (time(16, 0), "1.00"))
    lt = local(ts)
    minutes = lambda t: t.hour * 60 + t.minute + Decimal(getattr(t, "second", 0)) / 60  # noqa: E731
    m = minutes(lt.time())
    frac = Decimal("1")
    if m <= minutes(points[0][0]):
        frac = ZERO
    else:
        for (t0, f0), (t1, f1) in zip(points, points[1:]):
            if minutes(t0) <= m <= minutes(t1):
                span = minutes(t1) - minutes(t0)
                frac = Decimal(f0) + (Decimal(f1) - Decimal(f0)) * (m - minutes(t0)) / span
                break
    return max(frac, Decimal("0.06"))


def pace_rvol(day_volume_so_far: Decimal, avg_daily_volume: Decimal, as_of: datetime) -> Decimal | None:
    """Time-of-day adjusted relative volume, both volumes from the SAME feed (IEX vs IEX)."""
    if avg_daily_volume is None or avg_daily_volume <= 0 or day_volume_so_far is None:
        return None
    return d(day_volume_so_far) / (d(avg_daily_volume) * cumulative_volume_fraction(as_of))


# ---- internal helpers -------------------------------------------------------------------
def _session_start_ok(bars: Sequence[DTBar]) -> bool:
    return bool(bars) and local(bars[0].start).time() == time(9, 30)


def _pct(a: Decimal, b: Decimal) -> Decimal:
    return (a - b) / b * Decimal(100) if b > 0 else ZERO


def _entry_checks(bars, i, params, level: Decimal, pullback_low_index: int | None):
    """Trigger bar must close above ``level`` with a candle confirmation and a volume surge."""
    bar = bars[i]
    if not (bar.high > level and bar.close > level):
        return None, "waiting_close_above_trigger"
    conf = confirmations(bars, i, params, pullback_low_index)
    if not conf:
        return None, "no_candle_confirmation"
    surge = session_surge(bars, i, params)
    if surge is None:
        return None, "not_enough_bars_for_volume_surge"
    if surge < params.min_session_surge:
        return None, "no_volume_surge"
    return (conf, surge), "ok"


def _first_pullback(bars: Sequence[DTBar], impulse_start: int) -> bool:
    """True when no earlier pullback in the session was followed by a new high of day."""
    if impulse_start <= 0:
        return True
    hod = bars[0].high
    pulled = False
    for k in range(1, impulse_start):
        if bars[k].high > hod:
            if pulled:
                return False
            hod = bars[k].high
        elif bars[k].high < bars[k - 1].high:
            pulled = True
    return True


# ---- detectors -------------------------------------------------------------------------
def detect_pullback_breakout(bars: Sequence[DTBar], vwaps: Sequence[Decimal], i: int,
                             params: PatternParams = ONE_MINUTE) -> tuple[PatternSignal | None, str]:
    """Bull flag / first pullback: impulse, short shallow pullback above VWAP on lighter volume, break."""
    if i < params.min_impulse_bars + params.min_pullback_bars:
        return None, "flag_not_enough_bars"
    window_start = max(0, i - params.max_pullback_bars - 1)
    # The impulse peak is the highest high just before the pullback.
    peak = max(range(window_start, i), key=lambda k: (bars[k].high, k))
    pullback = list(bars[peak + 1:i])
    if len(pullback) < params.min_pullback_bars:
        return None, "flag_no_pullback"
    if len(pullback) > params.max_pullback_bars:
        return None, "flag_pullback_too_long"
    if any(b.high > bars[peak].high for b in pullback):
        return None, "flag_pullback_made_new_high"
    imp_from = max(0, peak - params.impulse_lookback_bars + 1)
    start = min(range(imp_from, peak + 1), key=lambda k: (bars[k].low, -k))
    impulse = list(bars[start:peak + 1])
    if len(impulse) < params.min_impulse_bars or sum(1 for b in impulse if b.green) < params.min_impulse_bars:
        return None, "flag_no_impulse"
    imp_low, imp_high = bars[start].low, bars[peak].high
    if _pct(imp_high, imp_low) < params.min_impulse_pct:
        return None, "flag_impulse_too_small"
    pb_low_k = min(range(peak + 1, i), key=lambda k: (bars[k].low, k))
    pb_low = bars[pb_low_k].low
    retrace = (imp_high - pb_low) / (imp_high - imp_low)
    if retrace > params.max_retrace:
        return None, "flag_retrace_over_50pct"
    if any(bars[k].low < vwaps[k] for k in range(peak + 1, i)):
        return None, "flag_broke_vwap"
    imp_vol, pb_vol = _mean_volume(impulse), _mean_volume(pullback)
    if imp_vol and pb_vol is not None and pb_vol > params.max_flag_volume_ratio * imp_vol:
        return None, "flag_pullback_volume_not_lighter"
    level = pullback[-1].high
    checked, why = _entry_checks(bars, i, params, level, pb_low_k)
    if checked is None:
        return None, "flag_" + why
    conf, surge = checked
    kind = "first_pullback" if _first_pullback(bars, start) else "bull_flag"
    return PatternSignal(kind, params.timeframe, i, level + params.tick, pb_low, conf, {
        "impulse_pct": _pct(imp_high, imp_low), "retrace": retrace, "pullback_bars": len(pullback),
        "flag_volume_ratio": (pb_vol / imp_vol) if imp_vol else None, "session_surge": surge}), kind


def detect_micro_pullback(bars: Sequence[DTBar], vwaps: Sequence[Decimal], i: int,
                          params: PatternParams = ONE_MINUTE) -> tuple[PatternSignal | None, str]:
    """1-minute only: a fast run, ONE pause candle that keeps most of the run, then a close over its high."""
    if params.timeframe != "1m":
        return None, "micro_1m_only"
    m = i - 1
    run_end = m - 1
    run_start = run_end - params.micro_run_bars + 1
    if run_start < 0:
        return None, "micro_not_enough_bars"
    run = bars[run_start:run_end + 1]
    for a, b in zip(run, run[1:]):
        if not (b.high > a.high and b.close >= a.close):
            return None, "micro_no_run"
    if sum(1 for b in run if b.green) < len(run) - 1:
        return None, "micro_no_run"
    if any(b.close <= vwaps[run_start + n] for n, b in enumerate(run)):
        return None, "micro_run_below_vwap"
    run_low, run_high = run[0].low, run[-1].high
    if _pct(run_high, run_low) < params.micro_min_run_pct:
        return None, "micro_run_too_small"
    pause = bars[m]
    is_pause = pause.high <= run_high and (pause.red or lower_wick(pause) >= body(pause))
    if not is_pause:
        return None, "micro_no_pause_candle"
    if (run_high - pause.low) / (run_high - run_low) > params.micro_max_retrace:
        return None, "micro_pause_too_deep"
    if pause.low < vwaps[m]:
        return None, "micro_pause_broke_vwap"
    checked, why = _entry_checks(bars, i, params, pause.high, m)
    if checked is None:
        return None, "micro_" + why
    conf, surge = checked
    return PatternSignal("micro_pullback", params.timeframe, i, pause.high + params.tick, pause.low, conf, {
        "run_pct": _pct(run_high, run_low), "session_surge": surge}), "micro_pullback"


def detect_opening_range_breakout(bars: Sequence[DTBar], vwaps: Sequence[Decimal], i: int,
                                  params: PatternParams = ONE_MINUTE) -> tuple[PatternSignal | None, str]:
    """Green opening range (first 5 minutes), first close above its high before 10:30 ET."""
    if not _session_start_ok(bars):
        return None, "orb_session_start_missing"
    n_or = max(1, params.opening_range_minutes // params.bar_minutes)
    if i < n_or:
        return None, "orb_range_forming"
    or_bars = bars[:n_or]
    or_end = bars[0].start + timedelta(minutes=params.opening_range_minutes)
    if any(b.start >= or_end for b in or_bars):
        return None, "orb_range_has_gaps"
    hh, mm = params.orb_end.split(":")
    if local(bars[i].end).time() > time(int(hh), int(mm)):
        return None, "orb_after_window"
    orh, orl = max(b.high for b in or_bars), min(b.low for b in or_bars)
    if not or_bars[-1].close > or_bars[0].open:
        return None, "orb_range_not_green"
    if any(b.close > orh for b in bars[n_or:i]):
        return None, "orb_already_broken"
    if bars[i].close <= vwaps[i]:
        return None, "orb_below_vwap"
    checked, why = _entry_checks(bars, i, params, orh, None)
    if checked is None:
        return None, "orb_" + why
    conf, surge = checked
    base = bars[max(0, i - params.orb_base_bars + 1):i + 1]
    stop_low = max(orl, min(b.low for b in base))
    return PatternSignal("opening_range_breakout", params.timeframe, i, orh + params.tick, stop_low, conf, {
        "or_high": orh, "or_low": orl, "session_surge": surge}), "opening_range_breakout"


def detect_vwap_reclaim(bars: Sequence[DTBar], vwaps: Sequence[Decimal], i: int,
                        params: PatternParams = ONE_MINUTE) -> tuple[PatternSignal | None, str]:
    """Was above VWAP, closed under it for a few bars, now closes back above it."""
    below = 0
    k = i - 1
    while k >= 0 and bars[k].close < vwaps[k]:
        below += 1
        k -= 1
    if below < params.reclaim_min_below_bars:
        return None, "reclaim_no_dip_below_vwap"
    if below > params.reclaim_max_below_bars:
        return None, "reclaim_too_long_below_vwap"
    if k < 0 or not any(bars[j].close > vwaps[j] for j in range(0, k + 1)):
        return None, "reclaim_never_above_vwap"
    lo = max(1, i - params.reclaim_cross_lookback)
    crosses = sum(1 for j in range(lo, i + 1) if (bars[j].close > vwaps[j]) != (bars[j - 1].close > vwaps[j - 1]))
    if crosses > params.reclaim_max_crosses:
        return None, "reclaim_vwap_chop"
    level = vwaps[i]
    dip = range(k + 1, i)
    dip_low_k = min(dip, key=lambda j: (bars[j].low, j))
    checked, why = _entry_checks(bars, i, params, level, dip_low_k)
    if checked is None:
        return None, "reclaim_" + why
    conf, surge = checked
    stop_low = min(bars[dip_low_k].low, bars[i].low)
    return PatternSignal("vwap_reclaim", params.timeframe, i, level + params.tick, stop_low, conf, {
        "bars_below_vwap": below, "vwap_crosses": crosses, "session_surge": surge}), "vwap_reclaim"


def _pivot_lows(bars: Sequence[DTBar], lo: int, hi: int) -> list[int]:
    """Indices k in [lo, hi] whose low is below both neighbours' lows (needs k-1 and k+1)."""
    return [k for k in range(max(lo, 1), hi + 1)
            if k + 1 < len(bars) and bars[k].low < bars[k - 1].low and bars[k].low <= bars[k + 1].low]


def detect_higher_low_breakout(bars: Sequence[DTBar], vwaps: Sequence[Decimal], i: int,
                               params: PatternParams = ONE_MINUTE) -> tuple[PatternSignal | None, str]:
    """Swing low, bounce high, higher swing low above VWAP, first close above the bounce high."""
    lo = max(0, i - params.higher_low_lookback_bars)
    pivots = _pivot_lows(bars, lo, i - 2)
    if len(pivots) < 2:
        return None, "higher_low_no_pivots"
    b = pivots[-1]
    earlier = [a for a in pivots[:-1] if b - a >= 2 and bars[a].low < bars[b].low]
    if not earlier:
        return None, "higher_low_not_higher"
    a = earlier[-1]
    bounce_k = max(range(a + 1, b), key=lambda k: (bars[k].high, k))
    level = bars[bounce_k].high
    if bars[b].low < vwaps[b]:
        return None, "higher_low_below_vwap"
    if any(bars[j].close > level for j in range(b + 1, i)):
        return None, "higher_low_already_broken"
    checked, why = _entry_checks(bars, i, params, level, b)
    if checked is None:
        return None, "higher_low_" + why
    conf, surge = checked
    return PatternSignal("higher_low_breakout", params.timeframe, i, level + params.tick, bars[b].low, conf, {
        "first_low": bars[a].low, "higher_low": bars[b].low, "bounce_high": level,
        "session_surge": surge}), "higher_low_breakout"


DETECTORS = (detect_micro_pullback, detect_pullback_breakout, detect_opening_range_breakout,
             detect_higher_low_breakout, detect_vwap_reclaim)


def scan_patterns(bars: Sequence[DTBar], vwaps: Sequence[Decimal], i: int,
                  params: PatternParams = ONE_MINUTE, enabled: Sequence[str] = PATTERN_KINDS
                  ) -> tuple[PatternSignal | None, list[str]]:
    """Run every detector in priority order; first enabled hit wins. Returns (signal, reasons)."""
    reasons = []
    for det in DETECTORS:
        sig, why = det(bars, vwaps, i, params)
        if sig is not None and sig.kind in enabled:
            return sig, reasons + [why]
        reasons.append(why)
    return None, reasons


def describe() -> dict[str, Any]:
    return {"patterns": list(PATTERN_KINDS), "confirmations": list(CONFIRMATIONS),
            "timeframes": ["1m", "5m"], "volume": "ratios against the symbol's own bars on the same feed"}
