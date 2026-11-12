from common import *
import subprocess

prompts = inputs(); baseline = json.loads(prompts["contextual"]); reduced = json.loads(prompts["required_only"])
for left, right in zip(baseline["claims"], reduced["claims"], strict=True):
    for a, b in zip(left["excerpts"], right["excerpts"], strict=True):
        assert a["quotation"] == b["quotation"] and a["source_id"] == b["source_id"]
        a["preceding_context"], a["following_context"] = b["preceding_context"], b["following_context"]
assert baseline == reduced
for name, prompt in prompts.items(): (HERE / (name + "-prompt.json")).write_text(prompt)
prior = json.loads((BASE / "manifest.json").read_text())
files = ["fixture.json", "common.py", "prepare.py", "run.py", "score.py", "contextual-prompt.json", "required_only-prompt.json"]
upstream = {str(path.relative_to(HERE.parent)): digest(path.read_bytes()) for path in (BASE / "local-prompt.json", BASE / "manifest.json")}
m = {"implementation_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
     "model": prior["model"], "generation_parameters": prior["generation_parameters"],
     "system_sha256": digest(REVIEW_SYSTEM_PROMPT.encode()), "review_method": REVIEW_METHOD,
     "files": {f: digest((HERE / f).read_bytes()) for f in files}, "source_files": upstream,
     "conditions": [{"name": n, "prompt_sha256": digest(prompts[n].encode()), "prompt_bytes": len(prompts[n].encode())}
                    for n in ("contextual", "required_only")], "input_limit": 6000, "output_limit": 4000,
     "max_calls": 2, "per_call_max_tokens": 10000, "max_total_tokens": 20000, "timeout_seconds": 60,
     "retries": 0, "cost_usd": "unknown", "previous_known_tokens": 194626}
(HERE / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n")
