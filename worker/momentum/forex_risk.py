"""Margin-aware leverage risk model for OANDA practice forex entries.

PAPER PRACTICE ONLY. Conservative caps well below OANDA's default 50:1
leverage, with mandatory per-trade stop losses. Never use this screen to block
a protective exit/close.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re
from typing import Mapping

FOREX_PAIR = re.compile(r"^[A-Z]{3}/[A-Z]{3}$")
MAX_CONFIGURABLE_LEVERAGE = Decimal("5.0")


def _finite_decimal(value, *, allow_zero=False, name="value") -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} cannot be a boolean")
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a valid decimal") from exc
    if not dec.is_finite():
        raise ValueError(f"{name} must be finite")
    if allow_zero:
        if dec < 0:
            raise ValueError(f"{name} must be non-negative")
    else:
        if dec <= 0:
            raise ValueError(f"{name} must be positive")
    return dec


@dataclass(frozen=True)
class ForexRiskConfig:
    """Conservative leverage and margin limits for practice forex trading."""

    max_units_per_order: int = 1000
    max_order_notional_usd: Decimal = Decimal("100")
    max_notional_per_pair_usd: Decimal = Decimal("250")
    max_total_open_exposure_usd: Decimal = Decimal("1000")
    max_effective_leverage: Decimal = Decimal("2.0")
    max_margin_utilization: Decimal = Decimal("0.20")
    max_risk_per_trade_usd: Decimal = Decimal("25")
    max_risk_per_trade_pct: Decimal = Decimal("0.01")
    min_stop_distance_pct: Decimal = Decimal("0.0005")
    max_stop_distance_pct: Decimal = Decimal("0.05")
    max_daily_loss_usd: Decimal = Decimal("50")
    max_open_positions: int = 3
    max_feed_age_ms: int = 2000
    max_spread_bps: Decimal = Decimal("15")
    default_pair_margin_rate: Decimal = Decimal("0.02")
    require_stop_loss: bool = True

    @property
    def config_id(self) -> str:
        from hashlib import sha256
        import json

        payload = {
            "max_units_per_order": self.max_units_per_order,
            "max_order_notional_usd": format(self.max_order_notional_usd, "f"),
            "max_notional_per_pair_usd": format(self.max_notional_per_pair_usd, "f"),
            "max_total_open_exposure_usd": format(self.max_total_open_exposure_usd, "f"),
            "max_effective_leverage": format(self.max_effective_leverage, "f"),
            "max_margin_utilization": format(self.max_margin_utilization, "f"),
            "max_risk_per_trade_usd": format(self.max_risk_per_trade_usd, "f"),
            "max_risk_per_trade_pct": format(self.max_risk_per_trade_pct, "f"),
            "min_stop_distance_pct": format(self.min_stop_distance_pct, "f"),
            "max_stop_distance_pct": format(self.max_stop_distance_pct, "f"),
            "max_daily_loss_usd": format(self.max_daily_loss_usd, "f"),
            "max_open_positions": self.max_open_positions,
            "max_feed_age_ms": self.max_feed_age_ms,
            "max_spread_bps": format(self.max_spread_bps, "f"),
            "default_pair_margin_rate": format(self.default_pair_margin_rate, "f"),
            "require_stop_loss": self.require_stop_loss,
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def __post_init__(self) -> None:
        if self.require_stop_loss is not True:
            raise ValueError("Per-trade stop loss is mandatory for forex practice orders")
        if type(self.max_units_per_order) is not int or not 1 <= self.max_units_per_order <= 10_000:
            raise ValueError("max_units_per_order must be an integer between 1 and 10000")
        if type(self.max_open_positions) is not int or not 1 <= self.max_open_positions <= 10:
            raise ValueError("max_open_positions must be an integer between 1 and 10")
        if type(self.max_feed_age_ms) is not int or not 1 <= self.max_feed_age_ms <= 5000:
            raise ValueError("max_feed_age_ms must be between 1 and 5000")

        for field_name in (
            "max_order_notional_usd",
            "max_notional_per_pair_usd",
            "max_total_open_exposure_usd",
            "max_effective_leverage",
            "max_margin_utilization",
            "max_risk_per_trade_usd",
            "max_risk_per_trade_pct",
            "min_stop_distance_pct",
            "max_stop_distance_pct",
            "max_daily_loss_usd",
            "max_spread_bps",
            "default_pair_margin_rate",
        ):
            object.__setattr__(self, field_name, _finite_decimal(getattr(self, field_name), name=field_name))

        if self.max_effective_leverage > MAX_CONFIGURABLE_LEVERAGE:
            raise ValueError("max_effective_leverage cannot exceed 5.0 (well below OANDA 50:1)")
        if not Decimal("0") < self.max_margin_utilization <= Decimal("0.50"):
            raise ValueError("max_margin_utilization must be in (0, 0.50]")
        if not Decimal("0") < self.max_risk_per_trade_pct <= Decimal("0.05"):
            raise ValueError("max_risk_per_trade_pct must be in (0, 0.05]")
        if self.min_stop_distance_pct >= self.max_stop_distance_pct:
            raise ValueError("min_stop_distance_pct must be less than max_stop_distance_pct")
        if self.max_order_notional_usd > self.max_notional_per_pair_usd:
            raise ValueError("max_order_notional_usd cannot exceed max_notional_per_pair_usd")
        if self.max_notional_per_pair_usd > self.max_total_open_exposure_usd:
            raise ValueError("max_notional_per_pair_usd cannot exceed max_total_open_exposure_usd")
        if self.max_risk_per_trade_usd > self.max_daily_loss_usd:
            raise ValueError("max_risk_per_trade_usd cannot exceed max_daily_loss_usd")
        if not Decimal("0.01") <= self.default_pair_margin_rate <= Decimal("1.0"):
            raise ValueError("default_pair_margin_rate must be in [0.01, 1.0]")


@dataclass(frozen=True)
class ForexOrderIntent:
    pair: str
    units: Decimal  # signed: >0 buy, <0 sell
    entry_price: Decimal
    stop_loss_price: Decimal | None
    client_order_id: str
    base_to_usd_rate: Decimal | None = None
    quote_to_usd_rate: Decimal | None = None
    pair_margin_rate: Decimal | None = None


@dataclass(frozen=True)
class ForexRiskState:
    nav_usd: Decimal
    margin_used_usd: Decimal
    margin_available_usd: Decimal
    total_open_exposure_usd: Decimal
    realized_daily_pnl_usd: Decimal
    open_positions: int
    feed_age_ms: int
    kill_switch: bool
    seen_order_ids: frozenset[str] = field(default_factory=frozenset)
    pair_exposures_usd: Mapping[str, Decimal] = field(default_factory=dict)
    spread_bps: Decimal | None = None


def _is_finite(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def compute_usd_metrics(
    pair: str,
    abs_units: Decimal,
    entry_price: Decimal,
    stop_loss_price: Decimal,
    *,
    base_to_usd_rate: Decimal | None = None,
    quote_to_usd_rate: Decimal | None = None,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (usd_per_unit, order_notional_usd, stop_loss_risk_usd).

    Raises ValueError if conversion rates are missing for cross pairs.
    """
    base, quote = pair.split("/")
    if quote == "USD":
        usd_per_unit = entry_price
        notional_usd = abs_units * entry_price
        risk_usd = abs_units * abs(entry_price - stop_loss_price)
        return usd_per_unit, notional_usd, risk_usd
    if base == "USD":
        usd_per_unit = Decimal("1")
        notional_usd = abs_units
        risk_usd = (abs_units * abs(entry_price - stop_loss_price)) / stop_loss_price
        return usd_per_unit, notional_usd, risk_usd
    if base_to_usd_rate is None or not _is_finite(base_to_usd_rate) or base_to_usd_rate <= 0:
        raise ValueError("cross_pair_usd_rate_missing")
    usd_per_unit = base_to_usd_rate
    notional_usd = abs_units * base_to_usd_rate
    if quote_to_usd_rate is not None:
        if not _is_finite(quote_to_usd_rate) or quote_to_usd_rate <= 0:
            raise ValueError("cross_pair_usd_rate_missing")
        q_usd = quote_to_usd_rate
    else:
        q_usd = base_to_usd_rate / entry_price
    risk_usd = abs_units * abs(entry_price - stop_loss_price) * q_usd
    return usd_per_unit, notional_usd, risk_usd


