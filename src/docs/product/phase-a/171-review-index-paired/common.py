from pathlib import Path
from hashlib import sha256
import importlib.util
import json
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
OLD = HERE.parent / "168-full-review-diagnostic"
SOURCE = HERE.parent / "161-fresh-source-evaluation"
sys.path.insert(0, str(ROOT / "apps/api"))
from app.research_verification import prepare_support_review, REVIEW_SYSTEM_PROMPT, REVIEW_METHOD  # noqa: E402


def digest(data):
    return sha256(data).hexdigest()


def inputs():
    spec = importlib.util.spec_from_file_location("source168_common", OLD / "common.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    _, sources = module.get_task("PCE_2022_07")
    content = json.loads((OLD / "PCE_2022_07-result.json").read_text())["content"]
    claims = content["claims"]
    # A synthetic eligibility restriction recreates a skipped first item;
    # it is not a statement that this claim is numerically incorrect.
    claims = [{**item, **({"numeric_discrepancy_for_review": True} if i == 0 else {})} for i, item in enumerate(claims)]
    batch = prepare_support_review(claims, sources, 6000-len(REVIEW_SYSTEM_PROMPT.encode())-512)
    assert batch.indexes == [1, 2, 3, 4, 5]
    local = json.loads(batch.prompt)
    original = json.loads(batch.prompt)
    for row in original["claims"]:
        row["claim_index"] = batch.indexes[row["claim_index"]]
    return claims, sources, batch, {"original": json.dumps(original, ensure_ascii=False, separators=(",", ":")), "local": batch.prompt}
