"""Safety tests for the isolated acceptance recorder; no network or model calls."""
import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / 'docs/product/phase-a/340-stage-b-followup-acceptance/runner.py'
spec = importlib.util.spec_from_file_location('independent_acceptance', SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_save_redacts_nested_credentials(tmp_path):
    path = tmp_path / 'journal.json'
    runner.save(path, {'operation_id': 'op', 'cancel_token': 'private', 'nested': [{'access_token': 'secret', 'status': 'running'}]})
    assert json.loads(path.read_text()) == {'operation_id': 'op', 'nested': [{'status': 'running'}]}
    assert not path.with_suffix('.json.tmp').exists()


def test_reconcile_only_reads_saved_operation_and_records_unknown(tmp_path, monkeypatch):
    path = tmp_path / 'journal.json'
    runner.save(path, {'topic_id': 'topic', 'operation_id': 'operation', 'status': 'terminal_unknown'})
    requests = []
    monkeypatch.setattr(runner, 'login', lambda: ('http://127.0.0.1:1234', 'not-saved'))
    def fake_request(base, method, target, **kwargs):
        requests.append((method, target))
        return (200, {'operation': {'status': 'running'}}) if not target.endswith('/settlement') else (503, {})
    monkeypatch.setattr(runner, 'request', fake_request)
    runner.reconcile(path)
    journal = json.loads(path.read_text())
    assert requests == [('GET', '/v2/research/topics/topic/runs/operation'), ('GET', '/v2/research/topics/topic/runs/operation/settlement')]
    assert journal['reconciliation']['operation_status'] == 'running'
    assert journal['reconciliation']['settlement'] is None
    assert journal['status'] == 'terminal_unknown'
    assert 'not-saved' not in path.read_text()


@__import__("pytest").mark.parametrize("upload_kind", ["text", "pdf"])
def test_batch_saves_only_authorized_export(tmp_path, monkeypatch, upload_kind):
    import hashlib
    digest = lambda data: hashlib.sha256(data).hexdigest()
    source = tmp_path / 'source.txt'; source.write_text('Public source text.')
    pdf = tmp_path / 'source.pdf'; pdf.write_bytes(b'pdf fixture')
    criteria = tmp_path / 'criteria.json'; criteria.write_text('{}')
    manifest = {'tasks': [{'id': 'ONE', 'sources': ['one'], 'question': 'Question?', 'require_counterarguments': False, 'gap_followup': True}],
                'sources': {'one': {'file': 'source.txt', 'sha256': digest(source.read_bytes()),
                                    'original_pdf': 'source.pdf', 'pdf_sha256': digest(pdf.read_bytes()),
                                    'title': 'Public', 'url': 'https://example.test', 'export_permitted': True, 'upload_kind': upload_kind}},
                'limits': {'tasks': 5, 'calls_per_task': 3, 'tokens_per_task': 20000, 'seconds_per_task': 180, 'total_calls': 15, 'total_tokens': 100000, 'total_seconds': 900, 'search_calls': 0},
                'runner_sha256': digest(SCRIPT.read_bytes()), 'criteria_sha256': digest(criteria.read_bytes())}
    runner.save(tmp_path / 'preregistration.json', manifest)
    runner.save(tmp_path / 'code-freeze.json', {'sha256': {}})
    monkeypatch.setattr(runner, 'HERE', tmp_path)
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner, 'login', lambda: ('http://127.0.0.1:1', 'secret'))
    calls = []
    def fake_request(base, method, target, body=None, token=None, key=None):
        calls.append((method, target, body))
        if target == '/v2/research/sources': return 201, {'source_id': 'source'}
        if target == '/v2/research/sources/pdf': return 201, {'source_id': 'source', 'response_sha256': digest(pdf.read_bytes())}
        if target == '/v2/research/topics': return 201, {'topic_id': 'topic', 'versions': [{'version_id': 'v1'}]}
        if target.endswith('/runs'): return 202, {'operation': {'operation_id': 'op', 'cancel_token': 'secret'}}
        if target.endswith('/settlement'): return 200, {'unresolved_stage_count': 0}
        if target.endswith('/runs/op'): return 200, {'operation': {'status': 'completed'}}
        if target.endswith('/export'): return 200, {'version': {'content': {'summary': 'authorized'}}}
        if target.endswith('/topics/topic'): return 200, {'versions': [{'version_id': 'v2'}], 'private_workspace_only': 'never saved'}
        raise AssertionError(target)
    monkeypatch.setattr(runner, 'request', fake_request)
    runner.main()
    assert any(path.endswith('/versions/v2/export') for _, path, _ in calls)
    assert calls[0][2]['permissions']['export'] is True
    assert next(body for _, path, body in calls if path.endswith('/runs'))['gap_followup'] is True
    assert calls[0][1] == ('/v2/research/sources/pdf' if upload_kind == 'pdf' else '/v2/research/sources')
    if upload_kind == 'pdf': assert 'pdf_base64' in calls[0][2] and 'text' not in calls[0][2]
    assert json.loads((tmp_path / 'ONE-result.json').read_text())['version']['content']['summary'] == 'authorized'
    assert 'never saved' not in (tmp_path / 'ONE-result.json').read_text()
    assert 'secret' not in (tmp_path / 'ONE-journal.json').read_text()


