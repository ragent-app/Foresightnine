from pathlib import Path
from hashlib import sha256
import json
import sys
from pydantic import ValidationError

HERE=Path(__file__).resolve().parent; ROOT=HERE.parents[3]; digest=lambda b:sha256(b).hexdigest()
sys.path.insert(0,str(ROOT/"apps/api"))
from app.research_verification import ReviewOutput  # noqa: E402
mb=(HERE/"manifest.json").read_bytes();m=json.loads(mb);mh=digest(mb)
assert all(digest((HERE/f).read_bytes())==h for f,h in m["files"].items())
assert all(digest((HERE.parent/f).read_bytes())==h for f,h in m["source_files"].items())
assert json.loads((HERE/"run-started.json").read_text())["manifest_sha256"]==mh
results={}
for condition in m["conditions"]:
    name=condition["name"];path=HERE/(name+"-result.json")
    if not path.exists():results[name]={"status":"not_run"};continue
    out=json.loads(path.read_text());assert out["manifest_sha256"]==mh
    if out["status"]!="completed":results[name]={"status":out["status"]};continue
    try:
        parsed=ReviewOutput.model_validate(out["raw"]);verdicts={r.claim_index:r.verdict for r in parsed.reviews}
        expected=set(range(4)) if name=="batched" else {0}
        if len(verdicts)!=len(parsed.reviews) or not verdicts or not set(verdicts).issubset(expected):raise ValueError("invalid indexes")
        target=1 if name=="batched" else 0; verdict=verdicts.get(target)
        results[name]={"valid":True,"verdicts":verdicts,"missing_indexes":sorted(expected-set(verdicts)),
                       "target_verdict":verdict,"target_uncertain_or_unsupported":verdict in ("uncertain","does_not_support")}
    except (ValidationError,ValueError):results[name]={"valid":False,"reason":"invalid_schema_or_indexes"}
(HERE/"score.json").write_text(json.dumps(results,ensure_ascii=False,indent=2)+"\n")
print(json.dumps(results,ensure_ascii=False,indent=2))
