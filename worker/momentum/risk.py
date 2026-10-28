"""Independent, fail-closed risk sizing for paper intentions only.

The caller owns orders and fills. It must reserve pending opening quantity before
submitting, count partial fills immediately, persist to_state(), and reconcile
its account after reconnect. Nothing in this module submits or cancels an order.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Mapping

from .models import (Decision, InputError, NY, PROFILE, Quote, config_fingerprint, decimal_value,
                     integer_value, plain, positive_decimal, session_date, timestamp)


@dataclass(frozen=True)
class RiskConfig:
    mode: str = "paper"
    per_trade_risk: Decimal = Decimal("25")
    max_daily_loss: Decimal = Decimal("75")
    max_notional: Decimal = Decimal("1000")
    max_shares: int = 500
    execution_allowance_per_share: Decimal = Decimal("0.02")
    max_volume_participation: Decimal = Decimal("0.01")
    max_quote_age_seconds: Decimal = Decimal("2")
    max_intent_age_seconds: Decimal = Decimal("2")
    max_account_age_seconds: Decimal = Decimal("5")
    max_spread: Decimal = Decimal("0.05")
    max_spread_bps: Decimal = Decimal("100")
    min_reward_r: Decimal = Decimal("2")

    @property
    def config_id(self) -> str:
        return config_fingerprint(self)

    def __post_init__(self) -> None:
        if self.mode != "paper":
            raise ValueError("Risk manager supports paper mode only")
        for name in ("per_trade_risk", "max_daily_loss", "max_notional", "execution_allowance_per_share",
                     "max_volume_participation", "max_quote_age_seconds", "max_intent_age_seconds",
                     "max_account_age_seconds", "max_spread", "max_spread_bps", "min_reward_r"):
            object.__setattr__(self, name, positive_decimal(getattr(self, name), name))
        object.__setattr__(self, "max_shares", integer_value(self.max_shares, "max_shares", minimum=1))
        if self.per_trade_risk > self.max_daily_loss:
            raise ValueError("Per-trade risk cannot exceed the daily loss limit")
        if self.max_volume_participation > 1:
            raise ValueError("Volume participation must be in (0, 1]")
        if self.min_reward_r < 2:
            raise ValueError("experimental_v1 requires at least 2R planned reward")


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


class RiskManager:
    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()
        self._daily_locks: dict[str, str] = {}
        self._kill_latched = False
        self._last_session: str | None = None
        self._last_observed_at: datetime | None = None
        self._reconciliation_required = False

    @property
    def locked(self) -> bool:
        return self._kill_latched or self._last_session in self._daily_locks

    def to_state(self) -> dict[str, Any]:
        return plain({"version": 1, "profile": PROFILE, "daily_locks": dict(self._daily_locks),
                      "kill_latched": self._kill_latched, "last_session": self._last_session,
                      "last_observed_at": self._last_observed_at,
                      "reconciliation_required": self._reconciliation_required})

    def restore_state(self, state: Mapping[str, Any]) -> None:
        """Restore validated risk latches, without assuming broker reconciliation."""
        if not isinstance(state, Mapping):
            raise InputError("invalid_risk_state")
        if state.get("version") != 1 or state.get("profile") != PROFILE:
            raise InputError("invalid_risk_state_version")
        if type(state.get("kill_latched")) is not bool or type(state.get("reconciliation_required")) is not bool:
            raise InputError("invalid_risk_state")
        raw_locks = state.get("daily_locks")
        if not isinstance(raw_locks, Mapping):
            raise InputError("invalid_risk_state")
        locks: dict[str, str] = {}
        for key, reason in raw_locks.items():
            day = session_date(key, "risk_state.daily_locks").isoformat()
            if reason != "daily_loss_limit":
                raise InputError("invalid_risk_state")
            locks[day] = reason
        last_session = state.get("last_session")
        if last_session is not None:
            last_session = session_date(last_session).isoformat()
        last_time = state.get("last_observed_at")
        if last_time is not None:
            last_time = timestamp(last_time)
            if last_session != last_time.astimezone(NY).date().isoformat():
                raise InputError("invalid_risk_state")
        self._daily_locks = locks
        self._kill_latched = state["kill_latched"]
        self._last_session = last_session
        self._last_observed_at = last_time
        # A saved 'reconciled' flag is not current proof after a restart.
        self._reconciliation_required = True

    def assess(self, intent: Mapping[str, Any] | None, account: Mapping[str, Any],
               quote: Mapping[str, Any], now: datetime | str, *, connected: bool = False,
               reconciled: bool = False, kill_switch: bool = False,
               halt_new_entries: bool = False) -> Decision:
        """Check account every event, including assess(None, ...) when no entry.

        P&L fields are GROSS realized and unrealized, with fees provided separately.
        Each partial position and each pending opening remainder count as exposure.
        No allocation or fill is inferred from an accepted intention.
        """
        result = self._assess(intent, account, quote, now, connected=connected,
                              reconciled=reconciled, kill_switch=kill_switch,
                              halt_new_entries=halt_new_entries)
        return replace(result, metrics={**result.metrics, "risk_config_id": self.config.config_id})

    def _assess(self, intent: Mapping[str, Any] | None, account: Mapping[str, Any],
                quote: Mapping[str, Any], now: datetime | str, *, connected: bool,
                reconciled: bool, kill_switch: bool, halt_new_entries: bool) -> Decision:
        cfg = self.config
        metrics: dict[str, Any] = {}
        try:
            current = timestamp(now, "now")
            day = current.astimezone(NY).date().isoformat()
            if any(type(value) is not bool for value in (connected, reconciled, kill_switch, halt_new_entries)):
                return Decision.reject("invalid_operational_flags")
            if kill_switch:
                self._kill_latched = True
            if not connected or not reconciled:
                self._reconciliation_required = True
            if self._last_observed_at is not None and current < self._last_observed_at:
                return Decision.reject("out_of_order_account_event")
            if not isinstance(account, Mapping):
                return Decision.reject("invalid_account")
            if session_date(account.get("session_date")).isoformat() != day:
                return Decision.reject("account_session_mismatch")
            if account.get("mode", "paper") != "paper":
                return Decision.reject("live_mode_forbidden")
            equity = positive_decimal(account.get("equity"), "account.equity")
            buying_power = decimal_value(account.get("buying_power"), "account.buying_power", minimum=Decimal(0))
            realized = decimal_value(account.get("realized_pnl"), "account.realized_pnl")
            unrealized = decimal_value(account.get("unrealized_pnl"), "account.unrealized_pnl")
            fees = decimal_value(account.get("fees"), "account.fees", minimum=Decimal(0))
            position_qty = integer_value(account.get("position_qty"), "account.position_qty")
            open_order_qty = integer_value(account.get("open_order_qty"), "account.open_order_qty")
            account_time = timestamp(account.get("as_of"), "account.as_of")
            if account_time > current or Decimal(str((current - account_time).total_seconds())) > cfg.max_account_age_seconds:
                return Decision.reject("stale_or_future_account")
            net_pnl = realized + unrealized - fees
            metrics = plain({"net_pnl": net_pnl, "daily_loss_limit": cfg.max_daily_loss,
                             "position_qty": position_qty, "open_order_qty": open_order_qty,
                             "flatten_required": False, "cancel_entries_required": False})
            old_session = self._last_session
            self._last_session, self._last_observed_at = day, current
            if net_pnl <= -cfg.max_daily_loss:
                self._daily_locks[day] = "daily_loss_limit"
            if self._kill_latched or day in self._daily_locks:
                metrics["flatten_required"] = position_qty > 0
                metrics["cancel_entries_required"] = open_order_qty > 0
                metrics["locked"] = True
                return Decision.reject("kill_switch_latched" if self._kill_latched else "daily_loss_limit", metrics=metrics)
            if old_session is not None and day != old_session and (position_qty or open_order_qty):
                return Decision.reject("overnight_exposure_requires_reconciliation", metrics=metrics)
            if halt_new_entries:
                metrics["cancel_entries_required"] = open_order_qty > 0
                return Decision.reject("new_entries_halted", metrics=metrics)
            if not connected:
                return Decision.reject("feed_disconnected", metrics=metrics)
            if not reconciled:
                return Decision.reject("reconciliation_required", metrics=metrics)
            # This True is the caller's new reconciliation assertion, not persisted state.
            self._reconciliation_required = False
            if position_qty or open_order_qty:
                return Decision.reject("existing_exposure", metrics=metrics)
            market_quote = Quote.from_dict(quote)
            market_quote.check_time(current, cfg.max_quote_age_seconds)
            spread = market_quote.ask - market_quote.bid
            if spread > cfg.max_spread or spread / market_quote.bid * 10000 > cfg.max_spread_bps:
                return Decision.reject("spread_too_wide", metrics=metrics)
            if intent is None:
                return Decision.reject("no_entry_intent", metrics=metrics)
            if not isinstance(intent, Mapping):
                return Decision.reject("invalid_intent", metrics=metrics)
            if intent.get("mode") != "paper" or intent.get("profile") != PROFILE:
                return Decision.reject("unapproved_intent_mode_or_profile", metrics=metrics)
            if intent.get("side") != "buy":
                return Decision.reject("only_long_entries_supported", metrics=metrics)
            symbol = intent.get("symbol")
            if not isinstance(symbol, str) or not symbol:
                return Decision.reject("missing_or_invalid_symbol", metrics=metrics)
            if quote.get("symbol", symbol) != symbol:
                return Decision.reject("quote_symbol_mismatch", metrics=metrics)
            if intent.get("feed") != market_quote.feed:
                return Decision.reject("mixed_feed", metrics=metrics)
            observed = timestamp(intent.get("observed_at"), "intent.observed_at")
            if observed > current or Decimal(str((current - observed).total_seconds())) > cfg.max_intent_age_seconds:
                return Decision.reject("stale_or_future_intent", metrics=metrics)
            entry = positive_decimal(intent.get("entry_limit"), "intent.entry_limit")
            stop = positive_decimal(intent.get("stop_price"), "intent.stop_price")
            target = positive_decimal(intent.get("target_price"), "intent.target_price")
            if stop >= entry or target <= entry:
                return Decision.reject("invalid_stop_or_target", metrics=metrics)
            if target - entry < cfg.min_reward_r * (entry - stop):
                return Decision.reject("insufficient_reward_room", metrics=metrics)
            if market_quote.ask > entry:
                return Decision.reject("entry_limit_not_marketable", metrics=metrics)
            if cfg.per_trade_risk > equity or cfg.max_daily_loss > equity:
                return Decision.reject("risk_budget_exceeds_equity", metrics=metrics)
            liquidity_volume = integer_value(intent.get("liquidity_volume"), "intent.liquidity_volume", minimum=1)
            risk_per_share = entry - stop + cfg.execution_allowance_per_share
            remaining_daily_risk = cfg.max_daily_loss + net_pnl
            budget = min(cfg.per_trade_risk, remaining_daily_risk)
            caps = {"risk": _floor(budget / risk_per_share),
                    "buying_power": _floor(buying_power / entry),
                    "notional": _floor(min(cfg.max_notional, equity) / entry),
                    "liquidity": _floor(Decimal(liquidity_volume) * cfg.max_volume_participation),
                    "configured_shares": cfg.max_shares}
            quantity = min(caps.values())
            metrics.update(plain({"quantity_caps": caps, "risk_budget": budget,
                                  "remaining_daily_risk": remaining_daily_risk, "risk_per_share": risk_per_share}))
            if quantity < 1:
                return Decision.reject("zero_quantity_under_limits", metrics=metrics)
            approved = dict(intent)
            approved.update(plain({"qty": quantity, "notional": entry * quantity,
                                   "planned_risk": risk_per_share * quantity,
                                   "risk_per_share": risk_per_share,
                                   "risk_checked_at": current, "risk_config_id": cfg.config_id,
                                   "live_enabled": False, "paper_only": True}))
            metrics["locked"] = False
            return Decision(True, ("paper_risk_approved",), plain(approved), metrics)
        except InputError as error:
            metrics["field"] = error.field_name
            return Decision.reject(error.code, metrics=metrics)
        except (ValueError, TypeError, KeyError, ArithmeticError) as error:
            metrics["error_type"] = type(error).__name__
            return Decision.reject("invalid_risk_input", metrics=metrics)
