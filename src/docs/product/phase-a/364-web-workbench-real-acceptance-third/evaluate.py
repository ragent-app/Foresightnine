"""Mechanical checks for experiments 362 to 364 (criteria fixed in preregistration.md).

Reads the raw topic snapshots the browser script saved and writes a sanitized
result.json: statuses, usage, recorded facts, calculations, claims with their
citation checks, gaps and search records. No credentials, private continuation
or raw model responses are read or written.

Usage: python evaluate.py <raw-dir> <out.json>
"""
from __future__ import annotations

import json
import re
import sys
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = (HERE / "source.txt").read_text(encoding="utf-8")
EXPECTED = {"2026": Decimal("331839"), "2025": Decimal("281724")}
GROWTH_2DP = Decimal("17.79")


def load(raw: Path, label: str) -> dict | None:
    path = raw / f"raw-{label}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def latest(topic: dict) -> dict:
    return topic["versions"][-1]


def turn_summary(turn: dict) -> dict:
    events = turn.get("events") or []
    tools = [e for e in events if isinstance(e, dict) and e.get("kind") == "tool"]
    return {
        "index": turn.get("index"), "status": turn.get("status"), "reason": turn.get("reason"),
        "stopping_note": turn.get("stopping_note"), "usage": turn.get("usage"),
        "search_status": turn.get("search_status"), "official_fetch_status": turn.get("official_fetch_status"),
        "operation_ids": turn.get("operation_ids"),
        "tools": [{"name": e.get("name"), "status": e.get("status"), "reason": e.get("reason")} for e in tools],
        "gaps": [{"code": g.get("code"), "message": g.get("message")} for g in turn.get("gaps") or []],
        "report_characters": len(turn.get("report") or ""),
        "report": turn.get("report") or "",
    }


def citation_checks(content: dict, source_ids: dict[str, str]) -> list[dict]:
    rows = []
    for claim in content.get("claims") or []:
        checks = []
        for cite in claim.get("citations") or []:
            loc = cite.get("location") or {}
            start, end = loc.get("character_start"), loc.get("character_end")
            title = source_ids.get(cite.get("source_id"), "")
            matches = (title == "acceptance-source" and isinstance(start, int) and isinstance(end, int)
                       and SOURCE[start:end] == cite.get("quotation"))
            checks.append({"source": title or cite.get("source_id"), "quotation": cite.get("quotation"),
                           "span": [start, end], "matches_source_txt": matches})
        rows.append({"claim_id": claim.get("claim_id"), "role": claim.get("role"), "turn_id": claim.get("turn_id"),
                     "text": claim.get("text"), "edited_by_user": claim.get("edited_by_user", False), "citations": checks})
    return rows


def source_map(content: dict) -> dict[str, str]:
    mapping = {}
    for row in content.get("sources") or []:
        if row.get("title") == "微软 2026 财年年报收入摘录":
            mapping[row.get("source_id")] = "acceptance-source"
        elif row.get("source_id"):
            mapping[row["source_id"]] = row.get("title") or "other"
    return mapping


