"""Single-owner observation scheduler. This runtime cannot submit or cancel orders.

All broker I/O is GET-only. A saved dashboard link is not evidence here: the
worker needs separately provisioned server credentials, verifies the actual
account, and binds its durable journal to that account before importing data.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import threading
import time
import uuid

from .activities import fetch_activities, timestamp_parts
from .alpaca import BrokerError, PaperClient, PaperDisabled, parse_time
from .data import SnapshotBuilder
from .journal import Journal
from .ledger import AccountingLedger
from .market_observation import (MarketWatchCollector, market_data_evidence,
                                 market_watch_from_snapshots, sanitize_market_watch)
from .models import NY
from .publisher import Publisher
from .price_history import PriceHistoryCollector, price_history_summary, sanitize_price_history
from .crypto_observation import (CryptoQuoteReader, CryptoWatchCollector,
                                 sanitize_crypto_watch, crypto_watch_summary)
from .session import PaperSession


class ObservationError(RuntimeError):
    """Only fixed diagnostic codes should cross the runtime boundary."""


class ReadOnlyPaperClient(PaperClient):
    def __init__(self, config, *, transport=None, clock=None, stop_event=None):
        super().__init__(config, transport=transport, clock=clock)
        self.stop_event = stop_event or threading.Event()

    def _enabled(self):
        if self.config.mode != "paper" or not self.config.api_key or not self.config.api_secret:
            raise PaperDisabled("Paper observation requires paper mode and server credentials")
        if self.stop_event.is_set():
            raise PaperDisabled("Paper observation is stopping")

    def _request(self, method, path, **kwargs):
        if method != "GET" or kwargs.get("payload") is not None:
            raise PaperDisabled("Observation broker transport is GET-only")
        return super()._request(method, path, **kwargs)

    def submit(self, *args, **kwargs):
        raise PaperDisabled("Order submission is unavailable in observation mode")

    def cancel(self, *args, **kwargs):
        raise PaperDisabled("Order cancellation is unavailable in observation mode")


class _ReadOnlyProvider:
    """Also deny mutations on injected providers at the session boundary."""
    _reads = frozenset({"account", "positions", "open_orders", "order_by_client_id",
                        "calendar", "snapshots", "bars", "observation_bars", "activity_page"})

    def __init__(self, provider, stop_event):
        self._provider, self._stop = provider, stop_event
        self.clock = provider.clock

    def __getattr__(self, name):
        if name not in self._reads:
            raise PaperDisabled("Observation provider exposes only read operations")
        def read(*args, **kwargs):
            if self._stop.is_set():
                raise PaperDisabled("Paper observation is stopping")
            return getattr(self._provider, name)(*args, **kwargs)
        return read


def _fresh(stamp, now, seconds):
    try:
        return 0 <= (now - parse_time(stamp)).total_seconds() <= seconds
    except (ValueError, TypeError, ArithmeticError):
        return False


def _account_identity(account):
    if not isinstance(account, dict):
        raise ObservationError("invalid_paper_account_response")
    try:
        identity = str(uuid.UUID(account.get("id", "")))
        if identity != account.get("id"):
            raise ValueError
        for key in ("equity", "buying_power"):
            amount = Decimal(str(account[key]))
            if not amount.is_finite() or amount < 0:
                raise ValueError
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError):
        raise ObservationError("invalid_paper_account_response") from None
    if (account.get("status") != "ACTIVE" or account.get("trading_blocked") is not False
            or account.get("account_blocked") is not False
            or account.get("trade_suspended_by_user", False) is not False):
        raise ObservationError("paper_account_not_active_or_blocked")
    return identity


def _baseline(path, *, account_id, window_start):
    """Read explicit operator evidence, never infer historical flatness today."""
    if not path:
        return None
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(262_145)
        if len(raw) > 262_144:
            raise ValueError
        value = json.loads(raw)
        required = {"schema_version", "account_id", "environment", "window_start", "evidence", "opening_lots"}
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError
        if (value["schema_version"] != "1.0" or value["account_id"] != account_id
                or value["environment"] != "paper"
                or timestamp_parts(value["window_start"])[1] != timestamp_parts(window_start)[1]
                or not isinstance(value["evidence"], str) or not value["evidence"].strip()
                or len(value["evidence"]) > 2000 or not isinstance(value["opening_lots"], list)):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError, ArithmeticError, RecursionError, UnicodeError):
        raise ObservationError("accounting_baseline_invalid_or_mismatched") from None


class ObservationWorker:
    def __init__(self, config, *, provider=None, journal=None, snapshot_builder=None,
                 publisher=None, activity_fetcher=None, clock=None, instance_id=None,
                 price_history_collector=None, crypto_collector=None):
        if config.mode != "paper":
            raise ObservationError("observation_requires_explicit_paper_mode")
        self.config = config
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.instance_id = str(uuid.UUID(instance_id)) if instance_id else str(uuid.uuid4())
        self.stop_event = threading.Event()
        raw = provider or ReadOnlyPaperClient(config, clock=self.clock, stop_event=self.stop_event)
        self.provider = _ReadOnlyProvider(raw, self.stop_event)
        self.journal = journal or Journal(config.db_path)
        self._injected_builder = snapshot_builder
        self.builder = snapshot_builder or SnapshotBuilder(self.provider, config)
        self.market_collector = MarketWatchCollector(self.provider, config, clock=self.clock)
        self.history_collector = (None if snapshot_builder is not None else
            price_history_collector or PriceHistoryCollector(self.provider, config, clock=self.clock))
        # Crypto is opt-in and never enters the stock strategy snapshot builder.
        self.crypto_collector = (crypto_collector or CryptoWatchCollector(
            CryptoQuoteReader(config), pairs=config.crypto_watchlist, clock=self.clock)
            if config.crypto_watchlist else None)
        self.session = PaperSession(config, self.provider, journal=self.journal,
                                    snapshot_builder=self.builder, clock=self.clock)
        cloud = (config.supabase_url, config.supabase_service_role_key, config.owner_user_id)
        self.publisher = publisher if publisher is not None else (Publisher(config, clock=self.clock) if all(cloud) else None)
        if any(cloud) and not all(cloud):
            raise ObservationError("incomplete_cloud_worker_configuration")
        self.fetch_activities = activity_fetcher or fetch_activities
        self._cycle_lock, self._state_lock = threading.Lock(), threading.Lock()
        self._active = False
        self._latest = None
        self._account_id = None
        self._baseline_cache = None

    def __enter__(self):
        if self._active or self.stop_event.is_set():
            raise ObservationError("observation_instance_cannot_be_reentered")
        self.session.__enter__()
        try:
            binding = self.journal.event_by_key("observation:account-binding")
            if binding and (binding["payload"].get("environment") != "paper"
                    or binding["payload"].get("owner_user_id") != (self.config.owner_user_id or None)):
                raise ObservationError("paper_account_or_owner_binding_mismatch")
            self.journal.append("paper-observation", "observation_started",
                {"instance_id": self.instance_id, "mode": "paper", "execution_enabled": False},
                key="observe-start:" + self.instance_id, timestamp=self.clock().isoformat())
            self._active = True
        except BaseException:
            self.session.__exit__()
            raise
        return self

    def __exit__(self, *args):
        self.stop_event.set()
        try:
            if self.publisher is not None:
                try:
                    self.publisher.publish_worker_state(instance_id=self.instance_id,
                        halted=bool(self.journal.kill_switch().get("enabled")))
                except Exception:
                    # No stale evidence is refreshed; the last cloud heartbeat expires.
                    pass
            self.journal.append("paper-observation", "observation_stopped",
                {"instance_id": self.instance_id, "mode": "paper", "execution_enabled": False},
                key="observe-stop:" + self.instance_id, timestamp=self.clock().isoformat())
        finally:
            self._active = False
            self.session.__exit__(*args)

    def _bind_account(self, account):
        identity = _account_identity(account)
        expected = {"account_id": identity, "environment": "paper",
                    "owner_user_id": self.config.owner_user_id or None}
        prior = self.journal.event_by_key("observation:account-binding")
        if prior and prior["payload"] != expected:
            raise ObservationError("paper_account_or_owner_binding_mismatch")
        if not prior:
            self.journal.append("paper-observation", "observation_account_bound", expected,
                                key="observation:account-binding", timestamp=self.clock().isoformat())
        self._account_id = identity
        self.session.expected_account_id = identity
        return identity

    def _accounting(self, account_id, cycle_id, started_at):
        if not self.config.accounting_history_start:
            return {"complete": False, "fees": None, "realized_pnl": None,
                    "blockers": ["accounting_history_start_missing"]}, None
        imported = self.fetch_activities(self.provider, account_id=account_id,
            after=self.config.accounting_history_start, until=started_at.isoformat())
        # REST scan metadata and financial-completeness evidence are separate.
        keys = ("account_id", "environment", "source", "window_start", "window_end", "as_of",
                "executions_complete", "fees_complete", "opening_lots_complete", "opening_lots", "evidence", "blockers")
        coverage = {key: deepcopy(imported.coverage[key]) for key in keys}
        if self.config.accounting_baseline_path:
            if self._baseline_cache is None:
                self._baseline_cache = _baseline(self.config.accounting_baseline_path,
                    account_id=account_id, window_start=coverage["window_start"])
            coverage["opening_lots_complete"] = True
            coverage["opening_lots"] = self._baseline_cache["opening_lots"]
            coverage["opening_evidence"] = self._baseline_cache["evidence"]
        ledger = AccountingLedger(self.config.db_path, account_id=account_id)
        receipt = ledger.ingest_batch(imported.records, batch_id=cycle_id, coverage=coverage)
        snapshot = ledger.snapshot(session_date=self.clock().astimezone(NY).date().isoformat(),
            now=self.clock(), cost_policy=self.config.accounting_cost_policy).to_dict()
        return snapshot, {"record_count": len(imported.records), "receipt": receipt,
                          "activity_pages_exhausted": imported.coverage.get("activity_pages_exhausted") is True}

    def _snapshots(self):
        # The normal observer fetches current prices only. Its separately owned
        # PaperSession never builds history because snapshots=[] is explicit.
        # Keep the injected builder contract for deterministic component tests.
        snapshots, watch, blockers = [], None, []
        if self._injected_builder is not None and self.config.watchlist:
            try:
                snapshots = self._injected_builder.build_watchlist()
                watch = market_watch_from_snapshots(snapshots, allowed_symbols=self.config.watchlist,
                    feed=self.config.data_feed, now=self.clock(), max_age_seconds=self.config.quote_max_age_seconds)
            except (BrokerError, ValueError, TypeError, KeyError, OSError, ArithmeticError):
                snapshots = []
        if watch is None:
            watch = self.market_collector.collect()
        if not snapshots:
            blockers.append("strategy_inputs_unavailable")
        if not self.config.watchlist:
            blockers.append("watchlist_missing")
        data = market_data_evidence(watch)
        if not data["verified"]:
            blockers.append("current_same_feed_quotes_unavailable")
        return snapshots, data, blockers, watch

    def _commands(self):
        if self.publisher is None:
            return []
        return self.publisher.acknowledge_halts(self.journal)

    def _history_state(self, history, *, now, watch=None):
        if history is None:
            return None
        try:
            result = sanitize_price_history(history, allowed_symbols=self.config.watchlist,
                                             feed=self.config.data_feed, now=now)
            if watch is not None and timestamp_parts(result["collected_at"])[1] > timestamp_parts(watch["collected_at"])[1]:
                raise ValueError
            return result
        except (ValueError, TypeError, KeyError, ArithmeticError, UnicodeError, RecursionError):
            return None

    def _crypto_state(self, report, *, now):
        if report is None or not self.config.crypto_watchlist:
            return None
        try:
            return sanitize_crypto_watch(report, allowed_pairs=self.config.crypto_watchlist, now=now)
        except (ValueError, TypeError, KeyError, ArithmeticError, UnicodeError, RecursionError):
            return None

    def cycle(self):
        if not self._active or self.stop_event.is_set():
            raise ObservationError("observation_cycle_requires_active_lease")
        if not self._cycle_lock.acquire(blocking=False):
            raise ObservationError("observation_cycle_already_running")
        try:
            started = self.clock()
            cycle_id = uuid.uuid4().hex
            result = {"schema_version": "1.0", "mode": "paper", "runtime": "observation_only",
                "instance_id": self.instance_id, "cycle_id": cycle_id, "started_at": started.isoformat(),
                "provenance": "provider_observations", "orders_submitted": 0, "execution_enabled": False,
                "broker": {"verified": False, "checked_at": None},
                "data_feed": {"verified": False, "feed": self.config.data_feed, "as_of": None, "received_at": None},
                "market_watch": None, "price_history": None,
                "accounting": {"complete": False, "realized_pnl": None, "fees": None},
                "session": None, "blockers": ["observation_runtime_cannot_execute_orders"],
                "acknowledged_commands": [], "error_code": None}
            stage = "command_dispatch"
            try:
                result["acknowledged_commands"].extend(self._commands())
                stage = "paper_account_check"
                checked_at = self.clock()
                account_id = self._bind_account(self.provider.account())
                # Account identity alone is not the full worker read contract.
                # Verification becomes true after the session also reads current
                # positions and open orders successfully.
                result["broker"] = {"verified": False, "checked_at": checked_at.isoformat()}
                stage = "market_observation"
                if self.history_collector is not None:
                    try:
                        # Bounded cached history runs before the fresh quote GET.
                        # Failure here never discards account or quote evidence.
                        result["price_history"] = self.history_collector.collect()
                    except (RuntimeError, ValueError, TypeError, KeyError, OSError, ArithmeticError, AttributeError):
                        result["blockers"].append("price_history_unavailable")
                snapshots, data, blockers, watch = self._snapshots()
                result["data_feed"] = data
                result["market_watch"] = watch
                result["blockers"].extend(blockers)
                if self.crypto_collector is not None and not self.stop_event.is_set():
                    # Separate GET-only quote source; crypto failure cannot fail stock reads.
                    try:
                        result["crypto_watch"] = self.crypto_collector.collect()
                    except (RuntimeError, ValueError, TypeError, KeyError, OSError, ArithmeticError):
                        result["crypto_watch"] = None
                        result["blockers"].append("crypto_watch_unavailable")
                stage = "activity_accounting"
                ledger, receipt = self._accounting(account_id, cycle_id, self.clock())
                result["accounting"] = {k: deepcopy(v) for k, v in ledger.items()
                    if k not in {"open_lots", "matches", "revisions", "coverage", "evidence"}}
                result["accounting_import"] = receipt
                result["blockers"].extend(ledger.get("blockers", []))
                stage = "session_evaluation"
                # Unconditional False plus the read-only provider/transport are
                # independent boundaries. No config value can arm this runtime.
                session = self.session.cycle(snapshots=snapshots, session_ledger=ledger, execute=False)
                result["session"] = session
                result["blockers"].extend(session.get("errors", []))
                if session.get("broker_account_id") not in {None, account_id}:
                    result["broker"] = {"verified": False, "checked_at": None}
                    raise ObservationError("paper_account_changed_during_cycle")
                if session.get("connected") is True and session.get("broker_account_id") == account_id and session.get("broker_account_verified") is True:
                    result["broker"]["verified"] = True
                    result["broker"]["checked_at"] = session["broker_checked_at"]
                elif session.get("broker_account_verified") is False:
                    result["broker"] = {"verified": False, "checked_at": None}
                stage = "command_dispatch"
                result["acknowledged_commands"].extend(self._commands())
            except (RuntimeError, ValueError, TypeError, KeyError, OSError, ArithmeticError):
                result["error_code"] = stage + "_failed"
                result["blockers"].append(result["error_code"])
                if stage == "paper_account_check":
                    result["broker"] = {"verified": False, "checked_at": None}
                if stage == "command_dispatch":
                    self.journal.set_kill_switch(True, "cloud_command_dispatch_failed", request_id="dispatcher:" + cycle_id)
            result["halted"] = bool(self.journal.kill_switch().get("enabled"))
            result["completed_at"] = self.clock().isoformat()
            if result["market_watch"] is not None:
                # Account/session reads may take time after quote receipt. Keep
                # the original timestamps and expire rows before every mirror.
                result["market_watch"] = sanitize_market_watch(result["market_watch"],
                    allowed_symbols=self.config.watchlist, feed=self.config.data_feed, now=self.clock(),
                    max_age_seconds=self.config.quote_max_age_seconds)
                result["data_feed"] = market_data_evidence(result["market_watch"])
            result["price_history"] = self._history_state(result["price_history"], now=self.clock(), watch=result["market_watch"])
            if "crypto_watch" in result:
                result["crypto_watch"] = self._crypto_state(result["crypto_watch"], now=self.clock())
            result["blockers"] = sorted(set(result["blockers"]))
            result["acknowledged_commands"] = sorted(set(result["acknowledged_commands"]))
            if self.publisher is not None:
                try:
                    publish_args = dict(instance_id=self.instance_id,
                        broker_verified=result["broker"]["verified"], broker_checked_at=result["broker"]["checked_at"],
                        data_verified=result["data_feed"]["verified"], data_feed=self.config.data_feed,
                        data_as_of=result["data_feed"].get("as_of"), data_received_at=result["data_feed"].get("received_at"),
                        halted=result["halted"], error_code=result["error_code"], market_watch=result["market_watch"],
                        price_history=result["price_history"])
                    # Preserve the existing stock-only publisher invocation by default.
                    if "crypto_watch" in result:
                        publish_args["crypto_watch"] = result["crypto_watch"]
                    self.publisher.publish_worker_state(**publish_args)
                    result["cloud_state_published"] = True
                except Exception:
                    result["cloud_state_published"] = False
                    result["blockers"].append("cloud_state_publication_failed")
            else:
                result["cloud_state_published"] = False
                result["blockers"].append("cloud_worker_not_configured")
            journal_result = {**result, "price_history": price_history_summary(result["price_history"])}
            if "crypto_watch" in journal_result:
                journal_result["crypto_watch"] = crypto_watch_summary(result["crypto_watch"])
            self.journal.append("paper-observe-" + started.astimezone(NY).date().isoformat(),
                "paper_observation_cycle", journal_result, key="observe-cycle:" + cycle_id,
                timestamp=result["completed_at"])
            with self._state_lock:
                self._latest = deepcopy(result)
            return deepcopy(result)
        finally:
            self._cycle_lock.release()

    def state(self):
        with self._state_lock:
            latest = deepcopy(self._latest)
        now = self.clock()
        if latest and latest.get("market_watch") is not None:
            latest["market_watch"] = sanitize_market_watch(latest["market_watch"],
                allowed_symbols=self.config.watchlist, feed=self.config.data_feed, now=now,
                max_age_seconds=self.config.quote_max_age_seconds)
            latest["data_feed"] = market_data_evidence(latest["market_watch"])
        if latest and latest.get("price_history") is not None:
            latest["price_history"] = self._history_state(latest["price_history"], now=now, watch=latest.get("market_watch"))
        if latest and latest.get("crypto_watch") is not None:
            latest["crypto_watch"] = self._crypto_state(latest["crypto_watch"], now=now)
        current = bool(self._active and latest and _fresh(latest["completed_at"], now, 30))
        broker = bool(current and latest["broker"]["verified"]
                      and _fresh(latest["broker"]["checked_at"], now, 30))
        data = bool(current and latest["data_feed"]["verified"]
                    and _fresh(latest["data_feed"].get("as_of"), now, self.config.quote_max_age_seconds)
                    and _fresh(latest["data_feed"].get("received_at"), now, self.config.quote_max_age_seconds))
        return {"instance_id": self.instance_id, "running": self._active,
                "paper_connected": broker, "data_connected": data,
                "paper_reconciled": bool(current and broker and data and latest.get("session")
                    and latest["session"].get("reconciled")
                    and _fresh(latest["session"].get("broker_checked_at"), now, 5)
                    and _fresh(latest["accounting"].get("as_of"), now, 5)),
                "paper_execution_running": False, "latest_cycle": latest}

    def run(self, *, max_cycles=None, elapsed_clock=time.monotonic):
        if not self._active:
            raise ObservationError("observation_scheduler_requires_active_lease")
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
            raise ValueError("max_cycles must be a positive integer")
        completed = 0
        while not self.stop_event.is_set() and (max_cycles is None or completed < max_cycles):
            began = elapsed_clock()
            self.cycle()
            completed += 1
            if max_cycles is not None and completed >= max_cycles:
                break
            # One synchronous cycle at a time, no catch-up burst after an outage.
            delay = max(1.0, self.config.poll_seconds - (elapsed_clock() - began))
            self.stop_event.wait(delay)
        return completed
