"""Isolated 357 API acceptance. No real provider request until explicitly run."""
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
    ('macro', 'bea-2022-q3.txt', 'f36fc389b6b0420ccd8416938f1028fd8c644b1c14c2e0624e5ba7ebb15aa4ef',
     'Using the supplied BEA release, report the real GDP growth rates for the second and third quarters of 2022 and calculate the difference in percentage points. Explain the estimate and period limitations.',
     'https://www.bea.gov/sites/default/files/2022-10/gdp3q22_adv.pdf'),
    ('qualitative', 'fed-2020-june.txt', 'fec9898ca7650e7ed9a8271704c57ea4c7157d34174546273a1169c150f415ed',
     'Using the supplied Federal Reserve statement, identify the policy decision and the strongest explicitly stated uncertainty or qualification; cite the relevant passages and explain whether the qualification challenges the decision or instead limits what can be concluded.',
     'https://www.federalreserve.gov/monetarypolicy/files/monetary20200610a1.pdf'),
)

def digest(value): return hashlib.sha256(value).hexdigest()
def files():
    return sorted([*API.joinpath('app').rglob('*.py'), HERE/'runner.py', HERE/'isolated_api.py', HERE/'preregistration.md', *(SNAPSHOTS / t[1] for t in TASKS)])
def current_freeze():
    return {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in files()}
def save(name, obj):
    path = HERE / name
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
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
    diag_status, diagnostics = request(base, 'GET', '/_357/private/' + operation, experiment_token=control['experiment_token'])
    progress_status, progress = request(base, 'GET', endpoint + '/progress', token=control['token'])
    result = {'operation_http_status': status, 'state': (state.get('operation') or {}).get('status'),
              'settlement_http_status': settled_status, 'diagnostic_http_status': diag_status, 'progress_http_status': progress_status}
    if settled_status == 200:
        result['settlement'] = {k: settlement.get(k) for k in ('operation_status', 'checkpoint_version_id', 'model_ledger', 'unresolved_stage_count', 'reported_tokens', 'reported_tokens_scope', 'cost_status')}
    if diag_status == 200:
        result['diagnostics'] = diagnostics.get('diagnostics', [])
        result['diagnostic_failures'] = diagnostics.get('diagnostic_failures', [])
    if progress_status == 200:
        result['progress'] = {k: progress.get(k) for k in ('version_id', 'checkpoint', 'result_status')}
    save(control['task'] + '-reconciliation.json', result)

def score(name, result, source_id, source_text):
    """Evidence inventory; semantic conclusions require independent review."""
    content = result.get('version', {}).get('content', {})
    citations = []
    for item in content.get('claims', []) + content.get('facts', []):
        if isinstance(item, dict): citations.extend(item.get('citations', []) if isinstance(item.get('citations'), list) else [])
    norm = lambda s: ' '.join(s.split())
    quote_checks = []
    for citation in citations:
        if not isinstance(citation, dict): continue
        quote = citation.get('quotation') or citation.get('quote') or ''
        quote_checks.append({'source_matches': citation.get('source_id') == source_id,
                             'quotation_found': bool(isinstance(quote, str) and quote.strip() and norm(quote) in norm(source_text))})
    criteria = (['Q2_2022_minus_0_6_annualized', 'Q3_2022_plus_2_6_annualized', 'difference_plus_3_2_percentage_points', 'advance_estimate_and_period_limitations', 'citations_support_each_claim'] if name == 'macro' else
                ['maintain_federal_funds_target_0_to_0_25_percent', 'public_health_near_term_weight_and_medium_term_risks', 'conditional_hold_until_economic_conditions_met', 'qualification_limits_conclusion_or_explicitly_explains_relation', 'citations_support_each_claim'])
    return {'task': name, 'reference_calculation': {'Q2_percent': -0.6, 'Q3_percent': 2.6, 'difference_percentage_points': str(Decimal('2.6') - Decimal('-0.6'))} if name == 'macro' else None, 'automated_checks': {'citation_count': len(quote_checks), 'citations': quote_checks,
            'citation_integrity': bool(quote_checks) and all(x['source_matches'] and x['quotation_found'] for x in quote_checks)},
            'criteria': {criterion: 'not_reviewed' for criterion in criteria}, 'quality_result': 'not_reviewed',
            'note': 'Exact source quotation matching is not semantic support; inspect the authorized export and score each criterion.'}