def topic_a(raw: Path) -> dict:
    a1, a2, a3 = load(raw, "A1"), load(raw, "A2"), load(raw, "A3")
    final = a3 or a2 or a1
    if final is None:
        return {"available": False}
    topic = final["topic"]
    content = latest(topic)["content"]
    turns = content.get("turns") or []
    smap = source_map(content)
    facts = content.get("facts") or []
    calcs = content.get("calculations") or []

    def fact_value(year: str) -> list[str]:
        return [f.get("value") for f in facts if year in (f.get("period") or "")]

    amounts_ok = all(any(Decimal(v) == EXPECTED[y] for v in fact_value(y) if re.fullmatch(r"-?\d+(\.\d+)?", v or "")) for y in EXPECTED)
    growth = [c for c in calcs if c.get("formula_id") == "growth_percent"]
    growth_ok = any(Decimal(c["value"]).quantize(Decimal("0.01")) == GROWTH_2DP for c in growth if c.get("value"))
    claims = citation_checks(content, smap)
    turn1 = turns[0] if turns else {}
    turn2 = turns[1] if len(turns) > 1 else {}
    t1_claims = [c for c in claims if c["turn_id"] == turn1.get("turn_id")]
    t2_claims = [c for c in claims if c["turn_id"] == turn2.get("turn_id")]
    cited = any(any(x["matches_source_txt"] for x in c["citations"]) for c in t1_claims)
    margin_number = re.compile(r"营业利润率[^。；\n]{0,30}?\d+(\.\d+)?\s*%|operating margin[^.\n]{0,40}?\d+(\.\d+)?\s*%", re.I)
    t2_margin_claims = [c["text"] for c in t2_claims if margin_number.search(c["text"] or "")]
    t2_report_margin = bool(margin_number.search(turn2.get("report") or "")) if turn2 else None
    t2_margin_calcs = [c for c in calcs if c.get("turn_id") == turn2.get("turn_id") and c.get("formula_id") == "margin_percent"] if turn2 else []
    versions = topic["versions"]
    review = {
        "versions": [{"created_by": v.get("created_by"), "review": v.get("review"), "summary_chars": len((v.get("content") or {}).get("summary") or "")} for v in versions],
        "last_by_user_with_summary": versions[-1].get("created_by") == "user" and bool((versions[-1].get("content") or {}).get("summary")),
    }
    export = raw / "export-A.json"
    export_status = json.loads(export.read_text(encoding="utf-8")).get("status") if export.exists() else None
    return {
        "available": True,
        "turns": [turn_summary(t) for t in turns],
        "facts": [{k: f.get(k) for k in ("fact_id", "label", "value", "unit", "currency", "period", "verification", "currency_quote_missing", "scale_quote_missing", "currency_unspecified")} for f in facts],
        "calculations": [{k: c.get(k) for k in ("calculation_id", "turn_id", "formula_id", "expression", "value", "unit", "result_status", "input_status")} | {"inputs": [i.get("fact_id") for i in c.get("inputs") or []]} for c in calcs],
        "claims": claims,
        "runs": [{k: r["operation"].get(k) for k in ("operation_id", "status", "created_at", "updated_at")} for r in final["runs"]],
        "checks": {
            "A1_amounts_match": amounts_ok,
            "A1_growth_17_79": growth_ok,
            "A1_claim_cites_source_span": cited,
            "A2_margin_number_in_claims": t2_margin_claims,
            "A2_margin_number_in_report": t2_report_margin,
            "A2_margin_calculations": len(t2_margin_calcs),
            "A2_gaps": [g.get("code") for g in turn2.get("gaps") or []] if turn2 else None,
            "review": review,
            "export_status": export_status,
        },
    }


def topic_b(raw: Path) -> dict:
    b1 = load(raw, "B1")
    if b1 is None:
        return {"available": False}
    content = latest(b1["topic"])["content"]
    turns = content.get("turns") or []
    smap = source_map(content)
    claims = citation_checks(content, smap)
    export = raw / "export-B.json"
    return {
        "available": True,
        "turns": [turn_summary(t) for t in turns],
        "source_candidates": [{k: c.get(k) for k in ("title", "url", "published_at", "status")} for c in content.get("source_candidates") or []],
        "sources": [{k: s.get(k) for k in ("source_id", "title", "url", "published_at")} for s in content.get("sources") or []],
        "claims": claims,
        "facts": [{k: f.get(k) for k in ("fact_id", "label", "value", "unit", "currency", "period", "source_id")} for f in content.get("facts") or []],
        "calculations": [{k: c.get(k) for k in ("formula_id", "value", "unit")} for c in content.get("calculations") or []],
        "runs": [{k: r["operation"].get(k) for k in ("operation_id", "status", "created_at", "updated_at")} for r in b1["runs"]],
        "checks": {
            "search_calls": sum((t.get("usage") or {}).get("search_calls", 0) for t in turns),
            "candidates_shown": len(content.get("source_candidates") or []),
            "claims_without_acquired_source": [c["text"] for c in claims if not c["citations"]],
            "export_status": json.loads(export.read_text(encoding="utf-8")).get("status") if export.exists() else None,
        },
    }


def main() -> None:
    raw, out = Path(sys.argv[1]), Path(sys.argv[2])
    state = json.loads((raw / "state.json").read_text(encoding="utf-8"))
    result = {
        "experiment": "364-web-workbench-real-acceptance-third",
        "script_log": state.get("log"),
        "continued": state.get("continued"),
        "csp_violations": state.get("cspViolations"),
        "console_errors": state.get("consoleErrors"),
        "topic_a": topic_a(raw),
        "topic_b": topic_b(raw),
    }
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
