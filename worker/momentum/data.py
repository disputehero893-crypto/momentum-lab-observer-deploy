"""Selected-watchlist snapshots. Missing or insufficient evidence stays missing."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .alpaca import parse_time, BrokerError
from .journal import utc_now

NY = ZoneInfo("America/New_York")

class SnapshotBuilder:
    def __init__(self, client, config):
        self.client = client
        self.config = config
        self._calendar_cache = None
        self._history_cache = {}

    def _prior_sessions(self, now):
        date = now.astimezone(NY).date()
        if self._calendar_cache and self._calendar_cache[0] == date:
            return self._calendar_cache[1]
        records = self.client.calendar((date - timedelta(days=65)).isoformat(), date.isoformat())
        sessions = sorted({record["date"] for record in records if record["date"] < date.isoformat()})[-30:]
        if len(sessions) != 30:
            raise BrokerError("A full 30-session trading calendar was not returned")
        self._calendar_cache = (date, sessions, any(record["date"] == date.isoformat() for record in records))
        return sessions

    def build_watchlist(self):
        at = datetime.now(timezone.utc)
        completed_until = at.replace(second=0, microsecond=0)
        prior_sessions = self._prior_sessions(at)
        try:
            metadata = json.loads(Path(self.config.metadata_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BrokerError("Cannot read point-in-time symbol metadata") from exc
        if not isinstance(metadata, dict):
            raise BrokerError("Symbol metadata must be an object keyed by symbol")
        first_date = datetime.fromisoformat(prior_sessions[0]).replace(hour=4, tzinfo=NY)
        first_utc = first_date.astimezone(timezone.utc).isoformat()
        end_utc = (completed_until - timedelta(microseconds=1)).isoformat()
        # Historical calls occur before fresh quotes. A slow history request cannot make a quote appear fresh.
        for symbol in self.config.watchlist:
            key = (symbol, self.config.data_feed, completed_until.isoformat())
            if key not in self._history_cache:
                history = self.client.bars(symbol, first_utc, end_utc)
                self._history_cache = {k: v for k, v in self._history_cache.items() if k[0] != symbol}
                self._history_cache[key] = (history, datetime.now(timezone.utc).isoformat())
        raw_snapshots = self.client.snapshots(self.config.watchlist)
        received = datetime.now(timezone.utc)
        stamp = received.isoformat().replace("+00:00", "Z")
        result = []
        for symbol in self.config.watchlist:
            raw = raw_snapshots.get(symbol, {})
            q = raw.get("latestQuote") or {}
            trade = raw.get("latestTrade") or {}
            previous = raw.get("prevDailyBar") or {}
            history, history_received = self._history_cache[(symbol, self.config.data_feed, completed_until.isoformat())]
            by_session = {}
            today = at.astimezone(NY).date().isoformat()
            cutoff_clock = completed_until.astimezone(NY).time().replace(tzinfo=None)
            todays_bars = []
            for bar in history:
                start = parse_time(bar["t"])
                local = start.astimezone(NY)
                if start + timedelta(seconds=60) > completed_until:
                    continue
                if local.hour < 4 or local.time().replace(tzinfo=None) >= cutoff_clock:
                    continue
                session = local.date().isoformat()
                by_session[session] = by_session.get(session, Decimal(0)) + Decimal(str(bar["v"]))
                if session == today:
                    todays_bars.append({
                        "start": start.isoformat(), "open": str(bar["o"]), "high": str(bar["h"]),
                        "low": str(bar["l"]), "close": str(bar["c"]), "volume": str(bar["v"]),
                        "feed": self.config.data_feed, "complete": True,
                        "available_at": history_received,
                        "bar_end": (start + timedelta(seconds=60)).isoformat(),
                        "observed_received_at": history_received,
                    })
            facts = dict(metadata.get(symbol, {}))
            # Never invent float, catalyst, or historical volume when inputs are absent.
            if previous.get("c") is not None:
                facts["previous_close"] = str(previous["c"])
            baseline = [
                {"session": session, "clock": cutoff_clock.isoformat(), "volume": str(by_session[session]),
                 "feed": self.config.data_feed, "known_at": stamp}
                for session in prior_sessions if session in by_session
            ]
            result.append({
                "symbol": symbol, "now": stamp, "feed": self.config.data_feed,
                "session": {"date": today, "is_trading_day": self._calendar_cache[2],
                            "source": "Alpaca exchange calendar"},
                "quote": {"bid": str(q["bp"]) if "bp" in q else None,
                          "ask": str(q["ap"]) if "ap" in q else None,
                          "timestamp": q.get("t"), "received_at": stamp, "feed": self.config.data_feed},
                "last_trade": {"symbol": symbol, "price": str(trade["p"]) if "p" in trade else None,
                               "timestamp": trade.get("t"), "received_at": stamp, "feed": self.config.data_feed},
                "bars": sorted(todays_bars, key=lambda b: b["start"])[-12:],
                "facts": facts,
                "rvol": {"current_volume": str(by_session[today]) if today in by_session else None,
                         "as_of": completed_until.isoformat(), "feed": self.config.data_feed,
                         "prior_sessions": prior_sessions, "history": baseline},
                "provenance": {"mode": "paper", "market_data": "Alpaca " + self.config.data_feed,
                               "metadata": "operator-supplied point-in-time facts",
                               "historical_adjustment": "raw", "synthetic": False},
            })
        return result

