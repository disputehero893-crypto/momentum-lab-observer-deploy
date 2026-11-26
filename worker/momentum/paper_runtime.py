"""Paper-stock gates shared by the one ``serve-paper`` process.

``serve-observe`` does not submit. ``serve-paper`` is the observation worker
with stock execution armed: one process, one journal, the crypto hook, the
resume path, and the OANDA watch. This module does not start a second loop
or the standalone crypto timer.

Orders are submitted only when every gate holds: paper mode, the explicit
paper-trading flag, Alpaca's paper trading URL, a confirmed paper account, a
clear kill switch, and a reviewed strategy/risk hash. Any miss keeps the cycle
at ``execute=False`` and records the reason for ``/health``. Live trading is
not a mode of this process.
"""
from datetime import datetime, timezone
import threading
import time
import uuid

from .alpaca import PaperClient, PaperDisabled, assert_paper_broker_url, fill_report
from .config import PAPER_API_HOST
from .data import SnapshotBuilder
from .journal import Journal
from .observation import ObservationError, _account_identity
from .session import PaperSession


class PaperRuntimeError(RuntimeError):
    pass


def kill_latched(journal):
    """True when the shared journal halt is on, or the latch cannot be read."""
    try:
        switch = journal.kill_switch()
    except Exception:
        return True
    if not isinstance(switch, dict):
        return True
    return bool(switch.get("enabled") or switch.get("flatten_requested"))


def ledger_ready(ledger):
    return isinstance(ledger, dict) and ledger.get("complete") is True and ledger.get("environment", "paper") == "paper"


def production_session_ledger(config, provider, clock):
    try:
        account = provider.account()
        if not paper_account_confirmed(account):
            return {"complete": False, "blockers": ["paper_account_check_failed"]}
        from .autopilot import ExecutionAccounting
        ledger = ExecutionAccounting(config, provider, clock).collect(account["id"])
        if isinstance(ledger, dict) and "environment" not in ledger:
            ledger = dict(ledger)
            ledger["environment"] = "paper"
        return ledger
    except Exception:
        return {"complete": False, "blockers": ["session_ledger_unavailable"]}


def evaluate_paper_gates(config, provider, journal, *, broker_url, broker_url_error):
    """Return ``(reason, active)``. Active is true only when every stock gate holds.

    A refused broker URL does not call the provider. Local configuration
    failures are decided before any account read. The kill latch is the same
    journal row the crypto loop reads.
    """
    if config.mode != "paper":
        return "mode_not_paper", False
    if not config.paper_enabled:
        return "paper_trading_not_enabled", False
    if broker_url_error:
        return broker_url_error, False
    try:
        assert_paper_broker_url(broker_url)
    except PaperDisabled as exc:
        return str(exc), False
    blockers = list(config.paper_blockers())
    earlier = [item for item in blockers if item != "exact_paper_configuration_not_reviewed"]
    if earlier:
        return earlier[0], False
    try:
        account = provider.account()
    except Exception:
        return "paper_account_check_failed", False
    if not paper_account_confirmed(account):
        return "paper_account_check_failed", False
    if kill_latched(journal):
        return "kill_switch_latched", False
    if "exact_paper_configuration_not_reviewed" in blockers:
        return "exact_paper_configuration_not_reviewed", False
    return "paper_gates_open", True


def paper_account_confirmed(account):
    """True only for an active, unblocked account that does not identify itself as live."""
    try:
        _account_identity(account)
    except ObservationError:
        return False
    if not isinstance(account, dict):
        return False
    if account.get("paper") is False:
        return False
    for key in ("environment", "account_type"):
        if key not in account or account.get(key) is None:
            continue
        if str(account.get(key)).lower() not in {"paper", "paper_trading"}:
            return False
    return True


