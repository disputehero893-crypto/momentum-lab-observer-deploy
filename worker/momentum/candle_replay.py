"""Offline replay / backtest for ``small_cap_candle_patterns_iex_v1`` (research only).

Runs the REAL engine over historical bars and simulates fills conservatively.
Nothing here places orders, reads the worker's config or touches a broker
account; it only reads public market data with keys passed in the environment
(``FMP_API_KEY``; optionally ``ALPACA_API_KEY``/``ALPACA_SECRET_KEY`` for IEX
bars) and never prints, logs or stores them.

Sources:

* ``--bars alpaca-iex`` (preferred, needs Alpaca keys): 1-minute bars from the
  free IEX feed, exactly what the live profile sees. Daily IEX bars give the
  same-feed average volume.
* ``--bars fmp-5m`` (what FMP Starter allows): 5-minute consolidated bars; the
  engine runs in 5-minute mode (micro pullback is 1-minute only, so it never
  fires). Volume gates are ratios, so the volume source scale cancels, but the
  IEX noise is NOT reproduced.

Universe (both): FMP company screener (CURRENT listings, price $1.5-$25, cap
< $2B), daily bars, and days whose HIGH reached +10% over the prior close
(reachable intraday, so no "closed up 10%" lookahead). Float = FMP's CURRENT
float (< 20M), not point-in-time. See ``report_caveats``.

Fill model: entry limit fills on the next bar only if price trades at or below
the limit (5m mode: only if that bar OPENS at or below it, since the limit
lives for 60 s); stops fill at min(stop, bar open) minus the cost allowance;
when a bar touches both stop and target the stop wins; the runner exits at
the close minus the cost allowance; positions are flattened by the session cut.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import candle_patterns as cp
from .day_trade_core import DTBar, ZERO, cost_allowance, d, local
from .models import NY
from .ross_cameron_small_cap_v1 import ProviderSet, ScannerRow
from .small_cap_candle_patterns_iex_v1 import CandleIexV1Params, SmallCapCandlePatternsIexV1

FMP = "https://financialmodelingprep.com/stable"
ALPACA_DATA = "https://data.alpaca.markets"


# ---- replay providers (point in time) -------------------------------------------------
class ReplayFloat:
    name = "replay-float"

    def __init__(self, floats: Mapping[str, int]):
        self.floats = dict(floats)

    def float_shares(self, symbol, as_of):
        return self.floats.get(symbol)


class ReplayScanner:
    """Rows built from bars already seen: last, % from prior close, same-source RVOL so far."""
    name = "replay-scanner"

    def __init__(self) -> None:
        self.rows: dict[str, ScannerRow] = {}

    def update(self, bar: DTBar, prior_close: Decimal, day_volume: Decimal, avg_daily_volume: Decimal) -> None:
        pct = (bar.close - prior_close) / prior_close * 100
        rvol = day_volume / avg_daily_volume if avg_daily_volume > 0 else ZERO
        self.rows[bar.symbol] = ScannerRow(bar.symbol, bar.close, prior_close, pct, rvol)

    def scan(self, as_of):
        return list(self.rows.values())


@dataclass
class Trade:
    symbol: str
    day: str
    kind: str
    timeframe: str
    signal_at: str
    entry: Decimal
    stop: Decimal
    qty: int
    exits: list = field(default_factory=list)
    pnl: Decimal = ZERO

    @property
    def risk(self) -> Decimal:
        return (self.entry - self.stop) * self.qty

    @property
    def r_multiple(self) -> Decimal:
        return self.pnl / self.risk if self.risk > 0 else ZERO


@dataclass
class DayInput:
    day: date
    bars: dict[str, list[DTBar]]                # symbol -> session bars (one timeframe)
    prior_close: dict[str, Decimal]
    avg_daily_volume: dict[str, Decimal]
    floats: dict[str, int]


def _risk_config():
    from .risk import RiskConfig
    return RiskConfig()


def replay_day(inp: DayInput, *, timeframe: str = "1m", params: CandleIexV1Params | None = None,
               risk_config: Any = None) -> tuple[list[Trade], dict]:
    """Replay one session across all its symbols with ONE engine (shared caps and loss streak)."""
    params = params or CandleIexV1Params()
    if timeframe == "5m":
        params = replace(params, timeframes=("5m",))
    scanner = ReplayScanner()
    eng = SmallCapCandlePatternsIexV1(risk_config or _risk_config(),
                                      ProviderSet(float_provider=ReplayFloat(inp.floats), gainers_scanner=scanner),
                                      data_feed="iex", params=params)
    eng.start_session(inp.day)
    stream = sorted((b for bars in inp.bars.values() for b in bars), key=lambda b: (b.start, b.symbol))
    cum_vol: dict[str, Decimal] = {}
    pending: dict[str, Trade] = {}
    open_trades: dict[str, Trade] = {}
    done: list[Trade] = []
    stats: dict[str, Any] = {"signals": 0, "unfilled": 0, "rejects": {}}

    def fill_exit(sym: str, qty: int, price: Decimal, reason: str, at: datetime) -> None:
        t = open_trades[sym]
        t.exits.append({"reason": reason, "qty": qty, "price": str(price), "at": at.isoformat()})
        total = eng.on_exit_filled(sym, qty, price, at)
        t.pnl += (price - t.entry) * qty
        if total is not None:
            done.append(open_trades.pop(sym))

    for bar in stream:
        sym = bar.symbol
        bar = replace(bar, received_at=bar.end + timedelta(seconds=2))
        # 1) session-level exits (flatten / daily loss) at the previous state
        for dec in eng.manage(bar.start, {}):
            intent = dec.intent or {}
            if intent.get("action") == "cancel_entry":
                if intent["symbol"] in pending:
                    pending.pop(intent["symbol"])
                    stats["unfilled"] += 1
            elif intent.get("action") == "exit" and intent["symbol"] == sym:
                fill_exit(sym, intent["qty"], bar.open - cost_allowance(bar.open), intent["reason"], bar.start)
        # 2) entry fill on this bar
        if sym in pending and sym in eng.pending:
            t = pending.pop(sym)
            limit = t.entry
            fills = (bar.open <= limit) if timeframe == "5m" else (bar.low <= limit)
            if fills:
                price = min(bar.open, limit)
                eng.on_entry_filled(sym, t.qty, price, bar.start)
                t.entry = price
                open_trades[sym] = t
            else:
                eng.pending.pop(sym, None)
                stats["unfilled"] += 1
        # 3) point-in-time scanner row, then the engine
        cum_vol[sym] = cum_vol.get(sym, ZERO) + bar.volume
        scanner.update(bar, inp.prior_close[sym], cum_vol[sym], inp.avg_daily_volume.get(sym, ZERO))
        eng.refresh_candidates(bar.end)
        for dec in eng.on_bar(bar, timeframe=timeframe):
            intent = dec.intent or {}
            if not dec.accepted:
                why = dec.reasons[0] if dec.reasons else "unknown"
                stats["rejects"][why] = stats["rejects"].get(why, 0) + 1
                continue
            if intent.get("side") == "buy":
                stats["signals"] += 1
                pending[sym] = Trade(sym, inp.day.isoformat(), intent["setup_class"], intent["timeframe"],
                                     bar.end.isoformat(), d(intent["entry_limit"]), d(intent["stop_price"]),
                                     int(intent["qty"]))
            elif intent.get("action") == "exit" and sym in open_trades:
                reason = intent["reason"]
                if reason in {"stop", "breakeven_stop"}:
                    stop = d(intent["price_hint"])
                    px = min(stop, bar.open) - cost_allowance(stop)
                elif reason.startswith("first_target"):
                    px = d(intent["price_hint"])
                else:
                    px = bar.close - cost_allowance(bar.close)
                fill_exit(sym, int(intent["qty"]), px, reason, bar.end)
    # end of data: anything still open is flattened at its last close
    for sym in list(open_trades):
        last = inp.bars[sym][-1]
        fill_exit(sym, eng.positions[sym]["qty"], last.close - cost_allowance(last.close), "end_of_data", last.end)
    stats["halted"] = eng.counters.halted_reason
    return done, stats


def summarize(trades: Sequence[Trade]) -> dict:
    if not trades:
        return {"trades": 0}
    rs = [t.r_multiple for t in trades]
    wins = [t for t in trades if t.pnl > 0]
    by_kind: dict[str, dict] = {}
    for t in trades:
        k = by_kind.setdefault(t.kind, {"trades": 0, "wins": 0, "sum_r": ZERO})
        k["trades"] += 1
        k["wins"] += int(t.pnl > 0)
        k["sum_r"] += t.r_multiple
    return {
        "trades": len(trades), "win_rate": round(len(wins) / len(trades), 3),
        "avg_r": round(float(sum(rs) / len(rs)), 3), "median_r": round(float(sorted(rs)[len(rs) // 2]), 3),
        "total_r": round(float(sum(rs)), 2), "total_pnl_usd": round(float(sum(t.pnl for t in trades)), 2),
        "symbols": len({t.symbol for t in trades}), "days": len({t.day for t in trades}),
        "by_pattern": {k: {"trades": v["trades"], "win_rate": round(v["wins"] / v["trades"], 3),
                           "avg_r": round(float(v["sum_r"] / v["trades"]), 3)} for k, v in sorted(by_kind.items())},
    }


def report_caveats(bars_source: str) -> list[str]:
    out = [
        "Universe is today's listed stocks (survivorship): names delisted or now under $1.5 are missing.",
        "Float is FMP's CURRENT float, not the float on the trade day; small caps often add shares after spikes.",
        "Live scanner only sees Alpaca's top movers from 09:30 ET; replay considers every qualifying stock.",
        "No news filter in replay (catalyst policy 'prefer' only logs it).",
        "Fills are modelled, not real: limit fills assume queue priority; no halts, no partial fills, no SSR.",
        "Small sample: a few weeks of one market regime. Not evidence of an edge.",
    ]
    if bars_source == "fmp-5m":
        out.insert(0, "Bars are FMP 5-minute consolidated bars, not the 1-minute IEX bars the live profile uses; "
                      "micro pullbacks and 1-minute patterns are not tested and IEX gaps/noise are not reproduced.")
    return out


# ---- data fetching (research CLI) ------------------------------------------------------------
class Http:
    def __init__(self, per_minute: int = 250, cache: Path | None = None) -> None:
        self.gap = 60.0 / per_minute
        self.last = 0.0
        self.cache = cache
        self.calls = 0
        self._lock = threading.Lock()

    def get(self, url: str, headers: Mapping[str, str] | None = None, cache_key: str | None = None) -> Any:
        if self.cache and cache_key:
            path = self.cache / (cache_key.replace("/", "_") + ".json")
            if path.exists():
                return json.loads(path.read_text())
        with self._lock:  # shared request budget across worker threads
            slot = max(self.last + self.gap, _time.monotonic())
            self.last = slot
            self.calls += 1
        wait = slot - _time.monotonic()
        if wait > 0:
            _time.sleep(wait)
        for attempt in range(3):
            try:
                with urlopen(Request(url, headers=dict(headers or {})), timeout=20) as resp:
                    data = json.loads(resp.read().decode())
                break
            except Exception as exc:  # noqa: BLE001 - research script; never echo the URL (it has the key)
                if attempt == 2:
                    print(f"fetch failed ({type(exc).__name__}) for {cache_key}", file=sys.stderr)
                    return None
                _time.sleep(2 + attempt * 3)
        if self.cache and cache_key:
            path.write_text(json.dumps(data))
        return data


def _fmp(http: Http, key: str, path: str, params: dict, cache_key: str) -> Any:
    return http.get(f"{FMP}/{path}?" + urlencode({**params, "apikey": key}), cache_key=cache_key)


def _et(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=NY)


def discover(http: Http, key: str, start: date, end: date, *, min_high_gain: Decimal = Decimal("10"),
             max_float: int = 20_000_000, log: Callable[[str], None] = print) -> dict:
    """Candidate (symbol, day) pairs whose high reached +10% over the prior close, price $2-$20, float < 20M."""
    screen = _fmp(http, key, "company-screener", {
        "priceMoreThan": 1.5, "priceLowerThan": 25, "marketCapLowerThan": 2_000_000_000, "isEtf": "false",
        "isFund": "false", "isActivelyTrading": "true", "exchange": "NASDAQ,NYSE,AMEX", "limit": 10000},
        f"screener_{end}") or []
    symbols = sorted({row["symbol"] for row in screen if isinstance(row, dict) and row.get("symbol")
                      and "." not in row["symbol"] and "-" not in row["symbol"]})
    log(f"screener symbols: {len(symbols)}")
    hist_from = start - timedelta(days=45)
    candidates: dict[str, list[dict]] = {}

    def history(sym: str) -> tuple[str, list]:
        return sym, _fmp(http, key, "historical-price-eod/full", {"symbol": sym, "from": hist_from, "to": end},
                         f"eod_{sym}_{hist_from}_{end}") or []

    with ThreadPoolExecutor(max_workers=8) as pool:
        for n, (sym, rows) in enumerate(pool.map(history, symbols)):
            rows = sorted((r for r in rows if isinstance(r, dict) and r.get("date")), key=lambda r: r["date"])
            for k in range(1, len(rows)):
                day = date.fromisoformat(rows[k]["date"])
                if not start <= day <= end:
                    continue
                prior, hi, lo = d(rows[k - 1]["close"]), d(rows[k]["high"]), d(rows[k]["low"])
                if prior <= 0 or hi < prior * (1 + min_high_gain / 100) or hi < 2 or lo > 20:
                    continue
                vols = [d(r["volume"]) for r in rows[max(0, k - 20):k] if r.get("volume")]
                if len(vols) < 10:
                    continue
                candidates.setdefault(sym, []).append({"day": day.isoformat(), "prior_close": str(prior),
                                                       "avg_volume": str(sum(vols) / len(vols))})
            if n % 250 == 0:
                log(f"  daily history {n}/{len(symbols)} ({len(candidates)} symbols with +10% days)")
    floats = {}

    def float_of(sym: str) -> tuple[str, Any]:
        return sym, _fmp(http, key, "shares-float", {"symbol": sym}, f"float_{sym}_{end}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        for sym, data in pool.map(float_of, sorted(candidates)):
            fl = data[0].get("floatShares") if isinstance(data, list) and data and isinstance(data[0], dict) else None
            if fl:
                floats[sym] = int(fl)
    kept = {s: v for s, v in candidates.items() if s in floats and floats[s] < max_float}
    log(f"+10%-high symbols: {len(candidates)}; float < 20M: {len(kept)}; "
        f"symbol-days: {sum(len(v) for v in kept.values())}")
    return {"floats": {s: floats[s] for s in kept}, "days": kept}


def fmp_5m_bars(http: Http, key: str, sym: str, day: str) -> list[DTBar]:
    rows = _fmp(http, key, "historical-chart/5min", {"symbol": sym, "from": day, "to": day}, f"m5_{sym}_{day}") or []
    out = []
    for r in sorted((r for r in rows if isinstance(r, dict) and r.get("date")), key=lambda r: r["date"]):
        start = _et(r["date"])
        lt = start.time()
        if start.date().isoformat() != day or not (datetime(2000, 1, 1, 9, 30).time() <= lt < datetime(2000, 1, 1, 16, 0).time()):
            continue
        try:
            o, h, l, c, v = (d(r[k]) for k in ("open", "high", "low", "close", "volume"))
        except (KeyError, TypeError):
            continue
        if min(o, h, l, c) <= 0 or h < max(o, c, l) or l > min(o, c):
            continue
        out.append(cp.DTBar5(symbol=sym, start=start, open=o, high=h, low=l, close=c, volume=int(v)))
    return out


def alpaca_iex_bars(http: Http, headers: Mapping[str, str], sym: str, day: str) -> list[DTBar]:
    """1-minute IEX bars for one regular session (needs Alpaca keys; free feed)."""
    start = datetime.fromisoformat(day + "T09:30:00").replace(tzinfo=NY)
    params = {"timeframe": "1Min", "start": start.isoformat(), "end": (start + timedelta(hours=6, minutes=30)).isoformat(),
              "feed": "iex", "adjustment": "split", "limit": 10000}
    data = http.get(f"{ALPACA_DATA}/v2/stocks/{sym}/bars?" + urlencode(params), headers, f"iex1m_{sym}_{day}") or {}
    out = []
    for r in data.get("bars") or []:
        try:
            out.append(DTBar.from_mapping(sym, r))
        except (ValueError, KeyError):
            continue
    return [b for b in out if local(b.start).date().isoformat() == day]


def main(argv: Sequence[str] | None = None) -> int:
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--bars", choices=("fmp-5m", "alpaca-iex"), default="fmp-5m")
    ap.add_argument("--cache", default="/tmp/candle-replay-cache")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    key = os.environ.get("FMP_API_KEY", "").strip()
    if not key:
        print("FMP_API_KEY is required (universe and float)", file=sys.stderr)
        return 2
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    http = Http(cache=cache)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    universe = discover(http, key, start, end)
    alpaca_headers = None
    if args.bars == "alpaca-iex":
        kid, sec = os.environ.get("ALPACA_API_KEY", "").strip(), os.environ.get("ALPACA_SECRET_KEY", "").strip()
        if not kid or not sec:
            print("ALPACA_API_KEY and ALPACA_SECRET_KEY are required for --bars alpaca-iex", file=sys.stderr)
            return 2
        alpaca_headers = {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec}
    by_day: dict[str, DayInput] = {}
    jobs = [(sym, item) for sym, days in universe["days"].items() for item in days]

    def fetch(job):
        sym, item = job
        day = item["day"]
        return job, (fmp_5m_bars(http, key, sym, day) if alpaca_headers is None
                     else alpaca_iex_bars(http, alpaca_headers, sym, day))

    with ThreadPoolExecutor(max_workers=8) as pool:
        for (sym, item), bars in pool.map(fetch, jobs):
            if len(bars) < 6:
                continue
            day = item["day"]
            inp = by_day.setdefault(day, DayInput(date.fromisoformat(day), {}, {}, {}, universe["floats"]))
            inp.bars[sym] = bars
            inp.prior_close[sym] = d(item["prior_close"])
            inp.avg_daily_volume[sym] = d(item["avg_volume"])
    timeframe = "5m" if alpaca_headers is None else "1m"
    trades, unfilled, signals, halted = [], 0, 0, 0
    rejects: dict[str, int] = {}
    for day in sorted(by_day):
        t, stats = replay_day(by_day[day], timeframe=timeframe)
        for why, n in stats["rejects"].items():
            rejects[why] = rejects.get(why, 0) + n
        trades.extend(t)
        unfilled += stats["unfilled"]
        signals += stats["signals"]
        halted += int(bool(stats["halted"]))
    result = {"profile": "small_cap_candle_patterns_iex_v1", "config_id": CandleIexV1Params().config_id,
              "window": [args.start, args.end], "bars_source": args.bars, "timeframe": timeframe,
              "sessions": len(by_day), "symbol_days": sum(len(x.bars) for x in by_day.values()),
              "signals": signals, "unfilled_entries": unfilled, "days_halted": halted,
              "reject_reasons": dict(sorted(rejects.items(), key=lambda kv: -kv[1])),
              "summary": summarize(trades), "caveats": report_caveats(args.bars),
              "trades": [{"symbol": t.symbol, "day": t.day, "kind": t.kind, "tf": t.timeframe,
                          "signal_at": t.signal_at, "entry": str(t.entry), "stop": str(t.stop), "qty": t.qty,
                          "pnl": str(round(t.pnl, 2)), "r": str(round(t.r_multiple, 2)), "exits": t.exits}
                         for t in trades], "api_calls": http.calls}
    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    print(json.dumps({k: v for k, v in result.items() if k != "trades"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
