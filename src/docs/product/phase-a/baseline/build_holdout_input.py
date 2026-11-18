"""Build candidate holdout inputs; do not run against models before freezing."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALLOWED = {"holdout-walmart-2024.json", "holdout-coca-cola-2024.json",
           "macro-bea-2023-advance.json", "macro-fed-2024-09.json"}


def utc(value):
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs timezone")
    return parsed.astimezone(datetime.timezone.utc)


def build(task_id):
    raw = (ROOT / "holdout_tasks_v01.json").read_bytes()
    bank = json.loads(raw)
    matches = [item for item in bank["tasks"] if item["id"] == task_id]
    if len(matches) != 1:
        raise ValueError("task missing or ambiguous")
    task = matches[0]
    sources = []
    for filename in task["sources"]:
        if filename not in ALLOWED:
            raise ValueError("source outside allowlist")
        data_bytes = (ROOT / filename).read_bytes()
        source = json.loads(data_bytes)
        if utc(source["as_of"]) > utc(task["as_of"]):
            raise ValueError("source after cutoff")
        if "source_publication" in source and utc(source["source_publication"]) > utc(task["as_of"]):
            raise ValueError("publication after cutoff")
        sources.append({"filename":filename,"sha256":hashlib.sha256(data_bytes).hexdigest(),"data":source})
    return {"status":bank["status"],"task_bank_sha256":hashlib.sha256(raw).hexdigest(),
            "task":task,"source_snapshots":sources,
            "limitations":["Candidate holdout is public and unreviewed, not a frozen blind test.",
                           "Snapshot timestamps and pretraining knowledge do not prove historical isolation."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    print(json.dumps(build(parser.parse_args().task_id), ensure_ascii=False, indent=2))
