"""Isolated 359 API acceptance. No real provider request until explicitly run."""
from __future__ import annotations
import hashlib
from decimal import Decimal
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]
API = ROOT / 'src/apps/api'
SNAPSHOTS = ROOT / 'src/docs/product/phase-a/337-stage-b-independent-acceptance/snapshots'
TASKS = (
    ('fed', 'fed-2022-march.txt', '02b0db2d39a15f8dc2759e219e8914f273506f4f5e72d3ded3b8b40fa8784466',
     'Using only the two supplied Federal Reserve statements from March and May 2022, compare the federal funds target ranges, calculate the change in each endpoint in percentage points, and explain the statements’ qualifications and limits. Cite supporting passages and show the calculation.',
     'https://www.federalreserve.gov/newsevents/pressreleases/monetary20220316a.htm'),
)
SECOND_SOURCE = ('fed-2022-may.txt', 'e1005a8ecab1b13c8d47271be03b1350c5c84adeb0f0aa8e0bd7d76e6d215c45', 'https://www.federalreserve.gov/newsevents/pressreleases/monetary20220504a.htm')

def digest(value): return hashlib.sha256(value).hexdigest()
def files():
    return sorted([*API.joinpath('app').rglob('*.py'), HERE/'runner.py', HERE/'isolated_api.py', HERE/'preregistration.md', HERE/'test_runner.py', *(SNAPSHOTS / t[1] for t in TASKS), SNAPSHOTS / SECOND_SOURCE[0]])
def current_freeze():
    return {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in files()}
def save(name, obj):
    path = HERE / name
    tmp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(obj, stream, indent=2, ensure_ascii=False); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(tmp, path)
def request(base, method, path, body=None, token=None, key=None, experiment_token=None):
    headers = {'Content-Type': 'application/json'}
    if token: headers['Authorization'] = 'Bearer ' + token
    if key: headers['Idempotency-Key'] = key
    if experiment_token: headers['X-Experiment-Token'] = experiment_token
    req = Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers)
    try:
        with urlopen(req, timeout=15) as response: return response.status, json.load(response)
    except HTTPError as error: return error.code, {}
    except (URLError, OSError, ValueError): return None, {}

def write_control(directory, data):
    path = Path(directory) / 'control.json'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream); stream.flush(); os.fsync(stream.fileno())

def reconcile(directory):
    directory = Path(directory)
    metadata = directory.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError('Unsafe control directory permissions')
    path = directory / 'control.json'
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError('Unsafe control file permissions')
    control = json.loads(path.read_text())
    if not control.get('operation_id') or not control.get('topic_id'):
        raise RuntimeError('Admission remains unknown; operation ID unavailable')
    base = control['base']
    if not base.startswith('http://127.0.0.1:') or not base.removeprefix('http://127.0.0.1:').isdigit():
        raise RuntimeError('Expected isolated loopback')
    operation = control['operation_id']
    endpoint = '/v2/research/topics/' + control['topic_id'] + '/runs/' + operation
    status, state = request(base, 'GET', endpoint, token=control['token'])
    settled_status, settlement = request(base, 'GET', endpoint + '/settlement', token=control['token'])
    diag_status, diagnostics = request(base, 'GET', '/_359/private/' + operation, experiment_token=control['experiment_token'])
    progress_status, progress = request(base, 'GET', endpoint + '/progress', token=control['token'])
    result = {'operation_http_status': status, 'state': (state.get('operation') or {}).get('status'),
              'settlement_http_status': settled_status, 'diagnostic_http_status': diag_status, 'progress_http_status': progress_status}
    if settled_status == 200:
        result['settlement'] = {k: settlement.get(k) for k in ('operation_status', 'checkpoint_version_id', 'model_ledger', 'unresolved_stage_count', 'reported_tokens', 'reported_tokens_scope', 'cost_status')}
    if diag_status == 200:
        result['diagnostics'] = diagnostics.get('diagnostics', [])
        result['diagnostic_failures'] = diagnostics.get('diagnostic_failures', [])
        result['recovery_snapshot'] = diagnostics.get('recovery_snapshot')
        result['runtime_checkpoint'] = diagnostics.get('runtime_checkpoint')
        result['recovery_pending'] = diagnostics.get('recovery_pending')
    if progress_status == 200:
        result['progress'] = {k: progress.get(k) for k in ('version_id', 'checkpoint', 'result_status')}
    save(control['task'] + '-reconciliation.json', result)

