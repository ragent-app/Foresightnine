"""Single-pass acceptance runner against an isolated persistent local API.

Credentials are read from environment and never recorded. An ambiguous admission
or settlement stops the entire batch; the runner never initiates a second run.
"""
from __future__ import annotations
import hashlib
import base64
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]
TERMINAL = {'completed', 'failed', 'cancelled'}
SECRET_KEYS = {'cancel_token', 'resume_token', 'access_token', 'refresh_token', 'authorization', 'api_key', 'password'}


def redact(value):
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if k.lower() not in SECRET_KEYS}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def save(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(redact(data), stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def request(base, method, path, body=None, token=None, key=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if key:
        headers['Idempotency-Key'] = key
    req = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                  method=method, headers=headers)
    try:
        with urlopen(req, timeout=15) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        # Error bodies can contain sensitive data and are not persisted.
        return exc.code, {}
    except (URLError, TimeoutError, OSError, ValueError):
        return None, {}


def login():
    base = os.environ['ACCEPTANCE_API_URL'].rstrip('/')
    parsed = urlparse(base)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise RuntimeError('Acceptance API must be loopback HTTP.')
    email, password = os.environ['ACCEPTANCE_EMAIL'], os.environ['ACCEPTANCE_PASSWORD']
    status, auth = request(base, 'POST', '/v1/auth/local/login', {'email': email, 'password': password})
    if status != 200 or not auth.get('access_token'):
        raise RuntimeError('Isolated account login failed.')
    return base, auth['access_token']


def reconcile(journal_path):
    """Read the original operation only; never repeat a run admission."""
    journal = json.loads(journal_path.read_text())
    if not journal.get('topic_id') or not journal.get('operation_id'):
        raise RuntimeError('No saved operation ID; admission remains unknown.')
    base, token = login()
    path = '/v2/research/topics/' + journal['topic_id'] + '/runs/' + journal['operation_id']
    status, state = request(base, 'GET', path, token=token)
    settlement_status, settlement = request(base, 'GET', path + '/settlement', token=token)
    journal['reconciliation'] = {'operation_http_status': status,
                                 'operation_status': (state.get('operation') or {}).get('status'),
                                 'settlement_http_status': settlement_status,
                                 'settlement': settlement if settlement_status == 200 else None}
    save(journal_path, journal)


def main():
    base, token = login()
    manifest = json.loads((HERE / 'preregistration.json').read_text())
    if len(manifest['tasks']) != 6 or manifest['limits'] != {'tasks': 6, 'calls_per_task': 6, 'tokens_per_task': 40000,
            'seconds_per_task': 360, 'total_calls': 36, 'total_tokens': 240000, 'total_seconds': 2160,
            'search_calls_first_five': 0, 'search_calls_autonomous': 3}:
        raise RuntimeError('Manifest differs from the authorized experimental limits.')
    if any(task.get('plan_research') is not False or task.get('gap_followup') is not True for task in manifest['tasks'][:5]):
        raise RuntimeError('Original tasks must preserve their no-planning policy.')
    if manifest['tasks'][5].get('manual_source_upload') is not False or manifest['tasks'][5].get('sources') != []:
        raise RuntimeError('Autonomous entry task must not receive uploaded sources.')
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != manifest['runner_sha256']:
        raise RuntimeError('Frozen runner changed.')
    if hashlib.sha256((HERE / 'criteria.json').read_bytes()).hexdigest() != manifest['criteria_sha256']:
        raise RuntimeError('Frozen criteria changed.')
    code = json.loads((HERE / 'code-freeze.json').read_text())
    for name, expected in code['sha256'].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Frozen implementation changed.')
    for source in manifest['sources'].values():
        if source.get('upload_kind') not in {'pdf', 'text', 'derived_text'} or source.get('export_permitted') is not True:
            raise RuntimeError('Source type or export permission is not authorized.')
        if source['upload_kind'] != 'pdf' and hashlib.sha256((ROOT / source['file']).read_bytes()).hexdigest() != source['sha256']:
            raise RuntimeError('Frozen source hash changed.')
        if source.get('original_pdf') and hashlib.sha256((ROOT / source['original_pdf']).read_bytes()).hexdigest() != source['pdf_sha256']:
            raise RuntimeError('Frozen original PDF hash changed.')
    if (HERE / 'run-started.json').exists():
        raise RuntimeError('Batch already started; do not rerun.')
    save(HERE / 'run-started.json', {'started_at_unix': time.time(), 'cost_status': 'unknown',
                                     'manifest_sha256': hashlib.sha256((HERE / 'preregistration.json').read_bytes()).hexdigest()})
    batch_start = time.monotonic()
    for task_index, task in enumerate(manifest['tasks']):
        if task_index == 5 and os.environ.get('ACCEPTANCE_SEARCH_AVAILABLE') != '1':
            save(HERE / 'batch-stop.json', {'reason': 'search_provider_not_confirmed', 'next_task_id': task['id']})
            return
        remaining_batch = manifest['limits']['total_seconds'] - (time.monotonic() - batch_start)
        if remaining_batch < manifest['limits']['seconds_per_task']:
            save(HERE / 'batch-stop.json', {'reason': 'insufficient_batch_time_for_next_task',
                 'next_task_id': task['id'], 'remaining_seconds': max(0, int(remaining_batch))})
            return
        journal_path = HERE / (task['id'] + '-journal.json')
        journal = {'task_id': task['id'], 'status': 'preparing', 'events': [], 'cost_status': 'unknown'}
        save(journal_path, journal)
        source_ids = []
        for source_id in task['sources']:
            source = manifest['sources'][source_id]
            permissions = {'store': True, 'display': True, 'model_input': True, 'export': source['export_permitted']}
            if source['upload_kind'] == 'pdf':
                payload = {'title': source['title'], 'pdf_base64': base64.b64encode((ROOT / source['original_pdf']).read_bytes()).decode('ascii'),
                           'text_order': 'layout', 'permissions': permissions}
                status, data = request(base, 'POST', '/v2/research/sources/pdf', payload, token)
                if status == 201 and data.get('response_sha256') != source['pdf_sha256']:
                    journal.update(status='source_hash_mismatch'); save(journal_path, journal); return
            else:
                payload = {'title': source['title'], 'text': (ROOT / source['file']).read_text(), 'url': source['url'],
                           'kind': 'user_supplied' if source['upload_kind'] == 'derived_text' else 'official', 'permissions': permissions}
                status, data = request(base, 'POST', '/v2/research/sources', payload, token)
            if status != 201 or not data.get('source_id'):
                journal.update(status='source_admission_failed', http_status=status); save(journal_path, journal); return
            source_ids.append(data['source_id'])
        status, topic = request(base, 'POST', '/v2/research/topics', {'question': task['question'], 'source_ids': source_ids}, token)
        if status != 201 or not topic.get('topic_id') or not topic.get('versions'):
            journal.update(status='topic_admission_failed', http_status=status); save(journal_path, journal); return
        topic_id = topic['topic_id']
        path = '/v2/research/topics/' + topic_id
        key = 'independent-' + uuid4().hex
        journal.update(status='admission_pending', topic_id=topic_id, idempotency_key=key)
        save(journal_path, journal)
        payload = {'parent_version_id': topic['versions'][0]['version_id'],
                   'max_model_calls': 6, 'max_input_tokens_per_call': 6000, 'max_search_calls': 3 if task_index == 5 else 0, 'max_total_tokens': 40000,
                   'max_elapsed_seconds': 360, 'auto_fetch_official': task_index == 5, 'plan_research': task.get('plan_research', False),
                   'gap_followup': task.get('gap_followup', False),
                   'support_review': True, 'require_counterarguments': task['require_counterarguments']}
        if task_index != 5:
            payload['selected_source_ids'] = source_ids
        else:
            payload.update(discover_sources=True, gap_search=True)
        remaining_batch = manifest['limits']['total_seconds'] - (time.monotonic() - batch_start)
        if remaining_batch < manifest['limits']['seconds_per_task']:
            journal['status'] = 'not_admitted_batch_time'
            save(journal_path, journal)
            save(HERE / 'batch-stop.json', {'reason': 'insufficient_batch_time_before_admission',
                 'next_task_id': task['id'], 'remaining_seconds': max(0, int(remaining_batch))})
            return
        status, receipt = request(base, 'POST', path + '/runs', payload, token, key)
        operation = receipt.get('operation') or {}
        operation_id = operation.get('operation_id')
        if status != 202 or not operation_id:
            journal.update(status='admission_unknown', http_status=status); save(journal_path, journal); return
        journal.update(status='running', operation_id=operation_id, admission_http_status=status)
        save(journal_path, journal)
        deadline = time.monotonic() + 370
        state = None
        while time.monotonic() < deadline:
            status, response = request(base, 'GET', path + '/runs/' + operation_id, token=token)
            state = response.get('operation') if isinstance(response.get('operation'), dict) else None
            journal['events'].append({'http_status': status, 'operation_status': state.get('status') if state else None})
            save(journal_path, journal)
            if status == 200 and state and state.get('status') in TERMINAL:
                break
            time.sleep(1)
        else:
            journal['status'] = 'terminal_unknown'; save(journal_path, journal); return
        status, settlement = request(base, 'GET', path + '/runs/' + operation_id + '/settlement', token=token)
        journal.update(status=state['status'], settlement_http_status=status,
                       settlement=settlement if status == 200 else None)
        save(journal_path, journal)
        if status != 200 or settlement.get('unresolved_stage_count') != 0:
            journal['status'] = 'settlement_unknown'; save(journal_path, journal); return
        status, topic_result = request(base, 'GET', path, token=token)
        versions = topic_result.get('versions') or []
        version_id = versions[-1].get('version_id') if versions and isinstance(versions[-1], dict) else None
        if status != 200 or not version_id:
            journal.update(status='result_unknown', result_http_status=status); save(journal_path, journal); return
        # Never persist a direct workspace read: authorization is checked by the export endpoint.
        status, result = request(base, 'GET', path + '/versions/' + version_id + '/export', token=token)
        if status != 200:
            journal.update(status='export_unavailable', export_http_status=status); save(journal_path, journal); return
        save(HERE / (task['id'] + '-result.json'), result)
        version = result.get('version') if isinstance(result.get('version'), dict) else {}
        content = version.get('content') if isinstance(version.get('content'), dict) else {}
        run = content.get('run') if isinstance(content.get('run'), dict) else {}
        if not run:
            journal['status'] = 'result_unavailable'; save(journal_path, journal); return
        if state['status'] != 'completed':
            journal['status'] = 'operation_failed'; save(journal_path, journal); return
        if task_index == 5 and run.get('search_status') in {'unavailable', 'failed'}:
            journal['status'] = ('search_not_started_unavailable' if run.get('search_status') == 'unavailable'
                                 else 'search_attempt_failed')
            save(journal_path, journal)
            return
        if run.get('error') == 'invalid_model_output':
            journal['status'] = 'settled_quality_failure'; save(journal_path, journal)
            continue
        if run.get('error'):
            journal['status'] = 'provider_or_result_unavailable'; save(journal_path, journal); return


def isolated_app():
    """Apply experiment-only source permission scope before importing API routes.

    This is not a production whitelist. Only the previously checked BEA
    release may be fetched; other candidates remain restricted leads.
    """
    if os.environ.get('ACCEPTANCE_ISOLATION') != '1':
        raise RuntimeError('Experiment source filter requires isolated startup.')
    import app.research_official_retrieval as official
    import app.research_structured_connector as structured
    original_capability = official.official_document_capability
    allowed_url = 'https://www.bea.gov/sites/default/files/2022-10/gdp3q22_adv.pdf'
    def filtered_capability(url, **kwargs):
        if url != allowed_url:
            return None
        return original_capability(url, **kwargs)
    def restrict_structured(*args, **kwargs):
        return structured.StructuredQueryResult('restricted', 'evaluation_source_scope')
    official.official_document_capability = filtered_capability
    structured.execute_structured_query = restrict_structured
    import app.main as application
    if os.environ.get('FORESIGHTNINE_SEARCH_PROVIDER') in {'glm_coding_mcp', 'zhipu_glm_coding_mcp'}:
        from app.glm_mcp_search import GlmCodingMcpSearchEvidenceProvider
        application.evidence_provider = GlmCodingMcpSearchEvidenceProvider(
            os.environ.get('FORESIGHTNINE_GLM_CODING_API_KEY'), max_attempts=1)
    return application.app


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'serve':
        import uvicorn
        uvicorn.run(isolated_app(), host='127.0.0.1', port=int(sys.argv[2]))
    elif len(sys.argv) == 3 and sys.argv[1] == 'reconcile':
        reconcile(Path(sys.argv[2]))
    elif len(sys.argv) == 1:
        main()
    else:
        raise SystemExit('Usage: runner.py [reconcile JOURNAL | serve PORT]')
