"""Separate day-trade equity session for ``small_cap_candle_patterns_iex_v1`` (PAPER ONLY).

Runs inside ``serve-paper`` next to the large-cap watchlist session and the
crypto loop, on the same paper client, journal, kill switch and session ledger,
but with its own scope (the way #65 segregates crypto):

* Symbols: only symbols OUTSIDE the large-cap ``WATCHLIST`` and never crypto.
* Orders: its own journal event types (``day_trade_order_*``), so the large-cap
  session never counts these orders or positions as its own, and this session
  only reconciles positions/orders for its own symbols.
* Data: ``build_provider_set`` (FMP float, Alpaca movers + news) and IEX 1-minute
  bars from the same paper client. Missing providers or keys block ONLY this
  session (reported in ``blockers``); they are not in ``Config.paper_blockers()``.
* Risk: the engine's caps (clamped to RiskConfig: $25/trade, $75/day, $1,000
  notional, one position) plus the #8 ``check_paper_intent`` screen on every
  entry with account-wide stock exposure and the shared realized-P&L ledger.
  Protective exits are not screened.
* Paper host only: everything goes through ``PaperClient`` (paper-api only).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from .activities import crypto_paper_pair
from .alpaca import PaperDisabled, parse_time
from .day_trade_core import DTBar, cents_down
from .models import NY, Decision, plain
from .small_cap_candle_patterns_iex_v1 import PROFILE_ID, CandleIexV1Params, SmallCapCandlePatternsIexV1

EVENT_PREFIX = "day_trade"
REQUESTED = EVENT_PREFIX + "_order_requested"
TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}
MAX_SYMBOLS = 9                    # one bounded bar request per cycle (API max 10)
SCAN_INTERVAL_SECONDS = 60.0


def _whole(value) -> int:
    number = Decimal(str(value))
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError("whole-share quantity required")
    return int(number)


def is_crypto_row(row: dict) -> bool:
    symbol = row.get("symbol")
    return (row.get("asset_class") == "crypto" or crypto_paper_pair(symbol) is not None
            or (isinstance(symbol, str) and "/" in symbol))


@dataclass(frozen=True)
class DayTradeScope:
    profile: str
    config_id: str
    excluded_symbols: frozenset
    event_prefix: str = EVENT_PREFIX

    def allows(self, symbol: str) -> bool:
        return (isinstance(symbol, str) and symbol not in self.excluded_symbols
                and crypto_paper_pair(symbol) is None and "/" not in symbol)


def day_trade_claims(journal) -> tuple[frozenset, frozenset]:
    """(client_order_ids, symbols) owned by the day-trade session, from the journal."""
    ids, symbols = set(), set()
    try:
        events = journal.events(event_type=REQUESTED)
    except Exception:
        return frozenset(), frozenset()
    for event in events:
        payload = event.get("payload") or {}
        if payload.get("client_order_id"):
            ids.add(str(payload["client_order_id"]))
        symbol = (payload.get("intent") or {}).get("symbol")
        if symbol:
            symbols.add(str(symbol))
    return frozenset(ids), frozenset(symbols)


def foreign_filter(watchlist, claims: tuple[frozenset, frozenset]):
    """Predicates for the large-cap session: rows owned by crypto or the day-trade session."""
    ids, symbols = claims
    watch = set(watchlist)

    def position(row):
        return is_crypto_row(row) or (row.get("symbol") in symbols and row.get("symbol") not in watch)

    def order(row):
        return is_crypto_row(row) or (str(row.get("client_order_id")) in ids and row.get("symbol") not in watch)
    return position, order


class DayTradeEquitySession:
    def __init__(self, config, provider, journal, *, providers=None, engine=None, clock=None,
                 execution_guard: Callable[[], bool] | None = None, env=None):
        from .day_trade_data import build_provider_set
        from .risk import RiskConfig
        self.config = config
        self.provider = provider
        self.journal = journal
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.execution_guard = execution_guard or (lambda: True)
        self.profile = config.day_trade_profile
        self.params = CandleIexV1Params()
        self.risk_config = RiskConfig(**config.risk_overrides)
        self.providers = providers if providers is not None else build_provider_set(os.environ if env is None else env)
        self.engine = engine or SmallCapCandlePatternsIexV1(self.risk_config, self.providers,
                                                            data_feed=config.data_feed, params=self.params)
        self.scope = DayTradeScope(PROFILE_ID, self.params.config_id, frozenset(config.watchlist))
        self.blockers = [] if self.profile == PROFILE_ID else ["day_trade_profile_not_selected"]
        self.blockers += list(self.engine.blockers)
        self._day = None
        self._applied: dict[str, int] = {}
        self._last_scan = None
        self._last_bar: dict[str, datetime] = {}
        self._loaded: set[str] = set()
        self.last_evidence: dict | None = None
        self.last_status: dict = {"profile": self.profile, "active": False, "blockers": list(self.blockers)}

    # ---- helpers -----------------------------------------------------------------
    def status(self) -> dict:
        return plain(self.last_status)

    def exposure(self) -> tuple[Decimal, int]:
        """Exposure and open positions owned by this session (for the large-cap screen)."""
        ev = self.last_evidence or {}
        exposure, count = Decimal("0"), 0
        for pos in (ev.get("owned") or {}).values():
            if pos["qty"] > 0:
                count += 1
                exposure += pos["qty"] * Decimal(str(pos["entry_price"]))
        for order in ev.get("open_own") or []:
            if order.get("side") == "buy":
                remaining = _whole(order.get("qty") or 0) - _whole(order.get("filled_qty") or 0)
                exposure += max(remaining, 0) * Decimal(str(order.get("limit_price") or "0"))
        return exposure, count

    def _run_id(self, day) -> str:
        return "day-trade-" + day.isoformat()

    def _start_day(self, day) -> None:
        close = None
        try:
            rows = self.provider.calendar(day.isoformat(), day.isoformat())
            for row in rows or []:
                if row.get("date") == day.isoformat() and row.get("close"):
                    hh, mm = str(row["close"]).split(":")[:2]
                    close = time(int(hh), int(mm))
        except Exception:
            close = None
        self.engine.start_session(day, close)
        self.engine.positions, self.engine.pending = {}, {}
        self._day, self._applied, self._last_scan = day, {}, None
        self._last_bar, self._loaded = {}, set()

    # ---- reconcile (own scope only) ----------------------------------------------
    def _reconcile(self, now) -> dict:
        errors = []
        requests = self.journal.events(event_type=REQUESTED)
        known = {}
        for event in requests:
            payload = event.get("payload") or {}
            if payload.get("client_order_id"):
                known[payload["client_order_id"]] = {**payload, "run_id": event.get("run_id")}
        orders = {}
        for client_id, request in known.items():
            order = self.provider.order_by_client_id(client_id)
            if order is None:
                gone = (self.journal.event_by_key(EVENT_PREFIX + "_order_rejected:" + client_id)
                        or self.journal.event_by_key("not-sent:" + client_id))
                if not gone:
                    errors.append("day_trade_unresolved_order_submission")
                continue
            if order.get("symbol") != request["intent"]["symbol"] or order.get("side") != request["intent"]["side"]:
                errors.append("day_trade_order_identity_mismatch")
                continue
            if order.get("filled_qty") in (None, ""):
                errors.append("day_trade_filled_quantity_unknown")
                continue
            orders[client_id] = order
        owned: dict[str, dict] = {}
        fills = []
        for client_id, request in known.items():
            order = orders.get(client_id)
            if not order:
                continue
            filled = _whole(order["filled_qty"])
            if not filled:
                continue
            symbol, side = order["symbol"], order["side"]
            price = Decimal(str(order.get("filled_avg_price") or "0"))
            if price <= 0:
                errors.append("day_trade_filled_price_unknown")
                continue
            pos = owned.setdefault(symbol, {"qty": 0, "entry_price": Decimal("0"),
                                            "setup_id": request["intent"].get("setup_id")})
            if side == "buy":
                cost = pos["entry_price"] * pos["qty"] + price * filled
                pos["qty"] += filled
                pos["entry_price"] = cost / pos["qty"]
                pos["setup_id"] = request["intent"].get("setup_id")
            elif filled > pos["qty"]:
                errors.append("day_trade_excess_sell_fill")
            else:
                pos["qty"] -= filled
            fills.append((client_id, request, order, filled, price))
        owned = {s: p for s, p in owned.items() if p["qty"] > 0}
        actual = {}
        for row in self.provider.positions():
            if is_crypto_row(row) or not self.scope.allows(row.get("symbol")):
                continue  # crypto loop or large-cap watchlist: not ours to reconcile
            if row.get("side") != "long":
                errors.append("day_trade_short_or_unknown_position")
                continue
            qty = _whole(row.get("qty") or 0)
            if qty:
                actual[row["symbol"]] = qty
        if {s: p["qty"] for s, p in owned.items()} != actual:
            errors.append("day_trade_position_mismatch_or_unowned_position")
        open_rows = self.provider.open_orders()
        open_own, all_equity_open = [], []
        for row in open_rows:
            if is_crypto_row(row):
                continue
            all_equity_open.append(row)
            if not self.scope.allows(row.get("symbol")):
                continue
            if row.get("client_order_id") in known:
                open_own.append(row)
            else:
                errors.append("day_trade_unowned_open_order")
        return {"known": known, "orders": orders, "owned": owned, "fills": fills, "open_own": open_own,
                "all_equity_open": all_equity_open, "errors": sorted(set(errors)), "as_of": now}

    def _apply_fills(self, evidence) -> None:
        """Feed broker-confirmed fill increments of today's orders into the engine (idempotent)."""
        day_run = self._run_id(self._day)
        eng = self.engine
        for client_id, request, order, filled, price in evidence["fills"]:
            if request.get("run_id") != day_run:
                continue
            done = self._applied.get(client_id, 0)
            step = filled - done
            if step <= 0:
                continue
            intent = request["intent"]
            sym = intent["symbol"]
            at = parse_time(order.get("filled_at") or order.get("updated_at") or self.clock().isoformat())
            if order["side"] == "buy":
                if sym in eng.positions:
                    pos = eng.positions[sym]
                    pos["entry"] = (pos["entry"] * pos["qty"] + price * step) / (pos["qty"] + step)
                    pos["qty"] += step
                    pos["initial_qty"] += step
                else:
                    eng.pending.setdefault(sym, {"stop": Decimal(str(intent["stop_price"])),
                                                 "entry": Decimal(str(intent["entry_limit"])),
                                                 "qty": int(intent["qty"]), "kind": intent.get("pattern", "unknown"),
                                                 "expires_at": at})
                    eng.on_entry_filled(sym, step, price, at)
            elif sym in eng.positions:
                eng.on_exit_filled(sym, step, price, at)
            self._applied[client_id] = filled
        # An entry order that ended without filling frees the engine slot.
        for client_id, request in evidence["known"].items():
            order = evidence["orders"].get(client_id)
            sym = request["intent"]["symbol"]
            if order and order["side"] == "buy" and order.get("status") in TERMINAL and sym in eng.pending:
                eng.pending.pop(sym, None)

    # ---- market data -------------------------------------------------------------
    def _quote(self, symbol) -> dict | None:
        raw = (self.provider.snapshots([symbol]) or {}).get(symbol) or {}
        q = raw.get("latestQuote") or {}
        if "bp" not in q or "ap" not in q:
            return None
        stamp = self.clock().isoformat().replace("+00:00", "Z")
        return {"symbol": symbol, "bid": str(q["bp"]), "ask": str(q["ap"]), "timestamp": q.get("t"),
                "received_at": stamp, "feed": self.config.data_feed}

    def _bars(self, symbols, now) -> list[DTBar]:
        if not symbols:
            return []
        minute = now.replace(second=0, microsecond=0)
        out = []
        new = [s for s in symbols if s not in self._loaded]
        open_utc = datetime.combine(self._day, time(9, 30), NY).astimezone(timezone.utc)
        for symbol in new:
            rows = self.provider.bars(symbol, open_utc.isoformat(), (minute - timedelta(microseconds=1)).isoformat())
            out.extend(DTBar.from_mapping(symbol, {**r, "received_at": now}) for r in rows or [])
            self._loaded.add(symbol)
        recent = [s for s in symbols if s not in new]
        if recent:
            page = self.provider.observation_bars(recent, timeframe="1Min", start=minute - timedelta(minutes=9),
                                                  end=minute - timedelta(microseconds=1))
            for symbol, rows in ((page or {}).get("bars") or {}).items():
                if symbol in recent:
                    out.extend(DTBar.from_mapping(symbol, {**r, "received_at": now}) for r in rows or [])
        fresh = []
        for bar in sorted(out, key=lambda b: (b.symbol, b.start)):
            if bar.end > now or bar.start <= self._last_bar.get(bar.symbol, datetime.min.replace(tzinfo=timezone.utc)):
                continue
            self._last_bar[bar.symbol] = bar.start
            fresh.append(bar)
        return fresh

    # ---- risk screen -------------------------------------------------------------
    def _screen(self, intent, quote, evidence, ledger) -> tuple[bool, str]:
        from .alpaca import PaperClient
        from .paper_risk import PaperIntent, RiskState, check_paper_intent, limits_for_risk
        try:
            exposure, count = Decimal("0"), 0
            for row in self.provider.positions():
                if is_crypto_row(row):
                    continue
                qty = _whole(row.get("qty") or 0)
                if qty > 0:
                    count += 1
                    exposure += qty * Decimal(str(row.get("avg_entry_price") or "0"))
            for row in evidence["all_equity_open"]:
                if row.get("side") == "buy":
                    remaining = _whole(row.get("qty") or 0) - _whole(row.get("filled_qty") or 0)
                    exposure += max(remaining, 0) * Decimal(str(row.get("limit_price") or "0"))
            seen = set()
            for kind in ("paper_order_requested", REQUESTED):
                for event in self.journal.events(event_type=kind):
                    cid = (event.get("payload") or {}).get("client_order_id")
                    if cid:
                        seen.add(str(cid))
            now = self.clock()
            age = max((now - parse_time(quote[k])).total_seconds() for k in ("timestamp", "received_at"))
            state = RiskState(exposure=exposure, realized_daily_pnl=Decimal(str(ledger["realized_pnl"])),
                              open_positions=count, feed_age_ms=int(age * 1000) if age >= 0 else -1,
                              kill_switch=self.journal.kill_switch().get("enabled") is not False,
                              seen_order_ids=frozenset(seen))
            return check_paper_intent(
                PaperIntent(intent["symbol"], Decimal(str(intent["qty"])), Decimal(str(intent["entry_limit"])),
                            PaperClient.client_order_id(intent)),
                state, limits_for_risk(self.risk_config, feed=self.config.data_feed))
        except Exception:
            return False, "paper_intent_screen_failed"

    # ---- cycle -------------------------------------------------------------------
    def cycle(self, *, execute: bool, session_ledger: dict | None, allow_entries: bool = True) -> dict:
        status = {"profile": self.profile, "active": False, "execute": bool(execute), "blockers": list(self.blockers),
                  "actions": [], "decisions": [], "errors": [], "orders_submitted": 0}
        self.last_status = status
        if self.blockers:
            return plain(status)
        now = self.clock()
        day = now.astimezone(NY).date()
        if day != self._day:
            self._start_day(day)
        run_id = self._run_id(day)
        evidence = self._reconcile(now)
        self.last_evidence = evidence
        status["errors"] = list(evidence["errors"])
        reconciled = not evidence["errors"]
        if reconciled:
            self._apply_fills(evidence)
        orphans = []
        for symbol, pos in evidence["owned"].items():
            if symbol not in self.engine.positions:
                status["errors"].append("day_trade_position_not_from_today")
                orphans.append(Decision(True, ("exit", "orphan_flatten"), {
                    "symbol": symbol, "side": "sell", "action": "exit", "qty": pos["qty"], "reason": "orphan_flatten",
                    "pattern": "unknown", "mode": "paper", "profile": PROFILE_ID}, profile=PROFILE_ID))
        ledger_ok = isinstance(session_ledger, dict) and session_ledger.get("complete") is True
        control = self.journal.kill_switch()
        halted = bool(control.get("enabled")) or not allow_entries
        engine_ok = self.engine.clock is not None and self.engine.clock.in_regular_session(now)
        decisions = []
        if engine_ok:
            local = now.astimezone(NY).time()
            if (self._last_scan is None or (now - self._last_scan).total_seconds() >= SCAN_INTERVAL_SECONDS) \
                    and local <= time(11, 30):
                self._last_scan = now
                try:
                    self.engine.refresh_candidates(now)
                except Exception:
                    status["errors"].append("day_trade_scanner_failed")
            symbols = sorted(self.engine.positions) + [s for s in sorted(self.engine.candidates)
                                                       if s not in self.engine.positions and self.scope.allows(s)]
            try:
                for bar in self._bars(symbols[:MAX_SYMBOLS], now):
                    decisions.extend(self.engine.on_bar(bar))
            except Exception:
                status["errors"].append("day_trade_bars_failed")
        marks = {}
        for symbol in list(self.engine.positions):
            try:
                quote = self._quote(symbol)
                if quote:
                    marks[symbol] = Decimal(quote["bid"])
            except Exception:
                pass
        decisions.extend(self.engine.manage(now, marks))
        # Between 1-minute bars the stop is also watched on the live bid.
        exiting = {(d.intent or {}).get("symbol") for d in decisions if d.accepted and (d.intent or {}).get("side") == "sell"}
        for symbol, pos in self.engine.positions.items():
            if symbol not in exiting and symbol in marks and marks[symbol] <= pos["stop"]:
                decisions.append(self.engine._exit(symbol, pos["qty"],
                                                   "breakeven_stop" if pos["partial_done"] else "stop", pos["stop"]))
        decisions.extend(orphans)
        status["active"] = bool(execute and reconciled and ledger_ok)
        status["decisions"] = [plain({"accepted": d.accepted, "reasons": list(d.reasons),
                                      "symbol": (d.intent or {}).get("symbol") or d.metrics.get("symbol")})
                               for d in decisions if d.accepted or d.reasons[:1] not in (("not_a_candidate",),)][-20:]
        risk_config_id = self.risk_config.config_id

        def guard(side):
            def check():
                if self.execution_guard() is not True:
                    raise PaperDisabled("Execution service is stopping")
                if self.config.paper_blockers():
                    raise PaperDisabled("Paper execution configuration is not approved")
                if side == "buy" and (self.journal.kill_switch().get("enabled") or not allow_entries):
                    raise PaperDisabled("New entries are durably halted")
                return True
            return check

        for decision in decisions:
            if not decision.accepted:
                continue
            intent = dict(decision.intent or {})
            sym = intent.get("symbol")
            action = {"kind": None, "symbol": sym, "submitted": False, "pattern": intent.get("pattern")}
            try:
                if intent.get("action") == "cancel_entry":
                    action["kind"] = "day_trade_cancel_entry"
                    for order in evidence["open_own"]:
                        if order.get("symbol") == sym and order.get("side") == "buy" and execute:
                            self.provider.cancel(order, self.journal, run_id, guard=self.execution_guard)
                            action["submitted"] = True
                elif intent.get("side") == "sell":
                    action["kind"] = "day_trade_exit"
                    if not execute or sym not in evidence["owned"]:
                        action["skipped"] = "not_executing_or_not_owned"
                    elif any(o.get("symbol") == sym and o.get("side") == "sell" for o in evidence["open_own"]):
                        action["skipped"] = "exit_already_working"
                        for o in evidence["open_own"]:
                            if o.get("symbol") == sym and o.get("side") == "sell":
                                placed = parse_time(o.get("submitted_at") or self.clock().isoformat())
                                if (self.clock() - placed).total_seconds() >= self.params.limit_ttl_seconds:
                                    # Re-price next cycle at the then-current bid.
                                    self.provider.cancel(o, self.journal, run_id, guard=self.execution_guard)
                                    action["skipped"] = "stale_exit_canceled_for_reprice"
                    else:
                        quote = self._quote(sym)
                        if quote is None:
                            raise PaperDisabled("no_quote_for_exit")
                        setup = evidence["owned"][sym]["setup_id"]
                        done = sum(1 for cid, o in evidence["orders"].items() if o["side"] == "sell"
                                   and o["status"] in TERMINAL and evidence["known"][cid]["intent"].get("setup_id") == setup)
                        bid = cents_down(Decimal(quote["bid"]))
                        qty = min(int(intent["qty"]), evidence["owned"][sym]["qty"])
                        order = {"symbol": sym, "side": "sell", "mode": "paper", "profile": PROFILE_ID,
                                 "config_id": self.params.config_id, "setup_id": setup, "exit_generation": str(done),
                                 "qty": qty, "entry_limit": str(bid), "limit_price": str(bid),
                                 "observed_at": self.clock().isoformat(), "feed": self.config.data_feed,
                                 "reason": intent.get("reason"), "pattern": intent.get("pattern"), "paper_only": True,
                                 "live_enabled": False}
                        self.provider.submit(order, self.journal, run_id, quote=quote, guard=guard("sell"), scope=self.scope)
                        action["submitted"] = True
                else:
                    action["kind"] = "day_trade_entry"
                    if not (execute and reconciled and ledger_ok and not halted and not status["errors"]):
                        action["skipped"] = "entries_not_armed_or_not_reconciled"
                    elif not self.scope.allows(sym):
                        action["skipped"] = "symbol_belongs_to_another_session"
                    else:
                        quote = self._quote(sym)
                        if quote is None:
                            raise PaperDisabled("no_quote_for_entry")
                        stamp = self.clock().isoformat()
                        intent.update({"observed_at": stamp, "risk_checked_at": stamp, "risk_config_id": risk_config_id,
                                       "feed": self.config.data_feed, "live_enabled": False, "paper_only": True})
                        approved, reason = self._screen(intent, quote, evidence, session_ledger)
                        action["paper_intent_reason"] = reason
                        if approved:
                            self.provider.submit(intent, self.journal, run_id, quote=quote, guard=guard("buy"),
                                                 scope=self.scope)
                            action["submitted"] = True
                            status["orders_submitted"] += 1
            except Exception as exc:
                action["error"] = str(exc)[:120] if isinstance(exc, PaperDisabled) else type(exc).__name__
            if action["kind"] == "day_trade_entry" and not action["submitted"]:
                self.engine.pending.pop(sym, None)  # never hold a slot for an order that was not sent
            status["actions"].append(action)
        status["errors"] = sorted(set(status["errors"]))
        status["diagnostics"] = {k: v for k, v in self.engine.diagnostics().items() if k != "scan"}
        self.last_status = status
        return plain(status)
