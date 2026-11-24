"""Deterministic AI-only entry intents for cotrade mode (paper only).

The AI contributes only: which watchlist symbol, that the call is "buy", and
the pattern label, confidence and reason for the log. Everything that matters
for an order is computed here from market data with the rule strategy's own
gates and parameters, and RiskManager then sets quantity exactly as it does for
rule candidates. Sell calls never become orders (the engine is long-only;
exits stay with the rule strategy's stop/target/time logic).
"""
from __future__ import annotations

from datetime import time
from decimal import Decimal
from hashlib import sha256
from typing import Any, Mapping, Sequence

from .models import NY, Bar, InputError, plain, positive_decimal, session_date, timestamp
from .strategy import StrategyConfig, _market

STOP_LOOKBACK_BARS = 5
MAX_PRICE_DRIFT = Decimal("0.005")   # quote may not have moved >0.5% since the AI read


def build_ai_intent(snapshot: Mapping[str, Any], cfg: StrategyConfig, signal: Any,
                    watchlist: Sequence[str], *, min_reward_r: Decimal = Decimal("2")) -> tuple[dict | None, str]:
    """Return (intent, "ok") or (None, reason). Never raises for bad input."""
    try:
        if signal is None or getattr(signal, "status", None) != "ok" or signal.call != "buy":
            return None, "ai_signal_not_buy"
        spec = cfg.preset_spec
        if spec.gated and not cfg.enable_gated_presets:
            return None, "gated_preset_disabled"
        now, symbol, feed, quote = _market(snapshot, cfg)
        if symbol != signal.symbol:
            return None, "ai_symbol_mismatch"
        if symbol not in tuple(watchlist):
            return None, "ai_symbol_outside_watchlist"
        if cfg.allowed_symbols and symbol not in cfg.allowed_symbols:
            return None, "symbol_not_in_preset_universe"
        if spec.required_feed == "iex" and feed != "iex":
            return None, "liquid_preset_requires_iex"
        if spec.required_feed == "sip" and feed != "sip":
            return None, "small_cap_requires_sip_feed"
        local = now.astimezone(NY)
        if not time.fromisoformat(cfg.trade_start_et) <= local.time() < time.fromisoformat(cfg.trade_end_et):
            return None, "outside_entry_window"
        if snapshot.get("mode", "paper") != "paper":
            return None, "live_mode_forbidden"
        session = snapshot.get("session", {})
        if not isinstance(session, Mapping) or session.get("is_trading_day") is not True:
            return None, "session_eligibility_unconfirmed"
        if session_date(session.get("date")) != local.date() or local.weekday() >= 5:
            return None, "session_ineligible"
        spread = quote.ask - quote.bid
        if spread > cfg.max_spread or spread / quote.bid * 10000 > cfg.max_spread_bps:
            return None, "spread_too_wide"
        entry = quote.ask
        if not cfg.min_price <= entry <= cfg.max_price:
            return None, "price_outside_preset_range"
        if signal.price is not None:
            read_price = positive_decimal(signal.price, "signal.price")
            if abs(entry - read_price) / read_price > MAX_PRICE_DRIFT:
                return None, "price_moved_since_ai_read"
        bars = [Bar.from_dict(b, feed) for b in snapshot.get("bars", ())]
        bars = [b for b in bars if b.end <= now]
        if len(bars) < STOP_LOOKBACK_BARS:
            return None, "not_enough_completed_bars"
        recent = bars[-STOP_LOOKBACK_BARS:]
        stop = min(b.low for b in recent) - cfg.tick_size
        if stop <= 0 or stop >= entry:
            return None, "invalid_stop_distance"
        reward_r = max(Decimal(str(cfg.target_r)), Decimal(str(min_reward_r)))
        target = entry + reward_r * (entry - stop)
        identity = f"ai|{cfg.profile}|{cfg.config_id}|{symbol}|{feed}|{recent[-1].start.isoformat()}"
        setup_id = "ai" + sha256(identity.encode()).hexdigest()[:22]
        intent = plain({"symbol": symbol, "side": "buy", "mode": "paper", "profile": cfg.profile,
                        "config_id": cfg.config_id, "entry_limit": entry, "limit_price": entry,
                        "stop_price": stop, "target_price": target, "setup_id": setup_id,
                        "observed_at": timestamp(now), "feed": feed, "liquidity_volume": recent[-1].volume,
                        "reason": "ai_chart_" + str(signal.pattern or "other")})
        return intent, "ok"
    except (InputError, ValueError, TypeError, KeyError, ArithmeticError, AttributeError) as error:
        return None, "ai_intent_invalid_input_" + type(error).__name__
