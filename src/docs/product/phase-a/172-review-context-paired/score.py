from common import *
from app.research_verification import InvalidReview, apply_partial_support_review

mb = (HERE / "manifest.json").read_bytes(); m = json.loads(mb); mh = digest(mb)
assert all(digest((HERE / f).read_bytes()) == h for f, h in m["files"].items())
assert all(digest((HERE.parent / f).read_bytes()) == h for f, h in m["source_files"].items())
assert json.loads((HERE / "run-started.json").read_text())["manifest_sha256"] == mh
claims, sources, batch, _ = inputs(); results = {}
for condition in m["conditions"]:
    name = condition["name"]; path = HERE / (name + "-result.json")
    if not path.exists(): results[name] = {"status": "not_run"}; continue
    out = json.loads(path.read_text()); assert out["manifest_sha256"] == mh
    if out["status"] != "completed": results[name] = {"status": out["status"]}; continue
    try:
        reviewed, missing = apply_partial_support_review(claims, batch, out["raw"])
        verdicts = {str(i): c["support_review"]["verdict"] for i, c in enumerate(reviewed) if c.get("support_review")}
        results[name] = {"valid": True, "verdicts_by_original_index": verdicts, "missing_original_indexes": missing,
                         "claim_3_unsupported_or_uncertain": verdicts.get("3") in ("does_not_support", "uncertain")}
    except InvalidReview:
        results[name] = {"valid": False, "reason": "invalid_schema_or_indexes"}
(HERE / "score.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(results, ensure_ascii=False, indent=2))
