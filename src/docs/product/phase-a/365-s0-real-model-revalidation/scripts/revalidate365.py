#!/usr/bin/env python3
"""365 S0 revalidation driver (stdlib only). Talks only to 127.0.0.1:8005 and the isolated stack's DB.

  revalidate365.py dryrun
  revalidate365.py run --label Q-A1 --question '<text>'

`dryrun` never starts a research run. `run` submits exactly one run per label (a state file outside the repo guards
against resubmission; a rerun of the same label resumes polling instead).
Credentials of the stack are never read or printed; the mock account password is random per run.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

BASE = "http://127.0.0.1:8005"
DB_CONTAINER = "foresightnine-revalidation-365-postgres-1"
API_CONTAINER = "foresightnine-revalidation-365-api-1"
API_IMAGE = "foresightnine-api:s0-9434192"
HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / "results"
# Session tokens of the throwaway mock accounts stay outside the repository.
STATE_DIR = Path("/home/ubuntu/.local/state/foresightnine/revalidation-365/state")
POLL_SECONDS = 15
GUARD_WALL_SECONDS = 60 * 60       # pre-registered experiment guard (not a product budget)
GUARD_TOTAL_TOKENS = 1_500_000
TERMINAL = {"completed", "failed", "cancelled"}

# Exactly what the production Web client sends for a first turn with default settings
# (pages/research/options.ts runRequestFields(DEFAULT_OPTIONS)): no budget fields at all.
def run_body(parent_version_id: str) -> dict:
    return {"parent_version_id": parent_version_id, "discover_sources": True, "auto_fetch_official": True}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def http(method: str, path: str, body: dict | None = None, token: str | None = None, headers: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    req = request.Request(BASE + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with request.urlopen(req, timeout=60) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw[:300].decode("utf-8", "replace")}


def must(status_body, ok=(200, 201, 202), what=""):
    status, body = status_body
    if status not in ok:
        raise SystemExit(f"{what} failed: HTTP {status} {json.dumps(body, ensure_ascii=False)[:400]}")
    return body


def new_account(label: str):
    email = f"reval365-{label.lower()}-{uuid.uuid4().hex[:8]}@example.com"
    password = "reval-365-" + uuid.uuid4().hex
    must(http("POST", "/v1/auth/local/register", {"email": email, "password": password}), what="register")
    session = must(http("POST", "/v1/auth/local/login", {"email": email, "password": password}), what="login")
    token = session.get("access_token")
    if not token:
        raise SystemExit(f"login response has no access_token (keys: {sorted(session)})")
    return email, token


def create_topic(token: str, question: str) -> dict:
    return must(http("POST", "/v2/research/topics", {"question": question, "source_ids": []}, token), what="create topic")


def psql(sql: str) -> str:
    out = subprocess.run(
        ["sudo", "-n", "docker", "exec", DB_CONTAINER, "psql", "-U", "foresightnine", "-d", "foresightnine_reval", "-At", "-c", sql],
        capture_output=True, text=True, timeout=120)
    if out.returncode:
        raise SystemExit("psql failed: " + out.stderr[:300])
    return out.stdout


def validate_body_offline(body: dict) -> str:
    """Validate the run body against the image's own ResearchRunRequest model (no network, no DB)."""
    code = ("import json,sys;from app.research_runs import ResearchRunRequest as R, resolve_automatic_research_request as r;"
            "b=json.loads(sys.argv[1]);m=R.model_validate(b);x=r(m,search_available=True,historical_prices_available=True);"
            "print(json.dumps({'unset_fields':sorted(set(b)^set(m.model_fields_set)),'resolved':{k:v for k,v in x.model_dump(mode='json').items() if v is not None}}))")
    out = subprocess.run(
        ["sudo", "-n", "docker", "run", "--rm", "--network", "none", "--entrypoint", "/app/apps/api/.venv/bin/python",
         "-w", "/app/apps/api", API_IMAGE, "-c", code, json.dumps(body)],
        capture_output=True, text=True, timeout=120)
    if out.returncode:
        return "offline validation FAILED: " + out.stderr[-300:]
    return out.stdout.strip()


def cmd_dryrun(_args) -> int:
    print("readyz:", http("GET", "/readyz"))
    email, token = new_account("dryrun")
    print("mock login: ok, account", email, "(token not shown)")
    topic = create_topic(token, "dry run: harmless connectivity check (no research run will be started)")
    print("topic created:", topic["topic_id"], "versions:", len(topic["versions"]))
    back = must(http("GET", f"/v2/research/topics/{topic['topic_id']}", token=token), what="read topic")
    print("topic read back:", back["topic_id"], "question ok:", back["request"]["question"] == topic["request"]["question"])
    runs = must(http("GET", f"/v2/research/topics/{topic['topic_id']}/runs", token=token), what="list runs")
    print("runs list:", runs, "(must be empty)")
    body = run_body(back["versions"][-1]["version_id"])
    print("run body that `run` would POST:", json.dumps(body))
    print("offline model validation:", validate_body_offline(body))
    print("NOT posted: dryrun never starts a run.")
    return 0 if runs == [] else 1


