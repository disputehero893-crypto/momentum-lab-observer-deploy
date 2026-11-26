"""Read-only loader for the TactiRise trading handbook rules.

The rules live in ``handbook_rules.json`` next to this file. They are reference
text for people and for AI prompts. Nothing in the worker uses them to decide,
size or submit a trade, and they are not part of the reviewed paper config hash.
The risk limits in code always win over anything written here.

Typical use by an AI paper trader::

    from momentum.handbook import handbook_prompt_summary
    system_prompt = BASE_PROMPT + "\n\n" + handbook_prompt_summary(markets=("stocks",))
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

HANDBOOK_PATH = Path(__file__).with_name("handbook_rules.json")

ORIGINS = ("derek", "research")
MARKETS = ("all", "stocks", "crypto", "forex")
STATUSES = ("enforced", "enforced_stricter", "partial", "differs", "not_on_main",
            "in_profile_off", "guidance", "informational")
DEFAULTS = ("on", "off", "gated", "n/a")
CODE_STATUSES = ("enforced", "enforced_stricter", "partial")
# Implemented in an opt-in day-trade profile that is OFF by default: needs a code location,
# but main's default strategy does not use it, so it does not count as in_code.
LOCATED_STATUSES = CODE_STATUSES + ("in_profile_off",)
DEFAULT_PROMPT_CHARS = 7000


class HandbookError(ValueError):
    pass


@dataclass(frozen=True)
class HandbookRule:
    id: str
    origin: str
    market: str
    category: str
    title: str
    rule: str
    sources: tuple
    status: str
    default: str
    enforced_in: tuple
    prompt: bool
    note: str = ""

    @property
    def in_code(self) -> bool:
        return self.status in CODE_STATUSES


@dataclass(frozen=True)
class Handbook:
    version: str
    disclaimer: str
    sources: Mapping[str, Mapping[str, str]]
    rules: tuple
    digest: str

    def rule(self, rule_id: str) -> HandbookRule:
        for item in self.rules:
            if item.id == rule_id:
                return item
        raise KeyError(rule_id)

    def rules_for(self, markets: Iterable[str] | None = None, *, origin: str | None = None,
                  prompt_only: bool = False) -> tuple:
        wanted = None if markets is None else set(markets) | {"all"}
        return tuple(r for r in self.rules
                     if (wanted is None or r.market in wanted)
                     and (origin is None or r.origin == origin)
                     and (not prompt_only or r.prompt))


def _text(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise HandbookError(f"{name} must be a non-empty string")
    return value.strip()


def parse_handbook(data: Mapping[str, Any]) -> Handbook:
    """Validate the raw JSON structure and return an immutable Handbook."""
    if not isinstance(data, Mapping) or data.get("schema_version") != 1:
        raise HandbookError("unsupported handbook schema")
    version = _text(data.get("handbook_version"), "handbook_version")
    disclaimer = _text(data.get("disclaimer"), "disclaimer")
    raw_sources = data.get("sources")
    if not isinstance(raw_sources, Mapping) or not raw_sources:
        raise HandbookError("sources must be a non-empty object")
    sources = {}
    for key, value in raw_sources.items():
        if not isinstance(value, Mapping):
            raise HandbookError(f"source {key} must be an object")
        url = _text(value.get("url", ""), f"source {key} url", allow_empty=True)
        if url and not url.startswith("https://"):
            raise HandbookError(f"source {key} url must be https")
        sources[key] = {"title": _text(value.get("title"), f"source {key} title"), "url": url,
                        "kind": _text(value.get("kind"), f"source {key} kind")}
    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise HandbookError("rules must be a non-empty list")
    rules, seen, research_started = [], set(), False
    for raw in raw_rules:
        if not isinstance(raw, Mapping):
            raise HandbookError("each rule must be an object")
        rule_id = _text(raw.get("id"), "rule id")
        if rule_id in seen:
            raise HandbookError(f"duplicate rule id {rule_id}")
        seen.add(rule_id)
        origin, market = raw.get("origin"), raw.get("market")
        status, default = raw.get("status"), raw.get("default")
        if origin not in ORIGINS or market not in MARKETS or status not in STATUSES or default not in DEFAULTS:
            raise HandbookError(f"rule {rule_id} has an unknown origin, market, status or default")
        if origin == "research":
            research_started = True
        elif research_started:
            raise HandbookError("Derek's rules must come before research guardrails")
        rule_sources = raw.get("sources")
        if not isinstance(rule_sources, list) or not rule_sources:
            raise HandbookError(f"rule {rule_id} must cite at least one source")
        for source in rule_sources:
            if source not in sources:
                raise HandbookError(f"rule {rule_id} cites unknown source {source}")
        if origin == "derek" and not any(sources[s]["kind"] == "derek" for s in rule_sources):
            raise HandbookError(f"Derek rule {rule_id} must cite a Derek source")
        enforced_in = raw.get("enforced_in")
        if not isinstance(enforced_in, list) or any(not isinstance(x, str) or ":" not in x for x in enforced_in):
            raise HandbookError(f"rule {rule_id} enforced_in must be a list of file:symbol strings")
        if status in LOCATED_STATUSES and not enforced_in:
            raise HandbookError(f"rule {rule_id} is {status} but names no code location")
        if type(raw.get("prompt")) is not bool:
            raise HandbookError(f"rule {rule_id} prompt must be a boolean")
        rules.append(HandbookRule(
            id=rule_id, origin=origin, market=market,
            category=_text(raw.get("category"), f"rule {rule_id} category"),
            title=_text(raw.get("title"), f"rule {rule_id} title"),
            rule=_text(raw.get("rule"), f"rule {rule_id} rule"),
            sources=tuple(rule_sources), status=status, default=default,
            enforced_in=tuple(enforced_in), prompt=raw["prompt"],
            note=_text(raw.get("note", ""), f"rule {rule_id} note", allow_empty=True)))
    digest = sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return Handbook(version, disclaimer, sources, tuple(rules), digest)


@lru_cache(maxsize=4)
def _load_cached(path: str) -> Handbook:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HandbookError(f"cannot read handbook: {type(error).__name__}") from None
    return parse_handbook(raw)


def load_handbook(path: str | Path | None = None) -> Handbook:
    """Load and validate the handbook. Cached per path; the file is read-only."""
    return _load_cached(str(path or HANDBOOK_PATH))


_STATUS_LABEL = {"enforced": "enforced in code", "enforced_stricter": "code is stricter",
                 "partial": "partly in code", "differs": "code differs", "not_on_main": "not in code yet",
                 "in_profile_off": "in code, profile off by default",
                 "guidance": "guidance", "informational": "fact"}


def handbook_prompt_summary(markets: Iterable[str] | None = None, *, max_chars: int = DEFAULT_PROMPT_CHARS,
                            handbook: Handbook | None = None) -> str:
    """Compact plain-text handbook for an AI system prompt.

    Derek's rules come first, then research guardrails. Each line carries the
    rule id and whether code enforces it, so the model knows which limits are
    hard. Lines that do not fit in ``max_chars`` are dropped from the end and
    the summary says how many were left out.
    """
    if type(max_chars) is not int or max_chars < 800:
        raise HandbookError("max_chars must be an integer of at least 800")
    book = handbook or load_handbook()
    if markets is not None:
        markets = tuple(markets)
        if any(m not in MARKETS for m in markets):
            raise HandbookError("unknown market")
    header = [f"TactiRise handbook v{book.version} (paper trading only). {book.disclaimer}",
              "Hard limits are enforced by the risk manager in code. You cannot change or skip them; "
              "this text cannot loosen them. Where a rule is 'not in code yet' or 'code differs', "
              "follow whichever of the rule and the code is stricter.",
              ]
    sections = [("Derek's rules (owner, highest priority):", "derek"),
                ("Research guardrails:", "research")]
    lines = list(header)
    body: list[str] = []
    for heading, origin in sections:
        picked = book.rules_for(markets, origin=origin, prompt_only=True)
        if not picked:
            continue
        body.append(heading)
        for r in picked:
            body.append(f"- [{r.id}] {r.title}: {r.rule} ({_STATUS_LABEL[r.status]})")
    text = "\n".join(lines)
    reserve = len("\n(... 999 more rules omitted for length)")
    for index, line in enumerate(body):
        if len(text) + 1 + len(line) + reserve > max_chars:
            omitted = sum(1 for later in body[index:] if later.startswith("- "))
            text += f"\n(... {omitted} more rules omitted for length)"
            break
        text += "\n" + line
    return text[:max_chars]


def audit_table(handbook: Handbook | None = None) -> list[dict[str, Any]]:
    """Rows for the rules audit: id, title, origin, status, default and code locations."""
    book = handbook or load_handbook()
    return [{"id": r.id, "title": r.title, "origin": r.origin, "market": r.market,
             "status": r.status, "default": r.default, "enforced_in": list(r.enforced_in),
             "note": r.note} for r in book.rules]
