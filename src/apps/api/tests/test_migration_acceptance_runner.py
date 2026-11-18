"""Safety tests for the isolated acceptance recorder; no network or model calls."""
import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / 'docs/product/phase-a/338-stage-b-migration-acceptance/runner.py'
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


def test_batch_saves_only_authorized_export(tmp_path, monkeypatch):
    import hashlib
    digest = lambda data: hashlib.sha256(data).hexdigest()
    source = tmp_path / 'source.txt'; source.write_text('Public source text.')
    pdf = tmp_path / 'source.pdf'; pdf.write_bytes(b'pdf fixture')
    criteria = tmp_path / 'criteria.json'; criteria.write_text('{}')
    manifest = {'tasks': [{'id': 'ONE', 'sources': ['one'], 'question': 'Question?', 'require_counterarguments': False}],
                'sources': {'one': {'file': 'source.txt', 'sha256': digest(source.read_bytes()),
                                    'original_pdf': 'source.pdf', 'pdf_sha256': digest(pdf.read_bytes()),
                                    'title': 'Public', 'url': 'https://example.test', 'export_permitted': True}},
                'limits': {'calls_per_task': 3, 'tokens_per_task': 20000, 'seconds_per_task': 180, 'search_calls': 0},
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
    assert json.loads((tmp_path / 'ONE-result.json').read_text())['version']['content']['summary'] == 'authorized'
    assert 'never saved' not in (tmp_path / 'ONE-result.json').read_text()
    assert 'secret' not in (tmp_path / 'ONE-journal.json').read_text()
