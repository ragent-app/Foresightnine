from common import *
import os, subprocess, time
from threading import Event
from app.research_verification import REVIEW_METHOD, REVIEW_SYSTEM_PROMPT, prepare_isolated_support_reviews, parse_isolated_support_review, review_record

raw_manifest = (HERE / 'manifest.json').read_bytes(); m = json.loads(raw_manifest); mh = digest(raw_manifest)
assert all(digest((HERE / name).read_bytes()) == expected for name, expected in m['files'].items())
prior = HERE.parent / '258-requested-facts-prompt-check' / 'GDP_COUNTER_2024-result.json'
assert digest(prior.read_bytes()) == m['claims_sha256']
assert digest((SOURCE / 'bea-gdp1q24.pdf').read_bytes()) == m['source_sha256']
subprocess.run(['git', 'diff', '--quiet', m['implementation_commit'], '--', 'apps'], cwd=ROOT, check=True)
claims = json.loads(prior.read_text())['content']['claims']; _, sources = get_task('GDP_COUNTER_2024')
assert sources[0].content_sha256 == m['source_text_sha256'] and REVIEW_METHOD == m['review_method']
assert digest(REVIEW_SYSTEM_PROMPT.encode()) == m['system_sha256']
plan = prepare_isolated_support_reviews(claims, sources, m['review_input_limit'] - len(REVIEW_SYSTEM_PROMPT.encode()) - 512)
assert not plan.omissions and len(plan.batches) == m['max_calls']
for batch, fixed in zip(plan.batches, m['batches']):
    assert batch.indexes == [fixed['index']] and batch.prompt_sha256 == fixed['prompt_sha256']
    assert batch.guarded[batch.indexes[0]] == fixed['guards'] and batch.segments == fixed['segments']
for line in json.loads(subprocess.check_output(['docker', 'inspect', '--format', '{{json .Config.Env}}', 'docker-api-1'])):
    key, sep, value = line.partition('=')
    if sep and (key.startswith('FORESIGHTNINE_LLM_') or key == 'FORESIGHTNINE_GLM_CODING_API_KEY'):
        os.environ[key] = value
os.environ.update(FORESIGHTNINE_STORAGE_MODE='memory', FORESIGHTNINE_LOCAL_AUTH_MODE='mock',
                  FORESIGHTNINE_LLM_TIMEOUT_SECONDS='60', FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS='6000')
import app.main as api
provider = api.challenge_provider.for_research()
assert provider.name == 'openai-compatible' and provider.settings.model == m['model']
params = json.loads(provider._request_body(None, 'probe', system='probe')); params.pop('messages')
assert params == m['generation_parameters'] and provider.policy.max_output_tokens == 4000
with (HERE / 'run-started.json').open('x') as handle:
    json.dump({'manifest_sha256': mh, 'status': 'started_once'}, handle, indent=2)
start = time.monotonic(); total = 0; records = []
for batch in plan.batches:
    remaining = m['elapsed_seconds'] - (time.monotonic() - start)
    if remaining <= 0 or total + 4000 > m['max_total_tokens']:
        break
    index = batch.indexes[0]
    with (HERE / f'claim-{index}-started.json').open('x') as handle:
        json.dump({'manifest_sha256': mh, 'index': index, 'prompt_sha256': batch.prompt_sha256}, handle, indent=2)
    try:
        raw, used = provider.generate_structured(batch.prompt, cancel_event=Event(), system=REVIEW_SYSTEM_PROMPT,
                                                 timeout_seconds=min(m['timeout_seconds'], remaining))
        mapped, verdict = parse_isolated_support_review(batch, raw)
        record = {'status': 'completed', 'index': mapped, 'payload': raw, 'reported_tokens': used,
                  'stored_review': review_record(batch, mapped, verdict), 'manifest_sha256': mh}
    except Exception as error:
        record = {'status': 'failed_or_unknown', 'index': index, 'error': type(error).__name__,
                  'reported_tokens': getattr(error, 'reported_tokens', None), 'manifest_sha256': mh}
    serialized = json.dumps(record, ensure_ascii=False, indent=2)
    for key in ('FORESIGHTNINE_LLM_API_KEY', 'FORESIGHTNINE_GLM_CODING_API_KEY'):
        if os.environ.get(key): serialized = serialized.replace(os.environ[key], '[REDACTED]')
    (HERE / f'claim-{index}-result.json').write_text(serialized + '\n')
    records.append(record)
    if record['status'] != 'completed' or type(record['reported_tokens']) is not int:
        break
    total += record['reported_tokens']
    if total > m['max_total_tokens']: break
(HERE / 'run-result.json').write_text(json.dumps({'manifest_sha256': mh, 'calls': len(records),
    'known_tokens': total, 'elapsed_seconds': round(time.monotonic() - start, 3),
    'complete': len(records) == m['max_calls'] and all(r['status'] == 'completed' and type(r['reported_tokens']) is int for r in records),
    'cost_usd': 'unknown'}, indent=2) + '\n')
print(json.dumps({'calls': len(records), 'known_tokens': total, 'statuses': [r['status'] for r in records]}))