def main():
    if (HERE/'run-started.json').exists(): raise RuntimeError('Already started; never replay')
    freeze = json.loads((HERE/'freeze.json').read_text())
    if freeze.get('sha256') != current_freeze(): raise RuntimeError('Code or inputs changed since freeze')
    for _, name, expected, _, _ in TASKS:
        if digest((SNAPSHOTS/name).read_bytes()) != expected: raise RuntimeError('Snapshot mismatch')
    # Isolate every data service; inherit provider settings only, never database or production credentials.
    env = {k: v for k, v in os.environ.items() if k in {'PATH', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR'} or k.startswith(('FORESIGHTNINE_LLM_', 'FORESIGHTNINE_GLM_CODING_'))}
    if env.get('FORESIGHTNINE_LLM_PROVIDER', 'mock') in {'mock', 'deterministic_mock', ''}: raise RuntimeError('Actual provider must be configured')
    temp_dir = tempfile.mkdtemp(prefix='foresightnine-357-')
    experiment_token = secrets.token_urlsafe(32)
    env.update(PYTHONPATH=str(API), TMPDIR=temp_dir, FORESIGHTNINE_357_ISOLATED='1', FORESIGHTNINE_STORAGE_MODE='memory', FORESIGHTNINE_ABUSE_MODE='memory', FORESIGHTNINE_SEARCH_PROVIDER='client', FORESIGHTNINE_LOCAL_AUTH_MODE='mock', FORESIGHTNINE_357_TOKEN=experiment_token)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    server = subprocess.Popen([sys.executable, str(HERE/'isolated_api.py'), str(port)], env=env, cwd=temp_dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    started = False
    active = False
    try:
        for _ in range(100):
            status, config = request(base, 'GET', '/_357/config', experiment_token=experiment_token)
            if status == 200: break
            if server.poll() is not None: raise RuntimeError('Isolated API startup failed')
            time.sleep(.1)
        else: raise RuntimeError('Isolated API startup unavailable')
        if not config.get('model') or not config.get('provider'): raise RuntimeError('Actual provider metadata unavailable')
        # Atomic, durable one-time marker before any research submission.
        fd = os.open(HERE/'run-started.json', os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'freeze_sha256': digest((HERE/'freeze.json').read_bytes()), 'provider_config': config, 'state': 'started', 'local_control_dir': temp_dir}, stream); stream.flush(); os.fsync(stream.fileno())
        started = True
        password = secrets.token_urlsafe(32)
        status, auth = request(base, 'POST', '/v1/auth/local/register', {'email': f'acceptance-{uuid4().hex}@example.test', 'password': password})
        if status != 201 or not auth.get('access_token'): save('batch-stop.json', {'reason': 'isolated_registration_failed'}); return
        token = auth['access_token']
        control = {'base': base, 'token': token, 'experiment_token': experiment_token}
        write_control(temp_dir, control)
        for name, filename, source_hash, question, url in TASKS:
            record = {'task': name, 'source_sha256': source_hash, 'question_sha256': digest(question.encode()), 'state': 'preparing', 'provider_usage': 'unknown'}
            save(name + '.json', record)
            source_text = (SNAPSHOTS/filename).read_text()
            status, source = request(base, 'POST', '/v2/research/sources', {'title': filename, 'text': source_text, 'url': url, 'kind': 'official', 'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}}, token)
            if status != 201 or not source.get('source_id'): record['state']='source_admission_failed'; save(name+'.json',record); return
            source_id = source['source_id']
            status, topic = request(base, 'POST', '/v2/research/topics', {'question': question, 'source_ids': [source_id]}, token)
            if status != 201 or not topic.get('topic_id') or not topic.get('versions'): record['state']='topic_admission_failed'; save(name+'.json',record); return
            path = '/v2/research/topics/' + topic['topic_id']
            payload = {'parent_version_id': topic['versions'][0]['version_id'], 'selected_source_ids': [source_id], 'max_search_calls': 0, 'discover_sources': False, 'auto_fetch_official': False, 'gap_search': False, 'plan_research': False, 'support_review': True, 'require_counterarguments': True}
            record.update(state='admission_pending', topic_id=topic['topic_id'], payload_sha256=digest(json.dumps(payload, sort_keys=True).encode())); save(name+'.json',record)
            control.update(task=name, topic_id=topic['topic_id'], operation_id=None)
            write_control(temp_dir, control)
            active = True  # The admission can be ambiguous even if the HTTP request fails.
            status, receipt = request(base, 'POST', path+'/runs', payload, token, 'acceptance-357-'+uuid4().hex)
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
                    active = False
                    break
                time.sleep(2)
            status, settlement = request(base, 'GET', path+'/runs/'+operation+'/settlement', token=token)
            diag_status, diagnostics = request(base, 'GET', '/_357/private/'+operation, experiment_token=experiment_token)
            progress_status, progress = request(base, 'GET', path+'/runs/'+operation+'/progress', token=token)
            record.update(state=state, settlement_http_status=status, diagnostic_http_status=diag_status, progress_http_status=progress_status)
            if status != 200 or diag_status != 200 or progress_status != 200:
                active = True
                record['state']='reconciliation_unknown'; save(name+'.json',record); return
            record['settlement'] = {k: settlement.get(k) for k in ('operation_status','checkpoint_version_id','model_ledger','unresolved_stage_count','reported_tokens','reported_tokens_scope','cost_status')}
            record['diagnostics'] = diagnostics.get('diagnostics', [])
            record['diagnostic_failures'] = diagnostics.get('diagnostic_failures', [])
            record['progress'] = {k: progress.get(k) for k in ('version_id','checkpoint','result_status')}
            save(name+'.json',record)
            version = settlement.get('checkpoint_version_id')
            if version:
                export_status, result = request(base, 'GET', path+'/versions/'+version+'/export', token=token)
                if export_status != 200: record['state']='export_unavailable'; save(name+'.json',record); return
                save(name+'-authorized-export.json', result)
                save(name+'-score.json', score(name, result, source_id, source_text))
            if settlement.get('unresolved_stage_count') != 0:
                active = True  # Keep the isolated journal available for read-only reconciliation.
                return
            if state != 'completed': return
        save('batch-finished.json', {'state':'finished', 'quality_result':'not_reviewed'})
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
