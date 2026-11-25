"""Conservative spot-crypto momentum decisions. No network and no orders.

The equity strategy is an intraday bull flag: New York session, a $2–$20
price band, float, and a news catalyst. Crypto quotes are only a bid and an
ask, and the market does not close. This rule keeps the same idea in a
smaller form: buy a short rise, one long position, and sell if that rise is
given back. It never shorts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

RULE_NAME = "crypto_spot_momentum_v1"
# Four fresh mids. The newest must be at least this far above the oldest.
HISTORY_POINTS = 4
MIN_RISE = Decimal("0.005")
# One percent under the entry. Sizes the buy and is the exit line.
STOP_FRACTION = Decimal("0.01")
QTY_STEP = Decimal("0.000000001")
# Same open-position cap as the shared paper risk screen.
MAX_OPEN_POSITIONS = 3


def _decimal(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("nonfinite")
    return number


def _aware(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("naive timestamp")
    return parsed.astimezone(timezone.utc)


def quote_from_row(row, *, now):
    """A fresh crypto-watch row, or None when it cannot be traded."""
    if not isinstance(row, dict) or row.get("status") != "fresh":
        return None
    try:
        bid = _decimal(row.get("bid"))
        ask = _decimal(row.get("ask"))
        source_at = _aware(row.get("source_at"))
        received_at = _aware(row.get("received_at"))
        moment = _aware(now)
    except (ValueError, TypeError, ArithmeticError):
        return None
    if bid <= 0 or ask <= 0 or bid > ask:
        return None
    if source_at > received_at or received_at > moment or source_at > moment:
        return None
    mid = (bid + ask) / Decimal("2")
    age_ms = int((moment - source_at).total_seconds() * 1000)
    if age_ms < 0:
        return None
    return {
        "pair": row.get("pair"),
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "source_at": source_at,
        "age_ms": age_ms,
    }


def _qty(ask, *, max_notional, max_shares, room):
    """Largest fractional qty that stays inside the notional and exposure room."""
    if ask <= 0 or room <= 0 or max_notional <= 0:
        return Decimal("0")
    cap = min(max_notional / ask, room / ask, Decimal(max_shares))
    qty = cap.quantize(QTY_STEP, rounding=ROUND_DOWN)
    if qty <= 0 or qty * ask > max_notional or qty * ask > room:
        return Decimal("0")
    return qty


def decide_crypto(quote, history, position, *, risk_cfg, exposure, open_positions, realized_pnl):
    """Return a buy, a sell, or a hold. `history` is older mids, not including `quote`."""
    pair = quote["pair"]
    held = position if position and position.get("qty", Decimal("0")) > 0 else None
    points = list(history) + [quote["mid"]]
    rise_anchor = points[-HISTORY_POINTS] if len(points) >= HISTORY_POINTS else None
    trend_up = (
        rise_anchor is not None
        and all(points[index] <= points[index + 1] for index in range(len(points) - HISTORY_POINTS, len(points) - 1))
        and rise_anchor > 0
        and (quote["mid"] - rise_anchor) / rise_anchor >= MIN_RISE
    )
    age_limit_ms = int(risk_cfg.max_quote_age_seconds * 1000)
    spread_bps = (quote["ask"] - quote["bid"]) / quote["mid"] * Decimal("10000")

    if held is not None:
        stop_line = held["entry"] * (Decimal("1") - STOP_FRACTION)
        if quote["mid"] <= stop_line:
            return {
                "rule": RULE_NAME, "pair": pair, "action": "sell", "reason": "stop_hit",
                "order": {
                    "symbol": pair, "side": "sell", "type": "market", "time_in_force": "ioc",
                    "qty": format(held["qty"], "f"), "price": format(quote["bid"], "f"),
                    "protective": True,
                },
            }
        return {"rule": RULE_NAME, "pair": pair, "action": "hold", "reason": "position_open", "order": None}

    if quote["age_ms"] > age_limit_ms:
        return {"rule": RULE_NAME, "pair": pair, "action": "hold", "reason": "stale_quote", "order": None}
    if spread_bps > risk_cfg.max_spread_bps:
        return {"rule": RULE_NAME, "pair": pair, "action": "hold", "reason": "spread_too_wide", "order": None}
    if realized_pnl <= -risk_cfg.max_daily_loss:
        return {"rule": RULE_NAME, "pair": pair, "action": "hold", "reason": "daily_loss_limit", "order": None}
    if open_positions >= MAX_OPEN_POSITIONS:
        return {"rule": RULE_NAME, "pair": pair, "action": "hold", "reason": "position_limit", "order": None}
    if not trend_up:
        reason = "momentum_history_short" if len(points) < HISTORY_POINTS else "momentum_not_rising"
        return {"rule": RULE_NAME, "pair": pair, "action": "hold", "reason": reason, "order": None}

    room = min(risk_cfg.max_notional, Decimal("1000")) - exposure
    risk_unit = quote["ask"] * STOP_FRACTION
    risk_qty = (risk_cfg.per_trade_risk / risk_unit).quantize(QTY_STEP, rounding=ROUND_DOWN)
    notional_qty = _qty(
        quote["ask"], max_notional=risk_cfg.max_notional,
        max_shares=risk_cfg.max_shares, room=room,
    )
    qty = min(risk_qty, notional_qty)
    if qty <= 0 or qty * risk_unit > risk_cfg.per_trade_risk:
        return {"rule": RULE_NAME, "pair": pair, "action": "hold", "reason": "quantity_below_risk_step", "order": None}
    stop = quote["ask"] * (Decimal("1") - STOP_FRACTION)
    return {
        "rule": RULE_NAME, "pair": pair, "action": "buy", "reason": "momentum_rise",
        "order": {
            "symbol": pair, "side": "buy", "type": "limit", "time_in_force": "gtc",
            "qty": format(qty, "f"), "limit_price": format(quote["ask"], "f"),
            "stop_price": format(stop, "f"), "protective": False,
            "anchor": format(rise_anchor, "f"),
        },
    }
