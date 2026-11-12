from pathlib import Path
from unittest.mock import patch
import json
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE.parent / "151-full-pipeline-recheck"))
from common import digest, get_task, run_analysis, SYSTEM_PROMPT  # noqa: E402
import app.research_analysis as analysis  # noqa: E402

task, sources = get_task("STRESS_2023")
conditions = []
for name in ("baseline", "candidate"):
    prompts = []
    with patch.object(analysis, "_replace_empty_table_with_counterpoint", side_effect=lambda _t, _q, windows, _s=None: windows) if name == "baseline" else patch.object(analysis, "_replace_empty_table_with_counterpoint", wraps=analysis._replace_empty_table_with_counterpoint):
        result = run_analysis(task, sources, lambda prompt: (prompts.append(prompt) or {}, 0))
    assert len(prompts) == 1
    conditions.append({"name": name, "prompt_sha256": digest(prompts[0].encode()), "prompt_bytes": len(prompts[0].encode()), "segments": result.segments})
assert conditions[0]["prompt_sha256"] != conditions[1]["prompt_sha256"]
prior = json.loads((HERE.parent / "149-short-reference-paired" / "manifest.json").read_text())
files = ["prepare.py", "run.py"]
manifest = {"implementation_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "model": prior["model"], "generation_parameters": prior["generation_parameters"],
            "system_sha256": digest(SYSTEM_PROMPT.encode()), "source_hashes": {s.source_id: s.content_sha256 for s in sources},
            "task_sha256": digest((HERE.parent / "140-trade-bank-fresh" / "STRESS_2023-input.json").read_bytes()),
            "files": {f: digest((HERE / f).read_bytes()) for f in files}, "conditions": conditions,
            "input_limit": 3000, "output_limit": 4000, "max_calls": 2, "max_total_tokens": 20000,
            "timeout_seconds": 60, "retries": 0, "cost_usd": "unknown", "previous_known_tokens": 171980}
(HERE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
