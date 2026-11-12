from pathlib import Path
from hashlib import sha256
import json
import sys

HERE = Path(__file__).resolve().parent; ROOT = HERE.parents[3]; digest = lambda b: sha256(b).hexdigest()
sys.path.insert(0, str(ROOT/"apps/api"))
from app.research_verification import ReviewOutput
mb = (HERE/"manifest.json").read_bytes(); m = json.loads(mb); mh = digest(mb)
assert all(digest((HERE/f).read_bytes()) == h for f,h in m["files"].items())
assert json.loads((HERE/"run-started.json").read_text())["manifest_sha256"] == mh
expected = {case["index"]:case["expected"] for case in m["cases"]}; scores = {}
for condition in m["conditions"]:
    name = condition["name"]; path = HERE/f"{name}-result.json"; indexes = condition["indexes"]
    if not path.exists(): scores[name] = {"status":"not_run"}; continue
    out = json.loads(path.read_text()); assert out["manifest_sha256"] == mh
    if out["status"] != "completed": scores[name] = {"status":out["status"]}; continue
    try:
        entries = ReviewOutput.model_validate(out["raw"]).reviews
        local = {v.claim_index:v.verdict for v in entries}
        if len(local) != len(entries) or set(local) != set(range(len(indexes))): raise ValueError("missing, duplicate or invalid index")
        mapped = {indexes[i]:verdict for i,verdict in local.items()}
        checks = {index:(verdict == "supports" if expected[index] == "supports" else verdict != "supports") for index,verdict in mapped.items()}
        scores[name] = {"status":"completed","verdicts":mapped,"checks":checks,"passed":sum(checks.values()),"total":len(checks)}
    except Exception as error: scores[name] = {"status":"invalid", "reason":type(error).__name__}
(HERE/"score.json").write_text(json.dumps(scores,ensure_ascii=False,indent=2)+"\n")
print(json.dumps(scores,ensure_ascii=False,indent=2))
