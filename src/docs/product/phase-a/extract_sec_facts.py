"""Fetch SEC company facts once and retain bounded facts plus provenance.

Run only with a real contact in SEC_USER_AGENT, per SEC fair-access policy.
No source documents, credentials, or environment values are written.
"""
import datetime
import hashlib
import json
import os
import pathlib
import time
import urllib.request

URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json"
TARGET = pathlib.Path(__file__).with_name("source-probe-sec-operating-2026-11-11.json")
ACCNS = {"0000950170-23-035122", "0000950170-24-087843"}
TAGS = {"RevenueFromContractWithCustomerExcludingAssessedTax", "OperatingIncomeLoss"}


def main():
    agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not agent or "@" not in agent:
        raise SystemExit("SEC_USER_AGENT must include the operator's real contact email")
    if TARGET.exists():
        raise SystemExit("Output already exists; preserve existing evidence")
    started = time.monotonic()
    retrieved = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": agent}), timeout=20) as response:
        raw = response.read()
        payload = json.loads(raw)
        status = response.status
    facts = []
    for tag in sorted(TAGS):
        for item in payload["facts"]["us-gaap"][tag]["units"]["USD"]:
            if item.get("accn") in ACCNS and item.get("form") == "10-K":
                facts.append({"tag": tag, **{k: item.get(k) for k in ("start", "end", "val", "accn", "form", "filed")}})
    record = {"source": URL, "retrieved_at_utc": retrieved, "status": status,
              "source_bytes": len(raw), "source_sha256": hashlib.sha256(raw).hexdigest(),
              "duration_seconds": round(time.monotonic() - started, 3), "units": "USD",
              "facts": facts, "method": "one GET, no retries; full response not retained"}
    with TARGET.open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    print(f"Saved {len(facts)} bounded facts to {TARGET}")


if __name__ == "__main__":
    main()
