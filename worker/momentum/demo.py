"""Deterministic synthetic replay that actually calls the strategy and risk engine.

No import from a broker adapter is used here. A completed replay is an executable
example and an integration fixture, never evidence of market profitability.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re

from .journal import Journal, canonical_json
from .models import NY, plain
from .risk import RiskConfig, RiskManager
from .strategy import StrategyConfig, evaluate_exit, evaluate_setup

FIXTURE_VERSION = "synthetic-replay-v1"
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
FEE_PER_SHARE = Decimal("0.003")


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def synthetic_snapshot(symbol="DEMO-A", at="2026-10-16T13:35:05Z"):
    """Invented market, news and float inputs. Weekdays are a synthetic calendar."""
    now = datetime.fromisoformat(at.replace("Z", "+00:00"))
    minute = now.replace(second=0, microsecond=0)
    day = now.astimezone(NY).date()
    previous = []
    cursor = day - timedelta(days=1)
    while len(previous) < 30:
        if cursor.weekday() < 5:
            previous.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    previous.reverse()
    rows = [
        ("8.80", "9.10", "8.75", "9.08", 100000),
        ("9.08", "9.50", "9.05", "9.48", 120000),
        ("9.48", "10.20", "9.46", "10.18", 150000),
        ("10.15", "10.16", "9.74", "9.79", 35000),
        ("9.79", "9.80", "9.74", "9.76", 30000),
    ]
    bars = []
    for index, row in enumerate(rows):
        start = minute - timedelta(minutes=5-index)
        bars.append(dict(zip(("open", "high", "low", "close", "volume"), row),
                         start=iso(start), feed="synthetic", complete=True,
                         available_at=iso(start + timedelta(minutes=1))))
    return {
        "symbol": symbol, "now": iso(now), "feed": "synthetic",
        "session": {"date": day.isoformat(), "is_trading_day": True,
                    "source": "invented_demo_weekday_calendar"},
        "quote": {"symbol": symbol, "bid": "9.81", "ask": "9.82", "feed": "synthetic",
                  "timestamp": iso(now), "received_at": iso(now)},
        "last_trade": {"symbol": symbol, "price": "9.82", "feed": "synthetic",
                       "timestamp": iso(now), "received_at": iso(now)},
        "bars": bars,
        "facts": {"previous_close": "8.00", "float_shares": 5000000,
                  "float_effective_at": iso(now-timedelta(days=1)),
                  "float_known_at": iso(now-timedelta(days=1)),
                  "catalyst_confirmed": True,
                  "catalyst_published_at": iso(now-timedelta(hours=1)),
                  "catalyst_known_at": iso(now-timedelta(minutes=59))},
        "rvol": {"current_volume": 1400000, "as_of": iso(minute), "feed": "synthetic",
                 "prior_sessions": previous,
                 "history": [{"session": session, "clock": minute.astimezone(NY).time().isoformat(),
                              "volume": 200000, "feed": "synthetic", "known_at": iso(now)}
                             for session in previous]},
        "provenance": "synthetic",
    }


def scenarios():
    def step(symbol, at, name, *, bid=None, ask=None, partial=None, **flags):
        snapshot = synthetic_snapshot(symbol, at)
        if bid is not None:
            snapshot["quote"]["bid"] = bid
            snapshot["last_trade"]["price"] = bid
        if ask is not None:
            snapshot["quote"]["ask"] = ask
        return {"name": name, "snapshot": snapshot, "fill_pending": partial, **flags}
    steps = [
        step("DEMO-C", "2026-10-16T13:34:52Z", "stale entry quote", stale=True),
        step("DEMO-A", "2026-10-16T13:35:05Z", "valid entry intention"),
        step("DEMO-A", "2026-10-16T13:35:06Z", "partial entry fill", partial=40),
        step("DEMO-A", "2026-10-16T13:35:07Z", "remaining entry fill", partial="all"),
        step("DEMO-A", "2026-10-16T13:35:20Z", "target exit", bid="10.00", ask="10.01"),
        step("DEMO-B", "2026-10-16T13:36:05Z", "second valid entry"),
        step("DEMO-B", "2026-10-16T13:36:06Z", "second entry fill", partial="all"),
        step("DEMO-B", "2026-10-16T13:36:20Z", "stale quote blocks stop exit",
             bid="9.69", ask="9.71", stale=True),
        step("DEMO-B", "2026-10-16T13:36:21Z", "fresh quote confirms stop exit",
             bid="9.70", ask="9.72"),
        step("DEMO-C", "2026-10-16T13:37:05Z", "insufficient relative volume", low_rvol=True),
        step("DEMO-C", "2026-10-16T13:38:05Z", "unknown float", missing_float=True),
        step("DEMO-C", "2026-10-16T13:39:05Z", "wide spread", bid="9.71", ask="9.82"),
        step("DEMO-C", "2026-10-16T13:40:05Z", "reconciliation missing", reconciled=False),
        step("DEMO-C", "2026-10-16T13:41:05Z", "halt new entries", halt_new_entries=True),
        step("DEMO-C", "2026-10-16T13:42:05Z", "emergency kill latch", kill_switch=True),
        step("DEMO-C", "2026-10-16T13:43:05Z", "restart preserves emergency lock", restart=True),
    ]
    for item in steps:
        snapshot = item["snapshot"]
        if item.get("stale"):
            old = datetime.fromisoformat(snapshot["now"].replace("Z", "+00:00")) - timedelta(seconds=10)
            snapshot["quote"]["timestamp"] = iso(old)
            snapshot["quote"]["received_at"] = iso(old)
        if item.get("low_rvol"):
            snapshot["rvol"]["current_volume"] = 400000
        if item.get("missing_float"):
            snapshot["facts"]["float_shares"] = None
    return steps


class SyntheticAccount:
    def __init__(self, equity):
        self.initial = Decimal(str(equity))
        self.cash = self.initial
        self.realized = Decimal(0)
        self.fees = Decimal(0)
        self.position = None
        self.pending = None
        self.trades = []
        self.fills = []

    def mark(self, snapshot):
        bid = Decimal(snapshot["quote"]["bid"])
        quantity = self.position["qty"] if self.position else 0
        entry = Decimal(self.position["entry_price"]) if self.position else Decimal(0)
        # All open positions in this fixture receive a matching-symbol mark.
        if self.position and self.position["symbol"] != snapshot["symbol"]:
            raise ValueError("Synthetic replay requires a matching quote for every open position")
        unrealized = (bid-entry) * quantity
        reserved = Decimal(self.pending["intent"]["entry_limit"]) * self.pending["remaining"] if self.pending else Decimal(0)
        return plain({"mode": "paper", "equity": self.cash+bid*quantity,
                      "buying_power": max(Decimal(0), self.cash-reserved),
                      "realized_pnl": self.realized, "unrealized_pnl": unrealized,
                      "fees": self.fees, "position_qty": quantity,
                      "open_order_qty": self.pending["remaining"] if self.pending else 0,
                      "session_date": datetime.fromisoformat(snapshot["now"].replace("Z", "+00:00")).astimezone(NY).date(),
                      "as_of": snapshot["now"]})

    def fill_entry(self, snapshot, amount):
        if not self.pending:
            return None
        intent = self.pending["intent"]
        ask = Decimal(snapshot["quote"]["ask"])
        if snapshot["symbol"] != intent["symbol"] or ask > Decimal(intent["entry_limit"]):
            return None
        qty = self.pending["remaining"] if amount == "all" else min(int(amount), self.pending["remaining"])
        fee = qty * FEE_PER_SHARE
        if self.position is None:
            self.position = {"symbol": intent["symbol"], "qty": 0, "entry_price": str(ask),
                             "stop_price": intent["stop_price"], "target_price": intent["target_price"],
                             "opened_at": snapshot["now"], "setup_id": intent["setup_id"], "entry_fees": "0"}
        old = self.position["qty"]
        self.position["entry_price"] = str((Decimal(self.position["entry_price"])*old+ask*qty)/(old+qty))
        self.position["qty"] += qty
        self.position["entry_fees"] = str(Decimal(self.position["entry_fees"])+fee)
        self.cash -= qty*ask+fee
        self.fees += fee
        self.pending["remaining"] -= qty
        fill = {"symbol": intent["symbol"], "side": "buy", "qty": qty, "price": str(ask),
                "fee": str(fee), "at": snapshot["now"], "remaining_qty": self.pending["remaining"],
                "provenance": "synthetic", "broker_confirmed": False}
        if not self.pending["remaining"]:
            self.pending = None
        self.fills.append(fill)
        return fill

    def fill_exit(self, intent, snapshot):
        position = self.position
        if position is None or self.pending:
            return None, None
        bid = Decimal(snapshot["quote"]["bid"])
        if bid < Decimal(intent["entry_limit"]):
            return None, None
        qty = position["qty"]
        fee = qty*FEE_PER_SHARE
        gross = (bid-Decimal(position["entry_price"]))*qty
        self.cash += qty*bid-fee
        self.realized += gross
        self.fees += fee
        fill = {"symbol": position["symbol"], "side": "sell", "qty": qty, "price": str(bid),
                "fee": str(fee), "at": snapshot["now"], "remaining_qty": 0,
                "provenance": "synthetic", "broker_confirmed": False}
        total_fees = Decimal(position["entry_fees"])+fee
        trade = {"symbol": position["symbol"], "qty": qty, "entry_price": position["entry_price"],
                 "exit_price": str(bid), "opened_at": position["opened_at"], "closed_at": snapshot["now"],
                 "stop_price": position["stop_price"], "target_price": position["target_price"],
                 "setup_id": position["setup_id"], "gross_pnl": str(gross), "fees": str(total_fees),
                 "net_pnl": str(gross-total_fees), "exit_reason": intent["reason"],
                 "provenance": "synthetic", "broker_confirmed": False}
        self.position = None
        self.fills.append(fill)
        self.trades.append(trade)
        return fill, trade


def run_demo(config, journal=None, *, run_id="demo-v1"):
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("run_id must be 1–80 letters, numbers, underscores or hyphens")
    journal = journal or Journal(config.db_path)
    strategy = StrategyConfig(**config.strategy_overrides)
    risk_config = RiskConfig(**config.risk_overrides)
    if strategy.config_id != StrategyConfig().config_id or risk_config.config_id != RiskConfig().config_id:
        raise ValueError("The fixed demo fixture requires the default strategy and risk settings; replaying edited drafts is not released")
    settings = {"strategy": plain(asdict(strategy)), "risk": plain(asdict(risk_config)),
                "initial_simulated_equity": config.demo_equity, "fixture_version": FIXTURE_VERSION}
    fingerprint = sha256(canonical_json(settings).encode()).hexdigest()
    start = journal.append(run_id, "demo_run_started",
                           {"mode": "demo", "provenance": "synthetic", "configuration_hash": fingerprint,
                            "settings": settings}, key=f"demo:{run_id}:start", timestamp="2026-10-16T13:34:52Z")
    old = journal.event_by_key(f"demo:{run_id}:complete")
    if old:
        payload = old["payload"]
        _write_exports(config, journal, run_id, payload)
        return payload
    risk = RiskManager(risk_config)
    ledger = SyntheticAccount(config.demo_equity)
    decisions, equity_curve, checks, display_data = [], [], [], []
    sequence = 0

    def emit(kind, payload, at):
        nonlocal sequence
        sequence += 1
        return journal.append(run_id, kind, {"mode": "demo", "provenance": "synthetic", **plain(payload)},
                              key=f"demo:{run_id}:{sequence:04d}:{kind}", timestamp=at)

    for index, item in enumerate(scenarios()):
        snapshot = item["snapshot"]
        at = snapshot["now"]
        display_data.append({"scenario": item["name"], "symbol": snapshot["symbol"], "at": at,
                             "feed": snapshot["feed"], "provenance": "synthetic",
                             "session": snapshot["session"], "quote": snapshot["quote"],
                             "last_trade": snapshot["last_trade"], "bars": snapshot["bars"],
                             "facts": snapshot["facts"],
                             "rvol": {"as_of": snapshot["rvol"]["as_of"],
                                      "current_volume": snapshot["rvol"]["current_volume"],
                                      "prior_same_clock_mean_volume": str(sum(Decimal(str(row["volume"])) for row in snapshot["rvol"]["history"])/30),
                                      "prior_session_count": 30}})
        emit("demo_market_snapshot", {"scenario": item["name"], "snapshot": snapshot}, at)
        if item.get("restart"):
            saved = risk.to_state()
            risk = RiskManager(risk_config)
            risk.restore_state(saved)
            emit("demo_risk_state_restored", {"state": saved, "broker_reconciliation_assumed": False}, at)
        if item.get("fill_pending"):
            fill = ledger.fill_entry(snapshot, item["fill_pending"])
            if fill:
                emit("demo_fill", fill, at)
        setup = evaluate_setup(snapshot, strategy)
        account = ledger.mark(snapshot)
        checked = risk.assess(setup.intent if setup.accepted else None, account, snapshot["quote"], at,
                              connected=True, reconciled=item.get("reconciled", True),
                              kill_switch=item.get("kill_switch", False),
                              halt_new_entries=item.get("halt_new_entries", False))
        emit("demo_setup_decision", {"scenario": item["name"], "decision": setup.to_dict()}, at)
        emit("demo_risk_decision", {"scenario": item["name"], "decision": checked.to_dict(),
                                    "account_input": account, "connection_source": "synthetic_fixture"}, at)
        exit_decision = None
        if ledger.position:
            forced = checked.reasons[0] if checked.metrics.get("flatten_required") else None
            exit_decision = evaluate_exit(ledger.position, snapshot, strategy, force_reason=forced)
            emit("demo_exit_decision", {"scenario": item["name"], "decision": exit_decision.to_dict(),
                                        "position_before": deepcopy(ledger.position)}, at)
            if exit_decision.accepted:
                emit("demo_order_intent", {"side": "sell", "intent": exit_decision.intent,
                                           "position_flat": False}, at)
                fill, trade = ledger.fill_exit(exit_decision.intent, snapshot)
                if fill:
                    emit("demo_fill", fill, at)
                if trade:
                    trade_id = f"demo:{run_id}:{trade['setup_id']}"
                    journal.append_trade(run_id, trade_id, trade)
                    emit("demo_trade_closed", {"trade_id": trade_id, "trade": trade}, at)
        elif setup.accepted and checked.accepted:
            ledger.pending = {"intent": checked.intent, "remaining": checked.intent["qty"]}
            emit("demo_order_intent", {"side": "buy", "intent": checked.intent,
                                       "filled_qty": 0, "remaining_qty": checked.intent["qty"]}, at)
        after = ledger.mark(snapshot)
        row = {"index": index+1, "at": at, "symbol": snapshot["symbol"], "scenario": item["name"],
               "strategy": setup.to_dict(), "risk": checked.to_dict(),
               "exit": exit_decision.to_dict() if exit_decision else None,
               "position_qty_after": after["position_qty"], "pending_qty_after": after["open_order_qty"]}
        decisions.append(row)
        equity_curve.append({"at": at, "simulated_equity": after["equity"],
                             "net_pnl": str(Decimal(after["realized_pnl"])+Decimal(after["unrealized_pnl"])-Decimal(after["fees"]))})
        checks.append({"scenario": item["name"], "strategy_reasons": list(setup.reasons),
                       "risk_reasons": list(checked.reasons),
                       "exit_reasons": list(exit_decision.reasons) if exit_decision else [],
                       "position_qty_after": after["position_qty"], "pending_qty_after": after["open_order_qty"]})
        emit("demo_account_state", {"account": after, "risk_state": risk.to_state()}, at)
    account = ledger.mark(scenarios()[-1]["snapshot"])
    payload = {
        "schema_version": "1.0", "run_id": run_id, "run_key": run_id,
        "mode": "demo", "provenance": "synthetic", "status": "completed",
        "strategy_version": "experimental_v1", "fixture_version": FIXTURE_VERSION,
        "configuration_hash": fingerprint, "strategy_config_id": strategy.config_id,
        "risk_config_id": risk_config.config_id, "generated_at": start["recorded_at"],
        "started_at": "2026-10-16T13:34:52Z", "completed_at": "2026-10-16T13:43:05Z",
        "broker": {"name": "Alpaca paper", "connected": False, "reconciled": False,
                   "orders_submitted": 0, "live_execution_supported": False},
        "settings": settings, "account": {**account, "mode": "demo", "provenance": "synthetic",
                                           "initial_equity": config.demo_equity},
        "positions": [ledger.position] if ledger.position else [],
        "open_orders": [ledger.pending] if ledger.pending else [],
        "trades": ledger.trades, "fills": ledger.fills, "decisions": decisions,
        "display_data": display_data,
        "equity_curve": equity_curve, "scenario_checks": checks, "risk_state": risk.to_state(),
        "metrics": {"completed_trades": len(ledger.trades), "simulated_fills": len(ledger.fills),
                    "gross_pnl": str(ledger.realized), "fees": str(ledger.fees),
                    "net_pnl": str(ledger.realized-ledger.fees),
                    "scenario_count": len(decisions), "event_count": sequence+2,
                    "actual_broker_orders": 0},
        "assumptions": [
            "Every symbol, quote, bar, float, catalyst and volume history is invented for this replay.",
            "The weekday-only synthetic calendar is not an exchange holiday calendar.",
            "Engine connection/reconciliation flags describe the in-process simulator; no broker was contacted.",
            "Synthetic buy fills use the displayed ask; sells use the displayed bid; one entry fills in two parts.",
            "The invented fee is $0.003 per share per side. Queue priority, latency, impact and real depth are absent.",
            "The stop scenario includes a stale quote and a fill below the planned stop once a fresh quote arrives.",
            "Default risk budgets are demonstration settings, not an approved paper or real-money allocation.",
            "This replay validates software behavior. Its P&L is not backtest, paper-account or live performance."
        ],
    }
    journal.append(run_id, "demo_run_completed", payload, key=f"demo:{run_id}:complete",
                   timestamp=payload["completed_at"])
    _write_exports(config, journal, run_id, payload)
    return payload


def _write_exports(config, journal, run_id, payload):
    directory = Path(config.export_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{run_id}.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    temporary.replace(destination)
    journal.export_ndjson(run_id, directory / f"{run_id}.ndjson")

