"""OANDA practice-only live-price order placement and performance summary.

All requests target https://api-fxpractice.oanda.com exclusively via
OandaPracticeClient. Order placement is OFF by default and requires:
- MOMENTUM_MODE=paper
- OANDA_ENVIRONMENT=practice
- OANDA_PRACTICE_ORDERS_ENABLED=true
- PAPER_TRADING_ENABLED=true and matching PAPER_REVIEWED_CONFIG_HASH
- #8 check_paper_intent(feed="oanda_practice")
- check_forex_leverage_intent (margin-aware caps + mandatory stop loss)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .activities import timestamp_parts
from .forex_observation import build_forex_watch
from .forex_risk import ForexRiskConfig, ForexRiskState, compute_usd_metrics
from .oanda import (
    APPROVED_FOREX_PAIRS, OandaPracticeClient, OandaPracticeDisabled,
    OandaPracticeError, display_pair, instrument_code,
)
from .paper_risk import FOREX_PRACTICE_FEED, RiskLimits, RiskState


def _dec(value, *, default=None):
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError("Missing decimal value")
    out = value if isinstance(value, Decimal) else Decimal(str(value))
    if not out.is_finite():
        raise ValueError("Non-finite decimal value")
    return out


def _quantize_price(value: Decimal, pair: str) -> Decimal:
    step = Decimal("0.001") if pair.endswith("/JPY") else Decimal("0.00001")
    return value.quantize(step, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class LivePracticeQuote:
    pair: str
    instrument: str
    bid: Decimal
    ask: Decimal
    mid: Decimal
    spread_bps: Decimal
    source_at: str
    received_at: str
    age_ms: int


class ForexPracticeTrader:
    """Place practice trades from live practice quotes and summarize performance."""

    def __init__(
        self,
        config,
        *,
        client: OandaPracticeClient | None = None,
        risk_config: ForexRiskConfig | None = None,
        paper_limits: RiskLimits | None = None,
        clock=None,
    ):
        self.config = config
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.client = client or OandaPracticeClient(config, clock=self.clock)
        overrides = getattr(config, "forex_risk_overrides", None) or {}
        self.risk_config = risk_config or ForexRiskConfig(**overrides)
        self.paper_limits = paper_limits or RiskLimits(
            max_order_notional=self.risk_config.max_order_notional_usd,
            max_total_exposure=self.risk_config.max_total_open_exposure_usd,
            max_daily_loss=self.risk_config.max_daily_loss_usd,
            max_open_positions=self.risk_config.max_open_positions,
            max_feed_age_ms=self.risk_config.max_feed_age_ms,
            feed=FOREX_PRACTICE_FEED,
        )

    def fetch_live_quote(self, pair: str) -> LivePracticeQuote:
        normalized = display_pair(pair)
        if normalized not in APPROVED_FOREX_PAIRS:
            raise OandaPracticeError("forex_pair_not_approved")
        configured_watchlist = getattr(self.config, "forex_watchlist", ()) or ()
        if configured_watchlist and normalized not in configured_watchlist:
            raise OandaPracticeError("forex_pair_not_in_watchlist")

        raw = self.client.pricing((normalized,))
        now = self.clock()
        _, now_ns = timestamp_parts(now)
        watch = build_forex_watch(
            raw,
            (normalized,),
            received_at=now,
            collected_at=now,
            max_age_seconds=max(1, (self.risk_config.max_feed_age_ms + 999) // 1000),
        )
        row = watch["rows"][0]
        if row["status"] != "fresh" or row["bid"] is None or row["ask"] is None:
            code = row.get("error_code") or "quote_unavailable"
            raise OandaPracticeError(f"forex_live_quote_unusable:{code}")

        _, source_ns = timestamp_parts(row["source_at"])
        age_ms = max(0, (now_ns - source_ns) // 1_000_000)
        if age_ms > self.risk_config.max_feed_age_ms:
            raise OandaPracticeError("forex_live_quote_unusable:quote_stale")

        bid = Decimal(row["bid"])
        ask = Decimal(row["ask"])
        mid = (bid + ask) / Decimal("2")
        if mid <= 0:
            raise OandaPracticeError("forex_live_quote_unusable:quote_invalid")
        spread_bps = ((ask - bid) / mid) * Decimal("10000")
        return LivePracticeQuote(
            pair=normalized,
            instrument=instrument_code(normalized),
            bid=bid,
            ask=ask,
            mid=mid,
            spread_bps=spread_bps,
            source_at=row["source_at"],
            received_at=row["received_at"],
            age_ms=age_ms,
        )

    def _account_snapshot(self):
        summary_resp = self.client.summary()
        acct = summary_resp.get("account") if isinstance(summary_resp, dict) else None
        if not isinstance(acct, dict):
            raise OandaPracticeError("forex_account_summary_invalid")
        currency = str(acct.get("currency") or "").upper()
        if currency != "USD":
            raise OandaPracticeError("forex_account_currency_must_be_usd")

        nav = _dec(acct.get("NAV"))
        balance = _dec(acct.get("balance"))
        margin_used = _dec(acct.get("marginUsed"), default=Decimal("0"))
        margin_available = _dec(acct.get("marginAvailable"), default=Decimal("0"))
        unrealized_pl = _dec(acct.get("unrealizedPL"), default=Decimal("0"))
        realized_pl = _dec(acct.get("pl"), default=Decimal("0"))

        trades_resp = self.client.open_trades()
        raw_trades = trades_resp.get("trades") if isinstance(trades_resp, dict) else None
        if not isinstance(raw_trades, list):
            raise OandaPracticeError("forex_open_trades_invalid")

        pair_exposures = {}
        total_exposure = Decimal("0")
        for trade in raw_trades:
            if not isinstance(trade, dict):
                raise OandaPracticeError("forex_open_trades_invalid")
            t_pair = display_pair(str(trade.get("instrument") or ""))
            t_units = abs(_dec(trade.get("currentUnits") or trade.get("initialUnits")))
            t_price = _dec(trade.get("price"))
            base, quote = t_pair.split("/")
            if quote == "USD":
                exp_usd = t_units * t_price
            elif base == "USD":
                exp_usd = t_units
            else:
                # Fail closed on unmanaged cross-pair exposure without margin proxy.
                margin_req = _dec(trade.get("marginUsed"), default=Decimal("0"))
                exp_usd = margin_req / self.risk_config.default_pair_margin_rate if margin_req > 0 else t_units * t_price
            pair_exposures[t_pair] = pair_exposures.get(t_pair, Decimal("0")) + exp_usd
            total_exposure += exp_usd

        effective_leverage = (total_exposure / nav) if nav > 0 else Decimal("0")
        return {
            "nav": nav,
            "balance": balance,
            "margin_used": margin_used,
            "margin_available": margin_available,
            "unrealized_pl": unrealized_pl,
            "realized_pl": realized_pl,
            "open_trades": raw_trades,
            "open_trade_count": len(raw_trades),
            "pair_exposures_usd": pair_exposures,
            "total_exposure_usd": total_exposure,
            "effective_leverage": effective_leverage,
        }

    def place_order_from_live_price(
        self,
        pair: str,
        *,
        side: str,
        units: Decimal | int | str,
        client_order_id: str,
        stop_loss_price: Decimal | str | None = None,
        stop_loss_distance: Decimal | str | None = None,
        take_profit_price: Decimal | str | None = None,
        order_type: str = "MARKET",
        max_slippage_bps: Decimal = Decimal("10"),
        realized_daily_pnl_usd: Decimal = Decimal("0"),
        kill_switch: bool = False,
        seen_order_ids: frozenset[str] = frozenset(),
        base_to_usd_rate: Decimal | None = None,
        quote_to_usd_rate: Decimal | None = None,
        pair_margin_rate: Decimal | None = None,
    ) -> dict:
        """Read live practice pricing + account state, screen risk, and submit."""
        # Enforce full execution gate before fetching or placing anything.
        self.client._execution_ready()

        side_norm = str(side).strip().lower()
        if side_norm not in {"buy", "sell"}:
            raise OandaPracticeError("Order side must be 'buy' or 'sell'")
        raw_units = _dec(units)
        if raw_units <= 0:
            raise OandaPracticeError("Order units must be positive")
        signed_units = raw_units if side_norm == "buy" else -raw_units

        quote = self.fetch_live_quote(pair)
        entry_price = quote.ask if side_norm == "buy" else quote.bid

        if stop_loss_price is not None:
            sl_price = _dec(stop_loss_price)
        elif stop_loss_distance is not None:
            dist = _dec(stop_loss_distance)
            raw_sl = (entry_price - dist) if side_norm == "buy" else (entry_price + dist)
            sl_price = _quantize_price(raw_sl, quote.pair)
        else:
            raise OandaPracticeError("forex_leverage_rejected:stop_loss_required")

        snap = self._account_snapshot()

        slip_bps = _dec(max_slippage_bps, default=Decimal("10"))
        if slip_bps <= 0 or slip_bps > Decimal("50"):
            raise OandaPracticeError("max_slippage_bps must be in (0, 50]")
        slip_factor = slip_bps / Decimal("10000")
        bound = (
            _quantize_price(entry_price * (Decimal("1") + slip_factor), quote.pair)
            if side_norm == "buy"
            else _quantize_price(entry_price * (Decimal("1") - slip_factor), quote.pair)
        )

        forex_state = ForexRiskState(
            nav_usd=snap["nav"],
            margin_used_usd=snap["margin_used"],
            margin_available_usd=snap["margin_available"],
            total_open_exposure_usd=snap["total_exposure_usd"],
            realized_daily_pnl_usd=_dec(realized_daily_pnl_usd, default=Decimal("0")),
            open_positions=snap["open_trade_count"],
            feed_age_ms=quote.age_ms,
            kill_switch=kill_switch,
            seen_order_ids=seen_order_ids,
            pair_exposures_usd=snap["pair_exposures_usd"],
            spread_bps=quote.spread_bps,
        )
        risk_state = RiskState(
            exposure=snap["total_exposure_usd"],
            realized_daily_pnl=_dec(realized_daily_pnl_usd, default=Decimal("0")),
            open_positions=snap["open_trade_count"],
            feed_age_ms=quote.age_ms,
            kill_switch=kill_switch,
            seen_order_ids=seen_order_ids,
        )

        intent = {
            "instrument": quote.instrument,
            "units": format(signed_units, "f"),
            "type": order_type,
            "price": format(entry_price, "f"),
            "stop_loss_price": format(sl_price, "f"),
            "client_order_id": client_order_id,
            "price_bound": format(bound, "f") if order_type.upper() == "MARKET" else None,
            "take_profit_price": format(_dec(take_profit_price), "f") if take_profit_price is not None else None,
            "base_to_usd_rate": base_to_usd_rate,
            "quote_to_usd_rate": quote_to_usd_rate,
            "pair_margin_rate": pair_margin_rate,
        }
        response = self.client.submit_order(
            intent,
            risk_state=risk_state,
            limits=self.paper_limits,
            forex_state=forex_state,
            forex_config=self.risk_config,
        )
        usd_per_unit, notional_usd, stop_risk_usd = compute_usd_metrics(
            quote.pair,
            raw_units,
            entry_price,
            sl_price,
            base_to_usd_rate=base_to_usd_rate,
            quote_to_usd_rate=quote_to_usd_rate,
        )
        return {
            "environment": "practice",
            "host": "api-fxpractice.oanda.com",
            "pair": quote.pair,
            "side": side_norm,
            "units": format(signed_units, "f"),
            "quoted_entry_price": format(entry_price, "f"),
            "stop_loss_price": format(sl_price, "f"),
            "notional_usd": format(notional_usd, "f"),
            "max_stop_risk_usd": format(stop_risk_usd, "f"),
            "usd_per_unit": format(usd_per_unit, "f"),
            "quote_source_at": quote.source_at,
            "broker_response": response,
        }

    def performance_report(self, *, closed_count: int = 50) -> dict:
        """Read-only practice account & trade performance summary (no secrets)."""
        snap = self._account_snapshot()
        closed_resp = self.client.trades(state="CLOSED", count=closed_count)
        closed_trades = closed_resp.get("trades") if isinstance(closed_resp, dict) else []
        if not isinstance(closed_trades, list):
            closed_trades = []
        wins = 0
        losses = 0
        closed_realized = Decimal("0")
        for trade in closed_trades:
            if not isinstance(trade, dict):
                continue
            pnl = _dec(trade.get("realizedPL"), default=Decimal("0"))
            closed_realized += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
        return {
            "environment": "practice",
            "host": "api-fxpractice.oanda.com",
            "currency": "USD",
            "nav": format(snap["nav"], "f"),
            "balance": format(snap["balance"], "f"),
            "unrealized_pl": format(snap["unrealized_pl"], "f"),
            "account_realized_pl": format(snap["realized_pl"], "f"),
            "recent_closed_realized_pl": format(closed_realized, "f"),
            "margin_used": format(snap["margin_used"], "f"),
            "margin_available": format(snap["margin_available"], "f"),
            "total_open_exposure_usd": format(snap["total_exposure_usd"], "f"),
            "effective_leverage": format(snap["effective_leverage"], "f"),
            "open_trade_count": snap["open_trade_count"],
            "closed_trades_sampled": len(closed_trades),
            "closed_wins": wins,
            "closed_losses": losses,
        }
