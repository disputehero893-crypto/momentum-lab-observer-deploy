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


def record_cycle(worker, result):
    logger = getattr(worker, "paper_run_logger", None)
    if logger is None or not getattr(logger, "enabled", False) or not getattr(logger, "run_id", None):
        return
    try:
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
        for blocker in result.get("blockers") or []:
            reason = _map_blocker(blocker)
            logger.log_blocker(
                reason,
                detail=blocker,
                event_key="blocker:" + logger.run_key + ":" + cycle_id + ":" + str(blocker)[:80],
                occurred_at=result.get("completed_at"),
            )
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
