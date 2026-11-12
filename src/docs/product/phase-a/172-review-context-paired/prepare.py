from common import *
import subprocess

claims, sources, batch, prompts = inputs()
left, right = json.loads(prompts["contextual"]), json.loads(prompts["quotation_only"])
for a, b in zip(left["claims"], right["claims"], strict=True):
    for x, y in zip(a["excerpts"], b["excerpts"], strict=True):
        x["preceding_context"] = x["following_context"] = ""
assert left == right
for name, prompt in prompts.items(): (HERE / (name + "-prompt.json")).write_text(prompt)
prior = json.loads((BASE / "manifest.json").read_text())
files = ["common.py", "prepare.py", "run.py", "score.py", "contextual-prompt.json", "quotation_only-prompt.json"]
upstream = {str(path.relative_to(HERE.parent)): digest(path.read_bytes()) for path in
            (BASE / "common.py", BASE / "manifest.json", HERE.parent / "168-full-review-diagnostic" / "common.py",
             HERE.parent / "168-full-review-diagnostic" / "PCE_2022_07-result.json",
             HERE.parent / "161-fresh-source-evaluation" / "tasks.json", HERE.parent / "161-fresh-source-evaluation" / "bea-2022-07.pdf")}
m = {"implementation_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
     "model": prior["model"], "generation_parameters": prior["generation_parameters"],
     "system_sha256": digest(REVIEW_SYSTEM_PROMPT.encode()), "review_method": REVIEW_METHOD,
     "files": {f: digest((HERE / f).read_bytes()) for f in files}, "source_files": upstream,
     "source_hashes": {s.source_id: s.content_sha256 for s in sources}, "original_indexes": batch.indexes,
     "segments": batch.segments, "conditions": [{"name": name, "prompt_sha256": digest(prompts[name].encode()),
     "prompt_bytes": len(prompts[name].encode())} for name in ("contextual", "quotation_only")],
     "input_limit": 6000, "output_limit": 4000, "max_calls": 2, "per_call_max_tokens": 10000,
     "max_total_tokens": 20000, "timeout_seconds": 60, "retries": 0, "cost_usd": "unknown", "previous_known_tokens": 192047}
(HERE / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n")
