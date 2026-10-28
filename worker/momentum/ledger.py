"""Durable, observation-only FIFO accounting with explicit evidence gates.

This module has no broker client and cannot place an order. Provider adapters
must normalize records and state what their bounded import actually proves.
Missing executions, opening inventory, or costs never become assumed zeroes.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo


NY = ZoneInfo("America/New_York")
UTC = timezone.utc
ACTUAL_COSTS = "actual"
PAPER_ZERO_FEES = "paper_zero_fee_simulation"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,255}\Z")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,31}\Z")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z")
_TIMESTAMP = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})\Z")
_MAX_BATCH = 10000
_MAX_PROJECTION = 100000
_DECIMAL_CONTEXT = Context(prec=96, rounding=ROUND_HALF_EVEN)


class LedgerError(ValueError):
    """Invalid or unsupported accounting evidence."""


class LedgerConflict(LedgerError):
    """An immutable source revision or batch identity changed its meaning."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _identity(value, name):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise LedgerError(f"Invalid {name}")
    return value


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise LedgerError("Expected an ISO session date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise LedgerError("Invalid session date") from exc


def _time(value):
    try:
        if not isinstance(value, datetime) and (not isinstance(value, str) or not _TIMESTAMP.fullmatch(value)):
            raise ValueError("invalid timestamp")
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("timezone required")
        return stamp.astimezone(UTC)
    except (TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise LedgerError("Expected a timezone-qualified timestamp") from exc


def _stamp(value):
    stamp = _time(value)
    if isinstance(value, datetime):
        fraction = f"{stamp.microsecond:06d}000"
    else:
        fraction = (_TIMESTAMP.fullmatch(value).group(2) or "").ljust(9, "0")
    return stamp.replace(microsecond=0).isoformat(timespec="seconds")[:19] + "." + fraction + "Z"


def _instant(value):
    """Integer nanoseconds; never use truncated datetime as a FIFO tie key."""
    stamp = _stamp(value)
    moment = _time(stamp).replace(microsecond=0) - datetime(1970, 1, 1, tzinfo=UTC)
    return (moment.days * 86400 + moment.seconds) * 1000000000 + int(stamp[20:29])


def _text(value, name, *, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise LedgerError(f"Invalid {name}")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise LedgerError(f"Invalid {name}")
    return value


def _decimal(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise LedgerError(f"{name} must be an exact decimal string or integer")
    encoded = str(value)
    if len(encoded) > 80 or not _NUMBER.fullmatch(encoded):
        raise LedgerError(f"Invalid {name}")
    try:
        number = Decimal(encoded)
    except InvalidOperation as exc:
        raise LedgerError(f"Invalid {name}") from exc
    if (not number.is_finite() or number.copy_abs() > Decimal("1e18")
            or number.as_tuple().exponent < -18
            or positive and number <= 0 or nonnegative and number < 0):
        raise LedgerError(f"Out-of-range {name}")
    return number


def _amount(number):
    if not number:
        return "0"
    encoded = format(number, "f")
    return encoded.rstrip("0").rstrip(".") if "." in encoded else encoded


def _only(mapping, permitted, required, name):
    if not isinstance(mapping, dict) or set(mapping) - permitted or required - set(mapping):
        raise LedgerError(f"Invalid {name} fields")


def _lot(raw):
    fields = {"lot_id", "symbol", "quantity", "price", "opened_at", "execution_id"}
    _only(raw, fields, fields - {"execution_id"}, "opening lot")
    if not isinstance(raw["symbol"], str) or not _SYMBOL.fullmatch(raw["symbol"]):
        raise LedgerError("Invalid opening lot symbol")
    value = {"lot_id": _identity(raw["lot_id"], "lot identity"), "symbol": raw["symbol"],
             "quantity": _amount(_decimal(raw["quantity"], "lot quantity", positive=True)),
             "price": _amount(_decimal(raw["price"], "lot price", positive=True)),
             "opened_at": _stamp(raw["opened_at"])}
    if raw.get("execution_id") is not None:
        value["execution_id"] = _identity(raw["execution_id"], "opening execution identity")
    return value


def _record(raw):
    base = {"source_id", "revision", "kind", "status"}
    fields = base | {"effective_at", "effective_date", "date_basis", "symbol", "side",
                     "quantity", "price", "order_id", "amount", "execution_id", "source_fingerprint"}
    _only(raw, fields, base, "source record")
    if type(raw["revision"]) is not int or not 1 <= raw["revision"] <= 2147483647:
        raise LedgerError("Revision must be a positive provider revision")
    if not isinstance(raw["kind"], str) or not isinstance(raw["status"], str) or raw["kind"] not in {"fill", "fee"} or raw["status"] not in {"active", "canceled"}:
        raise LedgerError("Unsupported record kind or status")
    value = {"source_id": _identity(raw["source_id"], "source identity"),
             "revision": raw["revision"], "kind": raw["kind"], "status": raw["status"]}
    if "source_fingerprint" in raw:
        if not isinstance(raw["source_fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", raw["source_fingerprint"]):
            raise LedgerError("Invalid source fingerprint")
        value["source_fingerprint"] = raw["source_fingerprint"]
    if raw["kind"] == "fill":
        if set(raw) & {"effective_date", "date_basis", "amount", "execution_id"}:
            raise LedgerError("Invalid fill fields")
        value["effective_at"] = _stamp(raw.get("effective_at"))
        if raw["status"] == "active":
            if not isinstance(raw.get("side"), str) or raw["side"] not in {"buy", "sell"} or not isinstance(raw.get("symbol"), str) or not _SYMBOL.fullmatch(raw["symbol"]):
                raise LedgerError("Unsupported fill side or symbol")
            value.update(symbol=raw["symbol"], side=raw["side"],
                         quantity=_amount(_decimal(raw.get("quantity"), "fill quantity", positive=True)),
                         price=_amount(_decimal(raw.get("price"), "fill price", positive=True)))
            if raw.get("order_id") is not None:
                value["order_id"] = _identity(raw["order_id"], "order identity")
        elif set(raw) & {"symbol", "side", "quantity", "price", "order_id"}:
            raise LedgerError("A cancellation contains identity, revision and original time only")
    else:
        if set(raw) & {"symbol", "side", "quantity", "price", "order_id"}:
            raise LedgerError("Invalid fee fields")
        if "effective_at" in raw:
            if set(raw) & {"effective_date", "date_basis"}:
                raise LedgerError("A fee needs one explicit time/date basis")
            value["effective_at"] = _stamp(raw["effective_at"])
        else:
            value["effective_date"] = _day(raw.get("effective_date")).isoformat()
            if raw.get("date_basis") != "provider_activity_or_settlement_date":
                raise LedgerError("Unsupported fee date basis")
            value["date_basis"] = raw["date_basis"]
        if raw["status"] == "active":
            value["amount"] = _amount(_decimal(raw.get("amount"), "signed fee cost"))
            if raw.get("execution_id") is not None:
                value["execution_id"] = _identity(raw["execution_id"], "fee execution identity")
        elif set(raw) & {"amount", "execution_id"}:
            raise LedgerError("A canceled fee contains no amount or allocation")
    return value


@dataclass(frozen=True)
class LedgerSnapshot:
    """A snapshot is evidence, never an instruction to enable execution."""
    _encoded: str

    def __init__(self, payload):
        object.__setattr__(self, "_encoded", _json(payload))

    def to_dict(self):
        return json.loads(self._encoded)

    @property
    def payload(self):
        return self.to_dict()

    @property
    def complete(self):
        return self.to_dict()["complete"]

    @property
    def blockers(self):
        return tuple(self.to_dict()["blockers"])

    @property
    def projection_id(self):
        return self.to_dict()["projection_id"]


class AccountingLedger:
    """Append-only source evidence in the same SQLite file as ``Journal``.

    An instance binds one exact account, paper environment and source namespace.
    Source revisions are not receipt sequence numbers. Unversioned adapters use
    revision 1 and must fail on a changed payload, rather than fabricate order.
    """

    def __init__(self, db_path, *, account_id, environment="paper", source="alpaca_account_activities"):
        if environment != "paper":
            raise LedgerError("This ledger supports paper observations only")
        self.path = str(db_path)
        if self.path == ":memory:":
            raise LedgerError("Use a durable SQLite path")
        self.account_id = _identity(account_id, "account identity")
        self.environment = environment
        self.source = _identity(source, "source namespace")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS accounting_revisions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL, environment TEXT NOT NULL, source TEXT NOT NULL,
                    source_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    payload TEXT NOT NULL, payload_hash TEXT NOT NULL, recorded_at TEXT NOT NULL,
                    UNIQUE(account_id, environment, source, source_id, revision)
                );
                CREATE TABLE IF NOT EXISTS accounting_batches (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL, environment TEXT NOT NULL, source TEXT NOT NULL,
                    batch_id TEXT NOT NULL, payload_hash TEXT NOT NULL, coverage TEXT NOT NULL,
                    revision_seq INTEGER NOT NULL, recorded_at TEXT NOT NULL,
                    UNIQUE(account_id, environment, source, batch_id)
                );
                CREATE TABLE IF NOT EXISTS accounting_conflicts (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL, environment TEXT NOT NULL, source TEXT NOT NULL,
                    source_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    original_hash TEXT NOT NULL, conflicting_hash TEXT NOT NULL,
                    conflicting_payload TEXT NOT NULL, recorded_at TEXT NOT NULL,
                    UNIQUE(account_id, environment, source, source_id, revision, conflicting_hash)
                );
                CREATE TABLE IF NOT EXISTS accounting_batch_conflicts (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL, environment TEXT NOT NULL, source TEXT NOT NULL,
                    batch_id TEXT NOT NULL, original_hash TEXT NOT NULL,
                    conflicting_hash TEXT NOT NULL, recorded_at TEXT NOT NULL,
                    UNIQUE(account_id, environment, source, batch_id, conflicting_hash)
                );
                CREATE INDEX IF NOT EXISTS accounting_latest
                    ON accounting_revisions(account_id, environment, source, source_id, revision);
                CREATE TRIGGER IF NOT EXISTS accounting_revisions_no_update BEFORE UPDATE ON accounting_revisions
                    BEGIN SELECT RAISE(ABORT, 'accounting revisions are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS accounting_revisions_no_delete BEFORE DELETE ON accounting_revisions
                    BEGIN SELECT RAISE(ABORT, 'accounting revisions are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS accounting_batches_no_update BEFORE UPDATE ON accounting_batches
                    BEGIN SELECT RAISE(ABORT, 'accounting batches are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS accounting_batches_no_delete BEFORE DELETE ON accounting_batches
                    BEGIN SELECT RAISE(ABORT, 'accounting batches are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS accounting_conflicts_no_update BEFORE UPDATE ON accounting_conflicts
                    BEGIN SELECT RAISE(ABORT, 'accounting conflicts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS accounting_conflicts_no_delete BEFORE DELETE ON accounting_conflicts
                    BEGIN SELECT RAISE(ABORT, 'accounting conflicts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS accounting_batch_conflicts_no_update BEFORE UPDATE ON accounting_batch_conflicts
                    BEGIN SELECT RAISE(ABORT, 'accounting batch conflicts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS accounting_batch_conflicts_no_delete BEFORE DELETE ON accounting_batch_conflicts
                    BEGIN SELECT RAISE(ABORT, 'accounting batch conflicts are append-only'); END;
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _scope(self):
        return self.account_id, self.environment, self.source

    def _coverage(self, raw):
        required = {"account_id", "environment", "source", "window_start", "window_end", "as_of",
                    "executions_complete", "fees_complete", "opening_lots_complete", "opening_lots",
                    "evidence", "blockers"}
        _only(raw, required | {"opening_evidence"}, required, "coverage")
        if (raw["account_id"], raw["environment"], raw["source"]) != self._scope():
            raise LedgerError("Coverage does not match the exact account, environment and source")
        value = {k: raw[k] for k in ("account_id", "environment", "source")}
        for key in ("window_start", "window_end", "as_of"):
            value[key] = _stamp(raw[key])
        if not _instant(value["window_start"]) < _instant(value["window_end"]) <= _instant(value["as_of"]):
            raise LedgerError("Coverage requires window_start < window_end <= as_of")
        for key in ("executions_complete", "fees_complete", "opening_lots_complete"):
            if type(raw[key]) is not bool:
                raise LedgerError("Coverage completeness flags must be explicit booleans")
            value[key] = raw[key]
        if not isinstance(raw["opening_lots"], list) or len(raw["opening_lots"]) > _MAX_BATCH:
            raise LedgerError("Opening inventory must be a bounded lot list")
        value["opening_lots"] = sorted((_lot(row) for row in raw["opening_lots"]), key=lambda row: row["lot_id"])
        if len({row["lot_id"] for row in value["opening_lots"]}) != len(value["opening_lots"]):
            raise LedgerError("Duplicate opening lot identity")
        if any(_instant(row["opened_at"]) >= _instant(value["window_start"]) for row in value["opening_lots"]):
            raise LedgerError("Opening lots must predate the exact coverage window")
        value["opening_evidence"] = None
        if raw.get("opening_evidence") is not None:
            value["opening_evidence"] = _text(raw["opening_evidence"], "opening evidence")
        if value["opening_lots_complete"] and not value["opening_evidence"]:
            raise LedgerError("A complete opening baseline requires explicit evidence, including a flat baseline")
        evidence = [raw["evidence"]] if isinstance(raw["evidence"], str) else raw["evidence"]
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 100:
            raise LedgerError("Coverage requires bounded evidence references")
        value["evidence"] = [_text(row, "coverage evidence") for row in evidence]
        if not isinstance(raw["blockers"], list) or len(raw["blockers"]) > 100:
            raise LedgerError("Coverage blockers must be a bounded list")
        value["blockers"] = sorted({_identity(row, "coverage blocker") for row in raw["blockers"]})
        return value

    def ingest_batch(self, records, *, batch_id, coverage):
        """Append one fully normalized import and coverage atomically.

        Duplicate source revisions are harmless. Conflicting revisions are
        durably recorded before ``LedgerConflict`` is raised; the remainder of
        that batch is not accepted. Only an explicit higher provider revision
        resolves such a conflict. Retrying with a new receipt ID does not.
        """
        batch_id = _identity(batch_id, "batch identity")
        if not isinstance(records, (list, tuple)) or len(records) > _MAX_BATCH:
            raise LedgerError("Source batch exceeds the bounded record limit")
        normalized = sorted((_record(row) for row in records), key=lambda row: (row["source_id"], row["revision"]))
        coverage = self._coverage(coverage)
        unique, conflicts = {}, []
        for row in normalized:
            key = row["source_id"], row["revision"]
            if key in unique and unique[key] != row:
                conflicts.append((row, _digest(unique[key])))
            else:
                unique[key] = row
        normalized = list(unique.values())
        fingerprint = _digest({"records": normalized, "coverage": coverage})
        stamp = _stamp(datetime.now(UTC))
        batch_conflict = False
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM accounting_batches WHERE account_id=? AND environment=? AND source=? AND batch_id=?",
                             (*self._scope(), batch_id)).fetchone()
            if old:
                if old["payload_hash"] != fingerprint:
                    # A batch identity is not an activity revision; do not let
                    # a changed receipt rewrite its coverage or audit meaning.
                    db.execute("INSERT OR IGNORE INTO accounting_batch_conflicts(account_id,environment,source,batch_id,original_hash,conflicting_hash,recorded_at) VALUES(?,?,?,?,?,?,?)",
                               (*self._scope(), batch_id, old["payload_hash"], fingerprint, stamp))
                    batch_conflict = True
                elif not conflicts:
                    return {"batch_id": batch_id, "inserted": 0, "duplicates": len(records),
                            "duplicate_batch": True, "coverage_id": old["payload_hash"]}
            for row in normalized:
                encoded = _json(row)
                previous = db.execute("SELECT * FROM accounting_revisions WHERE account_id=? AND environment=? AND source=? AND source_id=? AND revision=?",
                                      (*self._scope(), row["source_id"], row["revision"])).fetchone()
                if previous and previous["payload"] != encoded:
                    conflicts.append((row, previous["payload_hash"]))
            if conflicts:
                for row, original_hash in conflicts:
                    db.execute("INSERT OR IGNORE INTO accounting_conflicts(account_id,environment,source,source_id,revision,original_hash,conflicting_hash,conflicting_payload,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
                               (*self._scope(), row["source_id"], row["revision"], original_hash, _digest(row), _json(row), stamp))
            elif not batch_conflict:
                inserted = 0
                for row in normalized:
                    result = db.execute("INSERT OR IGNORE INTO accounting_revisions(account_id,environment,source,source_id,revision,payload,payload_hash,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                                        (*self._scope(), row["source_id"], row["revision"], _json(row), _digest(row), stamp))
                    inserted += result.rowcount
                highwater = db.execute("SELECT COALESCE(MAX(seq),0) FROM accounting_revisions WHERE account_id=? AND environment=? AND source=?", self._scope()).fetchone()[0]
                db.execute("INSERT INTO accounting_batches(account_id,environment,source,batch_id,payload_hash,coverage,revision_seq,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                           (*self._scope(), batch_id, fingerprint, _json(coverage), highwater, stamp))
        if batch_conflict:
            raise LedgerConflict("Conflicting immutable batch identity; accounting audit requires review")
        if conflicts:
            raise LedgerConflict("Conflicting source revision; accounting is blocked until an explicit higher revision")
        return {"batch_id": batch_id, "inserted": inserted, "duplicates": len(records) - inserted,
                "duplicate_batch": False, "coverage_id": fingerprint}

    def revisions(self):
        """Normalized audit records only; no raw provider response or secrets."""
        with self._connect() as db:
            rows = db.execute("SELECT payload FROM accounting_revisions WHERE account_id=? AND environment=? AND source=? ORDER BY source_id,revision LIMIT ?",
                              (*self._scope(), _MAX_PROJECTION + 1)).fetchall()
        if len(rows) > _MAX_PROJECTION:
            raise LedgerError("Accounting audit limit exceeded")
        return [json.loads(row["payload"]) for row in rows]

    def snapshot(self, *, session_date, now, cost_policy=ACTUAL_COSTS, max_age_seconds=5):
        """Project FIFO gross P&L and separately evidenced session costs.

        ``now`` is evaluation time; it never refreshes the import watermark.
        The zero-fee policy is an explicitly named paper simulation, not a claim
        that actual provider fees are zero. Any other missing evidence remains
        a blocker under either policy.
        """
        day = _day(session_date)
        now = _stamp(now)
        if not isinstance(cost_policy, str) or cost_policy not in {ACTUAL_COSTS, PAPER_ZERO_FEES}:
            raise LedgerError("Choose actual or explicit paper_zero_fee_simulation costs")
        max_age = _decimal(max_age_seconds, "maximum evidence age", positive=True)
        if max_age > 5:
            raise LedgerError("Evidence freshness cannot exceed the five-second session gate")
        with self._connect() as db:
            db.execute("BEGIN")
            batch = db.execute("SELECT * FROM accounting_batches WHERE account_id=? AND environment=? AND source=? ORDER BY seq DESC LIMIT 1",
                               self._scope()).fetchone()
            rows = db.execute("SELECT * FROM accounting_revisions WHERE account_id=? AND environment=? AND source=? ORDER BY source_id,revision DESC LIMIT ?",
                              (*self._scope(), _MAX_PROJECTION + 1)).fetchall()
            conflicts = db.execute("SELECT source_id,revision,original_hash,conflicting_hash FROM accounting_conflicts WHERE account_id=? AND environment=? AND source=? ORDER BY source_id,revision,conflicting_hash LIMIT ?",
                                   (*self._scope(), _MAX_PROJECTION + 1)).fetchall()
            batch_conflicts = db.execute("SELECT batch_id,original_hash,conflicting_hash FROM accounting_batch_conflicts WHERE account_id=? AND environment=? AND source=? ORDER BY batch_id,conflicting_hash LIMIT ?",
                                         (*self._scope(), _MAX_PROJECTION + 1)).fetchall()
        blockers = []
        if day != _time(now).astimezone(NY).date():
            blockers.append("not_current_new_york_session")
        if len(rows) > _MAX_PROJECTION or len(conflicts) > _MAX_PROJECTION or len(batch_conflicts) > _MAX_PROJECTION:
            blockers.append("accounting_projection_limit_exceeded")
        latest, kinds = {}, {}
        for row in rows[:_MAX_PROJECTION]:
            decoded = json.loads(row["payload"])
            kinds.setdefault(row["source_id"], set()).add(decoded["kind"])
            if row["source_id"] not in latest:
                latest[row["source_id"]] = decoded
        if any(len(values) > 1 for values in kinds.values()):
            blockers.append("execution_identity_kind_changed")
        if batch_conflicts:
            blockers.append("immutable_batch_identity_conflict")
        unresolved = [dict(row) for row in conflicts[:_MAX_PROJECTION]
                      if latest.get(row["source_id"], {}).get("revision", 0) <= row["revision"]]
        if unresolved:
            blockers.append("unresolved_source_revision_conflict")
        coverage = json.loads(batch["coverage"]) if batch else None
        realized, observed_fees = Decimal(0), Decimal(0)
        lots, matches, fees_by_execution, unallocated = [], [], {}, []
        as_of = None
        execution_complete = False
        actual_costs_complete = False
        if coverage is None:
            blockers.append("coverage_missing")
        else:
            blockers.extend(coverage["blockers"])
            start_ns = _instant(coverage["window_start"])
            end_ns = _instant(coverage["window_end"])
            as_of = min((coverage["as_of"], coverage["window_end"]), key=_instant)
            age_ns = _instant(now) - _instant(as_of)
            with localcontext(_DECIMAL_CONTEXT):
                max_age_ns = max_age * Decimal(1000000000)
            if age_ns < 0:
                blockers.append("coverage_from_future")
            elif Decimal(age_ns) > max_age_ns:
                blockers.append("coverage_stale")
            if _instant(coverage["as_of"]) > _instant(now):
                blockers.append("import_watermark_from_future")
            midnight = datetime.combine(day, time.min, tzinfo=NY)
            if start_ns > _instant(midnight) or end_ns < _instant(midnight):
                blockers.append("current_session_window_not_covered")
            if not coverage["executions_complete"]:
                blockers.append("execution_coverage_incomplete")
            if not coverage["opening_lots_complete"]:
                blockers.append("opening_inventory_unproven")
            if not coverage["fees_complete"] and cost_policy == ACTUAL_COSTS:
                blockers.append("actual_fee_coverage_incomplete")
            active = [row for row in latest.values() if row["status"] == "active"]
            fills = []
            fee_records = []
            for row in active:
                if row["kind"] == "fill":
                    instant = _instant(row["effective_at"])
                    if instant > end_ns:
                        blockers.append("execution_after_coverage_window")
                    elif instant >= start_ns:
                        fills.append(row)
                    else:
                        # REST query bounds may describe creation time. A
                        # newly posted execution can have an older economic
                        # time and must not disappear behind a flat anchor.
                        represented = [lot for lot in coverage["opening_lots"]
                                       if lot.get("execution_id") == row["source_id"]]
                        with localcontext(_DECIMAL_CONTEXT):
                            quantity = sum((Decimal(lot["quantity"]) for lot in represented), Decimal(0))
                        if (row["side"] != "buy" or not represented or quantity > Decimal(row["quantity"])
                                or any(lot["symbol"] != row["symbol"] or lot["price"] != row["price"]
                                       or lot["opened_at"] != row["effective_at"] for lot in represented)):
                            blockers.append("pre_window_execution_requires_rebuilt_history")
                else:
                    fee_day = (_day(row["effective_date"]) if "effective_date" in row
                               else _time(row["effective_at"]).astimezone(NY).date())
                    if "effective_at" in row and _instant(row["effective_at"]) > end_ns:
                        blockers.append("fee_after_coverage_window")
                    if fee_day == day:
                        fee_records.append(row)
            for row in latest.values():
                if row["kind"] == "fill" and row["revision"] > 1 and _instant(row["effective_at"]) < start_ns:
                    # A historical correction may change the reviewed anchor.
                    # Move the replay boundary earlier or review a new anchor;
                    # current positions alone cannot establish the old basis.
                    blockers.append("pre_window_correction_requires_rebuilt_history")
            with localcontext(_DECIMAL_CONTEXT):
                observed_fees = sum((Decimal(row["amount"]) for row in fee_records), Decimal(0))
                for row in active:
                    if row["kind"] == "fee":
                        if row.get("execution_id"):
                            key = row["execution_id"]
                            fees_by_execution[key] = fees_by_execution.get(key, Decimal(0)) + Decimal(row["amount"])
                        else:
                            unallocated.append(row["source_id"])
                fills.sort(key=lambda row: (row["effective_at"], row["source_id"]))
                by_instant = {}
                for fill in fills:
                    key = fill["symbol"], fill["effective_at"]
                    by_instant.setdefault(key, []).append(fill)
                if any(len({row["side"] for row in group}) > 1 for group in by_instant.values()):
                    blockers.append("ambiguous_simultaneous_fill_order")
                if any(sum(row["side"] == "buy" for row in group) > 1 for group in by_instant.values()):
                    # Opaque execution IDs provide deterministic display order,
                    # not proof of economic FIFO order. Equal prices can still
                    # carry different fees, so they do not resolve this tie.
                    blockers.append("ambiguous_simultaneous_buy_order")
                opening_instants = {}
                for lot in coverage["opening_lots"]:
                    key = lot["symbol"], lot["opened_at"]
                    opening_instants[key] = opening_instants.get(key, 0) + 1
                if any(count > 1 for count in opening_instants.values()):
                    blockers.append("ambiguous_opening_lot_order")
                active_fill_ids = {row["source_id"] for row in fills}
                allocation_known = (coverage["fees_complete"] and not unallocated
                                    and set(fees_by_execution) <= active_fill_ids
                                    and not any(code in blockers for code in (
                                        "ambiguous_simultaneous_fill_order", "ambiguous_simultaneous_buy_order",
                                        "ambiguous_opening_lot_order")))
                realized, lots, matches, projection_blockers = self._fifo(
                    coverage["opening_lots"], fills, day, fees_by_execution, allocation_known)
                blockers.extend(projection_blockers)
            if observed_fees < 0 and cost_policy == ACTUAL_COSTS:
                blockers.append("net_fee_credit_incompatible_with_session_gate")
            execution_complete = (coverage["executions_complete"] and coverage["opening_lots_complete"]
                                  and not any(code in blockers for code in (
                                      "accounting_projection_limit_exceeded", "unresolved_source_revision_conflict", "immutable_batch_identity_conflict",
                                      "execution_after_coverage_window", "pre_window_correction_requires_rebuilt_history", "pre_window_execution_requires_rebuilt_history",
                                      "ambiguous_simultaneous_fill_order", "ambiguous_simultaneous_buy_order",
                                      "ambiguous_opening_lot_order", "sale_exceeds_known_long_inventory",
                                      "opening_execution_repeated_in_window", "execution_identity_kind_changed")))
            actual_costs_complete = (coverage["fees_complete"] and observed_fees >= 0
                                     and not any(code in blockers for code in (
                                         "accounting_projection_limit_exceeded", "unresolved_source_revision_conflict",
                                         "immutable_batch_identity_conflict", "execution_identity_kind_changed", "fee_after_coverage_window")))
        blockers = sorted(set(blockers))
        projected_costs = (Decimal(0) if cost_policy == PAPER_ZERO_FEES else
                           observed_fees if actual_costs_complete else None)
        with localcontext(_DECIMAL_CONTEXT):
            net_realized = _amount(realized - projected_costs) if execution_complete and projected_costs is not None else None
        identity = {"version": "fifo-long-v1", "account_id": self.account_id,
                    "environment": self.environment, "source": self.source,
                    "session_date": session_date, "cost_policy": cost_policy,
                    "coverage": coverage, "records": sorted(latest.values(), key=lambda row: row["source_id"]),
                    "conflicts": unresolved, "batch_conflicts": [dict(row) for row in batch_conflicts]}
        payload = {
            "session_date": session_date, "realized_pnl": _amount(realized) if execution_complete else None,
            "fees": _amount(projected_costs) if projected_costs is not None else None,
            "as_of": as_of, "source": "durable_fifo:" + self.source, "complete": not blockers,
            "blockers": blockers, "projection_id": _digest(identity), "account_id": self.account_id,
            "environment": self.environment, "execution_armed": False, "cost_policy": cost_policy,
            "cost_basis": "explicit_paper_zero_fee_simulation" if cost_policy == PAPER_ZERO_FEES else "provider_activity_evidence",
            "observed_realized_pnl": _amount(realized), "observed_actual_fees": _amount(observed_fees),
            "actual_costs_complete": actual_costs_complete, "execution_accounting_complete": execution_complete,
            "net_realized_pnl": net_realized,
            "open_lots": lots, "matches": matches,
            "observed_execution_fees": {key: _amount(value) for key, value in sorted(fees_by_execution.items())},
            "unallocated_fee_ids": sorted(unallocated),
            "coverage": coverage,
        }
        return LedgerSnapshot(payload)

    @staticmethod
    def _fifo(opening, fills, session_day, fees_by_execution, allocation_known):
        inventory, matches, blockers = {}, [], []
        realized = Decimal(0)
        opening_ids = {row["execution_id"] for row in opening if row.get("execution_id")}
        for row in sorted(opening, key=lambda item: (item["opened_at"], item["lot_id"])):
            inventory.setdefault(row["symbol"], []).append({
                "lot_id": row["lot_id"], "execution_id": row.get("execution_id"), "symbol": row["symbol"],
                "quantity": Decimal(row["quantity"]), "price": Decimal(row["price"]),
                "opened_at": row["opened_at"], "remaining_entry_fees": None, "origin": "reviewed_opening_baseline"})
        for fill in fills:
            quantity, price = Decimal(fill["quantity"]), Decimal(fill["price"])
            symbol = fill["symbol"]
            if fill["source_id"] in opening_ids:
                blockers.append("opening_execution_repeated_in_window")
                continue
            symbol_lots = inventory.setdefault(symbol, [])
            fill_fee = fees_by_execution.get(fill["source_id"], Decimal(0)) if allocation_known else None
            if fill["side"] == "buy":
                symbol_lots.append({"lot_id": "fill:" + fill["source_id"], "execution_id": fill["source_id"],
                                    "symbol": symbol, "quantity": quantity, "price": price,
                                    "opened_at": fill["effective_at"], "remaining_entry_fees": fill_fee,
                                    "origin": "execution_activity"})
                continue
            if sum((lot["quantity"] for lot in symbol_lots), Decimal(0)) < quantity:
                blockers.append("sale_exceeds_known_long_inventory")
                # Do not repair a short sale with a later buy or fabricate an
                # entry price. Continue only for an explicitly partial report.
                continue
            while quantity:
                lot = symbol_lots[0]
                part = min(quantity, lot["quantity"])
                gross = (price - lot["price"]) * part
                entry_fee = (lot["remaining_entry_fees"] * part / lot["quantity"]
                             if lot["remaining_entry_fees"] is not None else None)
                exit_fee = fill_fee * part / quantity if fill_fee is not None else None
                fees = entry_fee + exit_fee if entry_fee is not None and exit_fee is not None else None
                match = {"entry_lot_id": lot["lot_id"], "entry_execution_id": lot["execution_id"],
                         "exit_execution_id": fill["source_id"], "symbol": symbol, "quantity": _amount(part),
                         "entry_price": _amount(lot["price"]), "exit_price": _amount(price),
                         "opened_at": lot["opened_at"], "closed_at": fill["effective_at"],
                         "gross_pnl": _amount(gross), "fees": _amount(fees) if fees is not None else None,
                         "net_pnl": _amount(gross - fees) if fees is not None else None,
                         "fee_allocation_complete": fees is not None}
                match["match_id"] = _digest({key: match[key] for key in
                                             ("entry_lot_id", "exit_execution_id", "quantity", "entry_price", "exit_price")})
                matches.append(match)
                if _time(fill["effective_at"]).astimezone(NY).date() == session_day:
                    realized += gross
                quantity -= part
                lot["quantity"] -= part
                if entry_fee is not None:
                    lot["remaining_entry_fees"] -= entry_fee
                if exit_fee is not None:
                    fill_fee -= exit_fee
                if not lot["quantity"]:
                    symbol_lots.pop(0)
        open_lots = [{**lot, "quantity": _amount(lot["quantity"]), "price": _amount(lot["price"]),
                      "cost_basis": _amount(lot["quantity"] * lot["price"]),
                      "remaining_entry_fees": _amount(lot["remaining_entry_fees"]) if lot["remaining_entry_fees"] is not None else None}
                     for symbol in sorted(inventory) for lot in inventory[symbol] if lot["quantity"]]
        return realized, open_lots, matches, blockers
