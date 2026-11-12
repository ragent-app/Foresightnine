"""One-shot, nonblind diagnostic of the bounded claim review; no retries."""
import hashlib, json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
p = Path(__file__).resolve().parent
root = p.parents[3]
sys.path.insert(0, str(root / 'apps/api'))
h = lambda data: hashlib.sha256(data).hexdigest()
manifest_bytes = (p / 'manifest.json').read_bytes()
m = json.loads(manifest_bytes)
prior = p.parent / '39-selection-controlled'
for name, expected in m['input_sha256'].items():
    assert h((prior / name).read_bytes()) == expected
assert h((p / 'run.py').read_bytes()) == m['runner_sha256']
from app.research_sources import ResearchSource, SourcePermissions
from app.research_verification import REVIEW_SYSTEM_PROMPT, prepare_support_review, apply_support_review
result = json.loads((prior / 'GDP_REV-result.json').read_text())
info = result['content']['sources'][0]
source = ResearchSource(source_id=info['source_id'], owner_id='owner', title=info['title'], text=(prior / 'GDP_REV-source.txt').read_text(), kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=info['content_sha256'], permissions=SourcePermissions(store=True, model_input=True, display=True))
claims = result['content']['claims']
batch = prepare_support_review(claims, [source], m['max_prompt_bytes'])
assert batch.indexes == [0, 1, 2] and batch.omitted == []
assert batch.prompt_sha256 == m['prompt_sha256'] and h(REVIEW_SYSTEM_PROMPT.encode()) == m['system_prompt_sha256']
assert len(batch.prompt.encode()) <= m['max_prompt_bytes']
# Read the existing local service configuration without displaying credentials.
env = json.loads(subprocess.check_output(['docker', 'inspect', '--format', '{{json .Config.Env}}', 'docker-api-1']))
for line in env:
    key, sep, value = line.partition('=')
    if sep and key.startswith('FORESIGHTNINE_'):
        os.environ[key] = value
os.environ.update(FORESIGHTNINE_LOCAL_AUTH_MODE='mock', FORESIGHTNINE_STORAGE_MODE='memory', FORESIGHTNINE_LLM_TIMEOUT_SECONDS='60')
import app.main as api
provider = api.challenge_provider
assert provider.name == 'openai-compatible' and provider.settings.model == m['model']
params = json.loads(provider._request_body(None, 'probe', system='probe')); params.pop('messages')
assert params == m['generation_parameters'] and provider.policy.max_input_tokens == 3000 and provider.policy.timeout_seconds == 60
with (p / 'run-started.json').open('x') as f:
    json.dump({'manifest_sha256': h(manifest_bytes), 'status': 'started_once', 'retry_external_calls': 0}, f, indent=2)
start = time.monotonic()
try:
    raw, tokens = provider.generate_structured(batch.prompt, cancel_event=Event(), system=REVIEW_SYSTEM_PROMPT, timeout_seconds=60)
    assessed = apply_support_review(claims, batch, raw)
    verdicts = [c['support_review']['verdict'] for c in assessed]
    scored = verdicts[0] == 'supports' and verdicts[1] == 'supports' and verdicts[2] in ('does_not_support', 'contradicts', 'uncertain')
    outcome = {'status': 'completed', 'raw': raw, 'verdicts': verdicts, 'narrow_nonblind_pass': scored, 'reported_tokens': tokens, 'elapsed_seconds': round(time.monotonic()-start, 3), 'cost_usd': 'unknown', 'segments': batch.segments}
except Exception as e:
    outcome = {'status': 'failed_or_unknown', 'error_type': type(e).__name__, 'elapsed_seconds': round(time.monotonic()-start, 3), 'cost_usd': 'unknown', 'reported_tokens': 'unknown'}
serialized = json.dumps(outcome, ensure_ascii=False, indent=2)
for key in ('FORESIGHTNINE_LLM_API_KEY', 'FORESIGHTNINE_GLM_CODING_API_KEY'):
    secret = os.environ.get(key)
    if secret: serialized = serialized.replace(secret, '[REDACTED]')
(p / 'result.json').write_text(serialized + '\n')
print(json.dumps({k: outcome[k] for k in ('status', 'reported_tokens', 'elapsed_seconds')}, ensure_ascii=False))
