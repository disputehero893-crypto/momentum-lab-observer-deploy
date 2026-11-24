"""Bind saved customer settings to the existing stock paper executor.

This module is a compiler, not authentication, strategy promotion, or consent.
Only server-loaded rows and a server-controlled reviewed-strategy map may enter
it. Compiling ALWAYS disarms execution; an operator must independently review
the resulting hash. The current executor owns at most one long stock position.
"""
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
import uuid

from .config import Config
from .risk import RiskConfig
from .strategy import load_strategy

_ROW_KEYS = {"owner_id", "version", "request_id", "configuration", "updated_at"}
_CONFIG_KEYS = {"mode", "market", "capital_allocation", "risk_per_trade",
                "daily_loss_limit", "max_position_exposure", "max_open_positions",
                "allowed_symbols", "strategy_version"}
_MONEY = re.compile(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,2})?\Z")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")
_STRATEGY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class SettingsBindingError(ValueError):
    """Fixed error codes only; never expose upstream data or credentials."""


def _uuid(value):
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) else None
        if parsed is None or str(parsed) != value or parsed.version not in range(1, 6):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise SettingsBindingError("invalid_settings_identity") from None
    return value


def _money(value):
    if not isinstance(value, str) or not _MONEY.fullmatch(value):
        raise SettingsBindingError("invalid_settings_amount")
    number = Decimal(value)
    if number <= 0:
        raise SettingsBindingError("invalid_settings_amount")
    return number


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_saved_settings(row, *, owner_id, now):
    """Validate a complete trusted row; never normalize a foreign owner away."""
    _uuid(owner_id)
    if not isinstance(row, dict) or set(row) != _ROW_KEYS:
        raise SettingsBindingError("invalid_saved_settings_row")
    if _uuid(row["owner_id"]) != owner_id:
        raise SettingsBindingError("settings_owner_mismatch")
    _uuid(row["version"])
    _uuid(row["request_id"])
    try:
        updated = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
        if updated.tzinfo is None or now.tzinfo is None or updated > now:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise SettingsBindingError("invalid_settings_timestamp") from None
    value = row["configuration"]
    if not isinstance(value, dict) or set(value) != _CONFIG_KEYS:
        raise SettingsBindingError("invalid_settings_fields")
    if value["mode"] != "paper":
        raise SettingsBindingError("customer_execution_is_paper_only")
    if value["market"] != "stocks":
        raise SettingsBindingError("customer_crypto_execution_not_implemented")
    amounts = {key: _money(value[key]) for key in (
        "capital_allocation", "risk_per_trade", "daily_loss_limit", "max_position_exposure")}
    if not (amounts["risk_per_trade"] <= amounts["daily_loss_limit"] <= amounts["capital_allocation"]
            and amounts["risk_per_trade"] <= amounts["max_position_exposure"] <= amounts["capital_allocation"]):
        raise SettingsBindingError("inconsistent_customer_risk_limits")
    positions = value["max_open_positions"]
    if type(positions) is not int or not 1 <= positions <= 20:
        raise SettingsBindingError("invalid_customer_position_limit")
    symbols = value["allowed_symbols"]
    if (not isinstance(symbols, list) or not 1 <= len(symbols) <= 10
            or any(not isinstance(s, str) or not _SYMBOL.fullmatch(s) for s in symbols)
            or len(set(symbols)) != len(symbols)):
        raise SettingsBindingError("invalid_or_unsupported_customer_symbols")
    if not isinstance(value["strategy_version"], str) or not _STRATEGY.fullmatch(value["strategy_version"]):
        raise SettingsBindingError("invalid_requested_strategy_version")
    clean = deepcopy(row)
    clean["updated_at"] = updated.astimezone(timezone.utc).isoformat()
    clean["configuration"].update({key: format(number, ".2f") for key, number in amounts.items()})
    return clean


@dataclass(frozen=True)
class PaperSettingsBinding:
    owner_id: str
    settings_version: str
    settings_fingerprint: str
    worker_review_hash: str
    requested_max_open_positions: int
    capital_allocation: str
    effective_per_trade_risk: str
    effective_daily_loss: str
    effective_position_notional: str
    effective_max_open_positions: int = 1

    def verify_worker(self, config):
        if (config.mode != "paper" or config.owner_user_id != self.owner_id
                or config.review_hash() != self.worker_review_hash):
            raise SettingsBindingError("worker_settings_binding_mismatch")

    def verify_current(self, row, *, now):
        clean = validate_saved_settings(row, owner_id=self.owner_id, now=now)
        if (clean["version"] != self.settings_version
                or sha256(_canonical(clean).encode()).hexdigest() != self.settings_fingerprint):
            raise SettingsBindingError("saved_settings_changed")

    def public(self):
        return {"settings_version": self.settings_version,
                "worker_review_hash": self.worker_review_hash,
                "capital_allocation": self.capital_allocation,
                "effective_per_trade_risk": self.effective_per_trade_risk,
                "effective_daily_loss": self.effective_daily_loss,
                "effective_position_notional": self.effective_position_notional,
                "requested_max_open_positions": self.requested_max_open_positions,
                "effective_max_open_positions": self.effective_max_open_positions,
                "market": "stocks", "mode": "paper"}


def compile_paper_settings(base, row, *, verified_owner_id, reviewed_strategy_ids, now):
    """Return (disarmed Config, binding); never widen operator or customer limits.

    reviewed_strategy_ids maps a requested identifier to an exact StrategyConfig
    fingerprint. It must come from a reviewed server registry, not the customer
    row. The caller must authenticate verified_owner_id and supply bounded fresh
    settings reads at runtime. No default registry approves any strategy.
    """
    if not isinstance(base, Config) or base.mode != "paper" or base.owner_user_id != verified_owner_id:
        raise SettingsBindingError("paper_owner_configuration_required")
    clean = validate_saved_settings(row, owner_id=verified_owner_id, now=now)
    value = clean["configuration"]
    actual_strategy = load_strategy(base.strategy_overrides).config_id
    if (not isinstance(reviewed_strategy_ids, dict)
            or reviewed_strategy_ids.get(value["strategy_version"]) != actual_strategy):
        raise SettingsBindingError("requested_strategy_not_reviewed")
    operator = RiskConfig(**base.risk_overrides)
    daily = min(operator.max_daily_loss, Decimal(value["daily_loss_limit"]))
    risk = min(operator.per_trade_risk, Decimal(value["risk_per_trade"]), daily)
    notional = min(operator.max_notional, Decimal(value["max_position_exposure"]),
                   Decimal(value["capital_allocation"]))
    effective = deepcopy(base.risk_overrides)
    effective.update(per_trade_risk=str(risk), max_daily_loss=str(daily), max_notional=str(notional))
    config = replace(base, paper_enabled=False, paper_reviewed_config_hash="",
                     watchlist=tuple(value["allowed_symbols"]), risk_overrides=effective,
                     strategy_overrides=deepcopy(base.strategy_overrides))
    binding = PaperSettingsBinding(
        owner_id=verified_owner_id, settings_version=clean["version"],
        settings_fingerprint=sha256(_canonical(clean).encode()).hexdigest(),
        worker_review_hash=config.review_hash(),
        requested_max_open_positions=value["max_open_positions"],
        capital_allocation=value["capital_allocation"],
        effective_per_trade_risk=str(risk), effective_daily_loss=str(daily),
        effective_position_notional=str(notional))
    return config, binding
