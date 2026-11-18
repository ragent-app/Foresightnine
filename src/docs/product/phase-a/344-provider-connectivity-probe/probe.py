"""Single isolated connectivity call; exclusive marker prevents replay."""
import json, os, time, hashlib
from pathlib import Path
from app.phase3 import build_llm_provider
HERE = Path(__file__).resolve().parent
KEY = 'FORESIGHTNINE_GLM_CODING_API_KEY'
for line in Path('../../infra/docker/staging.env').read_text().splitlines():
    if line.startswith(KEY + '='):
        os.environ[KEY] = line.split('=', 1)[1].strip().strip('"\'')
os.environ.update(FORESIGHTNINE_LLM_PROVIDER='glm_coding', FORESIGHTNINE_LLM_TIMEOUT_SECONDS='120', FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS='6000', FORESIGHTNINE_GLM_CODING_THINKING='enabled', FORESIGHTNINE_GLM_CODING_REASONING_EFFORT='low', FORESIGHTNINE_GLM_CODING_DO_SAMPLE='false')
provider = build_llm_provider().for_research(6000)
g = provider.settings.glm_generation if provider.settings else None
assert provider.settings and provider.settings.api_key and provider.settings.model == 'glm-5.3-flash' and provider.configuration_error is None
assert provider.policy.max_input_tokens == 6000 and provider.policy.max_output_tokens == 4000 and provider.policy.timeout_seconds == 120
assert g and g.thinking_enabled and g.reasoning_effort == 'low' and g.do_sample is False
prompt = 'Return exactly a JSON object containing the key ok with value true.'
system = 'Return only the requested JSON object.'
marker = HERE / 'attempt.json'
fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
def write(record):
    tmp = HERE / 'attempt.tmp'
    with tmp.open('w') as f:
        json.dump(record, f, ensure_ascii=False, indent=2); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, marker)
initial = {'state':'pending', 'reserved_tokens':10000, 'attempts':1, 'provider_response':'unknown', 'settlement':'unknown', 'actual_tokens':None, 'cost':'unknown'}
with os.fdopen(fd, 'w') as f:
    json.dump(initial, f); f.flush(); os.fsync(f.fileno())
started = time.monotonic()
try:
    data, usage = provider.generate_structured(prompt, system=system, timeout_seconds=120)
    result = {**initial, 'state':'returned', 'provider_response':'returned', 'settlement':'reported_usage' if usage is not None else 'unknown', 'actual_tokens':usage, 'schema_valid': data == {'ok': True}, 'elapsed_ms':round((time.monotonic()-started)*1000)}
except Exception as e:
    code = getattr(e, 'code', None)
    result = {**initial, 'state':'failed', 'error_code':code if isinstance(code,str) and code.startswith(('provider_', 'invalid_provider_')) else 'unknown', 'elapsed_ms':round((time.monotonic()-started)*1000)}
write(result)
print(json.dumps(result))
