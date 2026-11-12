from common import *
from uuid import UUID, uuid4
from unittest.mock import patch
import os, subprocess, time

manifest_bytes = (HERE / 'manifest.json').read_bytes(); m = json.loads(manifest_bytes); mh = digest(manifest_bytes)
assert all(digest((HERE / name).read_bytes()) == expected for name, expected in m['files'].items())
subprocess.run(['git', 'diff', '--quiet', m['implementation_commit'], '--', 'apps'], cwd=ROOT, check=True)
for line in json.loads(subprocess.check_output(['docker', 'inspect', '--format', '{{json .Config.Env}}', 'docker-api-1'])):
    key, sep, value = line.partition('=')
    if sep and (key.startswith('FORESIGHTNINE_LLM_') or key == 'FORESIGHTNINE_GLM_CODING_API_KEY'):
        os.environ[key] = value
os.environ.update(FORESIGHTNINE_STORAGE_MODE='memory', FORESIGHTNINE_LOCAL_AUTH_MODE='mock',
                  FORESIGHTNINE_LLM_TIMEOUT_SECONDS='60', FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS='6000')
import app.main as api
from fastapi.testclient import TestClient
assert digest(api.RESEARCH_SYSTEM_PROMPT.encode()) == m['system_sha256']
assert api.REVIEW_METHOD == m['review_method'] and digest(api.REVIEW_SYSTEM_PROMPT.encode()) == m['review_system_sha256']
api.challenge_provider = api.challenge_provider.for_research(); provider = api.challenge_provider
assert provider.name == 'openai-compatible' and provider.settings.model == m['model']
params = json.loads(provider._request_body(None, 'probe', system='probe')); params.pop('messages')
assert params == m['generation_parameters'] and provider.policy.max_output_tokens == m['output_limit']
original = provider.generate_structured; calls = {}; current = ''
with (HERE / 'run-started.json').open('x') as handle:
    json.dump({'manifest_sha256': mh, 'status': 'started_once'}, handle, indent=2)
def generate(prompt, *, cancel_event, system, timeout_seconds):
    kind = ('counterpoint' if 'counterpoint_request' in json.loads(prompt) else 'generation') if system == api.RESEARCH_SYSTEM_PROMPT else 'review'
    assert system in (api.RESEARCH_SYSTEM_PROMPT, api.REVIEW_SYSTEM_PROMPT)
    calls[current] = calls.get(current, 0) + 1
    assert calls[current] <= m['max_calls_per_task'] and sum(calls.values()) <= m['max_calls_total']
    if kind == 'generation':
        assert digest(prompt.encode()) == next(c['prompt_sha256'] for c in m['conditions'] if c['task'] == current)
    raw, used = original(prompt, cancel_event=cancel_event, system=system, timeout_seconds=timeout_seconds)
    value = json.dumps({'manifest_sha256': mh, 'kind': kind, 'payload': raw, 'reported_tokens': used,
                        'prompt_sha256': digest(prompt.encode()), 'system_sha256': digest(system.encode())}, ensure_ascii=False, indent=2)
    for key in ('FORESIGHTNINE_LLM_API_KEY', 'FORESIGHTNINE_GLM_CODING_API_KEY'):
        if os.environ.get(key): value = value.replace(os.environ[key], '[REDACTED]')
    (HERE / f'{current}-{kind}-{calls[current]}-provider.json').write_text(value + '\n')
    return raw, used
provider.generate_structured = generate
client = TestClient(api.app)
reg = client.post('/v1/auth/local/register', json={'email': 'holdout-' + uuid4().hex + '@example.test',
                                                    'password': 'correct horse battery staple!'})
assert reg.status_code == 201; headers = {'Authorization': 'Bearer ' + reg.json()['access_token']}
total = 0
for condition in m['conditions']:
    current = condition['task']; task, source, raw = load_task(current); start = time.monotonic(); operation_id = None
    assert digest(raw) == condition['source_sha256'] and source.content_sha256 == condition['text_sha256']
    try:
        permissions = {'store': True, 'display': True, 'model_input': True, 'export': True}
        body = {'title': task['source'], 'permissions': permissions}
        if task['published_at']: body['published_at'] = task['published_at']
        if task['format'] == 'html':
            endpoint = '/v2/research/sources/html'; body['html_base64'] = base64.b64encode(raw).decode()
        else:
            endpoint = '/v2/research/sources/pdf'; body.update(pdf_base64=base64.b64encode(raw).decode(), text_order='layout')
        with patch('app.research_sources.uuid4', return_value=UUID(task['source_id'].removeprefix('source_'))):
            created = client.post(endpoint, headers=headers, json=body)
        assert created.status_code == 201 and created.json()['source_id'] == source.source_id
        assert created.json()['content_sha256'] == source.content_sha256
        topic = client.post('/v2/research/topics', headers=headers, json={'question': task['question'], 'source_ids': [source.source_id]})
        assert topic.status_code == 201
        path = '/v2/research/topics/' + topic.json()['topic_id']
        receipt = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'holdout-' + current}, json={
            'parent_version_id': topic.json()['versions'][0]['version_id'], 'max_search_calls': 0,
            'max_model_calls': m['max_calls_per_task'], 'support_review': False, 'counterpoint_followup': True,
            'max_input_tokens_per_call': m['input_limit'], 'max_total_tokens': m['max_tokens_per_task'],
            'max_elapsed_seconds': m['elapsed_seconds']})
        assert receipt.status_code == 202; operation_id = receipt.json()['operation']['operation_id']
        (HERE / f'{current}-receipt.json').write_text(json.dumps({'manifest_sha256': mh, 'operation_id': operation_id}, indent=2) + '\n')
        status = 'unknown'; poll_errors = []
        while time.monotonic() - start < 150:
            check = client.get(path + '/runs/' + operation_id, headers=headers)
            if check.status_code == 200:
                status = check.json()['operation']['status']
                if status in ('completed', 'failed', 'cancelled'): break
            else:
                poll_errors.append(check.status_code)
                if check.status_code not in (429, 503): break
            time.sleep(2)
        output = {'task': current, 'status': status, 'operation_id': operation_id, 'manifest_sha256': mh,
                  'poll_errors': poll_errors, 'cost_usd': 'unknown'}
        if status in ('completed', 'failed', 'cancelled'):
            updated = client.get(path, headers=headers); assert updated.status_code == 200
            output['content'] = updated.json()['versions'][-1]['content']
    except Exception as error:
        output = {'task': current, 'status': 'local_failure', 'operation_id': operation_id,
                  'manifest_sha256': mh, 'error': type(error).__name__, 'cost_usd': 'unknown'}
    output['elapsed_seconds'] = round(time.monotonic() - start, 3)
    serialized = json.dumps(output, ensure_ascii=False, indent=2)
    for key in ('FORESIGHTNINE_LLM_API_KEY', 'FORESIGHTNINE_GLM_CODING_API_KEY'):
        if os.environ.get(key): serialized = serialized.replace(os.environ[key], '[REDACTED]')
    (HERE / f'{current}-result.json').write_text(serialized + '\n')
    usage = output.get('content', {}).get('run', {}).get('total_tokens')
    print(json.dumps({'task': current, 'status': output['status'], 'reported_tokens': usage, 'calls': calls.get(current, 0)}), flush=True)
    if output['status'] not in ('completed', 'failed', 'cancelled') or type(usage) is not int or usage > m['max_tokens_per_task']:
        break
    total += usage
    if total > m['max_tokens_total']: break
