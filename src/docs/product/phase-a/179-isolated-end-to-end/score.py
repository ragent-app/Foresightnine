from common import *
import importlib.util

mb = (HERE / "manifest.json").read_bytes(); manifest = json.loads(mb)
assert all(digest((HERE / f).read_bytes()) == h for f, h in manifest["files"].items())
assert all(digest((SOURCE / f).read_bytes()) == h for f, h in manifest["source_files"].items())
assert json.loads((HERE / "run-started.json").read_text())["manifest_sha256"] == digest(mb)
out = json.loads((HERE / "PCE_2022_07-result.json").read_text())
assert out["manifest_sha256"] == digest(mb)
spec = importlib.util.spec_from_file_location("score161", SOURCE / "score.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
_, sources = get_task("PCE_2022_07")
summary = module.score("PCE_2022_07", out, sources[0]) if out["status"] == "completed" else {"status": out["status"]}
run = out.get("content", {}).get("run", {})
summary["review_status"] = run.get("support_review_status")
summary["review_omissions"] = run.get("support_review_omissions")
summary["reviewed_verdicts"] = [{"index": i, "role": c.get("role"), "verdict": c.get("support_review", {}).get("verdict")}
                                for i, c in enumerate(out.get("content", {}).get("claims", []))]
(HERE / "score.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(summary, ensure_ascii=False, indent=2))
