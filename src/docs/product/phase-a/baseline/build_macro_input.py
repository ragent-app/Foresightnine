"""Construct macro pilot inputs with explicit snapshot and publication cutoffs."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALLOWED = {"macro-bea-2023-advance.json", "macro-bea-2023-second.json",
           "macro-bea-2024-advance.json", "macro-fed-2024-09.json"}
SNAPSHOTS = {"../source-snapshots/bea-gdp-2023-q4-advance.pdf",
             "../source-snapshots/bea-gdp-2023-q4-second.pdf",
             "../source-snapshots/bea-gdp-2024-q4-advance.pdf",
             "../source-snapshots/fed-fomc-2024-09-18.pdf"}


def utc(text):
    result = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return result.astimezone(datetime.timezone.utc)


def build(task_id):
    bank_bytes = (ROOT / "macro_tasks_v01.json").read_bytes()
    bank = json.loads(bank_bytes)
    matches = [item for item in bank["tasks"] if item["id"] == task_id]
    if len(matches) != 1:
        raise ValueError("task ID missing or ambiguous")
    task = matches[0]
    cutoff = utc(task["as_of"])
    sources = []
    for filename in task["sources"]:
        if filename not in ALLOWED:
            raise ValueError("source outside allowlist")
        raw = (ROOT / filename).read_bytes()
        source = json.loads(raw)
        if utc(source["source_publication"]) > utc(source["as_of"]) or utc(source["as_of"]) > cutoff:
            raise ValueError("source published or snapshot allowed after cutoff")
        if "source_snapshot" in source:
            if source["source_snapshot"] not in SNAPSHOTS:
                raise ValueError("original outside allowlist")
            original = (ROOT / source["source_snapshot"]).read_bytes()
            if hashlib.sha256(original).hexdigest() != source["source_sha256"]:
                raise ValueError("original snapshot hash mismatch")
        sources.append({"filename": filename, "sha256": hashlib.sha256(raw).hexdigest(), "data": source})
    return {"status": bank["status"], "task_bank_sha256": hashlib.sha256(bank_bytes).hexdigest(),
            "task": task, "source_snapshots": sources,
            "limitations": ["Extracted facts require independent review; present-day file hash does not prove historical immutability.",
                            "Time-filtered materials do not eliminate model training leakage."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    print(json.dumps(build(parser.parse_args().task_id), ensure_ascii=False, indent=2))
