from pathlib import Path
from unittest.mock import patch
from dataclasses import replace
from threading import Event
from time import monotonic
import json
import os
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE.parent / "151-full-pipeline-recheck"))
from common import digest, get_task, run_analysis, SYSTEM_PROMPT  # noqa: E402
import app.research_analysis as analysis  # noqa: E402
from app.phase3 import build_llm_provider, ProviderFailure  # noqa: E402
from app.openai_compatible import OpenAICompatibleLlmProvider  # noqa: E402

mb = (HERE / "manifest.json").read_bytes()
m = json.loads(mb)
mh = digest(mb)
assert all(digest((HERE / f).read_bytes()) == h for f, h in m["files"].items())
assert digest(SYSTEM_PROMPT.encode()) == m["system_sha256"]
assert digest((HERE.parent / "140-trade-bank-fresh" / "STRESS_2023-input.json").read_bytes()) == m["task_sha256"]
subprocess.run(["git", "diff", "--quiet", m["implementation_commit"], "--", "apps"], cwd=ROOT, check=True)
for line in json.loads(subprocess.check_output(["docker", "inspect", "--format", "{{json .Config.Env}}", "docker-api-1"])):
    key, sep, value = line.partition("=")
    if sep and (key.startswith("FORESIGHTNINE_LLM_") or key == "FORESIGHTNINE_GLM_CODING_API_KEY"):
        os.environ[key] = value
os.environ["FORESIGHTNINE_LLM_TIMEOUT_SECONDS"] = "60"
provider = build_llm_provider()
assert isinstance(provider, OpenAICompatibleLlmProvider) and provider.settings.model == m["model"]
params = json.loads(provider._request_body(None, "probe", system="probe"))
params.pop("messages")
assert params == m["generation_parameters"] and provider.policy.max_output_tokens == m["output_limit"]
instance = OpenAICompatibleLlmProvider(provider.settings, policy=replace(provider.policy, max_input_tokens=m["input_limit"], max_attempts=1), transport=provider.transport)
task, sources = get_task("STRESS_2023")
assert {s.source_id: s.content_sha256 for s in sources} == m["source_hashes"]
with (HERE / "run-started.json").open("x") as f:
    json.dump({"manifest_sha256": mh, "status": "started_once"}, f, indent=2)
for condition in m["conditions"]:
    captured = {}
    start = monotonic()
    def generate(prompt):
        assert digest(prompt.encode()) == condition["prompt_sha256"]
        raw, used = instance.generate_structured(prompt, cancel_event=Event(), system=SYSTEM_PROMPT, timeout_seconds=60)
        captured.update(raw=raw, usage=used)
        return raw, used
    out = {"condition": condition["name"], "manifest_sha256": mh, "cost_usd": "unknown"}
    try:
        context = patch.object(analysis, "_replace_empty_table_with_counterpoint", side_effect=lambda _t, _q, windows, _s=None: windows) if condition["name"] == "baseline" else patch.object(analysis, "_replace_empty_table_with_counterpoint", wraps=analysis._replace_empty_table_with_counterpoint)
        with context:
            result = run_analysis(task, sources, generate)
        out.update(status="completed", reported_tokens=result.total_tokens, prompt_sha256=result.prompt_sha256,
                   segments=result.segments, claims=result.claims, counterarguments=result.counterarguments,
                   facts=result.facts, calculations=result.calculations, limitations=result.limitations, raw=captured.get("raw"))
    except Exception as error:
        out.update(status="failure", error=error.code if isinstance(error, ProviderFailure) else type(error).__name__,
                   reported_tokens=error.reported_tokens if isinstance(error, ProviderFailure) else captured.get("usage"))
    out["elapsed_seconds"] = round(monotonic() - start, 3)
    serialized = json.dumps(out, ensure_ascii=False, indent=2)
    for key in ("FORESIGHTNINE_LLM_API_KEY", "FORESIGHTNINE_GLM_CODING_API_KEY"):
        if os.environ.get(key):
            serialized = serialized.replace(os.environ[key], "[REDACTED]")
    (HERE / (condition["name"] + "-result.json")).write_text(serialized + "\n")
    print(json.dumps({"condition": condition["name"], "status": out["status"], "reported_tokens": out.get("reported_tokens")}), flush=True)
    if out["status"] != "completed" or type(out.get("reported_tokens")) is not int or out["reported_tokens"] > 10000:
        break
