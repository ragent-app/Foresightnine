from pathlib import Path
from hashlib import sha256
import json
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
BASE = HERE.parent / "171-review-index-paired"
sys.path.insert(0, str(ROOT / "apps/api"))
from app.research_verification import REVIEW_SYSTEM_PROMPT, REVIEW_METHOD  # noqa: E402


def digest(data): return sha256(data).hexdigest()


def inputs():
    prior = json.loads((BASE / "local-prompt.json").read_text())
    # Original research indexes 2 and 3: a cited savings fact and a claim
    # whose quotation omits the asserted nominal spending increase.
    real = [prior["claims"][1], prior["claims"][2]]
    fixture = json.loads((HERE / "fixture.json").read_text())
    text, quote = fixture["text"], fixture["quotation"]
    start = text.index(quote); end = start + len(quote)
    assert text[:start].endswith("River's ")
    synthetic = [{"claim_index": i + 2, "text": claim, "excerpts": [{"source_id": fixture["source_id"],
                  "preceding_context": text[:start], "quotation": quote, "following_context": text[end:]}]}
                 for i, claim in enumerate(fixture["claims"])]
    for i, item in enumerate(real): item["claim_index"] = i
    baseline = {"claims": [*real, *synthetic], "output_schema": prior["output_schema"]}
    reduced = json.loads(json.dumps(baseline))
    for item in reduced["claims"]:
        for excerpt in item["excerpts"]:
            excerpt["preceding_context"] = "River's " if excerpt["source_id"] == fixture["source_id"] else ""
            excerpt["following_context"] = ""
    # Explicit synthetic provenance appears in the source id and in the
    # full-context input. Neither condition is persisted as product evidence.
    return {"contextual": json.dumps(baseline, ensure_ascii=False, separators=(",", ":")),
            "required_only": json.dumps(reduced, ensure_ascii=False, separators=(",", ":"))}
