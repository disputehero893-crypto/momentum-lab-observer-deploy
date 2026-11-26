"""Fail-closed completeness certification for an Alpaca paper activity import.

PAPER TRADING ONLY. ``fetch_activities`` always reports
``executions_complete=False`` and ``fees_complete=False``: one REST scan proves
nothing on its own. This module may raise both flags to True only when every
check below holds, and returns them False with fixed reason codes on any doubt
(including any exception):

1. The import is from the paper environment, for the expected account, with
   paginated pages exhausted and a terminal empty page observed (verified page
   end), and the window bounds equal the requested bounds.
2. The import carries no coverage blockers and no unsupported or correction
   observation. Reviewed crypto rows segregated by
   ``activities.NON_EQUITY_SEGREGATION_RULE`` are allowed: they are explicitly
   excluded from the equity ledger and owned by the crypto loop's accounting.
3. A second, independent full scan over the identical fixed bounds returns the
   identical ordered (activity id, content fingerprint) sequence, so no FILL,
   FEE or other activity appeared, vanished or mutated between page walks.
4. Broker positions are readable, and for every US-equity symbol the net
   quantity implied by opening lots plus all equity fills equals the broker
   position. Reviewed crypto positions are excluded from this check; any other
   non-equity or malformed position is doubt.

"Fees complete" means every fee activity (FEE type) Alpaca reports for the
creation-time window was captured, stable across both scans. Alpaca paper does
not simulate regulatory fees, so this is completeness of the provider's fee
stream, not a model of live costs. A fee or correction posted after the window
is picked up by the next cycle's full re-scan from ACCOUNTING_HISTORY_START.

No credentials, raw payloads or free-text descriptions are logged or returned.
The provider only needs ``activity_page`` and ``positions``, both GET-only.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import logging

from .activities import SEGREGATED_KIND, activity_request_params, crypto_paper_pair, fetch_activities
from .config import SYMBOL

LOG = logging.getLogger("momentum.accounting")
CERTIFIER = "alpaca_paper_rescan_position_reconciliation_v1"


@dataclass(frozen=True)
class CompletenessCertification:
    executions_complete: bool
    fees_complete: bool
    reasons: tuple[str, ...]
    evidence: tuple[str, ...]
    segregated_non_equity_count: int = 0
    rescan_matched: bool = False
    positions_matched: bool = False

    @property
    def complete(self):
        return self.executions_complete and self.fees_complete

    def to_dict(self):
        return {"certifier": CERTIFIER, "executions_complete": self.executions_complete,
                "fees_complete": self.fees_complete, "reasons": list(self.reasons),
                "evidence": list(self.evidence),
                "segregated_non_equity_count": self.segregated_non_equity_count,
                "rescan_matched": self.rescan_matched, "positions_matched": self.positions_matched}


def _sequence(batch):
    return [(row.source_id, row.source_fingerprint) for row in batch.observations]


def _equity_net(records, opening_lots):
    net = {}
    for lot in opening_lots:
        net[lot["symbol"]] = net.get(lot["symbol"], Decimal(0)) + Decimal(str(lot["quantity"]))
    for record in records:
        if record.get("kind") != "fill" or record.get("status", "active") != "active":
            continue
        sign = Decimal(1) if record["side"] == "buy" else Decimal(-1)
        net[record["symbol"]] = net.get(record["symbol"], Decimal(0)) + sign * Decimal(record["quantity"])
    return {symbol: qty for symbol, qty in net.items() if qty != 0}


def _broker_equity(positions):
    """Return ({symbol: qty}, reasons) for us_equity rows; crypto pairs excluded."""
    if not isinstance(positions, list):
        return None, ["broker_positions_unreadable"]
    equity, reasons = {}, []
    for row in positions:
        if not isinstance(row, dict):
            return None, ["broker_positions_unreadable"]
        symbol, asset_class = row.get("symbol"), row.get("asset_class", "us_equity")
        if asset_class == "crypto" or crypto_paper_pair(symbol) is not None:
            if crypto_paper_pair(symbol) is None:
                reasons.append("unreviewed_non_equity_position")
            continue
        if asset_class != "us_equity" or not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
            reasons.append("unsupported_broker_position")
            continue
        try:
            qty = Decimal(str(row.get("qty")))
        except (InvalidOperation, ValueError):
            reasons.append("broker_position_quantity_invalid")
            continue
        if not qty.is_finite() or symbol in equity:
            reasons.append("broker_position_quantity_invalid")
            continue
        if qty != 0:
            equity[symbol] = qty
    return equity, reasons


def certify_import(provider, batch, *, account_id, after, until, opening_lots=None, fetch=fetch_activities):
    """Certify ``batch`` (one ``fetch_activities`` result) or explain why not.

    ``opening_lots`` is the proven baseline (``None`` when no baseline is
    configured, treated as an empty inventory for reconciliation only; the
    ledger separately keeps ``opening_inventory_unproven`` in that case).
    """
    reasons = []
    coverage = batch.coverage if isinstance(getattr(batch, "coverage", None), dict) else {}
    segregated = sum(1 for row in getattr(batch, "observations", ()) if row.kind == SEGREGATED_KIND)
    rescan_matched = positions_matched = False
    try:
        if coverage.get("environment") != "paper":
            reasons.append("certification_requires_paper_environment")
        if coverage.get("account_id") != account_id:
            reasons.append("certification_account_mismatch")
        if coverage.get("activity_pages_exhausted") is not True or coverage.get("terminal_observation") not in {
                "empty_page_after_exclusive_id_cursor", "empty_first_page"}:
            reasons.append("activity_page_end_unverified")
        bounds = activity_request_params(after=after, until=until)
        if (coverage.get("window_start"), coverage.get("window_end")) != (bounds["after"], bounds["until"]):
            reasons.append("activity_window_bounds_mismatch")
        if coverage.get("blockers"):
            reasons.append("activity_import_has_blockers")
        if any(row.kind in {"unsupported", "correction"} for row in batch.observations):
            reasons.append("activity_requires_review")
        if coverage.get("segregated_non_equity_count", 0) != segregated:
            reasons.append("segregation_count_mismatch")
        if not reasons:
            positions = provider.positions()
            rescan = fetch(provider, account_id=account_id, after=after, until=until)
            rescan_matched = _sequence(rescan) == _sequence(batch) and not rescan.coverage.get("blockers")
            if not rescan_matched:
                reasons.append("activity_rescan_mismatch")
            broker, position_reasons = _broker_equity(positions)
            reasons.extend(position_reasons)
            if broker is not None:
                positions_matched = broker == _equity_net(batch.records, opening_lots or [])
                if not positions_matched:
                    reasons.append("equity_positions_do_not_reconcile")
                positions_matched = positions_matched and not position_reasons
    except Exception as error:  # noqa: BLE001 - any doubt fails closed
        code = getattr(error, "code", None)
        reasons.append("certification_error:" + code if isinstance(code, str) else "certification_error")
        rescan_matched = positions_matched = False
    reasons = tuple(dict.fromkeys(reasons))
    complete = not reasons and rescan_matched and positions_matched
    if complete:
        evidence = (
            f"{CERTIFIER}: two identical exhaustive all-types scans over the fixed window ended on an empty page.",
            "Equity net quantity from opening lots plus FILL activities equals broker us_equity positions.",
            "Fee completeness covers Alpaca-reported FEE activities only; paper omits regulatory fees.",
        ) + ((f"{segregated} reviewed crypto activities segregated from the equity ledger (crypto loop accounting).",)
             if segregated else ())
    else:
        evidence = (f"{CERTIFIER}: not certified ({', '.join(reasons) or 'unknown'}); flags stay false.",)
    LOG.info("accounting: completeness certification complete=%s reasons=%s segregated_non_equity=%d",
             complete, list(reasons), segregated)
    return CompletenessCertification(complete, complete, reasons, evidence, segregated,
                                     rescan_matched, positions_matched)


def apply_certification(coverage, certification):
    """Return ledger coverage carrying certified flags; never weakens blockers."""
    result = dict(coverage)
    result["executions_complete"] = bool(certification.executions_complete)
    result["fees_complete"] = bool(certification.fees_complete)
    result["evidence"] = (list(coverage.get("evidence", [])) + list(certification.evidence))[:100]
    return result