def create_marker(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
    dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(dirfd)
    finally: os.close(dirfd)

def reconciliation_safe(settlement, diagnostics):
    """Cleanup only after private state and every accounted call are settled."""
    if not isinstance(settlement, dict) or not isinstance(diagnostics, dict): return False
    ledger = settlement.get('model_ledger')
    checkpoint = diagnostics.get('runtime_checkpoint')
    if not isinstance(ledger, dict) or ledger.get('reserved_calls') != 0: return False
    if type(settlement.get('unresolved_stage_count')) is not int or settlement['unresolved_stage_count'] != 0: return False
    if not isinstance(checkpoint, dict) or checkpoint.get('pending') is not None: return False
    if not isinstance(diagnostics.get('recovery_pending'), list) or diagnostics['recovery_pending']: return False
    receipts = (checkpoint.get('_accounting') or {}).get('model_call_receipts')
    if not isinstance(receipts, dict): return False
    return all(isinstance(r, dict) and r.get('settlement') in {'settled', 'released'} for r in receipts.values())

def score(name, result, source_id, source_text):
    return {'task': name, 'reference': {'march_range_percent': [0.25, 0.50], 'may_range_percent': [0.75, 1.00], 'endpoint_change_percentage_points': [0.50, 0.50]}, 'criteria': {'range_and_calculation': 'not_reviewed', 'qualifications': 'not_reviewed', 'citation_support': 'not_reviewed', 'native_tool_feedback': 'not_reviewed'}, 'quality_result': 'not_reviewed'}

def main():
    if (HERE/'run-started.json').exists(): raise RuntimeError('Already started; never replay')
    freeze = json.loads((HERE/'freeze.json').read_text())
    if freeze.get('sha256') != current_freeze(): raise RuntimeError('Code or inputs changed since freeze')
    for _, name, expected, _, _ in TASKS:
        if digest((SNAPSHOTS/name).read_bytes()) != expected: raise RuntimeError('Snapshot mismatch')
    if digest((SNAPSHOTS / SECOND_SOURCE[0]).read_bytes()) != SECOND_SOURCE[1]: raise RuntimeError('Snapshot mismatch')
    # Isolate every data service; inherit provider settings only, never database or production credentials.
    env = {k: v for k, v in os.environ.items() if k in {'PATH', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}}
    secret = os.environ.get('FORESIGHTNINE_GLM_CODING_API_KEY', '')
    if not secret: raise RuntimeError('Prior experiment credential must be supplied server-side')
    env.update(FORESIGHTNINE_LLM_PROVIDER='openai_chat_completions', FORESIGHTNINE_LLM_BASE_URL='https://open.bigmodel.cn/api/coding/paas/v4', FORESIGHTNINE_LLM_MODEL='glm-5.3-flash', FORESIGHTNINE_LLM_API_KEY=secret, FORESIGHTNINE_LLM_ALLOWED_HOSTS='open.bigmodel.cn', FORESIGHTNINE_LLM_TIMEOUT_SECONDS='120', FORESIGHTNINE_LLM_ENDPOINT_VERSION='experiment-359', FORESIGHTNINE_LLM_CAPABILITY_PROFILE=json.dumps({'endpoint':'https://open.bigmodel.cn/api/coding/paas/v4/chat/completions','model':'glm-5.3-flash','version':'experiment-359','mode':'json_object','native_tools':True,'continuation':True,'continuation_fields':['reasoning_content'],'generation_parameters':['thinking','reasoning_effort','do_sample']}), FORESIGHTNINE_LLM_GENERATION_PARAMETERS=json.dumps({'thinking':{'type':'enabled'},'reasoning_effort':'low','do_sample':False}))
    temp_dir = tempfile.mkdtemp(prefix='foresightnine-359-')
    experiment_token = secrets.token_urlsafe(32)
    env.update(PYTHONPATH=str(API), TMPDIR=temp_dir, FORESIGHTNINE_359_ISOLATED='1', FORESIGHTNINE_STORAGE_MODE='memory', FORESIGHTNINE_ABUSE_MODE='memory', FORESIGHTNINE_SEARCH_PROVIDER='client', FORESIGHTNINE_LOCAL_AUTH_MODE='mock', FORESIGHTNINE_359_TOKEN=experiment_token)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    # Capture startup errors privately; runtime logs never enter experiment results.
    stderr_fd = os.open(HERE/'isolated-stderr.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        server = subprocess.Popen([sys.executable, str(HERE/'isolated_api.py'), str(port)], env=env, cwd=temp_dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr_fd, start_new_session=True)
    finally:
        os.close(stderr_fd)
    started = False
    active = False
    try:
        for _ in range(100):
            status, config = request(base, 'GET', '/_359/config', experiment_token=experiment_token)
            if status == 200: break
            if server.poll() is not None: raise RuntimeError('Isolated API startup failed')
            time.sleep(.1)
        else: raise RuntimeError('Isolated API startup unavailable')
        if not config.get('model') or not config.get('provider'): raise RuntimeError('Actual provider metadata unavailable')
        # Atomic, durable one-time marker before any research submission.
        create_marker(HERE/'run-started.json', {'freeze_sha256': digest((HERE/'freeze.json').read_bytes()), 'provider_config': config, 'state': 'started', 'local_control_dir': temp_dir})
        started = True
        password = secrets.token_urlsafe(32)
        status, auth = request(base, 'POST', '/v1/auth/local/register', {'email': f'acceptance-{uuid4().hex}@example.test', 'password': password})
        if status != 201 or not auth.get('access_token'): save('batch-stop.json', {'reason': 'isolated_registration_failed'}); return
        token = auth['access_token']
        control = {'base': base, 'token': token, 'experiment_token': experiment_token}
        write_control(temp_dir, control)
        for name, filename, source_hash, question, url in TASKS:
            record = {'task': name, 'source_sha256': [source_hash, SECOND_SOURCE[1]], 'question_sha256': digest(question.encode()), 'state': 'preparing', 'provider_usage': 'unknown'}
            save(name + '.json', record)
            source_text = (SNAPSHOTS/filename).read_text()
            status, source = request(base, 'POST', '/v2/research/sources', {'title': filename, 'text': source_text, 'url': url, 'kind': 'official', 'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}}, token)
            if status != 201 or not source.get('source_id'): record['state']='source_admission_failed'; save(name+'.json',record); return
            source_id = source['source_id']
            second_text = (SNAPSHOTS / SECOND_SOURCE[0]).read_text()
            status, second = request(base, 'POST', '/v2/research/sources', {'title': SECOND_SOURCE[0], 'text': second_text, 'url': SECOND_SOURCE[2], 'kind': 'official', 'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}}, token)
            if status != 201 or not second.get('source_id'): record['state']='source_admission_failed'; save(name+'.json',record); return
            source_ids = [source_id, second['source_id']]
            status, topic = request(base, 'POST', '/v2/research/topics', {'question': question, 'source_ids': source_ids}, token)
            if status != 201 or not topic.get('topic_id') or not topic.get('versions'): record['state']='topic_admission_failed'; save(name+'.json',record); return
            path = '/v2/research/topics/' + topic['topic_id']
            payload = {'parent_version_id': topic['versions'][0]['version_id'], 'selected_source_ids': source_ids, 'max_search_calls': 0, 'discover_sources': False, 'auto_fetch_official': False, 'gap_search': False, 'plan_research': False, 'support_review': True, 'require_counterarguments': True}
            record.update(state='admission_pending', topic_id=topic['topic_id'], payload_sha256=digest(json.dumps(payload, sort_keys=True).encode())); save(name+'.json',record)
            control.update(task=name, topic_id=topic['topic_id'], operation_id=None)
            write_control(temp_dir, control)
            active = True  # The admission can be ambiguous even if the HTTP request fails.
            status, receipt = request(base, 'POST', path+'/runs', payload, token, 'acceptance-359-'+uuid4().hex)
            operation = (receipt.get('operation') or {}).get('operation_id')
            if status != 202 or not operation: record.update(state='admission_unknown', http_status=status); save(name+'.json',record); return
            control['operation_id'] = operation
            write_control(temp_dir, control)
            record.update(state='running', operation_id=operation); save(name+'.json',record)
            while True:
                status, response = request(base, 'GET', path+'/runs/'+operation, token=token)
                state = (response.get('operation') or {}).get('status')
                if status != 200: record['state']='observation_unknown'; save(name+'.json',record); return
                if state in {'completed','failed','cancelled'}:
                    break
                time.sleep(2)
            status, settlement = request(base, 'GET', path+'/runs/'+operation+'/settlement', token=token)
            diag_status, diagnostics = request(base, 'GET', '/_359/private/'+operation, experiment_token=experiment_token)
            progress_status, progress = request(base, 'GET', path+'/runs/'+operation+'/progress', token=token)
            record.update(state=state, settlement_http_status=status, diagnostic_http_status=diag_status, progress_http_status=progress_status)
            if status != 200 or diag_status != 200 or progress_status != 200:
                active = True
                record['state']='reconciliation_unknown'; save(name+'.json',record); return
            record['settlement'] = {k: settlement.get(k) for k in ('operation_status','checkpoint_version_id','model_ledger','unresolved_stage_count','reported_tokens','reported_tokens_scope','cost_status')}
            record['diagnostics'] = diagnostics.get('diagnostics', [])
            record['diagnostic_failures'] = diagnostics.get('diagnostic_failures', [])
            record['recovery_snapshot'] = diagnostics.get('recovery_snapshot')
            record['runtime_checkpoint'] = diagnostics.get('runtime_checkpoint')
            record['recovery_pending'] = diagnostics.get('recovery_pending')
            record['progress'] = {k: progress.get(k) for k in ('version_id','checkpoint','result_status')}
            save(name+'.json',record)
            if not reconciliation_safe(settlement, diagnostics):
                record['state'] = 'reconciliation_unknown'
                save(name+'.json', record)
                return
            version = settlement.get('checkpoint_version_id')
            if version:
                export_status, result = request(base, 'GET', path+'/versions/'+version+'/export', token=token)
                if export_status != 200: record['state']='export_unavailable'; save(name+'.json',record); return
                save(name+'-authorized-export.json', result)
                save(name+'-score.json', score(name, result, source_id, source_text))
            record['operation_status'] = state
            record['research_result_status'] = progress.get('result_status')
            save(name+'.json', record)
            save('batch-finished.json', {'operation_status': state, 'research_result_status': progress.get('result_status'), 'quality_result':'not_reviewed'})
            active = False
            if state != 'completed': return
    finally:
        # Never cancel an admitted operation if its status is unknown: leave the isolated process running.
        if not active:
            server.terminate()
            shutil.rmtree(temp_dir)

if __name__ == '__main__':
    if sys.argv[1:] == ['freeze']:
        if (HERE/'run-started.json').exists(): raise RuntimeError('Already started; cannot refreeze')
        save('freeze.json', {'sha256': current_freeze()})
    elif len(sys.argv) == 3 and sys.argv[1] == 'reconcile': reconcile(sys.argv[2])
    elif sys.argv[1:]: raise SystemExit('Usage: runner.py [freeze | reconcile CONTROL_DIR]')
    else: main()
