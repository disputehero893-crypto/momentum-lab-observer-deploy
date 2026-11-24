"""Pattern hit-rate report from the AI advisor feedback log.

    python -m momentum.ai_report [--log ./data/ai_advisor/advisor_feedback.jsonl] [--json]

Joins each entry candidate (source tag confluence/rules/ai, AI pattern label
and call) with the outcome recorded when its paper trade exited, then reports
per source, per pattern, per source+pattern and per AI call: candidates,
trades, wins, win rate and average R multiple. No model training
happens; this is the review loop a human uses to decide what the AI is good at.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping


def load_records(path: str | Path) -> list[dict[str, Any]]:
    records = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue  # a torn final line must not break the report
        if isinstance(item, dict) and item.get("type") in {"signal", "candidate", "outcome"}:
            records.append(item)
    return records


def _bucket() -> dict[str, Any]:
    return {"verdicts": 0, "trades": 0, "wins": 0, "r_total": 0.0, "r_count": 0}


def _finish(bucket: Mapping[str, Any]) -> dict[str, Any]:
    trades = bucket["trades"]
    return {"verdicts": bucket["verdicts"], "trades": trades, "wins": bucket["wins"],
            "win_rate": round(bucket["wins"] / trades, 4) if trades else None,
            "avg_r": round(bucket["r_total"] / bucket["r_count"], 4) if bucket["r_count"] else None}


def build_report(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    candidates: dict[str, Mapping[str, Any]] = {}
    outcomes: dict[str, Mapping[str, Any]] = {}
    signals = {"total": 0, "ok": 0, "invalid": 0, "error": 0, "calls": {}}
    for item in records:
        kind = item.get("type")
        if kind == "signal":
            signals["total"] += 1
            status = item.get("status")
            if status in ("ok", "invalid", "error"):
                signals[status] += 1
            if status == "ok":
                call = str(item.get("call"))
                signals["calls"][call] = signals["calls"].get(call, 0) + 1
            continue
        sid = item.get("setup_id")
        if not sid:
            continue
        if kind == "candidate":
            previous = candidates.get(sid)
            if previous is None or item.get("submitted") or not previous.get("submitted"):
                candidates[sid] = item    # a submitted record outranks a proposal
        elif kind == "outcome":
            outcomes[sid] = item          # the latest exit for a setup wins
    tables: dict[str, dict[str, dict[str, Any]]] = {"by_source": {}, "by_pattern": {}, "by_source_and_pattern": {},
                                                    "by_ai_call": {}}
    for sid, c in candidates.items():
        o = outcomes.get(sid)
        source = str(c.get("source") or "unknown")
        pattern = str(c.get("pattern") or "no_ai_read")
        keys = (("by_source", source), ("by_pattern", pattern), ("by_source_and_pattern", f"{source}/{pattern}"),
                ("by_ai_call", str(c.get("call") or "no_ai_read")))
        for table, key in keys:
            b = tables[table].setdefault(key, _bucket())
            b["verdicts"] += 1
            if o is not None:
                b["trades"] += 1
                b["wins"] += 1 if o.get("win") is True else 0
                r = o.get("r_multiple")
                if isinstance(r, (int, float)) and not isinstance(r, bool):
                    b["r_total"] += float(r)
                    b["r_count"] += 1
    report = {"candidates": len(candidates), "outcomes": len(outcomes),
              "matched": sum(1 for sid in candidates if sid in outcomes), "signals": signals}
    report.update({name: {k: _finish(v) for k, v in sorted(rows.items())} for name, rows in tables.items()})
    report["note"] = ("Sources: confluence = rules and AI agreed, rules = rules only, ai = AI only. "
                      "Outcomes use the exit intent's bid before fill; small samples are noise.")
    return report


def render_text(report: Mapping[str, Any]) -> str:
    sig = report["signals"]
    out = [f"AI advisor feedback: {report['candidates']} candidates, {report['outcomes']} outcomes, "
           f"{report['matched']} matched",
           f"Chart reads: {sig['total']} total, {sig['ok']} valid, {sig['invalid']} rejected as invalid, "
           f"{sig['error']} errors; calls {json.dumps(sig['calls'], sort_keys=True)}", ""]
    for title, key in (("By source (rules vs AI vs confluence)", "by_source"), ("By AI pattern", "by_pattern"),
                       ("By source and pattern", "by_source_and_pattern"), ("By AI call", "by_ai_call")):
        out.append(f"{title}:")
        out.append(f"  {'label':<28}{'cands':>7}{'trades':>8}{'wins':>6}{'win%':>8}{'avg R':>8}")
        for label, row in report[key].items():
            win = f"{row['win_rate'] * 100:.0f}%" if row["win_rate"] is not None else "-"
            avg = f"{row['avg_r']:.2f}" if row["avg_r"] is not None else "-"
            out.append(f"  {label:<28}{row['verdicts']:>7}{row['trades']:>8}{row['wins']:>6}{win:>8}{avg:>8}")
        out.append("")
    out.append(report["note"])
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Hit rate per AI chart pattern from paper-trade outcomes")
    journal = os.environ.get("JOURNAL_PATH", "./data/momentum.sqlite3")
    default = os.environ.get("AI_ADVISOR_LOG_DIR") or str(Path(journal).parent / "ai_advisor")
    parser.add_argument("--log", default=str(Path(default) / "advisor_feedback.jsonl"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = build_report(load_records(args.log))
    print(json.dumps(report, indent=2) if args.json else render_text(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
