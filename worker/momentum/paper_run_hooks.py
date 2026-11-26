"""Best-effort paper_runs / paper_events hooks for ObservationWorker.

Failures never raise into the observation cycle. This module never places orders.
"""
from __future__ import annotations

import logging

from .models import NY
from .paper_run_logging import DisabledPaperRunLogger, PaperRunLoggingError, create_paper_run_logger

LOG = logging.getLogger("momentum.paper_run_hooks")


def attach_logger(worker):
    """Attach a PaperRunLogger (or disabled no-op) onto an ObservationWorker."""
    try:
        worker.paper_run_logger = create_paper_run_logger(worker.config, clock=worker.clock)
    except PaperRunLoggingError as exc:
        LOG.warning("paper run logger not attached: %s", exc.code)
        worker.paper_run_logger = DisabledPaperRunLogger(reason=exc.code)


def start_session(worker):
    logger = getattr(worker, "paper_run_logger", None)
    if logger is None:
        return None
    try:
        day = worker.clock().astimezone(NY).date().isoformat()
        strategy = getattr(getattr(worker, "session", None), "strategy", None)
        strategy_version = getattr(strategy, "preset", None) or getattr(strategy, "profile", None) or "unknown"
        return logger.start_run(
            run_key="paper-" + day + ":" + worker.instance_id,
            strategy_version=str(strategy_version),
            watchlist=tuple(worker.config.watchlist),
            config_hash=worker.config.review_hash() if hasattr(worker.config, "review_hash") else None,
            summary={"instance_id": worker.instance_id, "runtime": "observation_only"},
        )
    except Exception as exc:
        LOG.warning("paper run start_run failed: %s", type(exc).__name__)
        return None


def _feed_state(worker, logger):
    """Per-run dedupe memory so unchanged skips/blockers are not re-written every cycle."""
    state = getattr(worker, "_paper_feed_state", None)
    if not isinstance(state, dict) or state.get("run_key") != logger.run_key:
        state = {"run_key": logger.run_key, "blockers": None, "skip": {}}
        worker._paper_feed_state = state
    return state


def _window_payload(decision):
    window = decision.get("window") if isinstance(decision.get("window"), list) else []
    window = window[-8:]
    payload = {
        "window": window,
        "window_start": window[0].get("t") if window and isinstance(window[0], dict) else None,
        "window_end": window[-1].get("t") if window and isinstance(window[-1], dict) else None,
    }
    for key in ("bid", "ask", "mid", "quote_at"):
        if decision.get(key) is not None:
            payload[key] = decision.get(key)
    return payload


def record_crypto_paper(worker, logger, result):
    """Write crypto paper signal/entry/exit/stop/skip events. Raises on write failure."""
    crypto = result.get("crypto_paper")
    if not isinstance(crypto, dict):
        return
    state = _feed_state(worker, logger)
    run_key = logger.run_key
    cycle_id = str(result.get("cycle_id") or "unknown")
    at = result.get("completed_at")
    skipped = crypto.get("skipped")
    if skipped:
        if state["skip"].get("*") != skipped:
            logger.log_trade_event("skip", payload={
                "symbol": None, "asset_class": "crypto", "reason": str(skipped),
                "blockers": [str(b) for b in (crypto.get("blockers") or [])][:20],
            }, event_key="skip:" + run_key + ":*:" + cycle_id, occurred_at=at)
            state["skip"]["*"] = skipped
        return
    state["skip"].pop("*", None)
    for index, decision in enumerate(crypto.get("decisions") or []):
        if not isinstance(decision, dict):
            continue
        pair = decision.get("pair")
        action = decision.get("action")
        reason = str(decision.get("reason") or "unknown")
        base = {"symbol": pair, "asset_class": "crypto", "rule": decision.get("rule"),
                "reason": reason, **_window_payload(decision)}
        if action in {"buy", "sell"}:
            order_id = str(decision.get("client_order_id") or (cycle_id + ":" + str(index)))
            signal = dict(base, side=action, pattern=reason, price=decision.get("price"))
            logger.log_trade_event("signal", payload=signal,
                                   event_key="signal:" + run_key + ":" + order_id, occurred_at=at)
            kind = "entry" if action == "buy" else ("stop" if reason == "stop_hit" else "exit")
            trade = dict(base, side=action, pattern=reason)
            for key in ("qty", "price", "stop_price", "entry_price", "pnl", "client_order_id"):
                if decision.get(key) is not None:
                    trade[key] = decision.get(key)
            logger.log_trade_event(kind, payload=trade,
                                   event_key=kind + ":" + run_key + ":" + order_id, occurred_at=at)
            state["skip"].pop(pair, None)
        elif decision.get("intent") in {"buy", "sell"}:
            # The rule fired but the paper order was refused before or at submit.
            pattern = str(decision.get("signal_reason") or "unknown")
            key = run_key + ":" + str(pair) + ":" + cycle_id
            logger.log_trade_event("signal", payload=dict(base, side=decision["intent"], pattern=pattern),
                                   event_key="signal:" + key, occurred_at=at)
            logger.log_trade_event("skip", payload=dict(base, side=decision["intent"], pattern=pattern,
                                                        order_refused=True),
                                   event_key="skip:" + key, occurred_at=at)
            state["skip"][pair] = reason
        else:
            if state["skip"].get(pair) == reason:
                continue
            logger.log_trade_event("skip", payload=base,
                                   event_key="skip:" + run_key + ":" + str(pair) + ":" + cycle_id, occurred_at=at)
            state["skip"][pair] = reason