def check_forex_leverage_intent(
    intent: ForexOrderIntent,
    state: ForexRiskState,
    config: ForexRiskConfig = ForexRiskConfig(),
) -> tuple[bool, str]:
    """Fail-closed margin-aware entry screen for OANDA practice orders."""
    if not isinstance(config, ForexRiskConfig):
        return False, "invalid_risk_config"
    if not isinstance(state.kill_switch, bool) or state.kill_switch:
        return False, "kill_switch_or_invalid_state"
    if (
        not isinstance(intent.pair, str)
        or FOREX_PAIR.fullmatch(intent.pair.strip().upper()) is None
        or not isinstance(intent.client_order_id, str)
        or not intent.client_order_id.strip()
    ):
        return False, "invalid_or_duplicate_order"
    pair = intent.pair.strip().upper()
    if (
        not isinstance(state.seen_order_ids, (frozenset, set))
        or intent.client_order_id in state.seen_order_ids
    ):
        return False, "invalid_or_duplicate_order"

    if intent.stop_loss_price is None:
        return False, "stop_loss_required"

    if not all(
        _is_finite(v)
        for v in (
            intent.units,
            intent.entry_price,
            intent.stop_loss_price,
            state.nav_usd,
            state.margin_used_usd,
            state.margin_available_usd,
            state.total_open_exposure_usd,
            state.realized_daily_pnl_usd,
        )
    ):
        return False, "invalid_numeric_state"

    if intent.units == 0 or intent.entry_price <= 0 or intent.stop_loss_price <= 0:
        return False, "invalid_quantity_or_price"

    if (
        state.nav_usd <= 0
        or state.margin_used_usd < 0
        or state.margin_available_usd < 0
        or state.total_open_exposure_usd < 0
    ):
        return False, "invalid_account_margin_state"

    if type(state.open_positions) is not int or state.open_positions < 0:
        return False, "invalid_position_state"

    if (
        type(state.feed_age_ms) is not int
        or state.feed_age_ms < 0
        or state.feed_age_ms > config.max_feed_age_ms
    ):
        return False, "stale_or_invalid_feed"

    if state.spread_bps is not None:
        if not _is_finite(state.spread_bps) or state.spread_bps < 0 or state.spread_bps > config.max_spread_bps:
            return False, "spread_limit_exceeded"

    if state.realized_daily_pnl_usd <= -config.max_daily_loss_usd:
        return False, "daily_loss_limit"

    if state.open_positions >= config.max_open_positions:
        return False, "position_limit"

    abs_units = abs(intent.units)
    if abs_units > config.max_units_per_order:
        return False, "unit_cap_exceeded"

    # Directional stop-loss check + distance band
    if intent.units > 0 and intent.stop_loss_price >= intent.entry_price:
        return False, "stop_loss_wrong_side"
    if intent.units < 0 and intent.stop_loss_price <= intent.entry_price:
        return False, "stop_loss_wrong_side"

    stop_distance_pct = abs(intent.entry_price - intent.stop_loss_price) / intent.entry_price
    if stop_distance_pct < config.min_stop_distance_pct:
        return False, "stop_loss_too_tight"
    if stop_distance_pct > config.max_stop_distance_pct:
        return False, "stop_loss_too_wide"

    try:
        _, notional_usd, stop_risk_usd = compute_usd_metrics(
            pair,
            abs_units,
            intent.entry_price,
            intent.stop_loss_price,
            base_to_usd_rate=intent.base_to_usd_rate,
            quote_to_usd_rate=intent.quote_to_usd_rate,
        )
    except ValueError as exc:
        return False, str(exc) or "cross_pair_usd_rate_missing"
    except (InvalidOperation, ArithmeticError):
        return False, "invalid_numeric_state"

    if not notional_usd.is_finite() or not stop_risk_usd.is_finite():
        return False, "invalid_numeric_state"

    if notional_usd > config.max_order_notional_usd:
        return False, "order_notional_cap_exceeded"

    if not isinstance(state.pair_exposures_usd, Mapping):
        return False, "invalid_numeric_state"
    raw_pair_exp = state.pair_exposures_usd.get(pair, Decimal("0"))
    if not _is_finite(raw_pair_exp) or raw_pair_exp < 0:
        return False, "invalid_numeric_state"
    projected_pair_exposure = raw_pair_exp + notional_usd
    if projected_pair_exposure > config.max_notional_per_pair_usd:
        return False, "pair_notional_cap_exceeded"

    projected_total_exposure = state.total_open_exposure_usd + notional_usd
    if projected_total_exposure > config.max_total_open_exposure_usd:
        return False, "total_exposure_cap_exceeded"

    projected_leverage = projected_total_exposure / state.nav_usd
    if projected_leverage > config.max_effective_leverage:
        return False, "effective_leverage_cap_exceeded"

    # Margin check: take the more conservative of configured default or instrument margin rate.
    pair_margin_rate = config.default_pair_margin_rate
    if intent.pair_margin_rate is not None:
        if (
            not _is_finite(intent.pair_margin_rate)
            or not Decimal("0") < intent.pair_margin_rate <= Decimal("1")
        ):
            return False, "invalid_margin_rate"
        pair_margin_rate = max(pair_margin_rate, intent.pair_margin_rate)

    required_margin_usd = notional_usd * pair_margin_rate
    if required_margin_usd > state.margin_available_usd:
        return False, "insufficient_margin_available"

    projected_margin_used = state.margin_used_usd + required_margin_usd
    if projected_margin_used / state.nav_usd > config.max_margin_utilization:
        return False, "margin_utilization_cap_exceeded"

    max_allowed_trade_risk = min(
        config.max_risk_per_trade_usd,
        state.nav_usd * config.max_risk_per_trade_pct,
    )
    if stop_risk_usd > max_allowed_trade_risk:
        return False, "stop_loss_risk_cap_exceeded"

    return True, "approved_for_oanda_practice"
