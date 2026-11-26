"""Continuous, account-bound paper executor, separate from the GET-only observer.

This service is an execution component, not a verified profitable strategy. It
starts paused and requires a persisted command bound to the exact configuration.
The customer JWT gateway and learned-strategy promotion service are separate
integration dependencies. No default/observer command instantiates this runner.
"""
from copy import deepcopy
from datetime import datetime, timezone
import re
import threading
import time
import uuid

from .activities import fetch_activities
from .activity_completeness import apply_certification, certify_import
from .alpaca import PaperClient, parse_time
from .data import SnapshotBuilder
from .customer_settings import PaperSettingsBinding, SettingsBindingError
from .journal import Journal
from .ledger import AccountingLedger
from .models import NY
from .observation import _account_identity, _baseline
from .session import PaperSession


class AutopilotError(ValueError):
    pass


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise AutopilotError("invalid_request_id")
    return value


class ExecutionAccounting:
    """Reuse the actual activity ledger; never make absent fees/coverage zero."""
    def __init__(self, config, provider, clock):
        self.config, self.provider, self.clock = config, provider, clock

    def collect(self, account_id):
        """Exhaustive import, fail-closed certification, then ledger snapshot.

        Completeness flags come only from ``certify_import`` (identical re-scan
        plus broker equity position reconciliation); on any doubt they stay
        False and the ledger keeps its execution/fee coverage blockers.
        """
        cfg = self.config
        if not cfg.accounting_history_start:
            return {"complete": False, "blockers": ["accounting_history_start_missing"]}
        until = self.clock().isoformat()
        batch = fetch_activities(self.provider, account_id=account_id,
            after=cfg.accounting_history_start, until=until)
        keys = ("account_id", "environment", "source", "window_start", "window_end", "as_of",
                "executions_complete", "fees_complete", "opening_lots_complete", "opening_lots", "evidence", "blockers")
        coverage = {k: deepcopy(batch.coverage[k]) for k in keys}
        if cfg.accounting_baseline_path:
            baseline = _baseline(cfg.accounting_baseline_path, account_id=account_id,
                                 window_start=coverage["window_start"])
            coverage.update(opening_lots_complete=True, opening_lots=baseline["opening_lots"],
                            opening_evidence=baseline["evidence"])
        certification = certify_import(self.provider, batch, account_id=account_id,
            after=cfg.accounting_history_start, until=until,
            opening_lots=coverage["opening_lots"] if coverage["opening_lots_complete"] else None)
        coverage = apply_certification(coverage, certification)
        ledger = AccountingLedger(cfg.db_path, account_id=account_id)
        ledger.ingest_batch(batch.records, batch_id="autopilot-"+uuid.uuid4().hex, coverage=coverage)
        result = ledger.snapshot(session_date=self.clock().astimezone(NY).date().isoformat(),
            now=self.clock(), cost_policy=cfg.accounting_cost_policy).to_dict()
        result["completeness_certification"] = certification.to_dict()
        return result


