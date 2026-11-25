"""Fail-closed paper-entry screening. No broker access or order submission.

PAPER TRADING ONLY. Feed age is measured against the free IEX quote/trade
clock the worker already uses (Alpaca `feed=iex`). This helper never talks to
SIP, never places orders, and must never block a protective exit.

This is a preliminary screening helper, not a substitute for atomic broker
reconciliation, authoritative positions, or a separate safe exit pathway.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# Default feed label for docs/telemetry. RiskLimits does not fetch market data.
DEFAULT_FEED = "iex"
FOREX_PRACTICE_FEED = "oanda_practice"
# Alpaca US spot pairs only. These also match the 3-letter forex regex, so the
# screen classifies them as crypto before forex.
CRYPTO_PAPER_FEED = "alpaca_crypto_paper"
CRYPTO_PAPER_PAIRS = frozenset({"BTC/USD", "ETH/USD", "SOL/USD"})
_FOREX_PAIR = re.compile(r"^[A-Z]{3}[_/][A-Z]{3}$")


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: Decimal = Decimal("100")
    max_total_exposure: Decimal = Decimal("1000")
    max_daily_loss: Decimal = Decimal("50")
    max_open_positions: int = 3
    # Stale IEX quotes older than this fail closed (milliseconds).
    max_feed_age_ms: int = 2000
    feed: str = DEFAULT_FEED


@dataclass(frozen=True)
class PaperIntent:
    symbol: str
    quantity: Decimal
    price: Decimal
    client_order_id: str


@dataclass(frozen=True)
class RiskState:
    exposure: Decimal
    realized_daily_pnl: Decimal
    open_positions: int
    feed_age_ms: int
    kill_switch: bool
    seen_order_ids: frozenset[str]


def _finite(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def check_paper_intent(
    intent: PaperIntent,
    state: RiskState,
    limits: RiskLimits = RiskLimits(),
) -> tuple[bool, str]:
    """Screen entries only; never use this function to block protective exits."""
    symbol = getattr(intent, "symbol", "")
    normalized = symbol.strip().upper() if isinstance(symbol, str) else ""
    is_crypto = normalized in CRYPTO_PAPER_PAIRS
    is_forex = (not is_crypto) and isinstance(symbol, str) and _FOREX_PAIR.fullmatch(normalized) is not None
    feed = getattr(limits, "feed", DEFAULT_FEED)
    if is_crypto:
        if feed != CRYPTO_PAPER_FEED:
            return False, "non_paper_crypto_feed_rejected"
    elif is_forex:
        if feed != FOREX_PRACTICE_FEED:
            return False, "non_practice_forex_feed_rejected"
    elif feed != DEFAULT_FEED:
        return False, "non_iex_feed_rejected"
    if not isinstance(state.kill_switch, bool) or state.kill_switch:
        return False, "kill_switch_or_invalid_state"
    if (
        not isinstance(intent.symbol, str)
        or not intent.symbol.strip()
        or not isinstance(intent.client_order_id, str)
        or not intent.client_order_id.strip()
    ):
        return False, "invalid_or_duplicate_order"
    if (
        not isinstance(state.seen_order_ids, (frozenset, set))
        or intent.client_order_id in state.seen_order_ids
    ):
        return False, "invalid_or_duplicate_order"
    if not all(
        _finite(v)
        for v in (
            intent.quantity,
            intent.price,
            state.exposure,
            state.realized_daily_pnl,
            limits.max_order_notional,
            limits.max_total_exposure,
            limits.max_daily_loss,
        )
    ):
        return False, "invalid_numeric_state"
    if intent.quantity <= 0 or intent.price <= 0:
        return False, "invalid_quantity_or_price"
    if (
        state.exposure < 0
        or limits.max_order_notional <= 0
        or limits.max_total_exposure <= 0
        or limits.max_daily_loss <= 0
    ):
        return False, "invalid_numeric_state"
    if (
        type(state.open_positions) is not int
        or state.open_positions < 0
        or type(limits.max_open_positions) is not int
        or limits.max_open_positions <= 0
    ):
        return False, "invalid_position_state"
    if (
        type(state.feed_age_ms) is not int
        or type(limits.max_feed_age_ms) is not int
        or limits.max_feed_age_ms < 0
        or state.feed_age_ms < 0
        or state.feed_age_ms > limits.max_feed_age_ms
    ):
        return False, "stale_or_invalid_feed"
    if state.realized_daily_pnl <= -limits.max_daily_loss:
        return False, "daily_loss_limit"
    if state.open_positions >= limits.max_open_positions:
        return False, "position_limit"
    try:
        notional = intent.quantity * intent.price
        projected = state.exposure + notional
    except (InvalidOperation, OverflowError):
        return False, "invalid_numeric_state"
    if not notional.is_finite() or not projected.is_finite():
        return False, "invalid_numeric_state"
    if notional > limits.max_order_notional or projected > limits.max_total_exposure:
        return False, "exposure_limit"
    return True, "approved_for_paper_only"
