from pathlib import Path
from hashlib import sha256
import json
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / "apps/api"))
sys.path.insert(0, str(HERE.parent / "161-fresh-source-evaluation"))
from common import get_task
from app.research_verification import REVIEW_SYSTEM_PROMPT, REVIEW_METHOD, prepare_support_review, prepare_isolated_support_reviews

digest = lambda b: sha256(b).hexdigest()
_, sources = get_task("FOMC_2022_06")
source = sources[0]
items = [
    ("Inflation remained elevated, according to the statement.", "Inflation remains elevated, reflecting supply and demand imbalances\nrelated to the pandemic, higher energy prices, and broader price pressures.", "supports", "support"),
    ("Inflation remained elevated solely because of demand, with no supply-side contribution.", "Inflation remains elevated, reflecting supply and demand imbalances\nrelated to the pandemic, higher energy prices, and broader price pressures.", "not_supports", "counterargument"),
    ("Esther L. George voted against the monetary policy action.", "Voting against this action was Esther L. George, who preferred at this\nmeeting to raise the target range for the federal funds rate by 0.5 percentage point to\n1-1/4 percent to 1-1/2 percent.", "supports", "support"),
    ("The Federal Open Market Committee unanimously voted to raise the federal funds target range.", "The Board of Governors of the Federal Reserve System voted unanimously to raise\nthe interest rate paid on reserve balances to 1.65 percent, effective June 16, 2022.", "not_supports", "counterargument"),
]
claims = []
for text, quotation, _, role in items:
    start = source.text.index(quotation)
    assert source.text.count(quotation) == 1
    claims.append({"text": text, "role": role, "citations": [{"source_id": source.source_id, "quotation": quotation,
        "location": {"character_start": start, "character_end": start + len(quotation), "status": "located"}}]})
batched = prepare_support_review(claims, sources, 6000 - len(REVIEW_SYSTEM_PROMPT.encode()) - 512)
isolated = prepare_isolated_support_reviews(claims, sources, 6000 - len(REVIEW_SYSTEM_PROMPT.encode()) - 512)
assert sorted(batched.indexes) == list(range(4)) and not batched.omissions
assert sorted(b.indexes[0] for b in isolated.batches) == list(range(4)) and not isolated.omissions
batches = [("batch", batched)] + [(f"isolated-{b.indexes[0]}", b) for b in isolated.batches]
for name, batch in batches:
    (HERE / f"{name}-prompt.json").write_text(batch.prompt)
files = ["prepare.py", "run.py", "score.py"] + [f"{name}-prompt.json" for name, _ in batches]
prior = json.loads((HERE.parent / "175-review-batching-paired/manifest.json").read_text())
manifest = {"implementation_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
    "model": prior["model"], "generation_parameters": prior["generation_parameters"], "review_method": REVIEW_METHOD,
    "system_sha256": digest(REVIEW_SYSTEM_PROMPT.encode()), "source_pdf_sha256": digest((HERE.parent / "161-fresh-source-evaluation/fed-2022-06.pdf").read_bytes()),
    "source_text_sha256": digest(source.text.encode()), "files": {name:digest((HERE/name).read_bytes()) for name in files},
    "cases": [{"index":i,"expected":item[2]} for i,item in enumerate(items)],
    "conditions": [{"name":name,"indexes":batch.indexes,"prompt_sha256":batch.prompt_sha256} for name,batch in batches],
    "input_limit":6000,"output_limit":4000,"max_calls":5,"per_call_max_tokens":10000,"max_total_tokens":50000,
    "timeout_seconds":60,"retries":0,"cost_usd":"unknown","previous_known_tokens":197246}
(HERE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