def test_batch_never_starts_task_without_full_remaining_time(tmp_path, monkeypatch):
    import hashlib
    digest = lambda data: hashlib.sha256(data).hexdigest()
    criteria = tmp_path / 'criteria.json'; criteria.write_text('{}')
    manifest = {'tasks': [{'id': 'LATE', 'sources': [], 'question': 'Question?', 'require_counterarguments': False}],
                'sources': {}, 'limits': {'tasks': 5, 'calls_per_task': 3, 'tokens_per_task': 20000,
                    'seconds_per_task': 180, 'total_calls': 15, 'total_tokens': 100000,
                    'total_seconds': 900, 'search_calls': 0},
                'runner_sha256': digest(SCRIPT.read_bytes()), 'criteria_sha256': digest(criteria.read_bytes())}
    runner.save(tmp_path / 'preregistration.json', manifest)
    runner.save(tmp_path / 'code-freeze.json', {'sha256': {}})
    monkeypatch.setattr(runner, 'HERE', tmp_path)
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner, 'login', lambda: ('http://127.0.0.1:1', 'secret'))
    times = iter([0, 721])
    monkeypatch.setattr(runner.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(runner, 'request', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('must not submit')))
    runner.main()
    assert json.loads((tmp_path / 'batch-stop.json').read_text())['reason'] == 'insufficient_batch_time_for_next_task'
    assert not (tmp_path / 'LATE-journal.json').exists()


def test_batch_rechecks_time_after_upload_and_before_admission(tmp_path, monkeypatch):
    import hashlib
    digest = lambda data: hashlib.sha256(data).hexdigest()
    source = tmp_path / 'source.txt'; source.write_text('Public data.')
    criteria = tmp_path / 'criteria.json'; criteria.write_text('{}')
    manifest = {'tasks': [{'id': 'LATE', 'sources': ['one'], 'question': 'Question?', 'require_counterarguments': False}],
        'sources': {'one': {'file': 'source.txt', 'sha256': digest(source.read_bytes()), 'upload_kind': 'text',
                            'title': 'Public', 'url': 'https://example.test', 'export_permitted': True}},
        'limits': {'tasks': 5, 'calls_per_task': 3, 'tokens_per_task': 20000, 'seconds_per_task': 180,
                   'total_calls': 15, 'total_tokens': 100000, 'total_seconds': 900, 'search_calls': 0},
        'runner_sha256': digest(SCRIPT.read_bytes()), 'criteria_sha256': digest(criteria.read_bytes())}
    runner.save(tmp_path / 'preregistration.json', manifest); runner.save(tmp_path / 'code-freeze.json', {'sha256': {}})
    monkeypatch.setattr(runner, 'HERE', tmp_path); monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner, 'login', lambda: ('http://127.0.0.1:1', 'secret'))
    times = iter([0, 0, 721]); monkeypatch.setattr(runner.time, 'monotonic', lambda: next(times))
    calls = []
    def request(base, method, path, body=None, token=None, key=None):
        calls.append(path)
        if path == '/v2/research/sources': return 201, {'source_id': 'source'}
        if path == '/v2/research/topics': return 201, {'topic_id': 'topic', 'versions': [{'version_id': 'v'}]}
        raise AssertionError('must not submit a run')
    monkeypatch.setattr(runner, 'request', request)
    runner.main()
    assert calls == ['/v2/research/sources', '/v2/research/topics']
    assert json.loads((tmp_path / 'LATE-journal.json').read_text())['status'] == 'not_admitted_batch_time'
    assert json.loads((tmp_path / 'batch-stop.json').read_text())['reason'] == 'insufficient_batch_time_before_admission'