class PaperTradingWorker:
    """One journal lease and one synchronous loop. The default broker client can submit.

    An injected provider is for tests. Production builds ``PaperClient``, whose
    trading origin is fixed to ``https://paper-api.alpaca.markets``.
    """

    def __init__(self, config, *, provider=None, journal=None, snapshot_builder=None,
                 clock=None, broker_url=None, instance_id=None):
        self.config = config
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.instance_id = str(uuid.UUID(instance_id)) if instance_id else str(uuid.uuid4())
        self.stop_event = threading.Event()
        self.broker_url_error = None
        try:
            self.broker_url = assert_paper_broker_url(PAPER_API_HOST if broker_url is None else broker_url)
        except PaperDisabled as exc:
            self.broker_url = None
            self.broker_url_error = str(exc)
        self.provider = provider if provider is not None else PaperClient(config, clock=self.clock)
        self.journal = journal or Journal(config.db_path, sample_seconds=config.journal_sample_seconds,
                                          min_free_bytes=config.journal_min_free_mb * 1024 * 1024)
        self.builder = snapshot_builder or SnapshotBuilder(self.provider, config)
        self.session = PaperSession(config, self.provider, journal=self.journal,
                                    snapshot_builder=self.builder, clock=self.clock,
                                    execution_guard=lambda: not self.stop_event.is_set())
        self._active = False
        self._cycle_lock = threading.Lock()
        self.paper_execution_active = False
        self.execution_reason = "awaiting_first_cycle"
        self.orders_submitted = 0
        self.latest = None

    def __enter__(self):
        if self._active or self.stop_event.is_set():
            raise PaperRuntimeError("paper_runtime_cannot_be_reentered")
        self.session.__enter__()
        try:
            self.journal.append("paper-runtime", "paper_runtime_started",
                                {"instance_id": self.instance_id, "mode": "paper",
                                 "live_execution_supported": False, "live_execution_locked": True,
                                 "broker_url_locked": self.broker_url == PAPER_API_HOST},
                                key="paper-runtime-start:" + self.instance_id,
                                timestamp=self.clock().isoformat())
            self._active = True
        except BaseException:
            self.session.__exit__()
            raise
        return self

    def __exit__(self, *args):
        self.stop_event.set()
        try:
            try:
                self.journal.append("paper-runtime", "paper_runtime_stopped",
                                    {"instance_id": self.instance_id, "orders_submitted": self._acknowledged_orders(),
                                     "live_execution_supported": False},
                                    key="paper-runtime-stop:" + self.instance_id,
                                    timestamp=self.clock().isoformat())
            except Exception:
                pass
        finally:
            self._active = False
            self.paper_execution_active = False
            if self.execution_reason == "paper_gates_open":
                self.execution_reason = "paper_runtime_stopped"
            self.session.__exit__(*args)

    def _acknowledged_orders(self):
        try:
            return len(self.journal.events(event_type="paper_order_acknowledged"))
        except Exception:
            return self.orders_submitted

    def _kill_latched(self):
        return kill_latched(self.journal)

    def evaluate(self):
        """Return ``(reason, active)``. Active is true only when every gate holds.

        A refused broker URL does not call the provider. Local configuration
        failures are decided before any account read.
        """
        return evaluate_paper_gates(
            self.config, self.provider, self.journal,
            broker_url=self.broker_url, broker_url_error=self.broker_url_error)

    def _ledger_ready(self, ledger):
        return ledger_ready(ledger)

    def cycle(self, *, snapshots=None, session_ledger=None):
        if not self._active or self.stop_event.is_set():
            raise PaperRuntimeError("paper_cycle_requires_active_lease")
        if not self._cycle_lock.acquire(blocking=False):
            raise PaperRuntimeError("paper_cycle_already_running")
        try:
            reason, active = self.evaluate()
            execute = active
            session = None
            if self.broker_url_error or reason in {"live_broker_url_refused", "broker_url_not_paper_endpoint"}:
                execute = False
                active = False
            elif active and session_ledger is None:
                session_ledger = self._production_ledger()
                if not self._ledger_ready(session_ledger):
                    reason, active, execute = "session_ledger_incomplete", False, False
            elif active and not self._ledger_ready(session_ledger):
                reason, active, execute = "session_ledger_incomplete", False, False
            if self.broker_url_error is None and reason not in {"live_broker_url_refused", "broker_url_not_paper_endpoint",
                                                                "paper_trading_not_enabled", "mode_not_paper"}:
                # Observation-only cycle: reads may run, submits may not.
                try:
                    session = self.session.cycle(snapshots=snapshots, session_ledger=session_ledger or {},
                                                 execute=bool(execute))
                except Exception:
                    session = {"actions": [], "errors": ["paper_cycle_failed"], "execute_requested": bool(execute)}
                if execute and session.get("execute_requested") is not True:
                    reason, active = "paper_cycle_failed", False
            submitted = self._acknowledged_orders()
            self.orders_submitted = submitted
            self.paper_execution_active = bool(active)
            self.execution_reason = reason
            fills = []
            for action in (session or {}).get("actions") or []:
                order = action.get("order") if isinstance(action, dict) else None
                if isinstance(order, dict):
                    fills.append(fill_report(order))
            record = {
                "schema_version": "1.0",
                "runtime": "serve_paper",
                "instance_id": self.instance_id,
                "paper_trading_enabled": bool(self.config.paper_enabled),
                "paper_execution_active": self.paper_execution_active,
                "execution_reason": self.execution_reason,
                "live_execution_supported": False,
                "live_execution_locked": True,
                "orders_submitted": self.orders_submitted,
                "execute_requested": bool(execute),
                "session_errors": list((session or {}).get("errors") or []),
                "submitted_actions": sum(1 for action in (session or {}).get("actions") or []
                                         if isinstance(action, dict) and action.get("submitted") is True),
                "fills": fills,
            }
            try:
                self.journal.append("paper-runtime", "paper_execution_cycle", record,
                                    key="paper-cycle:" + uuid.uuid4().hex,
                                    timestamp=self.clock().isoformat())
            except Exception:
                record["journal_error"] = "paper_cycle_not_recorded"
            self.latest = record
            result = dict(record)
            result["session"] = session
            return result
        finally:
            self._cycle_lock.release()

    def _production_ledger(self):
        return production_session_ledger(self.config, self.provider, self.clock)

    def execution_status(self):
        orders = self._acknowledged_orders()
        self.orders_submitted = orders
        return {
            "paper_trading_enabled": bool(self.config.paper_enabled),
            "paper_execution_active": bool(self._active and self.paper_execution_active and not self.stop_event.is_set()),
            "execution_reason": self.execution_reason,
            "live_execution_supported": False,
            "live_execution_locked": True,
            "orders_submitted": orders,
        }

    def run(self, *, max_cycles=None, elapsed_clock=time.monotonic):
        if not self._active:
            raise PaperRuntimeError("paper_scheduler_requires_active_lease")
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
            raise ValueError("max_cycles must be a positive integer")
        completed = 0
        while not self.stop_event.is_set() and (max_cycles is None or completed < max_cycles):
            began = elapsed_clock()
            try:
                self.cycle()
            except (PaperRuntimeError, OSError):
                if max_cycles is not None:
                    raise
                self.paper_execution_active = False
                self.execution_reason = "paper_cycle_failed"
                self.stop_event.wait(max(1.0, self.config.poll_seconds))
                continue
            completed += 1
            if max_cycles is not None and completed >= max_cycles:
                break
            delay = max(1.0, self.config.poll_seconds - (elapsed_clock() - began))
            self.stop_event.wait(delay)
        return completed