class PaperAutopilot:
    """One owner/account/journal, one synchronous loop, durable control state.

    The injected accounting and snapshot dependencies exist for tests. Production
    uses actual provider data. Control calls must originate from an authenticated
    server; owner_id is checked but is not itself an authentication mechanism.
    """
    def __init__(self, config, *, account_id, provider=None, builder=None,
                 accounting=None, clock=None, customer_binding=None, settings_loader=None):
        if config.mode != "paper" or not config.owner_user_id:
            raise AutopilotError("paper_owner_configuration_required")
        try:
            if str(uuid.UUID(account_id)) != account_id:
                raise ValueError
            if str(uuid.UUID(config.owner_user_id)) != config.owner_user_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise AutopilotError("invalid_owner_or_account") from None
        if (customer_binding is None) != (settings_loader is None):
            raise AutopilotError("customer_binding_and_loader_required_together")
        if customer_binding is not None:
            if not isinstance(customer_binding, PaperSettingsBinding) or not callable(settings_loader):
                raise AutopilotError("invalid_customer_settings_dependencies")
            customer_binding.verify_worker(config)
        self.customer_binding = customer_binding
        self.settings_loader = settings_loader
        self.settings_blockers = []
        self.config, self.account_id = config, account_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.provider = provider or PaperClient(config, clock=self.clock)
        self.builder = builder or SnapshotBuilder(self.provider, config)
        self.accounting = accounting or ExecutionAccounting(config, self.provider, self.clock)
        self.journal = Journal(config.db_path)
        self.stop_event = threading.Event()
        self.session = PaperSession(config, self.provider, journal=self.journal,
            snapshot_builder=self.builder, clock=self.clock, expected_account_id=account_id,
            execution_guard=lambda: not self.stop_event.is_set(),
            entry_guard=self._settings_current)
        self.lock = threading.RLock()
        self.active = False
        self.latest = None
        self.instance_id = uuid.uuid4().hex
        self.worker_config_hash = config.review_hash()
        self.config_version = customer_binding.settings_version if customer_binding else self.worker_config_hash
        self.binding = {"owner_id": config.owner_user_id, "account_id": account_id, "mode": "paper"}

    def __enter__(self):
        if self.active or self.stop_event.is_set():
            raise AutopilotError("runner_cannot_be_reentered")
        self.session.__enter__()
        try:
            prior = self.journal.event_by_key("autopilot:binding")
            if prior and prior["payload"] != self.binding:
                raise AutopilotError("owner_account_binding_mismatch")
            observer = self.journal.event_by_key("observation:account-binding")
            if observer and observer["payload"] != {"owner_user_id": self.config.owner_user_id,
                    "account_id": self.account_id, "environment": "paper"}:
                raise AutopilotError("observer_account_binding_mismatch")
            self.journal.append("autopilot", "autopilot_binding", self.binding, key="autopilot:binding")
            prior_config = self.journal.latest("autopilot_configuration", "autopilot")
            if prior_config and (prior_config["payload"]["version"] != self.config_version
                    or prior_config["payload"].get("worker_review_hash", prior_config["payload"]["version"]) != self.worker_config_hash
                    or prior_config["payload"].get("settings_fingerprint") != self._settings_fingerprint()):
                # Do not reconfigure away from exposure or clear emergency latches.
                self._verify_account()
                if self.provider.positions() or self.provider.open_orders():
                    raise AutopilotError("configuration_change_requires_reconciled_flat_account")
                if self.journal.kill_switch().get("enabled") or self.session.risk.locked:
                    raise AutopilotError("configuration_change_cannot_clear_risk_latch")
            self.journal.append("autopilot", "autopilot_configuration",
                {"version": self.config_version, "worker_review_hash": self.worker_config_hash,
                 "customer_settings": self.customer_binding.public() if self.customer_binding else None,
                 "settings_fingerprint": self._settings_fingerprint(),
                 "configuration": self.config.public()},
                key="autopilot:config:"+self.instance_id)
            self.active = True
        except BaseException:
            self.session.__exit__()
            raise
        return self

    def __exit__(self, *args):
        self.stop_event.set()
        with self.lock:
            self.active = False
            self.session.__exit__(*args)

    def _settings_current(self):
        """Settings loss/changes halt entries, never the protective exit path.

        The loader is a bounded server-authenticated read, scoped to this owner.
        A gateway must not pass a browser-provided row or cached success here.
        """
        if self.customer_binding is None:
            return True
        try:
            self.customer_binding.verify_worker(self.config)
            self.customer_binding.verify_current(
                self.settings_loader(self.config.owner_user_id), now=self.clock())
        except SettingsBindingError:
            self.settings_blockers = ["customer_settings_changed_or_invalid"]
            # An observed revision change revokes the old entry authorization.
            # Restoring an old row later must not silently resume entries.
            if self.active and self._control().get("action") == "activate":
                request_id = "settings-change-" + uuid.uuid4().hex
                self.journal.append("autopilot", "autopilot_command", {
                    "owner_id": self.config.owner_user_id, "action": "pause", "mode": "paper",
                    "configuration_version": self.config_version,
                    "worker_review_hash": self.worker_config_hash,
                    "settings_fingerprint": self._settings_fingerprint(),
                    "request_id": request_id, "consent": False,
                    "reason": "customer_settings_changed_or_invalid"},
                    key="autopilot:command:" + request_id, timestamp=self.clock().isoformat())
            return False
        except Exception:
            self.settings_blockers = ["customer_settings_unavailable"]
            return False
        self.settings_blockers = []
        return True

    def _verify_account(self):
        actual = _account_identity(self.provider.account())
        if actual != self.account_id:
            raise AutopilotError("broker_account_identity_mismatch")
        return actual

    def _settings_fingerprint(self):
        return self.customer_binding.settings_fingerprint if self.customer_binding else None

    def _matches(self, control):
        # A UUID alone cannot authorize another operator configuration or row.
        # Legacy commands missing binding fields require new explicit consent.
        return (control.get("configuration_version") == self.config_version
                and control.get("worker_review_hash") == self.worker_config_hash
                and control.get("settings_fingerprint") == self._settings_fingerprint())

    def _control_snapshot(self):
        """Read payload and durable identity together; timestamps are not order."""
        row = self.journal.latest("autopilot_command", "autopilot")
        if row:
            return row["payload"], row["event_id"]
        return {"action": "pause", "configuration_version": self.config_version}, None

    def _control(self):
        return self._control_snapshot()[0]

    def command(self, *, owner_id, action, configuration_version, request_id, consent=False):
        """Persist before acknowledgment; command retries cannot undo a later pause."""
        if owner_id != self.config.owner_user_id:
            raise AutopilotError("owner_mismatch")
        if action not in {"activate", "pause", "emergency_stop"}:
            raise AutopilotError("unsupported_command")
        identifier(request_id)
        if type(consent) is not bool:
            raise AutopilotError("invalid_consent")
        if not isinstance(configuration_version, str) or not 1 <= len(configuration_version) <= 128:
            raise AutopilotError("invalid_configuration_version")
        request = {"owner_id": owner_id, "action": action, "mode": "paper",
            "configuration_version": configuration_version, "consent": consent,
            "request_id": request_id, "worker_review_hash": self.worker_config_hash,
            "settings_fingerprint": self._settings_fingerprint()}
        key = "autopilot:command:"+request_id
        with self.lock:
            if not self.active or self.stop_event.is_set():
                raise AutopilotError("worker_not_running")
            previous = self.journal.event_by_key(key)
            if previous:
                if previous["payload"] != request:
                    raise AutopilotError("request_id_conflict")
                return {"acknowledged": True, "duplicate": True, "request_id": request_id}
            if action == "activate":
                if not self._settings_current():
                    raise AutopilotError(self.settings_blockers[0])
                if configuration_version != self.config_version:
                    raise AutopilotError("configuration_version_mismatch")
                if not consent:
                    raise AutopilotError("explicit_paper_trading_consent_required")
                blockers = self.config.paper_blockers()
                if blockers:
                    raise AutopilotError("execution_configuration_incomplete")
                if self.journal.kill_switch().get("enabled") or self.session.risk.locked:
                    raise AutopilotError("risk_stop_requires_operator_resolution")
                # Fresh reconciliation must precede activation; never infer it
                # from a successful key/link check or a previous process.
                if not self.latest or not self.latest.get("reconciled") or not self._fresh():
                    raise AutopilotError("fresh_reconciliation_required")
                self._verify_account()
            if action == "emergency_stop":
                # Safety commands remain usable from a stale settings screen.
                # Only activation requires the current exact version.
                self.journal.set_kill_switch(True, "autopilot_emergency_stop", flatten=True,
                    request_id="autopilot-emergency:"+request_id)
            self.journal.append("autopilot", "autopilot_command", request, key=key,
                                timestamp=self.clock().isoformat())
            return {"acknowledged": True, "duplicate": False, "request_id": request_id}

    def _fresh(self):
        try:
            return bool(self.latest and 0 <= (self.clock()-parse_time(self.latest["completed_at"])).total_seconds() <= 30)
        except (ValueError, TypeError, KeyError):
            return False

    def cycle(self):
        with self.lock:
            if not self.active or self.stop_event.is_set():
                raise AutopilotError("worker_not_running")
            control, command_event_id = self._control_snapshot()
            matching = self._matches(control)
            settings_current = self._settings_current()
            activated = matching and control["action"] == "activate" and settings_current
            # Once authorized, pause continues protective order management. A
            # fresh process re-reconciles before it can submit another entry.
            authorization = any(row["payload"].get("action") == "activate" and
                self._matches(row["payload"])
                for row in self.journal.events("autopilot", event_type="autopilot_command"))
            errors = []
            try:
                self._verify_account()
                ledger = self.accounting.collect(self.account_id)
                if ledger.get("account_id") != self.account_id or ledger.get("environment") != "paper":
                    raise AutopilotError("accounting_binding_missing_or_mismatched")
                snapshots = self.builder.build_watchlist()
                outcome = self.session.cycle(snapshots=snapshots, session_ledger=ledger,
                    execute=bool(authorization), allow_entries=activated)
                errors = outcome.get("errors", []) + self.settings_blockers
            except Exception:
                # Errors are codes; do not expose transport messages or secrets.
                outcome = {"reconciled": False, "positions_confirmed_flat": False,
                           "actions": [], "errors": ["execution_cycle_failed"]}
                errors = outcome["errors"] + self.settings_blockers
            if outcome.get("reconciled") is not True and not errors:
                errors.append("execution_reconciliation_incomplete")
            blockers = sorted(set(errors + self.config.paper_blockers()))
            stopped = bool(self.journal.kill_switch().get("enabled") or self.session.risk.locked)
            state = "stopped" if stopped else "blocked" if blockers else "active" if activated else "paused"
            result = {"schema_version": "1.0", **self.binding, "instance_id": self.instance_id,
                "configuration_version": self.config_version, "worker_review_hash": self.worker_config_hash,
                "customer_settings": self.customer_binding.public() if self.customer_binding else None,
                "state": state, "command_event_id": command_event_id,
                "completed_at": self.clock().isoformat(), "reconciled": outcome.get("reconciled") is True,
                "execution_enabled": bool(authorization and not self.config.paper_blockers()),
                "entries_enabled": bool(activated and not stopped and not errors and
                                        not self.config.paper_blockers() and outcome.get("reconciled") is True),
                "positions_confirmed_flat": outcome.get("positions_confirmed_flat") is True,
                "blockers": blockers, "session": outcome}
            self.journal.append("autopilot", "autopilot_cycle", result,
                key="autopilot:cycle:"+uuid.uuid4().hex, timestamp=result["completed_at"])
            self.latest = result
            return deepcopy(result)

    def state(self):
        with self.lock:
            result = deepcopy(self.latest)
            if not result:
                return {"schema_version": "1.0", **self.binding, "state": "checking",
                        "configuration_version": self.config_version, "entries_enabled": False}
            control, command_event_id = self._control_snapshot()
            if not self.active or self.stop_event.is_set() or not self._fresh():
                result.update(state="offline", entries_enabled=False, execution_enabled=False)
            elif self.journal.kill_switch().get("enabled") or self.session.risk.locked:
                result.update(state="stopped", entries_enabled=False)
            elif control["action"] != "activate" or not self._matches(control):
                result.update(state="stopped" if control["action"] == "emergency_stop" else "paused", entries_enabled=False)
            elif result.get("state") == "active" and (
                    not isinstance(command_event_id, str) or not command_event_id
                    or result.get("command_event_id") != command_event_id
                    or result.get("instance_id") != self.instance_id):
                # A receipt cannot make an older cycle active for a new command.
                # Preserve the raw cycle and protective-management authorization.
                result.update(state="paused", entries_enabled=False)
            return result

    def run(self, *, max_cycles=None):
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
            raise AutopilotError("invalid_cycle_limit")
        count = 0
        while not self.stop_event.is_set() and (max_cycles is None or count < max_cycles):
            began = time.monotonic()
            self.cycle()
            count += 1
            if max_cycles is None or count < max_cycles:
                self.stop_event.wait(max(1.0, self.config.poll_seconds-(time.monotonic()-began)))
        return count
