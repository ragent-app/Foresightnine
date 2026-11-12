from common import *
import subprocess
from app.research_verification import REVIEW_METHOD, REVIEW_SYSTEM_PROMPT

prior = json.loads((HERE.parent / "158-counterpoint-paired" / "manifest.json").read_text())
conditions = []
for name in ("PCE_2022_07", "FOMC_2022_06"):
    task, sources = get_task(name)
    prompts = []
    result = run_analysis(task, sources, lambda p: (prompts.append(p) or {}, 0))
    assert len(prompts) == 1
    conditions.append({"task": name, "prompt_sha256": digest(prompts[0].encode()), "prompt_bytes": len(prompts[0].encode()),
                       "segments": result.segments, "source_hashes": {s.source_id: s.content_sha256 for s in sources}})
files = ["common.py", "prepare.py", "run.py", "score.py", "tasks.json", "reference.json", "bea-2022-07.pdf", "fed-2022-06.pdf"]
m = {"implementation_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
     "model": prior["model"], "generation_parameters": prior["generation_parameters"],
     "system_sha256": digest(SYSTEM_PROMPT.encode()), "review_method": REVIEW_METHOD, "review_system_sha256": digest(REVIEW_SYSTEM_PROMPT.encode()),
     "files": {f: digest((HERE / f).read_bytes()) for f in files}, "conditions": conditions,
     "input_limit": 3000, "output_limit": 4000, "max_model_calls_per_task": 2, "max_calls": 4,
     "per_task_token_budget": 10000, "max_total_tokens": 20000, "run_elapsed_seconds": 120,
     "timeout_seconds": 60, "search_calls": 0, "retries": 0, "cost_usd": "unknown", "previous_known_tokens": 174518}
(HERE / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n")
