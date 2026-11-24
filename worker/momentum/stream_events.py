"""Fail-closed Alpaca market stream message decoder (paper research only, IEX stocks).

This module does not open sockets, use credentials, or submit orders.
Callers must verify entitlement, source freshness and session eligibility.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
import json

@dataclass(frozen=True)
class MarketEvent:
    market: str
    symbol: str
    kind: str
    source_at: datetime
    price: Decimal | None
    bid: Decimal | None
    ask: Decimal | None

def _number(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid price")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid price") from None
    if not number.is_finite() or number <= 0:
        raise ValueError("invalid price")
    return number

def decode_message(raw: str | bytes, market: str) -> list[MarketEvent]:
    """Decode trade/quote frames; ignore subscription/control frames.

    Returns normalized observations only. Unknown/malformed price frames fail
    closed rather than silently providing a trading signal.
    """
    if market not in ("stocks", "crypto"):
        raise ValueError("unsupported market")
    payload = json.loads(raw)
    if not isinstance(payload, list) or len(payload) > 1000:
        raise ValueError("invalid frame")
    result = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("invalid message")
        kind = item.get("T")
        if kind not in ("t", "q"):
            continue
        symbol = item.get("S")
        timestamp = item.get("t")
        if not isinstance(symbol, str) or not (1 <= len(symbol) <= 32):
            raise ValueError("invalid symbol")
        if not isinstance(timestamp, str):
            raise ValueError("missing source time")
        try:
            source = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid source time") from None
        if source.tzinfo is None:
            raise ValueError("timezone required")
        source = source.astimezone(timezone.utc)
        price = _number(item.get("p")) if kind == "t" else None
        bid = _number(item.get("bp")) if kind == "q" else None
        ask = _number(item.get("ap")) if kind == "q" else None
        if kind == "t" and price is None:
            raise ValueError("trade price missing")
        if kind == "q" and (bid is None or ask is None or bid > ask):
            raise ValueError("invalid quote")
        result.append(MarketEvent(market, symbol, kind, source, price, bid, ask))
    return result
