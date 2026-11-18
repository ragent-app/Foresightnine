"""Build a scoped task input from draft, local derived fact snapshots only."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALLOWED = {"msft-fy2023.json", "msft-fy2024.json"}


def parse_utc(value):
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Every cutoff and snapshot timestamp needs a timezone")
    return parsed.astimezone(datetime.timezone.utc)


def build(task_id):
    raw_bank = (ROOT / "pilot_tasks.json").read_bytes()
    bank = json.loads(raw_bank)
    matches = [task for task in bank["tasks"] if task["id"] == task_id]
    if len(matches) != 1:
        raise ValueError("Task ID missing or ambiguous")
    task = matches[0]
    cutoff = parse_utc(task["as_of"])
    sources = []
    for name in task["sources"]:
        if name not in ALLOWED:
            raise ValueError("Snapshot is outside the allowed set")
        raw = (ROOT / name).read_bytes()
        document = json.loads(raw)
        if parse_utc(document["as_of"]) > cutoff:
            raise ValueError("Snapshot is newer than the task cutoff")
        sources.append({"filename": name, "sha256": hashlib.sha256(raw).hexdigest(), "data": document})
    return {"status": bank["status"], "task_bank_sha256": hashlib.sha256(raw_bank).hexdigest(),
            "task": task, "source_snapshots": sources,
            "limitations": ["Derived facts, not original filings; human review and licensing pending.",
                            "Time-filtering supplied materials does not eliminate model training leakage."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    args = parser.parse_args()
    print(json.dumps(build(args.task_id), ensure_ascii=False, indent=2))
