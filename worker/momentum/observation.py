"""Single-owner observation scheduler.

The default runtime cannot submit or cancel orders: broker I/O is GET-only.
``serve-paper`` constructs the same worker with ``stock_execution=True``. That
is still one process and one journal. It keeps the crypto hook, the resume
path, and the OANDA watch, and it may submit stock orders only when every
paper gate holds. It does not start the standalone crypto timer.

A saved dashboard link is not evidence here: the worker needs separately
provisioned server credentials, verifies the actual account, and binds its
durable journal to that account before importing data.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .activities import fetch_activities, timestamp_parts
from .alpaca import BrokerError, PaperClient, PaperDisabled, parse_time
from .data import SnapshotBuilder
from .journal import Journal, canonical_json
from .ledger import AccountingLedger
from .market_observation import (MarketWatchCollector, market_data_evidence,
                                 market_watch_from_snapshots, sanitize_market_watch)
from .models import NY
from .publisher import Publisher
from .resume import config_report, control_report, resume_refusal
from .price_history import PriceHistoryCollector, price_history_summary, sanitize_price_history
from .crypto_observation import (CryptoQuoteReader, CryptoWatchCollector,
                                 sanitize_crypto_watch)
from .forex_observation import (ForexWatchCollector, forex_watch_summary,
                                sanitize_forex_watch)
from .oanda import OandaPracticeClient
from . import paper_run_hooks
from .session import PaperSession


class ObservationError(RuntimeError):
    """Only fixed diagnostic codes should cross the runtime boundary."""


def journal_observation_record(result):
    """Small durable cycle record. Quotes and decisions stay in memory only."""
    broker = result.get("broker") or {}
    data = result.get("data_feed") or {}
    accounting = result.get("accounting") or {}
    session = result.get("session") or {}
    return {
        "schema_version": result.get("schema_version"),
        "mode": "paper",
        "runtime": result.get("runtime") or "observation_only",
        "cycle_id": result.get("cycle_id"),
        "started_at": result.get("started_at"),
        "completed_at": result.get("completed_at"),
        "execution_enabled": bool(result.get("execution_enabled")),
        "orders_submitted": int(result.get("orders_submitted") or 0),
        "error_code": result.get("error_code"),
        "halted": result.get("halted"),
        "blockers": result.get("blockers"),
        "broker_verified": broker.get("verified"),
        "data_verified": data.get("verified"),
        "accounting_availability": accounting.get("availability"),
        "accounting_complete": accounting.get("complete"),
        "session_reconciled": session.get("reconciled"),
        "session_errors": session.get("errors"),
    }


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

    def replace(self, *args, **kwargs):
        raise PaperDisabled("Order replacement is unavailable in observation mode")


class StockPaperClient(PaperClient):
    """Paper reads without ``PAPER_TRADING_ENABLED``. Mutations stay locked.

    ``serve-paper`` uses this so the observation cycle still runs when the
    trading flag is off. Submit, cancel, and replace re-check the paper lock
    and refuse every host other than ``https://paper-api.alpaca.markets``.
    """

    def __init__(self, config, *, transport=None, clock=None, stop_event=None):
        super().__init__(config, transport=transport, clock=clock)
        self.stop_event = stop_event or threading.Event()

    def _enabled(self):
        if self.config.mode != "paper" or not self.config.api_key or not self.config.api_secret:
            raise PaperDisabled("Paper runtime requires paper mode and server credentials")
        if self.stop_event.is_set():
            raise PaperDisabled("Paper runtime is stopping")

    def _request(self, method, path, **kwargs):
        if method != "GET":
            if self.config.mode != "paper" or not self.config.paper_enabled:
                raise PaperDisabled("paper_trading_not_enabled")
            blockers = list(self.config.paper_blockers())
            if blockers:
                raise PaperDisabled("Paper execution is blocked: " + ", ".join(blockers))
            from .alpaca import assert_paper_broker_url
            from .config import PAPER_API_HOST
            assert_paper_broker_url(PAPER_API_HOST)
        return super()._request(method, path, **kwargs)


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


AUTO_BASELINE_NAME = "accounting-baseline.auto.json"
_ACCOUNT_NUMBER = __import__("re").compile(r"^[A-Z0-9]{4,24}$")


def auto_baseline_path(config):
    return str(Path(config.db_path).resolve().parent / AUTO_BASELINE_NAME)


def auto_opening_baseline(config, provider, *, account_id, batch):
    """ACCOUNTING_BASELINE_AUTO: an EMPTY opening inventory, proven by the broker, or None.

    Proof (all required, all read from the paper broker with the worker's keys):
    the activity scan since ACCOUNTING_HISTORY_START is exhausted with no
    blockers and contains NO fills of any asset class (equity or crypto), and
    the account has zero positions and zero open orders right now. Flat now
    plus no fills since the start means the inventory at the start was empty.
    The proof is written once to ``accounting-baseline.auto.json`` next to the
    journal and then read back through ``_baseline`` (account and start must
    match), so later trades do not undo it. Anything else returns None and the
    ledger keeps ``opening_inventory_unproven``. Never overwrites a file.
    """
    if not config.accounting_baseline_auto or config.accounting_baseline_path:
        return None
    path = Path(auto_baseline_path(config))
    window_start = batch.coverage.get("window_start")
    if path.exists():
        try:
            return _baseline(str(path), account_id=account_id, window_start=window_start)
        except ObservationError:
            print("accounting: auto baseline on disk does not match this account/start; "
                  "opening inventory stays unproven", flush=True)
            return None
    coverage = batch.coverage
    if coverage.get("activity_pages_exhausted") is not True or coverage.get("blockers"):
        return None
    if coverage.get("environment") != "paper" or coverage.get("account_id") != account_id:
        return None
    observations = list(getattr(batch, "observations", ()) or ())
    if batch.records or any(o.activity_type in {"FILL", "FEE", "CFEE"} or o.kind != "cash" for o in observations):
        return None  # any fill/fee (stock or crypto) or unclassified activity: no automatic claim
    try:
        positions = provider.positions()
        orders = provider.open_orders()
    except Exception:
        return None
    if not isinstance(positions, list) or not isinstance(orders, list) or positions or orders:
        return None
    value = {"schema_version": "1.0", "account_id": account_id, "environment": "paper",
             "window_start": window_start, "opening_lots": [],
             "evidence": ("auto: paper broker showed 0 positions, 0 open orders and 0 fills "
                          f"(all asset classes) from {window_start} to {coverage.get('window_end')} "
                          f"({len(observations)} non-trade cash activities); empty opening inventory.")}
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
        import os
        os.link(tmp, path)  # fails if another writer created it first; never overwrite
    except OSError:
        return None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    print(f"accounting: auto opening baseline proven (empty inventory) for account {account_id} "
          f"from {window_start}", flush=True)
    try:
        return _baseline(str(path), account_id=account_id, window_start=window_start)
    except ObservationError:
        return None


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
                 price_history_collector=None, crypto_collector=None, forex_collector=None,
                 stock_execution=False, session_ledger_factory=None):
        if config.mode != "paper":
            raise ObservationError("observation_requires_explicit_paper_mode")
        self.config = config
        self.stock_execution = stock_execution is True
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.instance_id = str(uuid.UUID(instance_id)) if instance_id else str(uuid.uuid4())
        self.stop_event = threading.Event()
        self._session_ledger_factory = session_ledger_factory
        self.paper_execution_active = False
        self.execution_reason = ("awaiting_first_cycle" if self.stock_execution
                                 else "observation_runtime_cannot_execute_orders")
        self.orders_submitted = 0
        self.broker_url = None
        self.broker_url_error = None
        if self.stock_execution:
            from .alpaca import assert_paper_broker_url
            from .config import PAPER_API_HOST
            try:
                self.broker_url = assert_paper_broker_url(PAPER_API_HOST)
            except PaperDisabled as exc:
                self.broker_url_error = str(exc)
            # Same paper client the stock session submits through. serve-observe
            # keeps the read-only wrapper below and cannot be armed by config.
            raw = provider or StockPaperClient(config, clock=self.clock, stop_event=self.stop_event)
            self.provider = raw
        else:
            raw = provider or ReadOnlyPaperClient(config, clock=self.clock, stop_event=self.stop_event)
            self.provider = _ReadOnlyProvider(raw, self.stop_event)
        self.journal = journal or Journal(config.db_path, sample_seconds=config.journal_sample_seconds,
                                          min_free_bytes=config.journal_min_free_mb * 1024 * 1024)
        self._injected_builder = snapshot_builder
        self.builder = snapshot_builder or SnapshotBuilder(self.provider, config)
        self.market_collector = MarketWatchCollector(self.provider, config, clock=self.clock)
        self.history_collector = (None if snapshot_builder is not None else
            price_history_collector or PriceHistoryCollector(self.provider, config, clock=self.clock))
        # Crypto is opt-in and never enters the stock strategy snapshot builder.
        self.crypto_collector = (crypto_collector or CryptoWatchCollector(
            CryptoQuoteReader(config), pairs=config.crypto_watchlist, clock=self.clock)
            if config.crypto_watchlist else None)
        # Forex is opt-in practice observation and never enters the stock strategy builder.
        self.forex_collector = (forex_collector or ForexWatchCollector(
            OandaPracticeClient(config), pairs=config.forex_watchlist, clock=self.clock)
            if config.forex_watchlist else None)
        self.session = PaperSession(config, self.provider, journal=self.journal,
                                    snapshot_builder=self.builder, clock=self.clock,
                                    execution_guard=(lambda: not self.stop_event.is_set()) if self.stock_execution else None)
        # Separate day-trade equity session (candle profile), serve-paper only.
        # Its own scope, blockers and reconciliation; it never blocks the
        # large-cap session or the crypto loop.
        self.day_trade = None
        if self.stock_execution and getattr(config, "day_trade_profile", "") == "small_cap_candle_patterns_iex_v1":
            from .day_trade_session import DayTradeEquitySession
            self.day_trade = DayTradeEquitySession(
                config, self.provider, self.session.journal, clock=self.clock,
                execution_guard=lambda: not self.stop_event.is_set())
            self.session.external_exposure = self.day_trade.exposure
        cloud = (config.supabase_url, config.supabase_service_role_key, config.owner_user_id)
        self.publisher = publisher if publisher is not None else (Publisher(config, clock=self.clock) if all(cloud) else None)
        if any(cloud) and not all(cloud):
            raise ObservationError("incomplete_cloud_worker_configuration")
        paper_run_hooks.attach_logger(self)
        self.fetch_activities = activity_fetcher or fetch_activities
        self._cycle_lock, self._state_lock = threading.Lock(), threading.Lock()
        self._active = False
        self._latest = None
        self._account_id = None
        self._baseline_cache = None
        self.paper_account = None
        self._loop_error = None
        self._journal_degraded = False

    def __enter__(self):
        if self._active or self.stop_event.is_set():
            raise ObservationError("observation_instance_cannot_be_reentered")
        self.session.__enter__()
        try:
            binding = self.journal.event_by_key("observation:account-binding")
            if binding and (binding["payload"].get("environment") != "paper"
                    or binding["payload"].get("owner_user_id") != (self.config.owner_user_id or None)):
                raise ObservationError("paper_account_or_owner_binding_mismatch")
            try:
                self.journal.append("paper-observation", "observation_started",
                    {"instance_id": self.instance_id, "mode": "paper", "execution_enabled": False},
                    key="observe-start:" + self.instance_id, timestamp=self.clock().isoformat())
            except sqlite3.OperationalError as exc:
                # A full disk used to exit 1 here. Keep the process up and do not
                # delete the journal. The stored rows stay where they are.
                self.journal.note_write_error(exc)
                self._journal_degraded = True
            else:
                self._journal_degraded = False
            paper_run_hooks.start_session(self)
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
                        halted=bool(self.journal.kill_switch().get("enabled")),
                        kill_switch=control_report(self.journal), paper_config=config_report(self.config))
                except Exception:
                    # No stale evidence is refreshed; the last cloud heartbeat expires.
                    pass
            paper_run_hooks.close_session(self, status="stopped")
            try:
                self.journal.append("paper-observation", "observation_stopped",
                    {"instance_id": self.instance_id, "mode": "paper", "execution_enabled": False},
                    key="observe-stop:" + self.instance_id, timestamp=self.clock().isoformat())
            except sqlite3.OperationalError:
                # Never mask the original failure (e.g. volume full) with a second traceback.
                pass
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
        number = account.get("account_number") if isinstance(account, dict) else None
        number = number if isinstance(number, str) and _ACCOUNT_NUMBER.fullmatch(number) else None
        if self.paper_account is None or self.paper_account.get("account_id") != identity:
            self.paper_account = {"account_id": identity, "account_number": number, "environment": "paper"}
            key = "observation:account-identity:" + identity + ":" + str(number)
            if not self.journal.event_by_key(key):
                self.journal.append("paper-observation", "observation_account_identity", dict(self.paper_account),
                                    key=key, timestamp=self.clock().isoformat())
            print(f"paper account bound: id={identity} number={number}", flush=True)
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
        elif self.config.accounting_baseline_auto and self._baseline_cache is None:
            self._baseline_cache = auto_opening_baseline(self.config, self.provider,
                                                         account_id=account_id, batch=imported)
        if self._baseline_cache is not None:
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

    def _resumes(self, *, broker_verified, halt_seen):
        """Authenticated paper resume requests; absent channel means no change."""
        process = getattr(self.publisher, "process_resumes", None)
        if process is None:
            return []
        now = self.clock()

        def precheck(created_at):
            return resume_refusal(self.config, self.journal, broker_verified=broker_verified,
                                  halt_seen=halt_seen, created_at=created_at, now=now)
        try:
            return [{"id": ident, "outcome": outcome} for ident, outcome in process(self.journal, precheck)]
        except Exception:
            return [{"id": None, "outcome": "resume_channel_unavailable"}]

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

    def _maybe_crypto_paper(self, watch):
        """Run one paper-crypto decision. A false flag leaves the cycle unchanged."""
        if getattr(self.config, "crypto_paper_execution_enabled", False) is not True:
            return None
        try:
            from .crypto_paper_loop import CryptoPaperLoop
            if getattr(self, "_crypto_paper_loop", None) is None:
                # AI_ADVISOR_MODE=paper_execute (reviewed in the hash) swaps in
                # the Gemini-driven loop; it submits through the same client.
                from .ai_paper_trader import build_ai_crypto_loop
                transport = getattr(self, "crypto_order_transport", None)
                self._crypto_paper_loop = build_ai_crypto_loop(
                    self.config, journal=self.journal, clock=self.clock, transport=transport,
                ) or CryptoPaperLoop(
                    self.config, journal=self.journal, clock=self.clock, transport=transport)
            return self._crypto_paper_loop.cycle(watch)
        except Exception:
            return {"enabled": True, "orders_submitted": 0,
                    "skipped": "crypto_paper_cycle_failed", "decisions": [],
                    "host": "paper-api.alpaca.markets", "live_execution_supported": False}

    def _forex_state(self, report, *, now):
        if report is None or not self.config.forex_watchlist:
            return None
        try:
            return sanitize_forex_watch(report, allowed_pairs=self.config.forex_watchlist, now=now)
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
            result = {"schema_version": "1.0", "mode": "paper",
                "runtime": "serve_paper" if self.stock_execution else "observation_only",
                "instance_id": self.instance_id, "cycle_id": cycle_id, "started_at": started.isoformat(),
                "provenance": "provider_observations", "orders_submitted": 0, "execution_enabled": False,
                "broker": {"verified": False, "checked_at": None},
                "data_feed": {"verified": False, "feed": self.config.data_feed, "as_of": None, "received_at": None},
                "market_watch": None, "price_history": None,
                "accounting": {"complete": False, "realized_pnl": None, "fees": None},
                "session": None,
                "blockers": [] if self.stock_execution else ["observation_runtime_cannot_execute_orders"],
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
                if self.forex_collector is not None and not self.stop_event.is_set():
                    # Separate GET-only practice pricing; forex failure cannot fail stock reads.
                    try:
                        result["forex_watch"] = self.forex_collector.collect()
                    except (RuntimeError, ValueError, TypeError, KeyError, OSError, ArithmeticError):
                        result["forex_watch"] = None
                        result["blockers"].append("forex_watch_unavailable")
                stage = "activity_accounting"
                ledger, receipt = self._accounting(account_id, cycle_id, self.clock())
                result["accounting"] = {k: deepcopy(v) for k, v in ledger.items()
                    if k not in {"open_lots", "matches", "revisions", "coverage", "evidence"}}
                result["accounting_import"] = receipt
                result["blockers"].extend(ledger.get("blockers", []))
                stage = "session_evaluation"
                # serve-observe stays execute=False. The read-only provider is a
                # second boundary. No config value can arm that command.
                # serve-paper uses the same session and may execute only when
                # the shared paper gates are open.
                execute = False
                session_ledger = ledger
                if self.stock_execution:
                    from .paper_runtime import evaluate_paper_gates, ledger_ready, production_session_ledger
                    reason, active = evaluate_paper_gates(
                        self.config, self.provider, self.journal,
                        broker_url=self.broker_url, broker_url_error=self.broker_url_error)
                    execute = bool(active)
                    if active:
                        candidate = ledger if ledger_ready(ledger) else None
                        if candidate is None and self._session_ledger_factory is not None:
                            candidate = self._session_ledger_factory()
                        if candidate is None or not ledger_ready(candidate):
                            produced = production_session_ledger(self.config, self.provider, self.clock)
                            if ledger_ready(produced):
                                candidate = produced
                        if not ledger_ready(candidate):
                            reason, active, execute = "session_ledger_incomplete", False, False
                        else:
                            session_ledger = candidate
                    self.execution_reason = reason
                    self.paper_execution_active = bool(active and execute)
                session = self.session.cycle(snapshots=snapshots, session_ledger=session_ledger, execute=execute)
                result["session"] = session
                if self.day_trade is not None:
                    try:
                        result["day_trade"] = self.day_trade.cycle(
                            execute=bool(execute), session_ledger=session_ledger if ledger_ready(session_ledger) else None)
                    except Exception:
                        result["day_trade"] = {"profile": self.config.day_trade_profile, "active": False,
                                               "errors": ["day_trade_cycle_failed"]}
                if self.stock_execution:
                    if execute and session.get("execute_requested") is not True:
                        self.execution_reason = "paper_cycle_failed"
                        self.paper_execution_active = False
                    self.orders_submitted = self._acknowledged_stock_orders()
                    result["execution_enabled"] = bool(self.paper_execution_active)
                    result["orders_submitted"] = self.orders_submitted
                    result["execution_reason"] = self.execution_reason
                    result["paper_execution_active"] = bool(self.paper_execution_active)
                    if not self.paper_execution_active and self.execution_reason:
                        result["blockers"].append(self.execution_reason)
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
                halts_before = len(result["acknowledged_commands"])
                result["acknowledged_commands"].extend(self._commands())
                result["resume_requests"] = self._resumes(
                    broker_verified=result["broker"]["verified"] is True,
                    halt_seen=len(result["acknowledged_commands"]) > 0 or halts_before > 0)
            except (RuntimeError, ValueError, TypeError, KeyError, OSError, ArithmeticError):
                result["error_code"] = stage + "_failed"
                result["blockers"].append(result["error_code"])
                if self.stock_execution:
                    self.paper_execution_active = False
                    self.execution_reason = result["error_code"]
                    result["execution_reason"] = self.execution_reason
                    result["paper_execution_active"] = False
                if stage == "paper_account_check":
                    result["broker"] = {"verified": False, "checked_at": None}
                if stage == "command_dispatch":
                    self.journal.set_kill_switch(True, "cloud_command_dispatch_failed", request_id="dispatcher:" + cycle_id)
            result["halted"] = bool(self.journal.kill_switch().get("enabled"))
            result["kill_switch"] = control_report(self.journal)
            result["paper_config"] = config_report(self.config)
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
            if "forex_watch" in result:
                result["forex_watch"] = self._forex_state(result["forex_watch"], now=self.clock())
            # One crypto pass per cycle, on this journal. The standalone
            # crypto_paper_loop timer is not started. Stock orders, when
            # serve-paper armed them, already used the same kill latch.
            crypto_paper = self._maybe_crypto_paper(
                result.get("crypto_watch") if "crypto_watch" in result else None)
            if crypto_paper is not None:
                result["crypto_paper"] = crypto_paper
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
                    if "forex_watch" in result:
                        publish_args["forex_watch"] = result["forex_watch"]
                    publish_args["kill_switch"] = result["kill_switch"]
                    publish_args["paper_config"] = result["paper_config"]
                    self.publisher.publish_worker_state(**publish_args)
                    result["cloud_state_published"] = True
                except Exception:
                    result["cloud_state_published"] = False
                    result["blockers"].append("cloud_state_publication_failed")
            else:
                result["cloud_state_published"] = False
                result["blockers"].append("cloud_worker_not_configured")
            paper_run_hooks.record_cycle(self, result)
            # The rich cycle stays in memory for /api/state. SQLite gets a small
            # sampled record so quotes are not what fills the disk.
            record = journal_observation_record(result)
            record["price_history"] = price_history_summary(result.get("price_history"))
            if "forex_watch" in result:
                record["forex_watch"] = forex_watch_summary(result.get("forex_watch"))
            self.journal.append_sampled("paper-observe-" + started.astimezone(NY).date().isoformat(),
                "paper_observation_cycle", record, key="observe-cycle:" + cycle_id,
                timestamp=result["completed_at"],
                signature=canonical_json([record["error_code"], record["blockers"], record["halted"],
                                          record["broker_verified"], record["data_verified"]]))
            if not self.journal._low_disk():
                self.journal.checkpoint_wal()
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
        if latest and latest.get("forex_watch") is not None:
            latest["forex_watch"] = self._forex_state(latest["forex_watch"], now=now)
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
                "paper_execution_running": bool(self.stock_execution and self.execution_status()["paper_execution_active"]),
                "latest_cycle": latest}

    def _acknowledged_stock_orders(self):
        try:
            return len(self.journal.events(event_type="paper_order_acknowledged"))
        except Exception:
            return self.orders_submitted

    def execution_status(self):
        """Health fields. serve-observe stays inactive even if the flag env is true."""
        if not self.stock_execution:
            return {
                "paper_trading_enabled": bool(self.config.paper_enabled),
                "paper_execution_active": False,
                "execution_reason": "observation_runtime_cannot_execute_orders",
                "live_execution_supported": False,
                "live_execution_locked": True,
                "orders_submitted": 0,
                "paper_account": dict(self.paper_account) if self.paper_account else None,
            }
        orders = self._acknowledged_stock_orders()
        self.orders_submitted = orders
        status = {
            "paper_trading_enabled": bool(self.config.paper_enabled),
            "paper_execution_active": bool(self._active and self.paper_execution_active and not self.stop_event.is_set()),
            "execution_reason": self.execution_reason,
            "live_execution_supported": False,
            "live_execution_locked": True,
            "orders_submitted": orders,
            "paper_account": dict(self.paper_account) if self.paper_account else None,
        }
        if self.day_trade is not None:
            last = self.day_trade.status()
            status["day_trade_session"] = {
                "profile": last.get("profile"), "active": bool(last.get("active")),
                "blockers": list(last.get("blockers") or []), "errors": list(last.get("errors") or [])[:10],
                "orders_submitted": len(self.day_trade.journal.events(event_type="day_trade_order_acknowledged")),
            }
        return status

    def run(self, *, max_cycles=None, elapsed_clock=time.monotonic):
        if not self._active:
            raise ObservationError("observation_scheduler_requires_active_lease")
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
            raise ValueError("max_cycles must be a positive integer")
        completed = 0
        while not self.stop_event.is_set() and (max_cycles is None or completed < max_cycles):
            began = elapsed_clock()
            try:
                self.cycle()
            except (sqlite3.Error, ObservationError, OSError) as exc:
                # A persistent journal fault must not exit the long-running
                # process. Bounded one-shot runs still surface the error.
                self._loop_error = "journal_volume_full_grow_the_disk_or_free_space" if (
                    isinstance(exc, sqlite3.Error) and self.journal._is_full_volume_error(exc)
                ) else (str(exc) if isinstance(exc, ObservationError) else "observation_cycle_failed")
                if max_cycles is not None:
                    raise
                self.stop_event.wait(max(1.0, self.config.poll_seconds))
                continue
            completed += 1
            if max_cycles is not None and completed >= max_cycles:
                break
            # One synchronous cycle at a time, no catch-up burst after an outage.
            delay = max(1.0, self.config.poll_seconds - (elapsed_clock() - began))
            self.stop_event.wait(delay)
        return completed
