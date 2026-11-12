from common import *
import importlib.util
import json

mb = (HERE / "manifest.json").read_bytes(); manifest = json.loads(mb)
assert all(digest((HERE / f).read_bytes()) == h for f, h in manifest["files"].items())
assert all(digest((SOURCE_DIR / f).read_bytes()) == h for f, h in manifest["source_files"].items())
assert json.loads((HERE / "run-started.json").read_text())["manifest_sha256"] == digest(mb)
spec = importlib.util.spec_from_file_location("original_score", SOURCE_DIR / "score.py")
scorer = importlib.util.module_from_spec(spec); spec.loader.exec_module(scorer)
_, sources = get_task("PCE_2022_07"); results = {}
for condition in manifest["conditions"]:
    name = condition["name"]; path = HERE / (name + "-result.json")
    if not path.exists():
        results[name] = {"status": "not_run"}; continue
    out = json.loads(path.read_text()); assert out["manifest_sha256"] == digest(mb)
    if out["status"] != "completed":
        results[name] = {"status": out["status"]}; continue
    claims = [*out["claims"], *out["counterarguments"]]
    results[name] = scorer.score("PCE_2022_07", {"content": {"claims": claims}}, sources[0])
(HERE / "score.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(results, ensure_ascii=False, indent=2))
