"""One-shot paired development probe, two provider requests maximum."""
import hashlib, json, os, subprocess, sys, time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

folder = Path(__file__).resolve().parent
root = folder.parents[3]
hash_bytes = lambda b: hashlib.sha256(b).hexdigest()
manifest_bytes = (folder / "manifest.json").read_bytes()
manifest = json.loads(manifest_bytes)
for name in ("input.json", "run.py"):
    assert hash_bytes((folder / name).read_bytes()) == manifest["sha256"][name]
with (folder / "started.json").open("x") as f:
    json.dump({"manifest_sha256": hash_bytes(manifest_bytes), "once_only": True}, f)
# Read configured provider environment inside this process; never print it.
env_json = subprocess.check_output(["docker", "inspect", "docker-api-1", "--format", "{{json .Config.Env}}"], text=True)
allowed = {"FORESIGHTNINE_LLM_PROVIDER", "FORESIGHTNINE_LLM_BASE_URL", "FORESIGHTNINE_LLM_API_KEY",
           "FORESIGHTNINE_LLM_MODEL", "FORESIGHTNINE_LLM_ALLOWED_HOSTS", "FORESIGHTNINE_LLM_ALLOW_PRIVATE_NETWORK",
           "FORESIGHTNINE_GLM_CODING_API_KEY"}
for item in json.loads(env_json):
    key, _, value = item.partition("=")
    if key in allowed:
        os.environ[key] = value
os.environ.update(FORESIGHTNINE_STORAGE_MODE="memory", FORESIGHTNINE_LOCAL_AUTH_MODE="mock", FORESIGHTNINE_LLM_TIMEOUT_SECONDS="60")
sys.path.insert(0, str(root / "apps/api"))
import app.main as api
from app.research_analysis import analyze, SYSTEM_PROMPT
from app.research_sources import ResearchSource, SourcePermissions

provider = api.challenge_provider
assert provider.name == "openai-compatible" and provider.policy.timeout_seconds <= 60
source_input = json.loads((folder / "input.json").read_text())
text = source_input["source_text"]
source = ResearchSource(source_id="derived-filing", owner_id="development-probe", title=source_input["source_title"],
    text=text, url=source_input["source_url"], kind="user_supplied", obtained_at=datetime.now(timezone.utc),
    content_sha256=hash_bytes(text.encode()), permissions=SourcePermissions(store=True, display=True, model_input=True))
secrets = [os.environ.get(k) for k in ("FORESIGHTNINE_LLM_API_KEY", "FORESIGHTNINE_GLM_CODING_API_KEY")]
for variant in ("baseline", "counterevidence_guidance"):
    started = time.monotonic()
    actual_prompt_hash = None
    provider_used = None
    def generate(prompt):
        global actual_prompt_hash, provider_used
        payload = json.loads(prompt)
        if variant == "baseline":
            del payload["evidence_guidance"]
            payload["output_schema"]["counterarguments"][0]["citations"] = []
        actual = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        actual_prompt_hash = hash_bytes(actual.encode())
        result, provider_used = provider.generate_structured(actual, cancel_event=Event(), system=SYSTEM_PROMPT, timeout_seconds=60)
        return result, provider_used
    try:
        analysis = analyze(source_input["question"], [source], None, generate, max_prompt_chars=2200)
        output = {"variant": variant, "status": "returned", "provider_prompt_sha256": actual_prompt_hash,
                  "system_prompt_sha256": hash_bytes(SYSTEM_PROMPT.encode()), "source_sha256": hash_bytes(text.encode()),
                  "model": provider.settings.model, "reported_tokens": provider_used,
                  "elapsed_seconds": round(time.monotonic() - started, 3), "result": asdict(analysis), "cost_usd": None}
    except Exception as exc:
        output = {"variant": variant, "status": "failed_or_unknown", "error_type": type(exc).__name__,
                  "provider_prompt_sha256": actual_prompt_hash, "cost_usd": None}
    serialized = json.dumps(output, ensure_ascii=False, indent=2)
    for secret in secrets:
        if secret:
            serialized = serialized.replace(secret, "[REDACTED]")
    (folder / (variant + "-result.json")).write_text(serialized + "\n")
    print(json.dumps({"variant": variant, "status": output["status"], "reported_tokens": output.get("reported_tokens")}), flush=True)
    if output["status"] != "returned":
        break
