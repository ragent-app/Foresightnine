from pathlib import Path
from hashlib import sha256
import json
import subprocess
import sys

HERE = Path(__file__).resolve().parent; ROOT = HERE.parents[3]; BASE = HERE.parent / "174-subject-context-paired"
sys.path.insert(0, str(ROOT / "apps/api"))
from app.research_verification import REVIEW_SYSTEM_PROMPT, REVIEW_METHOD  # noqa: E402
digest = lambda b: sha256(b).hexdigest()
baseline = json.loads((BASE / "required_only-prompt.json").read_text())
target = json.loads(json.dumps(baseline["claims"][1])); assert target["claim_index"] == 1
target["claim_index"] = 0
isolated = {"claims": [target], "output_schema": baseline["output_schema"]}
for name, prompt in (("batched", baseline), ("isolated", isolated)):
    (HERE / (name + "-prompt.json")).write_text(json.dumps(prompt, ensure_ascii=False, separators=(",", ":")))
prior = json.loads((BASE / "manifest.json").read_text())
files = ["prepare.py", "run.py", "score.py", "batched-prompt.json", "isolated-prompt.json"]
upstream = {str(p.relative_to(HERE.parent)): digest(p.read_bytes()) for p in (BASE / "required_only-prompt.json", BASE / "manifest.json")}
m = {"implementation_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
     "model": prior["model"], "generation_parameters": prior["generation_parameters"], "review_method": REVIEW_METHOD,
     "system_sha256": digest(REVIEW_SYSTEM_PROMPT.encode()), "files": {f:digest((HERE/f).read_bytes()) for f in files},
     "source_files": upstream, "conditions": [{"name":n,"prompt_sha256":digest((HERE/(n+"-prompt.json")).read_bytes()),
     "prompt_bytes":(HERE/(n+"-prompt.json")).stat().st_size} for n in ("batched", "isolated")],
     "input_limit":6000,"output_limit":4000,"max_calls":2,"per_call_max_tokens":10000,"max_total_tokens":20000,
     "timeout_seconds":60,"retries":0,"cost_usd":"unknown","previous_known_tokens":196157}
(HERE / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2)+"\n")
