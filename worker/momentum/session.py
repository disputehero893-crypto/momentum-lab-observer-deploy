"""Reusable, bounded paper session controller; no live execution.

The injected provider uses PaperClient's interface. A cycle also requires an
independently reconciled session ledger (gross realized P&L and fees). Alpaca's
account response does not provide this ledger. The observation worker imports
activities into a separate revision-aware ledger and never requests execution.
"""
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import uuid

from .alpaca import BrokerError, PaperDisabled, parse_time
from .data import SnapshotBuilder
from .journal import Journal, canonical_json
from .lease import WorkerLease
from .models import NY, plain
from .risk import RiskConfig, RiskManager
from .strategy import evaluate_setup, evaluate_exit, load_strategy
from .ai_advisor import AIAdvisor, PRIORITY
from .ai_cotrade import build_ai_intent

TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}
ACTIVE = {"new", "accepted", "pending_new", "partially_filled", "pending_cancel", "pending_replace",
          "accepted_for_bidding", "stopped", "suspended", "calculated", "done_for_day"}


def whole(value):
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError("Nonnegative whole-share quantity required")
    return int(number)


class PaperSession:
    def __init__(self, config, provider, *, journal=None, snapshot_builder=None, clock=None,
                 expected_account_id=None, execution_guard=None, entry_guard=None, advisor=None):
        self.config = config
        self.provider = provider
        self.journal = journal or Journal(config.db_path)
        self.builder = snapshot_builder or SnapshotBuilder(provider, config)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.strategy = load_strategy(config.strategy_overrides)
        self.risk = RiskManager(RiskConfig(**config.risk_overrides))
        self.lease = WorkerLease(config.db_path)
        self._owner = False
        self.expected_account_id = expected_account_id
        self.execution_guard = execution_guard or (lambda: True)
        self.entry_guard = entry_guard or (lambda: True)
        # Optional Gemini chart reader (AI_ADVISOR.md). Inactive unless both
        # AI_ADVISOR_ENABLED and AI_ADVISOR_MODE say so; it never sizes orders.
        # Env-built advisors may run cotrade only when the reviewed config covers it.
        self._cotrade_reviewed = advisor is not None or bool(getattr(config, "ai_cotrade", None))
        if advisor is None:
            try:
                from pathlib import Path
                advisor = AIAdvisor.from_env(default_log_dir=str(Path(config.db_path).parent / "ai_advisor"))
            except Exception:
                advisor = None
        self.advisor = advisor

    def __enter__(self):
        self.lease.__enter__()
        try:
            # Load only after acquiring ownership; construction is not a lease.
            prior = self.journal.latest("paper_risk_state", "paper-state")
            if prior:
                self.risk.restore_state(prior["payload"]["state"])
            self._owner = True
        except Exception:
            self.lease.__exit__()
            raise
        return self

    def __exit__(self, *args):
        self._owner = False
        if self.advisor is not None:
            try:
                self.advisor.close()
            except Exception:
                pass
        self.lease.__exit__(*args)

    def _active_advisor(self):
        advisor = self.advisor
        try:
            return advisor if advisor is not None and advisor.enabled else None
        except Exception:
            return None

    def _ai_start_reads(self, ai, by_symbol):
        """Queue background chart reads; returns at once so the rules never wait."""
        try:
            items = []
            chart_bars = getattr(self.builder, "chart_bars", None)
            for symbol, snapshot in by_symbol.items():
                if not self._ai_read_window(snapshot):
                    continue   # no free-tier calls outside the rules' entry window
                bars = None
                if callable(chart_bars):
                    try:
                        bars = chart_bars(symbol)
                    except Exception:
                        bars = None
                quote = snapshot.get("quote") or {}
                items.append((symbol, bars or snapshot.get("bars") or (),
                              {"bid": quote.get("bid"), "ask": quote.get("ask"), "as_of": snapshot.get("now")}))
            ai.start_reads(items)
            ai.wait()
        except Exception:
            pass

    @staticmethod
    def _ai_safe(call, default=None):
        """Advisor bugs must never break a cycle; the rules path carries on."""
        try:
            return call()
        except Exception:
            return default

    def _ai_read_window(self, snapshot):
        try:
            from datetime import time as clock_time
            local = parse_time(snapshot.get("now")).astimezone(NY)
            session = snapshot.get("session") or {}
            return (session.get("is_trading_day") is True
                    and clock_time.fromisoformat(self.strategy.trade_start_et) <= local.time()
                    < clock_time.fromisoformat(self.strategy.trade_end_et))
        except Exception:
            return False

    def _ai_signal(self, ai, symbol):
        try:
            return ai.signal(symbol) if ai is not None else None
        except Exception:
            return None

    def _cancel_allowed(self):
        # Emergency halt must still permit protective cancellation; process
        # shutdown or loss of this session's ownership must not.
        return self._owner and self.execution_guard() is True

    def _reconcile(self, run_id, now):
        requests = self.journal.events(event_type="paper_order_requested")
        if len(requests) > 1000:
            raise BrokerError("Prototype recovery cap reached; import a complete execution ledger before continuing")
        known = {row["payload"]["client_order_id"]: row["payload"] for row in requests}
        account_requested_at = self.clock()
        account = self.provider.account()
        account_received_at = self.clock()
        actual_positions = self.provider.positions()
        open_orders = self.provider.open_orders()
        observed_at = self.clock()
        errors = []
        if self.expected_account_id is not None and account.get("id") != self.expected_account_id:
            errors.append("paper_account_identity_mismatch")
        if account.get("status") != "ACTIVE" or account.get("trading_blocked") is not False:
            errors.append("paper_account_not_verified_active")
        if account.get("account_blocked", False):
            errors.append("paper_account_blocked")
        observations = {}
        for client_id, request in known.items():
            order = self.provider.order_by_client_id(client_id)
            order_received_at = self.clock()
            if order is None:
                definitely_not_sent = (self.journal.event_by_key("paper_order_rejected:"+client_id)
                                       or self.journal.event_by_key("not-sent:"+client_id))
                if not definitely_not_sent:
                    errors.append("unresolved_order_submission")
                continue
            if (order.get("client_order_id") != client_id or order.get("symbol") != request["intent"]["symbol"]
                    or order.get("side") != request["intent"]["side"]
                    or whole(order.get("qty")) != whole(request["request"]["qty"])):
                errors.append("broker_order_identity_mismatch")
                continue
            if order.get("status") not in TERMINAL | ACTIVE:
                errors.append("unrecognized_order_status")
            raw_filled = order.get("filled_qty") if "filled_qty" in order else None
            if raw_filled is None or raw_filled == "":
                # A missing fill is unknown. It is not a flat zero that can close exposure.
                errors.append("filled_quantity_unknown")
                continue
            filled = whole(raw_filled)
            if filled > whole(order["qty"]):
                errors.append("invalid_cumulative_fill_quantity")
                continue
            if filled:
                price = order.get("filled_avg_price") if "filled_avg_price" in order else None
                try:
                    if price is None or price == "" or Decimal(str(price)) <= 0:
                        raise ValueError
                except (ValueError, TypeError, ArithmeticError):
                    errors.append("filled_price_unknown")
                    continue
            old = [e for e in self.journal.events(event_type="paper_order_observed")
                   if e["payload"]["order"].get("client_order_id") == client_id]
            if old and filled < whole(old[-1]["payload"]["order"].get("filled_qty")):
                errors.append("cumulative_fill_regressed")
            digest = sha256(canonical_json(order).encode()).hexdigest()[:24]
            self.journal.append(run_id, "paper_order_observed", {"order": order, "broker_confirmed": True},
                                key=f"observed:{run_id}:{client_id}:{digest}", timestamp=order_received_at.isoformat())
            observations[client_id] = order
        active_ids = {row.get("client_order_id") for row in open_orders}
        if any(client_id not in known for client_id in active_ids):
            errors.append("unowned_open_order")
        expected_active = {key for key, value in observations.items() if value["status"] not in TERMINAL}
        if expected_active != active_ids.intersection(known):
            errors.append("open_order_reconciliation_race")
        # Rebuild owned long exposure in request order. Full prior round trips net
        # to zero; partial fills immediately count even after a cancel response.
        rebuilt = {}
        for row in requests:
            request = row["payload"]
            order = observations.get(request["client_order_id"])
            if not order:
                continue
            quantity = whole(order["filled_qty"])
            if not quantity:
                continue
            symbol = order["symbol"]
            position = rebuilt.get(symbol)
            if order["side"] == "buy":
                price = Decimal(str(order["filled_avg_price"]))
                old_qty = position["qty"] if position else 0
                old_cost = Decimal(position["entry_price"])*old_qty if position else Decimal(0)
                rebuilt[symbol] = {"symbol": symbol, "qty": old_qty+quantity,
                                   "entry_price": str((old_cost+price*quantity)/(old_qty+quantity)),
                                   "stop_price": request["intent"]["stop_price"],
                                   "target_price": request["intent"]["target_price"],
                                   "opened_at": order.get("submitted_at") or request["intent"]["observed_at"],
                                   "holding_clock_basis": "conservative_order_submission_until_fill_activity_integration",
                                   "setup_id": request["intent"]["setup_id"], "mode": "paper",
                                   "entry_run_id": position.get("entry_run_id", row["run_id"]) if position else row["run_id"],
                                   "total_entry_qty": (position.get("total_entry_qty", 0) if position else 0)+quantity,
                                   "gross_realized_pnl": position.get("gross_realized_pnl", "0") if position else "0"}
            elif not position or quantity > position["qty"]:
                errors.append("unmatched_or_excess_sell_fill")
            else:
                position["gross_realized_pnl"] = str(Decimal(position["gross_realized_pnl"])
                    + (Decimal(str(order["filled_avg_price"]))-Decimal(position["entry_price"]))*quantity)
                position["qty"] -= quantity
                if position["qty"] == 0:
                    trade = {"mode": "paper", "provenance": "broker_cumulative_order_fills",
                             "symbol": symbol, "qty": position["total_entry_qty"],
                             "entry_price": position["entry_price"], "gross_pnl": position["gross_realized_pnl"],
                             "opened_at": position["opened_at"], "closed_at": order.get("filled_at") or order.get("updated_at"),
                             "setup_id": position["setup_id"], "fees": None, "net_pnl": None,
                             "fee_allocation_status": "requires_execution_activity_ledger",
                             "broker_confirmed": True}
                    self.journal.append_trade(position["entry_run_id"], "paper-trade:"+position["setup_id"], trade)
                    del rebuilt[symbol]
        actual = {}
        for row in actual_positions:
            if row.get("side") != "long":
                errors.append("unowned_or_short_position")
                continue
            quantity = whole(row["qty"])
            if quantity:
                actual[row["symbol"]] = row
        if {s: p["qty"] for s, p in rebuilt.items()} != {s: whole(p["qty"]) for s, p in actual.items()}:
            errors.append("position_quantity_mismatch_or_unowned_position")
        for symbol in set(rebuilt).intersection(actual):
            if abs(Decimal(rebuilt[symbol]["entry_price"])-Decimal(actual[symbol]["avg_entry_price"])) > Decimal("0.0001"):
                errors.append("position_cost_basis_mismatch")
        opening = [row for row in observations.values() if row["side"] == "buy" and row["status"] not in TERMINAL]
        closing = [row for row in observations.values() if row["side"] == "sell" and row["status"] not in TERMINAL]
        return {"account": account, "positions": list(rebuilt.values()), "actual_positions": actual_positions,
                "open_orders": open_orders, "opening": opening, "closing": closing,
                "orders": observations, "known": known, "as_of": account_requested_at.isoformat(),
                "account_received_at": account_received_at.isoformat(), "evidence_collected_at": self.clock().isoformat(),
                "errors": sorted(set(errors)), "reconciled": not errors}

    def _screen_equity_entry(self, intent, snapshot, evidence, account):
        """Run PR #8's check_paper_intent on one stock entry before submit.

        Fail closed. This is not used for protective exits.
        """
        from .alpaca import PaperClient
        from .paper_risk import PaperIntent, RiskState, check_paper_intent, limits_for_risk
        try:
            qty = Decimal(str(intent.get("qty")))
            price = Decimal(str(intent.get("entry_limit", intent.get("limit_price"))))
            client_id = PaperClient.client_order_id(intent)
            exposure = Decimal("0")
            open_positions = 0
            for position in evidence.get("positions") or []:
                held = Decimal(str(position.get("qty") or "0"))
                if held > 0:
                    open_positions += 1
                    exposure += held * Decimal(str(position.get("entry_price") or "0"))
            for order in evidence.get("opening") or []:
                remaining = Decimal(str(order.get("qty") or "0")) - Decimal(str(order.get("filled_qty") or "0"))
                limit = Decimal(str(order.get("limit_price") or "0"))
                if remaining > 0 and limit > 0:
                    exposure += remaining * limit
            realized = Decimal(str(account.get("realized_pnl")))
            quote = snapshot.get("quote") or {}
            now = self.clock()
            ages = [(now - parse_time(quote[key])).total_seconds() for key in ("timestamp", "received_at")]
            age = max(ages)
            feed_age_ms = int(age * 1000) if age >= 0 else -1
            seen = set()
            for event in self.journal.events(event_type="paper_order_requested"):
                cid = (event.get("payload") or {}).get("client_order_id")
                if cid:
                    seen.add(str(cid))
            halted = self.journal.kill_switch().get("enabled") is not False
            state = RiskState(
                exposure=exposure, realized_daily_pnl=realized, open_positions=open_positions,
                feed_age_ms=feed_age_ms, kill_switch=halted, seen_order_ids=frozenset(seen))
            limits = limits_for_risk(self.risk.config, feed=self.config.data_feed)
            return check_paper_intent(
                PaperIntent(intent.get("symbol"), qty, price, client_id), state, limits)
        except Exception:
            return False, "paper_intent_screen_failed"

    def cycle(self, *, snapshots=None, session_ledger=None, execute=False, allow_entries=True):
        """Run once with fresh provider evidence. `execute` defaults False.

        session_ledger must contain complete=True, session_date, as_of,
        realized_pnl (gross), fees, source. Never obtain these by interpreting an
        account equity change as realized trading P&L. The separately started
        autopilot service calls this component after durable authorization.
        """
        # Pausing entries must keep reconciliation and protective exits running.
        if type(allow_entries) is not bool or type(execute) is not bool:
            raise ValueError("Execution flags must be booleans")
        if not self._owner:
            raise PaperDisabled("PaperSession must own its persistent journal lease")
        if self.config.mode != "paper":
            raise PaperDisabled("PaperSession requires explicit paper mode")
        if type(execute) is not bool:
            raise ValueError("execute must be an explicit boolean")
        start = self.clock()
        day = start.astimezone(NY).date().isoformat()
        run_id = "paper-"+day
        cycle_id = uuid.uuid4().hex
        actions, decisions = [], []
        errors = []
        ledger = session_ledger or {}
        try:
            ledger_valid = (ledger.get("complete") is True and ledger.get("session_date") == day
                            and isinstance(ledger.get("source"), str) and bool(ledger["source"])
                            and 0 <= (start-parse_time(ledger.get("as_of"))).total_seconds() <= 5
                            and Decimal(str(ledger.get("fees"))).is_finite()
                            and Decimal(str(ledger.get("fees"))) >= 0
                            and Decimal(str(ledger.get("realized_pnl"))).is_finite())
        except (ValueError, TypeError, ArithmeticError):
            ledger_valid = False
        if not ledger_valid:
            errors.append("complete_current_session_pnl_and_fee_ledger_missing")
        try:
            evidence = self._reconcile(run_id, start)
            errors.extend(evidence["errors"])
            if ledger.get("account_id") is not None and ledger["account_id"] != evidence["account"].get("id"):
                errors.append("session_ledger_account_mismatch")
                ledger_valid = False
            if ledger.get("environment", "paper") != "paper":
                errors.append("session_ledger_environment_mismatch")
                ledger_valid = False
            snapshots = self.builder.build_watchlist() if snapshots is None else snapshots
            by_symbol = {row["symbol"]: row for row in snapshots}
            if len(by_symbol) != len(snapshots):
                errors.append("duplicate_snapshot_symbol")
            if any(symbol not in self.config.watchlist for symbol in by_symbol):
                errors.append("snapshot_outside_reviewed_watchlist")
            if any(position["symbol"] not in by_symbol for position in evidence["positions"]):
                errors.append("quote_missing_for_open_position")
            now = self.clock()
            if now.astimezone(NY).date().isoformat() != day:
                errors.append("session_changed_during_cycle")
            if (now-parse_time(evidence["as_of"])).total_seconds() > 5:
                errors.append("account_reconciliation_became_stale")
            if ledger_valid and (now-parse_time(ledger["as_of"])).total_seconds() > 5:
                errors.append("session_ledger_became_stale")
            account = {
                "mode": "paper", "session_date": day, "as_of": evidence["as_of"],
                "equity": str(evidence["account"]["equity"]), "buying_power": str(evidence["account"]["buying_power"]),
                "realized_pnl": str(ledger["realized_pnl"]) if ledger_valid else None,
                "fees": str(ledger["fees"]) if ledger_valid else None,
                "unrealized_pnl": str(sum(Decimal(str(p["unrealized_pl"])) for p in evidence["actual_positions"])),
                "position_qty": sum(whole(p["qty"]) for p in evidence["actual_positions"]),
                "open_order_qty": sum(whole(o["qty"])-whole(o["filled_qty"]) for o in evidence["opening"]),
            }
            reconciled = not errors
            control = self.journal.kill_switch()

            def checkpoint(label):
                (self.journal.append_sampled if not execute else self.journal.append)(
                    "paper-state", "paper_risk_state", {"state": self.risk.to_state()},
                    key=f"risk-state:{cycle_id}:{label}", timestamp=self.clock().isoformat())

            # Account loss/stop latches do not depend on an entry or usable quote.
            watchdog_quote = next(iter(by_symbol.values()))["quote"] if by_symbol else {}
            watchdog = self.risk.assess(None, account, watchdog_quote, now, connected=True,
                                       reconciled=reconciled and bool(by_symbol),
                                       kill_switch=bool(control.get("flatten_requested")),
                                       halt_new_entries=not allow_entries or bool(control.get("enabled") and not control.get("flatten_requested")))
            checkpoint("watchdog")
            for order in evidence["opening"]:
                submitted = parse_time(order.get("submitted_at") or evidence["known"][order["client_order_id"]]["intent"]["observed_at"])
                if not allow_entries or control.get("enabled") or watchdog.metrics.get("cancel_entries_required") or (now-submitted).total_seconds() >= self.config.order_ttl_seconds:
                    actions.append({"kind": "cancel_opening_remainder", "order_id": order["id"], "confirmed": False})
                    if execute and evidence["reconciled"] and self.execution_guard() is True:
                        self.provider.cancel(order, self.journal, run_id, guard=self._cancel_allowed)

            def guard(side):
                if self.execution_guard() is not True:
                    raise PaperDisabled("Execution service is stopping")
                if not self._owner or self.config.paper_blockers():
                    raise PaperDisabled("Paper execution configuration or ownership is not approved")
                if side == "buy" and self.entry_guard() is not True:
                    raise PaperDisabled("Customer settings no longer authorize new entries")
                # The settings read can block. Recheck local authority and
                # time-sensitive evidence only after it has completed.
                latest_control = self.journal.kill_switch()
                now = self.clock()
                if self.execution_guard() is not True:
                    raise PaperDisabled("Execution service is stopping")
                if not self._owner or self.config.paper_blockers():
                    raise PaperDisabled("Paper execution configuration or ownership is not approved")
                if not reconciled:
                    raise PaperDisabled("Complete current account/order/position/fee reconciliation required")
                if not 0 <= (now-parse_time(evidence["as_of"])).total_seconds() <= 5:
                    raise PaperDisabled("Account reconciliation stale before submission")
                if not ledger_valid or not 0 <= (now-parse_time(ledger["as_of"])).total_seconds() <= 5:
                    raise PaperDisabled("Session P&L/fee ledger stale before submission")
                if now.astimezone(NY).date().isoformat() != day:
                    raise PaperDisabled("Session changed before submission")
                if side == "buy" and (not allow_entries or latest_control.get("enabled") or self.risk.locked):
                    raise PaperDisabled("New entries are durably halted")

            ai = self._active_advisor()
            cotrade = (ai is not None and self._ai_safe(lambda: ai.mode) == "cotrade"
                       and self._cotrade_reviewed)
            entry_candidates = []
            if ai is not None:
                self._ai_start_reads(ai, by_symbol)

            def entry_flags():
                return dict(connected=True, reconciled=reconciled,
                            kill_switch=bool(control.get("flatten_requested")),
                            halt_new_entries=not allow_entries or bool(control.get("enabled") and not control.get("flatten_requested")))

            def submit_entry(checked, snapshot, extra=None):
                action = {"kind": "paper_entry_intent", "intent": checked.intent, "submitted": False, "filled_qty": 0}
                if extra:
                    action.update(extra)
                if execute:
                    guard("buy")
                    # Entry screen only. Protective exits use the sell path and
                    # must not be blocked by this notional/position check.
                    approved, reason = self._screen_equity_entry(checked.intent, snapshot, evidence, account)
                    action["paper_intent_reason"] = reason
                    if not approved:
                        actions.append(action)
                        return action
                    self.provider.submit(checked.intent, self.journal, run_id, quote=snapshot["quote"], guard=lambda: guard("buy"))
                    # Reserve acknowledged/uncertain opening quantity before considering another symbol.
                    account["open_order_qty"] += checked.intent["qty"]
                    action["submitted"] = True
                else:
                    # A dry run also proposes at most one allocation per cycle.
                    account["open_order_qty"] += checked.intent["qty"]
                actions.append(action)
                return action

            # The risk watchdog runs even if no symbol produces an entry.
            for symbol, snapshot in by_symbol.items():
                setup = evaluate_setup(snapshot, self.strategy)
                checked = self.risk.assess(setup.intent if setup.accepted else None, account, snapshot["quote"], now,
                                          connected=True, reconciled=reconciled,
                                          kill_switch=bool(control.get("flatten_requested")),
                                          halt_new_entries=not allow_entries or bool(control.get("enabled") and not control.get("flatten_requested")))
                checkpoint("decision-"+symbol)
                position = next((p for p in evidence["positions"] if p["symbol"] == symbol), None)
                exit_decision = evaluate_exit(position, snapshot, self.strategy,
                                             force_reason=checked.reasons[0] if checked.metrics.get("flatten_required") else None) if position else None
                decisions.append({"symbol": symbol, "strategy": setup.to_dict(), "risk": checked.to_dict(),
                                  "exit": exit_decision.to_dict() if exit_decision else None})
                signal = self._ai_signal(ai, symbol)
                if ai is not None:
                    decisions[-1]["ai_signal"] = self._ai_safe(lambda: signal.to_dict()) if signal is not None else None
                need_cancel_entries = bool(checked.metrics.get("cancel_entries_required") or (exit_decision and exit_decision.accepted))
                for order in evidence["opening"]:
                    submitted = parse_time(order.get("submitted_at") or evidence["known"][order["client_order_id"]]["intent"]["observed_at"])
                    expired = (now-submitted).total_seconds() >= self.config.order_ttl_seconds
                    if (need_cancel_entries or expired) and not any(a.get("order_id") == order["id"] for a in actions):
                        actions.append({"kind": "cancel_opening_remainder", "order_id": order["id"], "confirmed": False})
                        if execute and evidence["reconciled"] and self.execution_guard() is True:
                            self.provider.cancel(order, self.journal, run_id, guard=self._cancel_allowed)
                if exit_decision and exit_decision.accepted:
                    if evidence["opening"]:
                        actions.append({"kind": "exit_waiting_for_opening_order_reconciliation", "symbol": symbol})
                        continue
                    existing_exits = [o for o in evidence["closing"] if o["symbol"] == symbol]
                    if existing_exits:
                        for order in existing_exits:
                            submitted = parse_time(order.get("submitted_at") or now)
                            if (now-submitted).total_seconds() >= self.config.order_ttl_seconds:
                                actions.append({"kind": "cancel_stale_exit", "order_id": order["id"], "confirmed": False})
                                if execute and evidence["reconciled"] and self.execution_guard() is True:
                                    self.provider.cancel(order, self.journal, run_id, guard=self._cancel_allowed)
                        actions.append({"kind": "exit_pending_fill_or_cancel_confirmation", "symbol": symbol})
                        continue
                    completed_exits = [o for cid, o in evidence["orders"].items()
                                       if o["side"] == "sell" and o["status"] in TERMINAL
                                       and evidence["known"][cid]["intent"].get("setup_id") == position["setup_id"]]
                    intent = {**exit_decision.intent, "setup_id": position["setup_id"],
                              "exit_generation": str(len(completed_exits))}
                    action = {"kind": "paper_exit_intent", "intent": intent, "submitted": False,
                              "positions_confirmed_flat": False}
                    if ai is not None:
                        try:
                            ai.record_outcome(position, intent)
                        except Exception:
                            pass
                    if execute:
                        guard("sell")
                        self.provider.submit(intent, self.journal, run_id, quote=snapshot["quote"], guard=lambda: guard("sell"))
                        action["submitted"] = True
                    actions.append(action)
                elif allow_entries and not position and checked.accepted and not evidence["closing"] and not cotrade:
                    # Rules decide alone (advisor off or advise mode: AI calls are only tagged and logged).
                    tag = (self._ai_safe(lambda: {"source": ai.classify(True, signal),
                                                  "ai_signal": signal.to_dict() if signal else None},
                                         {"source": "rules", "ai_signal": None})
                           if ai is not None else None)
                    action = submit_entry(checked, snapshot, tag)
                    if ai is not None:
                        self._ai_safe(lambda: ai.note_candidate(action["source"], checked.intent, signal,
                                                                submitted=action["submitted"], outcome="entry_intent"))
                elif allow_entries and not position and not evidence["closing"]:
                    if ai is not None and not cotrade and not checked.accepted:
                        # advise mode: record what an AI-only candidate would have been; never trade it.
                        if self._ai_safe(lambda: ai.classify(False, signal)) == "ai":
                            ai_intent, _why = build_ai_intent(snapshot, self.strategy, signal, self.config.watchlist,
                                                              min_reward_r=self.risk.config.min_reward_r)
                            if ai_intent is not None:
                                self._ai_safe(lambda: ai.note_candidate("ai", ai_intent, signal, submitted=False,
                                                                        outcome="advise_only_not_traded"))
                    elif cotrade:
                        # Rules and AI run side by side; the combiner tags and queues candidates and
                        # every one is re-checked by the same risk manager below before submission.
                        source = self._ai_safe(lambda: ai.classify(checked.accepted, signal))
                        if source is None and checked.accepted:
                            source = "rules"   # an advisor fault never drops a rules candidate
                        if source in ("confluence", "rules"):
                            entry_candidates.append((PRIORITY[source], len(entry_candidates), source, symbol,
                                                     setup.intent, snapshot, signal))
                        elif source == "ai":
                            ai_intent, why = build_ai_intent(snapshot, self.strategy, signal, self.config.watchlist,
                                                             min_reward_r=self.risk.config.min_reward_r)
                            if ai_intent is not None:
                                entry_candidates.append((PRIORITY["ai"], len(entry_candidates), "ai", symbol,
                                                         ai_intent, snapshot, signal))
                            else:
                                decisions[-1]["ai_candidate_rejected"] = why
            # cotrade: confluence first, then rule-only, then AI-only. Each is sized and
            # limited by the unchanged RiskManager against the current account state.
            for _priority, _order, source, symbol, raw_intent, snapshot, signal in sorted(entry_candidates, key=lambda c: c[:2]):
                checked = self.risk.assess(raw_intent, account, snapshot["quote"], now, **entry_flags())
                if not checked.accepted:
                    actions.append({"kind": "entry_candidate_rejected_by_risk", "symbol": symbol, "source": source,
                                    "reasons": list(checked.reasons)})
                    continue
                limit = self._ai_safe(lambda: ai.wants_ai_entry(signal), "ai_advisor_error") if source == "ai" else None
                if limit is not None:
                    actions.append({"kind": "entry_candidate_rejected_by_ai_limits", "symbol": symbol, "source": source,
                                    "reasons": [limit]})
                    continue
                action = submit_entry(checked, snapshot, {"source": source, "ai_signal": self._ai_safe(
                    lambda: signal.to_dict()) if signal else None})
                self._ai_safe(lambda: ai.note_candidate(source, checked.intent, signal,
                                                        submitted=action["submitted"], outcome="entry_intent"))
            if not by_symbol:
                errors.append("no_current_snapshots")
            result = {"mode": "paper", "provenance": "provider_observations", "run_id": run_id,
                      "cycle_id": cycle_id, "as_of": now.isoformat(), "connected": True,
                      "broker_checked_at": evidence["as_of"],
                      "broker_account_id": evidence["account"].get("id"),
                      "broker_account_verified": (evidence["account"].get("status") == "ACTIVE"
                          and evidence["account"].get("trading_blocked") is False
                          and evidence["account"].get("account_blocked") is False
                          and not evidence["account"].get("trade_suspended_by_user", False)
                          and "paper_account_identity_mismatch" not in evidence["errors"]),
                      "reconciled": not errors, "execute_requested": execute, "entries_enabled": allow_entries,
                      "execution_configured": not self.config.paper_blockers(),
                      "errors": sorted(set(errors)), "account": account, "positions": evidence["positions"],
                      "broker_positions": evidence["actual_positions"],
                      "open_orders": evidence["open_orders"], "actions": actions, "decisions": decisions,
                      "session_ledger_source": ledger.get("source"),
                      "session_ledger_complete": ledger_valid,
                      "session_ledger_projection_id": ledger.get("projection_id"),
                      "positions_confirmed_flat": bool(reconciled and not evidence["positions"] and not evidence["open_orders"]
                                                        and not any(a.get("submitted") for a in actions)),
                      "live_execution_supported": False}
        except (BrokerError, PaperDisabled, ValueError, KeyError, TypeError, ArithmeticError) as exc:
            result = {"mode": "paper", "provenance": "provider_observations", "run_id": run_id,
                      "cycle_id": cycle_id, "as_of": self.clock().isoformat(), "connected": False,
                      "reconciled": False, "execute_requested": execute, "actions": actions, "decisions": decisions,
                      "errors": sorted(set(errors+[type(exc).__name__])),
                      "positions_confirmed_flat": False, "live_execution_supported": False}
        write = self.journal.append_sampled if not execute else self.journal.append
        write("paper-state", "paper_risk_state", {"state": self.risk.to_state()},
              key="risk-state:"+cycle_id, timestamp=self.clock().isoformat())
        write(run_id, "paper_cycle", plain(result), key="paper-cycle:"+cycle_id,
              timestamp=self.clock().isoformat(),
              **({"signature": canonical_json(plain([result.get("connected"), result.get("reconciled"),
                                               result.get("errors"), result.get("actions")]))}
                 if not execute else {}))
        return plain(result)
