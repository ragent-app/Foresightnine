"""Build synthetic failure cases; optional real facts remain cutoff scoped."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALLOWED = {"msft-fy2023.json"}


def utc(value):
    result = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp needs timezone")
    return result.astimezone(datetime.timezone.utc)


def build(task_id):
    raw = (ROOT / "failure_tasks_v01.json").read_bytes()
    bank = json.loads(raw)
    matches = [task for task in bank["tasks"] if task["id"] == task_id]
    if len(matches) != 1:
        raise ValueError("task missing or ambiguous")
    task = matches[0]
    if task["fixture"].get("synthetic") is not True:
        raise ValueError("simulated fixtures must be explicitly labeled")
    sources = []
    for filename in task["sources"]:
        if filename not in ALLOWED:
            raise ValueError("source outside allowlist")
        content = (ROOT / filename).read_bytes()
        data = json.loads(content)
        if utc(data["as_of"]) > utc(task["as_of"]):
            raise ValueError("source after cutoff")
        sources.append({"filename": filename, "sha256": hashlib.sha256(content).hexdigest(), "data": data})
    return {"status":bank["status"], "task_bank_sha256":hashlib.sha256(raw).hexdigest(), "task":task,
            "source_snapshots":sources,
            "limitations":["Synthetic fixtures represent test inputs, not real tool outcomes.",
                           "Derived facts require independent review and source permission checks."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    print(json.dumps(build(parser.parse_args().task_id), ensure_ascii=False, indent=2))
