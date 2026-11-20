#!/usr/bin/env python3
"""365 pre-registered evaluator (stdlib only). Reads results/<label>/ and computes C1-C6 plus observations.

  evaluate365.py [--results DIR] [--turn N] [--json]

Each result directory holds topic.json (API topic), and optionally run_inputs.json (list of the run's
research_run_inputs documents), operations.json, summary.json (operation_ids), timing.json.
Works on partial runs: anything that cannot be assessed is reported as None / "n/a", not as a pass.
Labels beginning Q-A are the Nasdaq question, Q-B the Anthropic IPO question.
--turn N selects turn N of a multi-turn topic (used for the production baseline smoke test).

C1/C2 reference date: the last completed NASDAQ session at the run's start time. The logic below is a
copy of app/research_clock.py (regular weekday sessions, 30 minute settle, no holiday calendar).
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent

# ---- copy of research_clock.py (NASDAQ only) -----------------------------------------------------
NASDAQ_TZ, NASDAQ_OPEN, NASDAQ_CLOSE, SETTLE_MINUTES = "America/New_York", time(9, 30), time(16, 0), 30


def last_completed_session(now: datetime) -> date:
    local = now.astimezone(ZoneInfo(NASDAQ_TZ))
    close_today = datetime.combine(local.date(), NASDAQ_CLOSE, tzinfo=local.tzinfo)
    day = local.date()
    if not (day.weekday() not in (5, 6) and local >= close_today + timedelta(minutes=SETTLE_MINUTES)):
        day -= timedelta(days=1)
    while day.weekday() in (5, 6):
        day -= timedelta(days=1)
    return day
# --------------------------------------------------------------------------------------------------

TITLE_RE = re.compile(r"^(?P<inst>[A-Z]+:[A-Z0-9.\-]+) daily close \((?P<a>\d{4}-\d{2}-\d{2}) to (?P<b>\d{4}-\d{2}-\d{2})\)")
FAIL_STATUSES = {"failed", "unavailable", "unknown", "invalid", "denied_or_unavailable", "ocr_unavailable"}
READ_ONLY_UNKNOWN = {"acquisition_result_unknown", "search_result_unknown", "external_result_unknown"}
DATE_RES = [re.compile(r"\d{4}-\d{2}-\d{2}"), re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"),
            re.compile(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? \d{4}")]
ASOF_RE = re.compile(r"截至|截止|数据至|as of|through|up to|latest (?:close|data)|最新(?:收盘|数据)", re.I)


def load(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def pdate(text) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def pick_turn(topic: dict, op_ids: list[str], index: int | None):
    turns = [(v, t) for v in topic.get("versions", []) for t in v["content"].get("turns", [])]
    cands = [(v, t) for v, t in turns if (index is None or t.get("index") == index)
             and (not op_ids or set(op_ids) & set(t.get("operation_ids", [])))]
    if not cands and index is None and turns:
        cands = turns
    return (cands[-1] if index is None else cands[0]) if cands else (None, None)


def tool_results(inputs: list[dict]):
    """Yield (tool_call_id, parsed result) from the checkpoint history, if exported."""
    for doc in inputs:
        for entry in (doc.get("runtime_checkpoint") or {}).get("history") or []:
            for res in entry.get("results") or []:
                try:
                    yield res.get("tool_call_id"), json.loads(res.get("content"))
                except (ValueError, TypeError):
                    continue


def price_sources(turn, inputs, topic):
    found: dict[tuple, dict] = {}

    def add(inst, last, via, coverage=None, first=None):
        if inst and last:
            found[(inst, last, via)] = {"instrument": inst, "actual_last_date": last, "actual_first_date": first, "via": via,
                                        "latest_completed_session": (coverage or {}).get("latest_completed_session"),
                                        "clamped": (coverage or {}).get("clamped")}
    for _id, result in tool_results(inputs):
        if isinstance(result, dict) and result.get("status") == "acquired" and "daily close" in str(result.get("title", "")):
            cov = result.get("coverage") or {}
            m = TITLE_RE.match(result["title"])
            inst = m.group("inst") if m else None
            add(inst, cov.get("actual_last_date") or (m.group("b") if m else None), "tool_result",
                cov, cov.get("actual_first_date") or (m.group("a") if m else None))
    for ev in (turn or {}).get("events", []):
        summary = ev.get("summary") if isinstance(ev, dict) else None
        if ev.get("kind") == "tool" and ev.get("name") == "historical_prices" and isinstance(summary, dict):
            m = TITLE_RE.match(str(summary.get("title") or ""))
            if m and ev.get("status") == "acquired":
                add(m.group("inst"), m.group("b"), "event_title", None, m.group("a"))
    if not found and turn:
        wanted = set()
        for v in topic.get("versions", []):
            for s in v["content"].get("sources", []):
                m = TITLE_RE.match(str(s.get("title") or ""))
                if m:
                    wanted.add((m.group("inst"), m.group("b"), m.group("a")))
        for inst, last, first in wanted:
            add(inst, last, "topic_source_title", None, first)
    return list(found.values())


def batches(turn):
    """Group tool events by the model call that issued them: [(call_ids, [tool events])]."""
    tools = {e.get("tool_call_id"): e for e in turn.get("events", []) if e.get("kind") == "tool"}
    out = []
    for e in turn.get("events", []):
        if e.get("kind") == "model" and e.get("tool_call_ids"):
            out.append([tools[i] for i in e["tool_call_ids"] if i in tools])
    return out


def is_dup(e):
    return bool(e.get("duplicate")) or e.get("status") == "duplicate_action"


def mentions_asof(report: str, last_dates: list[str]):
    sentences = re.split(r"(?<=[。！？.!?\n])", report)
    hits = []
    for sent in sentences:
        if ASOF_RE.search(sent) and any(r.search(sent) for r in DATE_RES):
            hits.append(sent.strip()[:120])
    return {"mentions_as_of_date": bool(hits), "sample": hits[:2],
            "mentions_actual_last_date": any(d in report for d in last_dates)}


def evaluate_run(label: str, rdir: Path, turn_index: int | None):
    topic = load(rdir / "topic.json", {})
    inputs = load(rdir / "run_inputs.json", []) or []
    inputs = inputs if isinstance(inputs, list) else [inputs]
    summary = load(rdir / "summary.json", {}) or {}
    op_ids = summary.get("operation_ids") or []
    version, turn = pick_turn(topic, op_ids, turn_index)
    r: dict = {"label": label, "has_turn": turn is not None}
    if turn is None:
        r["note"] = "no turn found (run not finished or export missing)"
        return r
    op_ids = op_ids or turn.get("operation_ids", [])
    ops = [d for d in inputs if d.get("operation_id") in op_ids] or inputs
    checkpoint = (ops[0].get("runtime_checkpoint") if ops else None) or {}
    start = (datetime.fromisoformat(turn["started_at"]) if turn.get("started_at") else
             datetime.fromisoformat(ops[0]["captured_at"]) if ops and ops[0].get("captured_at") else None)
    r["started_at"] = start.isoformat() if start else None
    r["ref_session"] = last_completed_session(start).isoformat() if start else None
    usage = turn.get("usage") or {}
    r.update(status=turn.get("status"), reason=turn.get("reason"), gaps=[g.get("code") for g in turn.get("gaps", [])],
             model_calls=usage.get("model_calls"), search_calls=usage.get("search_calls"), total_tokens=usage.get("total_tokens"),
             cancelled_reason=summary.get("cancelled_reason"))
    events = turn.get("events", [])
    # cache hit rate: cache_read / (uncached input + cache_read [+ cache_creation])
    inp = cache = created = 0
    for e in events:
        d = e.get("usage_detail") if e.get("kind") == "model" else None
        if isinstance(d, dict):
            inp += d.get("input_tokens") or 0
            cache += d.get("cache_read_input_tokens") or 0
            created += d.get("cache_creation_input_tokens") or 0
    r["cache"] = {"input_tokens": inp, "cache_read": cache, "cache_creation": created,
                  "hit_rate": round(cache / (inp + cache + created), 4) if inp + cache + created else None}
    receipts = checkpoint.get("context_receipts")
    r["compaction"] = ({"count": checkpoint.get("compaction_count", len(receipts or [])),
                        "prefix_resets": sum(1 for x in receipts or [] if x.get("prefix_reset"))}
                       if checkpoint else None)
    r["tool_failures"] = dict(Counter(f"{e['name']}:{e.get('status')}:{e.get('reason')}" for e in events
                                      if e.get("kind") == "tool" and e.get("status") in FAIL_STATUSES))
    r["duplicate_actions"] = sum(1 for e in events if e.get("kind") == "tool" and is_dup(e))
    r["ocr_events"] = sum(1 for e in events if e.get("kind") == "tool" and e.get("ocr"))
    r["ocr_unavailable_or_needed"] = sum(1 for e in events if e.get("kind") == "tool" and
                                         (e.get("status") == "ocr_unavailable" or str(e.get("reason", "")).startswith(("ocr", "no_ocr"))))
    r["large_doc_reads"] = sum(1 for e in events if e.get("kind") == "tool" and e.get("name") in {"read_pages", "outline_source", "request_ocr"})
    prices = price_sources(turn, ops, topic)
    r["price_sources"] = prices
    r["ant_prices_obtained"] = any("ANT" == p["instrument"].split(":")[-1] for p in prices)
    last_dates = sorted({p["actual_last_date"] for p in prices})
    ref = pdate(r["ref_session"])
    r["latest_price_date"] = last_dates[-1] if last_dates else None
    # criteria inputs
    r["c1c2_reached_ref"] = (bool(ref) and any(pdate(p["actual_last_date"]) and pdate(p["actual_last_date"]) >= ref for p in prices)
                             if label.startswith("Q-A") else None)
    r["c2_reached_ref"] = (bool(ref) and any(p["instrument"].split(":")[-1] in {"QQQ", "SPY"} and pdate(p["actual_last_date"]) >= ref
                                             for p in prices) if label.startswith("Q-B") else None)
    r["c2_reached_2026_11_19"] = (any(p["instrument"].split(":")[-1] in {"QQQ", "SPY"} and p["actual_last_date"] >= "2026-11-19"
                                      for p in prices) if label.startswith("Q-B") else None)
    r["c3_ok"] = not (turn.get("reason") in READ_ONLY_UNKNOWN or set(r["gaps"]) & READ_ONLY_UNKNOWN)
    bs = batches(turn)
    mixed = [any(is_dup(e) for e in b) and any(not is_dup(e) for e in b) for b in bs]
    r["mixed_duplicate_batches"] = sum(mixed)
    r["c4_ok"] = not (turn.get("reason") == "no_progress" and any(mixed[-2:]))
    # C5: price-tool failures carry message/hint; a later allowed-date retry exists
    failures = [(res, i) for i, (_id, res) in enumerate(tool_results(ops))
                if isinstance(res, dict) and res.get("status") in FAIL_STATUSES and res.get("reason", "").startswith("historical_price")]
    ev_failures = [e for e in events if e.get("kind") == "tool" and e.get("name") == "historical_prices" and e.get("status") in FAIL_STATUSES]
    if not ops or not checkpoint.get("history"):
        r["c5"] = {"failures": len(ev_failures), "assessable": False}
    else:
        guided = [bool(res.get("message") or res.get("hints")) for res, _ in failures]
        later_ok, seen_fail = False, False
        for e in events:
            if e.get("kind") == "tool" and e.get("name") == "historical_prices":
                if e.get("status") in FAIL_STATUSES:
                    seen_fail = True
                elif seen_fail and e.get("status") == "acquired":
                    later_ok = True
        r["c5"] = {"failures": len(failures), "all_guided": all(guided) if guided else None,
                   "retried_with_allowed_date": later_ok if failures else None, "assessable": True}
    report = turn.get("report") or ""
    r["report_chars"] = len(report)
    r["c6"] = mentions_asof(report, last_dates) if report else None
    return r


def verdict(values, need):
    vals = [v for v in values if v is not None]
    return {"passed": sum(1 for v in vals if v), "assessed": len(vals), "required": need,
            "result": ("PASS" if sum(1 for v in vals if v) >= need else
                       "PENDING" if len(vals) < len(values) or len(values) < 3 else "FAIL")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(HERE.parent / "results"))
    ap.add_argument("--turn", type=int)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    root = Path(args.results)
    runs = [evaluate_run(d.name, d, args.turn) for d in sorted(root.iterdir()) if d.is_dir() and (d / "topic.json").exists()]
    qa = [r for r in runs if r["label"].startswith("Q-A") and r["has_turn"]]
    qb = [r for r in runs if r["label"].startswith("Q-B") and r["has_turn"]]
    done = [r for r in runs if r["has_turn"]]
    crit = {
        "C1": verdict([r["c1c2_reached_ref"] for r in qa], 2) | {"expected_runs": 3},
        "C2": verdict([r["c2_reached_ref"] for r in qb], 2) | {"expected_runs": 3},
        "C3": verdict([r["c3_ok"] for r in done], 6) | {"expected_runs": 6},
        "C4": verdict([r["c4_ok"] for r in done], 6) | {"expected_runs": 6},
        "C5": {"per_run": {r["label"]: r["c5"] for r in done},
               "result": ("FAIL" if any(r["c5"].get("assessable") and (r["c5"]["all_guided"] is False) for r in done) else
                          "PENDING" if any(not r["c5"].get("assessable") for r in done) else
                          "PASS" if any(r["c5"].get("retried_with_allowed_date") for r in done) or
                          all(r["c5"]["failures"] == 0 for r in done) else "FAIL")},
        "C6": {"per_run": {r["label"]: (r["c6"] or {}).get("mentions_as_of_date") for r in done if r["status"] == "completed"},
               "result": ("PASS" if all((r["c6"] or {}).get("mentions_as_of_date") for r in done if r["status"] == "completed")
                          and any(r["status"] == "completed" for r in done) else
                          "PENDING" if not any(r["status"] == "completed" for r in done) else "FAIL")},
    }
    if args.json:
        print(json.dumps({"runs": runs, "criteria": crit}, ensure_ascii=False, indent=1))
        return
    for r in runs:
        print(f"== {r['label']}")
        if not r["has_turn"]:
            print("   ", r.get("note"))
            continue
        print(f"   start={r['started_at']} ref_session={r['ref_session']} status={r['status']} reason={r['reason']} gaps={r['gaps']}")
        print(f"   model_calls={r['model_calls']} search_calls={r['search_calls']} tokens={r['total_tokens']} cancelled={r['cancelled_reason']}")
        print(f"   cache={r['cache']} compaction={r['compaction']}")
        print(f"   price_sources={[(p['instrument'], p['actual_last_date'], p['via']) for p in r['price_sources']]}")
        print(f"   latest_price_date={r['latest_price_date']} reached_ref(C1)={r['c1c2_reached_ref']} reached_ref(C2)={r['c2_reached_ref']} "
              f"reached_2026-11-19(C2)={r['c2_reached_2026_11_19']} ANT_prices={r['ant_prices_obtained']}")
        print(f"   tool_failures={r['tool_failures']} duplicates={r['duplicate_actions']} mixed_dup_batches={r['mixed_duplicate_batches']} "
              f"ocr_events={r['ocr_events']} ocr_unavailable_or_needed={r['ocr_unavailable_or_needed']} large_doc_reads={r['large_doc_reads']}")
        print(f"   C3_ok={r['c3_ok']} C4_ok={r['c4_ok']} C5={r['c5']} report_chars={r['report_chars']} C6={r['c6']}")
    print("== criteria (pre-registered)")
    for k, v in crit.items():
        print(f"   {k}: {json.dumps(v, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
