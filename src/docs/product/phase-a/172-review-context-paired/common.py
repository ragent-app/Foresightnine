from pathlib import Path
from hashlib import sha256
import importlib.util
import json
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
BASE = HERE.parent / "171-review-index-paired"
sys.path.insert(0, str(ROOT / "apps/api"))
from app.research_verification import REVIEW_SYSTEM_PROMPT, REVIEW_METHOD  # noqa: E402


def digest(data): return sha256(data).hexdigest()


def inputs():
    spec = importlib.util.spec_from_file_location("source171_common", BASE / "common.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    claims, sources, batch, prior_prompts = module.inputs()
    assert batch.indexes == [1, 2, 3, 4, 5]
    # This controlled sample has no required subject context. Do not extend
    # this test's removal of optional context to required context in product.
    assert all("required_context_start" not in s and "required_context_end" not in s for s in batch.segments)
    contextual = prior_prompts["local"]
    quoted_only = json.loads(contextual)
    for claim in quoted_only["claims"]:
        for excerpt in claim["excerpts"]:
            excerpt["preceding_context"] = ""
            excerpt["following_context"] = ""
    return claims, sources, batch, {"contextual": contextual,
        "quotation_only": json.dumps(quoted_only, ensure_ascii=False, separators=(",", ":"))}