def export_run(label_dir: Path, topic_id: str, token: str, operation_ids: list[str]) -> None:
    topic = must(http("GET", f"/v2/research/topics/{topic_id}", token=token), what="export topic")
    (label_dir / "topic.json").write_text(json.dumps(topic, ensure_ascii=False, indent=1))
    runs = must(http("GET", f"/v2/research/topics/{topic_id}/runs", token=token), what="list runs")
    (label_dir / "runs.json").write_text(json.dumps(runs, ensure_ascii=False, indent=1))
    ops, inputs = [], []
    for op in operation_ids:
        row = psql(f"select operation from operation_state where operation_id='{op}'").strip()
        if row:
            ops.append(json.loads(row))
        row = psql("select document from research_state_documents where collection='research_run_inputs' "
                   f"and document_key='{op}'").strip()
        if row:
            inputs.append(json.loads(row))
    (label_dir / "operations.json").write_text(json.dumps(ops, ensure_ascii=False, indent=1))
    (label_dir / "run_inputs.json").write_text(json.dumps(inputs, ensure_ascii=False, indent=1))


def progress_usage(topic_id: str, op: str, token: str) -> dict:
    status, body = http("GET", f"/v2/research/topics/{topic_id}/runs/{op}/progress", token=token)
    return body.get("usage", {}) if status == 200 and isinstance(body, dict) else {}


def cmd_run(args) -> int:
    label_dir = RESULTS / args.label
    label_dir.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = STATE_DIR / f"{args.label}.json"
    timing = {}
    if state_path.exists():
        state = json.loads(state_path.read_text())
        print(f"state.json exists for {args.label}: resuming poll, NOT resubmitting")
    else:
        email, token = new_account(args.label)
        topic = create_topic(token, args.question)
        parent = topic["versions"][-1]["version_id"]
        state = {"label": args.label, "question": args.question, "email": email, "token": token,
                 "topic_id": topic["topic_id"], "parent_version_id": parent, "operation_id": None,
                 "submitted_at": None, "idempotency_key": "reval365-" + uuid.uuid4().hex}
        state_path.write_text(json.dumps(state, indent=1))
        state_path.chmod(0o600)
        body = run_body(parent)
        (label_dir / "run_request.json").write_text(json.dumps(body, indent=1))
        state["submitted_at"] = now()
        state_path.write_text(json.dumps(state, indent=1))
        response = must(http("POST", f"/v2/research/topics/{state['topic_id']}/runs", body, state["token"],
                             {"Idempotency-Key": state["idempotency_key"]}), what="start run")
        state["operation_id"] = response["operation"]["operation_id"]
        state_path.write_text(json.dumps(state, indent=1))
        print(f"{args.label}: submitted {state['operation_id']} at {state['submitted_at']}")
    token, topic_id, op = state["token"], state["topic_id"], state["operation_id"]
    t0 = datetime.fromisoformat(state["submitted_at"])
    cancelled_reason = None
    usage: dict = {}
    while True:
        record = must(http("GET", f"/v2/research/topics/{topic_id}/runs/{op}", token=token), what="poll")["operation"]
        usage = progress_usage(topic_id, op, token) or usage
        wall = (datetime.now(timezone.utc) - t0).total_seconds()
        print(f"{now()} {record['status']} wall={wall:.0f}s usage={usage}", flush=True)
        if record["status"] in TERMINAL:
            break
        reason = None
        if wall > GUARD_WALL_SECONDS:
            reason = f"experiment guard: wall time > {GUARD_WALL_SECONDS}s"
        elif usage.get("total_tokens", 0) > GUARD_TOTAL_TOKENS:
            reason = f"experiment guard: total tokens > {GUARD_TOTAL_TOKENS}"
        if reason and cancelled_reason is None:
            cancelled_reason = reason
            print("CANCEL:", reason)
            http("POST", f"/v1/operations/{op}/cancel", {"cancel_token": record["cancel_token"]}, token,
                 {"Idempotency-Key": "reval365-cancel-" + uuid.uuid4().hex})
        time.sleep(POLL_SECONDS)
    finished = now()
    timing = {"submitted_at": state["submitted_at"], "finished_at": finished,
              "wall_seconds": (datetime.fromisoformat(finished) - t0).total_seconds(), "cancelled_reason": cancelled_reason}
    (label_dir / "timing.json").write_text(json.dumps(timing, indent=1))
    topic = must(http("GET", f"/v2/research/topics/{topic_id}", token=token), what="final topic")
    op_ids = [op]
    export_run(label_dir, topic_id, token, op_ids)
    turn = next((t for v in reversed(topic["versions"]) for t in v["content"]["turns"] if op in t.get("operation_ids", [])), None)
    final_usage = (turn or {}).get("usage") or usage
    summary = {"label": args.label, "topic_id": topic_id, "operation_ids": op_ids, "operation_status": record["status"],
               "turn_status": (turn or {}).get("status"), "reason": (turn or {}).get("reason"),
               "gap_codes": [g.get("code") for g in (turn or {}).get("gaps", [])],
               "model_calls": final_usage.get("model_calls"), "search_calls": final_usage.get("search_calls"),
               "total_tokens": final_usage.get("total_tokens"), "wall_seconds": timing["wall_seconds"],
               "cancelled_reason": cancelled_reason}
    (label_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("dryrun")
    run = sub.add_parser("run")
    run.add_argument("--label", required=True)
    run.add_argument("--question", required=True)
    args = parser.parse_args()
    return cmd_dryrun(args) if args.cmd == "dryrun" else cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
