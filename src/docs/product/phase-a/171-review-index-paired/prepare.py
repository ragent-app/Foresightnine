from common import *
import subprocess

claims, sources, batch, prompts = inputs()
original, local = json.loads(prompts["original"]), json.loads(prompts["local"])
for left, right in zip(original["claims"], local["claims"], strict=True):
    assert left.pop("claim_index") == batch.indexes[right.pop("claim_index")]
assert original == local
prior = json.loads((OLD / "manifest.json").read_text())
for name, prompt in prompts.items():
    (HERE / (name + "-prompt.json")).write_text(prompt)
files = ["common.py", "prepare.py", "run.py", "score.py", "original-prompt.json", "local-prompt.json"]
source_files = {str(path.relative_to(HERE.parent)): digest(path.read_bytes()) for path in
                (OLD / "common.py", OLD / "PCE_2022_07-result.json", SOURCE / "tasks.json", SOURCE / "bea-2022-07.pdf")}
m = {"implementation_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
     "model": prior["model"], "generation_parameters": prior["generation_parameters"],
     "system_sha256": digest(REVIEW_SYSTEM_PROMPT.encode()), "review_method": REVIEW_METHOD,
     "files": {f: digest((HERE / f).read_bytes()) for f in files}, "source_files": source_files,
     "source_hashes": {s.source_id: s.content_sha256 for s in sources}, "original_indexes": batch.indexes,
     "segments": batch.segments, "omissions": batch.omissions,
     "conditions": [{"name": name, "prompt_sha256": digest(prompts[name].encode()), "prompt_bytes": len(prompts[name].encode())}
                    for name in ("original", "local")],
     "input_limit": 6000, "output_limit": 4000, "max_calls": 2, "per_call_max_tokens": 10000,
     "max_total_tokens": 20000, "timeout_seconds": 60, "retries": 0, "cost_usd": "unknown", "previous_known_tokens": 189041}
(HERE / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n")
