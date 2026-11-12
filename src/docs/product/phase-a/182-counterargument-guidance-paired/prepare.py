from common import *
from app.research_analysis import SYSTEM_PROMPT
import subprocess

task, sources = get_task("PCE_2022_07")
prompts = []
run_analysis(task, sources, lambda prompt: (prompts.append(prompt) or {}, 0))
assert len(prompts) == 1
baseline = json.loads(prompts[0]); candidate = json.loads(prompts[0])
candidate["evidence_guidance"] += (" When a claim has a material opposing observation in the supplied excerpts, "
    "cite that observation as a counterargument and explain the contrast. Do not relabel the same point or "
    "an unsupported data gap as counterevidence. If no opposing observation is present, return no counterargument "
    "and state the gap in limitations.")
assert all(baseline[k] == candidate[k] for k in baseline if k != "evidence_guidance")
conditions = []
for name, value in (("baseline", baseline), ("candidate", candidate)):
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    assert len(text.encode()) <= 6000 - len(SYSTEM_PROMPT.encode())
    path = HERE / f"{name}-prompt.json"; path.write_text(text)
    conditions.append({"name":name,"prompt_sha256":digest(text.encode()),"prompt_bytes":len(text.encode())})
prior = json.loads((HERE.parent / "178-review-fresh-cases/manifest.json").read_text())
files = ["common.py", "prepare.py", "run.py", "score.py", "baseline-prompt.json", "candidate-prompt.json"]
source_files = ["tasks.json", "reference.json", "bea-2022-07.pdf"]
manifest = {"implementation_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
    "model":prior["model"],"generation_parameters":prior["generation_parameters"],"system_sha256":digest(SYSTEM_PROMPT.encode()),
    "files":{name:digest((HERE/name).read_bytes()) for name in files},
    "source_files":{name:digest((SOURCE/name).read_bytes()) for name in source_files},
    "conditions":conditions,"input_limit":6000,"output_limit":4000,"max_calls":2,"max_total_tokens":20000,
    "per_call_max_tokens":10000,"timeout_seconds":60,"retries":0,"cost_usd":"unknown","previous_known_tokens":203705}
(HERE/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n")