def record_cycle(worker, result):
    logger = getattr(worker, "paper_run_logger", None)
    if logger is None or not getattr(logger, "enabled", False) or not getattr(logger, "run_id", None):
        return
    try:
        # Trade events first: they matter most. Any write failure stops the rest
        # of this cycle's writes so a slow Supabase cannot stall the loop further.
        record_crypto_paper(worker, logger, result)
    except Exception as exc:
        LOG.warning("paper run trade-event logging failed: %s", type(exc).__name__)
        return
    try:
        state = _feed_state(worker, logger)
        cycle_id = result.get("cycle_id") or "unknown"
        status = "error" if result.get("error_code") else ("halted" if result.get("halted") else "observing")
        logger.log_heartbeat(
            status=status,
            extra={
                "cycle_id": cycle_id,
                "cloud_state_published": result.get("cloud_state_published"),
                "broker_verified": (result.get("broker") or {}).get("verified"),
            },
            event_key="heartbeat:" + logger.run_key + ":" + cycle_id,
            occurred_at=result.get("completed_at"),
        )
        blockers = sorted(str(b) for b in (result.get("blockers") or []))
        # Only write blockers when the set changes; unchanged sets were the
        # bulk of paper_events growth (about 6 rows per cycle).
        logged_blockers = blockers if blockers != state.get("blockers") else []
        for blocker in logged_blockers:
            reason = _map_blocker(blocker)
            logger.log_blocker(
                reason,
                detail=blocker,
                event_key="blocker:" + logger.run_key + ":" + cycle_id + ":" + str(blocker)[:80],
                occurred_at=result.get("completed_at"),
            )
        state["blockers"] = blockers
        session = result.get("session") or {}
        for index, decision in enumerate(session.get("decisions") or []):
            symbol = decision.get("symbol")
            strategy = decision.get("strategy") or {}
            logger.log_signal_evaluated(
                symbol=symbol,
                accepted=bool(strategy.get("accepted")),
                reasons=tuple(strategy.get("reasons") or ()),
                metrics={"risk_accepted": (decision.get("risk") or {}).get("accepted")},
                event_key="signal:" + logger.run_key + ":" + cycle_id + ":" + str(symbol) + ":" + str(index),
                occurred_at=result.get("completed_at"),
            )
        for action in session.get("actions") or []:
            if action.get("kind") in {"paper_entry_intent", "paper_exit_intent"} and action.get("submitted"):
                intent = action.get("intent") or {}
                logger.log_paper_order(
                    client_order_id=str(intent.get("setup_id") or action.get("kind")),
                    side=intent.get("side") or ("buy" if "entry" in action.get("kind", "") else "sell"),
                    symbol=intent.get("symbol"),
                    qty=intent.get("qty"),
                    status="submitted",
                    event_key="paper-order:" + logger.run_key + ":" + cycle_id + ":" + str(intent.get("setup_id")),
                    occurred_at=result.get("completed_at"),
                )
    except Exception as exc:
        LOG.warning("paper run cycle logging failed: %s", type(exc).__name__)


def close_session(worker, *, status="stopped"):
    logger = getattr(worker, "paper_run_logger", None)
    if logger is None:
        return
    try:
        if getattr(logger, "run_id", None):
            logger.close_run(status=status, summary={"instance_id": worker.instance_id})
    except Exception as exc:
        LOG.warning("paper run close_run failed: %s", type(exc).__name__)


def _map_blocker(code):
    text = str(code)
    mapping = {
        "watchlist_outside_strategy_universe": "watchlist_outside_strategy_universe",
        "gated_preset_disabled": "gated_preset_disabled",
        "paper_execution_not_enabled": "paper_trading_disabled",
        "paper_trading": "paper_trading_disabled",
        "live_mode": "live_mode_rejected",
        "cloud_worker_not_configured": "supabase_unavailable",
        "cloud_state_publication_failed": "supabase_unavailable",
    }
    for key, value in mapping.items():
        if key in text:
            return value
    if "universe" in text or "watchlist_outside" in text:
        return "off_universe_symbol"
    return "other"
