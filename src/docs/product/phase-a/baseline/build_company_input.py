"""Build scoped company-task payloads from explicitly allowed draft facts."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALLOWED = {"msft-2023-details.json", "msft-2024-details.json", "apple-2024-details.json"}


def utc(value):
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(datetime.timezone.utc)


def build(task_id):
    raw = (ROOT / "company_tasks_v01.json").read_bytes()
    bank = json.loads(raw)
    matching = [task for task in bank["tasks"] if task["id"] == task_id]
    if len(matching) != 1:
        raise ValueError("task missing or ambiguous")
    task = matching[0]
    cutoff = utc(task["as_of"])
    sources = []
    for name in task["sources"]:
        if name not in ALLOWED:
            raise ValueError("source outside allowlist")
        content = (ROOT / name).read_bytes()
        data = json.loads(content)
        if utc(data["as_of"]) > cutoff:
            raise ValueError("source snapshot after cutoff")
        sources.append({"filename": name, "sha256": hashlib.sha256(content).hexdigest(), "data": data})
    return {"status": bank["status"], "task_bank_sha256": hashlib.sha256(raw).hexdigest(), "task": task,
            "source_snapshots": sources,
            "limitations": ["Derived facts, not original documents; source timing, permissions and human review pending.",
                            "Time-filtered input does not prevent model pretraining leakage."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    print(json.dumps(build(parser.parse_args().task_id), ensure_ascii=False, indent=2))
