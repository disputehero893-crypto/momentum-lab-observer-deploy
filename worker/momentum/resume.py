"""Paper-only, authenticated resume of a latched journal kill switch.

The ordinary halt endpoints still refuse ``enabled=false``
(``remote_resume_is_not_supported``). This module is the one sanctioned way
back: a ``resume_entries`` row in ``paper_commands`` that only the server-side
service role can write (the browser insert policy still allows queued
``halt_new_entries`` rows only). The worker polls it with its own
owner-scoped server key and clears the latch only when every check below holds
during the same cycle in which the paper account was freshly verified.

Nothing here reads, logs or publishes a credential. The configuration hash it
reports is a digest of non-secret settings only (see ``Config.review_hash``).
"""
import re

from .config import PAPER_API_HOST

RESUME_KIND = "resume_entries"
RESUME_MAX_AGE_SECONDS = 900
PAPER_ORIGIN = "https://paper-api.alpaca.markets"
_REASON_UNSAFE = re.compile(r"[^A-Za-z0-9 _:.,/()+-]")
REFUSALS = frozenset({
    "not_paper_mode", "live_execution_host_refused", "paper_configuration_blocked",
    "config_hash_mismatch", "broker_paper_check_failed", "kill_switch_unreadable",
    "emergency_flatten_requires_review", "superseded_by_halt", "resume_request_expired",
    "resume_request_from_future", "resume_check_failed",
})


def safe_reason(value):
    """Bounded, printable latch reason. Never a secret: reasons are worker codes."""
    if value is None:
        return None
    text = _REASON_UNSAFE.sub("_", str(value))[:120]
    return text or None


def control_report(journal):
    """The latch as the dashboard and /health may show it. Fails closed."""
    try:
        state = journal.kill_switch() if journal is not None else None
        if not isinstance(state, dict):
            raise ValueError
        return {"enabled": state.get("enabled") is not False,
                "flatten_requested": state.get("flatten_requested") is True,
                "reason": safe_reason(state.get("reason"))}
    except Exception:
        return {"enabled": True, "flatten_requested": True, "reason": "journal_unreadable_fail_closed"}


def config_report(config):
    """match / mismatch / unset for PAPER_REVIEWED_CONFIG_HASH, plus blocker codes.

    The computed digest covers non-secret settings only, so the owner can
    approve the exact value when it does not match. Keys never enter it.
    """
    try:
        computed = config.review_hash()
        reviewed = config.paper_reviewed_config_hash or ""
        status = "unset" if not reviewed else ("match" if reviewed == computed else "mismatch")
        blockers = sorted(set(str(code) for code in config.paper_blockers()))
        return {"hash_status": status, "computed_hash": computed, "blockers": blockers}
    except Exception:
        return {"hash_status": "unavailable", "computed_hash": None, "blockers": ["paper_configuration_unreadable"]}


def resume_refusal(config, journal, *, broker_verified, halt_seen, created_at, now):
    """Return None when a resume may proceed, else one refusal code."""
    try:
        if getattr(config, "mode", None) != "paper":
            return "not_paper_mode"
        if PAPER_API_HOST != PAPER_ORIGIN:
            return "live_execution_host_refused"
        from .crypto_execution import PAPER_HOST
        if PAPER_HOST != "paper-api.alpaca.markets":
            return "live_execution_host_refused"
        report = config_report(config)
        if report["hash_status"] != "match":
            return "config_hash_mismatch"
        if report["blockers"]:
            return "paper_configuration_blocked"
        if broker_verified is not True:
            return "broker_paper_check_failed"
        if halt_seen:
            return "superseded_by_halt"
        age = (now - created_at).total_seconds()
        if age < -5:
            return "resume_request_from_future"
        if age > RESUME_MAX_AGE_SECONDS:
            return "resume_request_expired"
        state = journal.kill_switch()
        if not isinstance(state, dict) or state.get("reason") == "journal_payload_unreadable_fail_closed":
            return "kill_switch_unreadable"
        if state.get("flatten_requested") is True:
            return "emergency_flatten_requires_review"
        return None
    except Exception:
        return "resume_check_failed"
