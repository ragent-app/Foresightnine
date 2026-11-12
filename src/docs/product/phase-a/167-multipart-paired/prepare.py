from common import *
import json
import subprocess

task, sources = get_task("PCE_2022_07")
conditions = []
for name in ("baseline", "candidate"):
    prompts = []
    with selected_context(name):
        result = run_analysis(task, sources, lambda p: (prompts.append(p) or {}, 0))
    assert len(prompts) == 1
    conditions.append({"name": name, "prompt_sha256": digest(prompts[0].encode()),
                       "prompt_bytes": len(prompts[0].encode()), "segments": result.segments})
assert conditions[0]["prompt_sha256"] != conditions[1]["prompt_sha256"]
prior = json.loads((HERE.parent / "161-fresh-source-evaluation" / "manifest.json").read_text())
files = ["common.py", "prepare.py", "run.py", "score.py"]
source_files = ["tasks.json", "reference.json", "bea-2022-07.pdf", "common.py", "score.py"]
m = {"implementation_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
     "model": prior["model"], "generation_parameters": prior["generation_parameters"],
     "system_sha256": digest(SYSTEM_PROMPT.encode()), "source_hashes": {s.source_id: s.content_sha256 for s in sources},
     "files": {f: digest((HERE / f).read_bytes()) for f in files},
     "source_files": {f: digest((SOURCE_DIR / f).read_bytes()) for f in source_files},
     "conditions": conditions, "input_limit": 6000, "output_limit": 4000, "max_calls": 2,
     "per_call_max_tokens": 10000, "max_total_tokens": 20000, "timeout_seconds": 60,
     "retries": 0, "search_calls": 0, "cost_usd": "unknown", "previous_known_tokens": 178711}
(HERE / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n")
