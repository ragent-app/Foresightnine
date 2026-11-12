from common import *
from app.research_verification import ReviewOutput
from pydantic import ValidationError

mb = (HERE / "manifest.json").read_bytes(); m = json.loads(mb); mh = digest(mb)
assert all(digest((HERE / f).read_bytes()) == h for f, h in m["files"].items())
assert all(digest((HERE.parent / f).read_bytes()) == h for f, h in m["source_files"].items())
assert json.loads((HERE / "run-started.json").read_text())["manifest_sha256"] == mh
allowed = {0: {"supports"}, 1: {"does_not_support", "uncertain"}, 2: {"supports"}, 3: {"does_not_support", "uncertain", "contradicts"}}
results = {}
for condition in m["conditions"]:
    name = condition["name"]; path = HERE / (name + "-result.json")
    if not path.exists(): results[name] = {"status": "not_run"}; continue
    out = json.loads(path.read_text()); assert out["manifest_sha256"] == mh
    if out["status"] != "completed": results[name] = {"status": out["status"]}; continue
    try:
        parsed = ReviewOutput.model_validate(out["raw"]); verdicts = {r.claim_index: r.verdict for r in parsed.reviews}
        if len(verdicts) != len(parsed.reviews) or set(verdicts) != set(allowed): raise ValueError("incomplete")
        results[name] = {"valid": True, "verdicts": verdicts, "checks": {str(i): verdicts[i] in allowed[i] for i in allowed}}
    except (ValueError, ValidationError): results[name] = {"valid": False, "reason": "invalid_schema_or_indexes"}
(HERE / "score.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(results, ensure_ascii=False, indent=2))
