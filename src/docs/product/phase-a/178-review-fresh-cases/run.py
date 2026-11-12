from pathlib import Path
from hashlib import sha256
from dataclasses import replace
from threading import Event
from time import monotonic
import json
import os
import subprocess
import sys

HERE = Path(__file__).resolve().parent; ROOT = HERE.parents[3]; digest = lambda b: sha256(b).hexdigest()
sys.path.insert(0, str(ROOT / "apps/api"))
from app.research_verification import REVIEW_SYSTEM_PROMPT, REVIEW_METHOD
from app.phase3 import build_llm_provider, ProviderFailure
from app.openai_compatible import OpenAICompatibleLlmProvider
mb = (HERE/"manifest.json").read_bytes(); m = json.loads(mb); mh = digest(mb)
assert all(digest((HERE/f).read_bytes()) == h for f,h in m["files"].items())
assert digest((HERE.parent/"161-fresh-source-evaluation/fed-2022-06.pdf").read_bytes()) == m["source_pdf_sha256"]
assert digest(REVIEW_SYSTEM_PROMPT.encode()) == m["system_sha256"] and REVIEW_METHOD == m["review_method"]
subprocess.run(["git", "diff", "--quiet", m["implementation_commit"], "--", "apps"], cwd=ROOT, check=True)
for line in json.loads(subprocess.check_output(["docker", "inspect", "--format", "{{json .Config.Env}}", "docker-api-1"])):
    key, sep, value = line.partition("=")
    if sep and (key.startswith("FORESIGHTNINE_LLM_") or key == "FORESIGHTNINE_GLM_CODING_API_KEY"): os.environ[key] = value
os.environ["FORESIGHTNINE_LLM_TIMEOUT_SECONDS"] = "60"
provider = build_llm_provider(); assert isinstance(provider, OpenAICompatibleLlmProvider) and provider.settings.model == m["model"]
params = json.loads(provider._request_body(None, "probe", system="probe")); params.pop("messages")
assert params == m["generation_parameters"] and provider.policy.max_output_tokens == m["output_limit"]
instance = OpenAICompatibleLlmProvider(provider.settings, policy=replace(provider.policy, max_input_tokens=m["input_limit"], max_attempts=1), transport=provider.transport)
with (HERE/"run-started.json").open("x") as f: json.dump({"manifest_sha256":mh,"status":"started_once"},f,indent=2)
for condition in m["conditions"]:
    name = condition["name"]; prompt = (HERE/f"{name}-prompt.json").read_text(); assert digest(prompt.encode()) == condition["prompt_sha256"]
    start = monotonic(); out = {"condition":name,"manifest_sha256":mh,"prompt_sha256":condition["prompt_sha256"],"cost_usd":"unknown"}
    try:
        raw, used = instance.generate_structured(prompt, cancel_event=Event(), system=REVIEW_SYSTEM_PROMPT, timeout_seconds=60)
        out.update(status="completed", reported_tokens=used, raw=raw)
    except Exception as error:
        out.update(status="failure", error=error.code if isinstance(error, ProviderFailure) else type(error).__name__,
                   reported_tokens=error.reported_tokens if isinstance(error, ProviderFailure) else None)
    out["elapsed_seconds"] = round(monotonic()-start,3); serialized = json.dumps(out, ensure_ascii=False, indent=2)
    for key in ("FORESIGHTNINE_LLM_API_KEY", "FORESIGHTNINE_GLM_CODING_API_KEY"):
        if os.environ.get(key): serialized = serialized.replace(os.environ[key], "[REDACTED]")
    (HERE/f"{name}-result.json").write_text(serialized + "\n")
    print(json.dumps({"condition":name,"status":out["status"],"reported_tokens":out.get("reported_tokens")}), flush=True)
    if out["status"] != "completed" or type(out.get("reported_tokens")) is not int or out["reported_tokens"] > m["per_call_max_tokens"]: break
