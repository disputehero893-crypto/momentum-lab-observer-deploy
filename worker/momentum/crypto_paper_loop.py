"""Automatic PAPER-ONLY crypto orders from the spot momentum rule.

One cycle looks at the latest crypto watch and may submit at most one order.
The order client is the paper-host client: https://api.alpaca.markets is refused
there. CRYPTO_PAPER_EXECUTION_ENABLED defaults false; a false flag returns
before any quote decision or HTTP call.

The existing observer calls `CryptoPaperLoop.cycle` only when the flag is on.
`python -m momentum.crypto_paper_loop` runs the same cycle on a timer for a
process that is not serve-paper. After the equity serve-paper release lands,
that process can call `cycle` with the watch it already collected.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import time

from .crypto_execution import CryptoPaperClient, CryptoPaperError, PAPER_HOST
from .crypto_paper_strategy import decide_crypto, quote_from_row
from .paper_risk import CRYPTO_PAPER_FEED, CRYPTO_PAPER_PAIRS, RiskLimits, RiskState
from .risk import RiskConfig

RUN_ID = "crypto-paper"


def _client_id(pair, side, anchor):
    digest = sha256(f"{pair}|{side}|{anchor}".encode()).hexdigest()[:20]
    return "mlab_" + digest


def _money(value):
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


class CryptoPaperLoop:
    def __init__(self, config, *, journal=None, clock=None, transport=None, client=None):
        self.config = config
        self.journal = journal
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.client = client or CryptoPaperClient(
            config, transport=transport, clock=self.clock)
        # The client journals the order. The loop journals the position book.
        self.history = {pair: [] for pair in CRYPTO_PAPER_PAIRS}
        self.positions = {}
        self.realized = Decimal("0")
        self.last_fill = None
        self._load_book()

    def _load_book(self):
        if self.journal is None:
            return
        try:
            events = self.journal.events(event_type="crypto_paper_position")
        except Exception:
            return
        for event in events:
            payload = event.get("payload") or {}
            pair = payload.get("pair")
            if pair not in CRYPTO_PAPER_PAIRS:
                continue
            try:
                qty = Decimal(str(payload.get("qty") or "0"))
                entry = Decimal(str(payload.get("entry") or "0"))
                self.realized = Decimal(str(payload.get("realized_pnl") or self.realized))
            except (ArithmeticError, ValueError, TypeError):
                continue
            if qty > 0 and entry > 0:
                self.positions[pair] = {"qty": qty, "entry": entry}
            else:
                self.positions.pop(pair, None)

    def _now(self):
        moment = self.clock()
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise CryptoPaperError("crypto_clock_must_be_timezone_aware")
        return moment.astimezone(timezone.utc)

    def _disabled(self, reason):
        return {
            "enabled": reason != "crypto_paper_execution_disabled",
            "orders_submitted": 0,
            "skipped": reason,
            "decisions": [],
            "host": PAPER_HOST,
            "live_execution_supported": False,
        }

    def _risk_cfg(self):
        overrides = getattr(self.config, "risk_overrides", None) or {}
        return RiskConfig(**overrides)

    def _limits(self, risk_cfg):
        return RiskLimits(
            max_order_notional=risk_cfg.max_notional,
            max_total_exposure=min(Decimal("1000"), risk_cfg.max_notional),
            max_daily_loss=risk_cfg.max_daily_loss,
            max_open_positions=3,
            max_feed_age_ms=int(risk_cfg.max_quote_age_seconds * 1000),
            feed=CRYPTO_PAPER_FEED,
        )

    def _exposure(self):
        total = Decimal("0")
        for position in self.positions.values():
            total += position["qty"] * position["entry"]
        return total

    def _remember(self, quote):
        series = self.history.setdefault(quote["pair"], [])
        stamp = quote["source_at"].isoformat()
        if series and series[-1][0] == stamp:
            return
        series.append((stamp, quote["mid"]))
        del series[:-8]

    def _record_position(self, pair, qty, entry, client_order_id):
        if self.journal is None:
            return
        self.journal.append(RUN_ID, "crypto_paper_position", {
            "pair": pair, "qty": _money(qty), "entry": _money(entry) if entry else "0",
            "realized_pnl": _money(self.realized), "client_order_id": client_order_id,
        }, key="crypto-paper-position:" + client_order_id)

    def _risk_state(self, quote):
        seen = set()
        if self.journal is not None:
            try:
                for event in self.journal.events(event_type="crypto_paper_order_requested"):
                    request = (event.get("payload") or {}).get("request") or {}
                    if request.get("client_order_id"):
                        seen.add(request["client_order_id"])
            except Exception:
                seen.add("journal_unreadable")
        halted = False
        if self.journal is not None:
            try:
                halted = self.journal.kill_switch().get("enabled") is not False
            except Exception:
                halted = True
        return RiskState(
            exposure=self._exposure(),
            realized_daily_pnl=self.realized,
            open_positions=len(self.positions),
            feed_age_ms=quote["age_ms"],
            kill_switch=halted,
            seen_order_ids=frozenset(seen),
        )

    def _submit(self, decision, quote, limits):
        order = dict(decision["order"])
        protective = order.pop("protective", False) is True
        anchor = order.pop("anchor", None)
        if decision["action"] == "buy":
            order["client_order_id"] = _client_id(quote["pair"], "buy", anchor or quote["source_at"].isoformat())
        else:
            order["client_order_id"] = _client_id(quote["pair"], "sell", _money(self.positions[quote["pair"]]["entry"]))
        result = self.client.submit_order(
            order, risk_state=self._risk_state(quote), limits=limits, journal=self.journal,
            run_id=RUN_ID, protective=protective,
        )
        if decision["action"] == "buy":
            qty = Decimal(order["qty"])
            entry = quote["ask"]
            self.positions[quote["pair"]] = {"qty": qty, "entry": entry}
            self._record_position(quote["pair"], qty, entry, order["client_order_id"])
            self.last_fill = {
                "side": "buy", "qty": _money(qty), "price": _money(entry),
                "stop_price": order.get("stop_price"), "client_order_id": order["client_order_id"],
            }
        else:
            held = self.positions.pop(quote["pair"])
            pnl = (quote["bid"] - held["entry"]) * held["qty"]
            self.realized += pnl
            self._record_position(quote["pair"], Decimal("0"), Decimal("0"), order["client_order_id"])
            self.last_fill = {
                "side": "sell", "qty": _money(held["qty"]), "price": _money(quote["bid"]),
                "entry_price": _money(held["entry"]), "pnl": _money(pnl),
                "client_order_id": order["client_order_id"],
            }
        return result

    def _context(self, quote):
        """Display-only quote and recent-window detail for the live feed. Never used to decide."""
        try:
            window = [{"t": stamp, "mid": _money(mid)} for stamp, mid in self.history.get(quote["pair"], [])]
            return {
                "bid": _money(quote["bid"]), "ask": _money(quote["ask"]), "mid": _money(quote["mid"]),
                "quote_at": quote["source_at"].isoformat(), "window": window,
            }
        except Exception:
            return {}

    def cycle(self, watch):
        """One pass over a crypto watch. Flag off performs no order work."""
        if getattr(self.config, "crypto_paper_execution_enabled", False) is not True:
            return {
                "enabled": False, "orders_submitted": 0,
                "skipped": "crypto_paper_execution_disabled", "decisions": [],
                "host": PAPER_HOST, "live_execution_supported": False,
            }
        blockers = list(self.config.paper_blockers()) if hasattr(self.config, "paper_blockers") else ["paper_lock_unavailable"]
        if blockers:
            summary = self._disabled("paper_lock")
            summary["blockers"] = blockers
            return summary
        if self.journal is not None:
            try:
                halted = self.journal.kill_switch().get("enabled") is not False
            except Exception:
                halted = True
            if halted:
                return self._disabled("kill_switch")

        now = self._now()
        risk_cfg = self._risk_cfg()
        limits = self._limits(risk_cfg)
        rows = watch.get("rows") if isinstance(watch, dict) else None
        if not isinstance(rows, list):
            rows = []
        decisions = []
        orders = 0
        # Exits first, then at most one new buy.
        quotes = []
        for row in rows:
            quote = quote_from_row(row, now=now)
            if quote is None or quote["pair"] not in CRYPTO_PAPER_PAIRS:
                decisions.append({"pair": (row or {}).get("pair") if isinstance(row, dict) else None,
                                  "action": "hold", "reason": "quote_unusable"})
                continue
            if quote["pair"] not in tuple(getattr(self.config, "crypto_watchlist", ()) or ()):
                decisions.append({"pair": quote["pair"], "action": "hold", "reason": "not_in_watchlist"})
                continue
            self._remember(quote)
            quotes.append(quote)

        acted = False
        for quote in quotes:
            if acted:
                break
            if quote["pair"] not in self.positions:
                continue
            decision = decide_crypto(
                quote, [mid for _, mid in self.history.get(quote["pair"], [])][:-1],
                self.positions.get(quote["pair"]), risk_cfg=risk_cfg,
                exposure=self._exposure(), open_positions=len(self.positions),
                realized_pnl=self.realized,
            )
            if decision["action"] != "sell":
                decisions.append({"pair": quote["pair"], "action": "hold", "reason": decision["reason"],
                                  "rule": decision.get("rule"), **self._context(quote)})
                continue
            try:
                self._submit(decision, quote, limits)
            except CryptoPaperError as exc:
                decisions.append({"pair": quote["pair"], "action": "hold", "reason": str(exc),
                                  "intent": "sell", "signal_reason": decision["reason"],
                                  "rule": decision.get("rule"), **self._context(quote)})
                acted = True
                continue
            decisions.append({"pair": quote["pair"], "action": "sell", "reason": decision["reason"],
                              "rule": decision.get("rule"), **self._context(quote), **(self.last_fill or {})})
            orders += 1
            acted = True

        if not acted:
            for quote in quotes:
                if quote["pair"] in self.positions:
                    continue
                decision = decide_crypto(
                    quote, [mid for _, mid in self.history.get(quote["pair"], [])][:-1],
                    None, risk_cfg=risk_cfg, exposure=self._exposure(),
                    open_positions=len(self.positions), realized_pnl=self.realized,
                )
                if decision["action"] != "buy":
                    decisions.append({"pair": quote["pair"], "action": "hold", "reason": decision["reason"],
                                      "rule": decision.get("rule"), **self._context(quote)})
                    continue
                try:
                    self._submit(decision, quote, limits)
                except CryptoPaperError as exc:
                    decisions.append({"pair": quote["pair"], "action": "hold", "reason": str(exc),
                                      "intent": "buy", "signal_reason": decision["reason"],
                                      "rule": decision.get("rule"), **self._context(quote)})
                    break
                decisions.append({"pair": quote["pair"], "action": "buy", "reason": decision["reason"],
                                  "rule": decision.get("rule"), **self._context(quote), **(self.last_fill or {})})
                orders += 1
                break

        return {
            "enabled": True, "orders_submitted": orders, "skipped": None,
            "decisions": decisions, "host": PAPER_HOST, "live_execution_supported": False,
        }

    def run(self, quote_source, *, max_cycles=None, wait=None):
        """Repeat `cycle` on the caller's clock. `wait` receives poll_seconds."""
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
            raise ValueError("max_cycles must be a positive integer")
        completed = 0
        while max_cycles is None or completed < max_cycles:
            self.cycle(quote_source())
            completed += 1
            if max_cycles is not None and completed >= max_cycles:
                break
            if wait is not None:
                wait(max(1.0, float(self.config.poll_seconds)))
        return completed


def _watch_from_reader(config, clock):
    from .crypto_observation import CryptoQuoteReader, CryptoWatchCollector
    collector = CryptoWatchCollector(
        CryptoQuoteReader(config), pairs=config.crypto_watchlist, clock=clock)
    return collector.collect()


def main(argv=None):
    """Standalone paper loop. Does not start unless this module is executed."""
    from .config import Config, ConfigError
    from .journal import Journal
    import sys
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Crypto paper loop stopped: {exc}", file=sys.stderr)
        return 2
    journal = Journal(config.db_path)
    loop = CryptoPaperLoop(config, journal=journal)
    loop.run(lambda: _watch_from_reader(config, loop.clock), wait=time.sleep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
