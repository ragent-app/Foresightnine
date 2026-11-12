import base64
from copy import deepcopy
from types import SimpleNamespace
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import app.main as api
from tests.test_research_table_provenance import xbrl_fixture


def test_xbrl_source_consistency_survives_export_and_is_removed_on_edit(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    source_id = ''
    class Provider:
        name = 'xbrl-source-consistency'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def fact(key, value, year, date):
                return {'fact_id': key, 'label': 'Total revenue FY' + year, 'value': value, 'unit': 'USD million',
                        'currency': 'USD', 'period': 'FY' + year + ' (year ended ' + date + ')',
                        'citation': {'source_id': source_id, 'quotation': 'Total revenue | 100 | 90'}}
            return {'facts': [fact('a', '100', '2024', 'July 27, 2024'), fact('b', '90', '2023', 'July 29, 2023')],
                    'calculations': [{'formula':'growth_percent', 'first_fact_id':'a', 'second_fact_id':'b'}]}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    reg = client.post('/v1/auth/local/register', json={'email': f'xbrl-{uuid4().hex}@example.test',
        'password':'correct horse battery staple!'}).json()
    headers = {'Authorization':'Bearer ' + reg['access_token']}
    source_id = client.post('/v2/research/sources/html', headers=headers, json={'title':'Synthetic filing',
        'html_base64':base64.b64encode(xbrl_fixture().encode()).decode(),
        'permissions':{'store':True,'display':True,'model_input':True,'export':True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question':'Compare revenue', 'source_ids':[source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    run = client.post(path + '/runs', headers={**headers,'Idempotency-Key':'xbrl-lifecycle'},
        json={'parent_version_id':topic['versions'][0]['version_id'], 'require_counterarguments':False})
    assert run.status_code == 202
    operation = run.json()['operation']['operation_id']
    for _ in range(100):
        status = client.get(path + '/runs/' + operation, headers=headers).json()['operation']['status']
        if status in {'completed','failed'}: break
        time.sleep(.02)
    assert status == 'completed'
    original = client.get(path, headers=headers).json()['versions'][-1]
    assert len(original['content']['facts']) == 2
    assert all(f['xbrl_source_consistency']['currency'] == 'USD' for f in original['content']['facts'])
    assert all(f['verification'] == 'model_extraction_not_independently_verified' for f in original['content']['facts'])
    assert original['content']['calculations'][0]['input_status'] == 'matched'
    exported = client.get(path + '/versions/' + original['version_id'] + '/export', headers=headers)
    assert exported.status_code == 200 and exported.json()['version']['content']['facts'][0]['xbrl_source_consistency']
    recalc = client.post(path + '/calculations/recalculate', headers=headers,
        json={'parent_version_id':original['version_id'], 'calculation_index':0})
    assert recalc.status_code == 201, recalc.text
    latest = recalc.json()['versions'][-1]
    changed = deepcopy(latest['content'])
    changed['facts'][0]['value'] = '101'
    changed['facts'][0]['xbrl_source_consistency']['entity_cik'] = '0000000001'
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id':latest['version_id'], 'content':changed})
    assert saved.status_code == 201, saved.text
    new = saved.json()['versions'][-1]
    assert 'xbrl_source_consistency' not in new['content']['facts'][0]
    assert new['content']['calculations'][0]['input_status'] != 'matched'
    assert saved.json()['versions'][-2]['content']['facts'][0]['xbrl_source_consistency']
