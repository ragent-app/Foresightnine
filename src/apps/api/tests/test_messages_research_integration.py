"""Offline acceptance for the configured Messages research route."""
import json
import time
import pytest
from uuid import uuid4
from fastapi.testclient import TestClient
import app.main as api
import app.openai_compatible as compatible
from app.model_credentials import ModelCredentialCipher
from app.anthropic_messages import AnthropicMessagesProvider
from app.openai_compatible import HttpResponse


@pytest.mark.parametrize("platform", [False, True])
def test_messages_runs_through_api_with_two_tool_turns(monkeypatch, platform):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS', '1000')
    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_PERSONAL_BASE_URL', 'https://models.example.test/v1')
    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_NATIVE_TOOLS', 'true')
    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_CONTINUATION', 'true')
    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_CAPABILITY_BASE_URL', 'https://models.example.test/v1')
    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_CAPABILITY_MODEL', 'fixture-model')
    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_CAPABILITY_VERSION', '2023-06-01')
    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_VERSION', '2023-06-01')
    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_AUTH_SCHEME', 'bearer')
    monkeypatch.setattr(compatible, '_require_public_dns', lambda *_a, **_k: None)
    monkeypatch.setattr(api.model_settings, 'cipher', ModelCredentialCipher(b'x'*32))
    captured = []
    original = AnthropicMessagesProvider.__init__
    source_id = None
    def transport(method, url, headers, body, timeout):
        wire = json.loads(body)
        captured.append((url, headers, wire))
        if len(captured) == 1:
            args = {'reads': [{'source_id': source_id}], 'rationale': 'disclosure'}
            name = 'read_sources'
        else:
            args = {'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': source_id, 'quotation': 'Revenue was 100 USD.', 'role': 'support'}], 'limitations': [], 'stopping_reason': 'answered'}
            name = 'submit_research'
        return HttpResponse(200, json.dumps({'role':'assistant', 'content':[{'type':'tool_use','id':str(len(captured)),'name':name,'input':args}], 'stop_reason':'tool_use', 'usage':{'input_tokens':20,'output_tokens':10,'cache_creation_input_tokens':0,'cache_read_input_tokens':0}}), {'request-id':'req-fixture'})
    def init(self, *a, **kw):
        kw['transport'] = transport
        original(self, *a, **kw)
    monkeypatch.setattr(AnthropicMessagesProvider, '__init__', init)
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email':f'{uuid4().hex}@example.test','password':'correct horse battery staple!'}).json()
    headers = {'Authorization':'Bearer '+auth['access_token']}
    config = {'mode':'personal','protocol':'anthropic_messages','base_url':'https://models.example.test/v1','model':'fixture-model','api_key':'fixture-secret-1234'}
    if platform:
        monkeypatch.setenv('FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL', 'anthropic_messages')
        monkeypatch.setenv('FORESIGHTNINE_RESEARCH_LLM_BASE_URL', config['base_url'])
        monkeypatch.setenv('FORESIGHTNINE_RESEARCH_LLM_MODEL', config['model'])
        monkeypatch.setenv('FORESIGHTNINE_RESEARCH_LLM_API_KEY', config['api_key'])
        monkeypatch.setenv('FORESIGHTNINE_RESEARCH_LLM_CREDENTIAL_REVISION', 'fixture-1')
        monkeypatch.setenv('FORESIGHTNINE_RESEARCH_LLM_ALLOWED_HOSTS', 'models.example.test')
        class ChallengeSentinel:
            name = 'unrelated-challenge'
        monkeypatch.setattr(api, 'challenge_provider', ChallengeSentinel())
    else:
        saved = client.put('/v1/account/model-config',headers=headers,json=config)
        assert saved.status_code == 200 and saved.json()['protocol'] == 'anthropic_messages'
        assert client.post('/v1/account/model-config/test',headers=headers).json()['status'] == 'unavailable'
    source_id = client.post('/v2/research/sources',headers=headers,json={'title':'Report','text':'Revenue was 100 USD.','permissions':{'store':True,'display':True,'model_input':True,'export':True}}).json()['source_id']
    topic = client.post('/v2/research/topics',headers=headers,json={'question':'Revenue?','source_ids':[source_id]}).json()
    path = '/v2/research/topics/'+topic['topic_id']
    response = client.post(path+'/runs',headers={**headers,'Idempotency-Key':uuid4().hex},json={'parent_version_id':topic['versions'][0]['version_id'],'discover_sources':False})
    assert response.status_code == 202, response.text
    operation_id = response.json()['operation']['operation_id']
    for _ in range(200):
        operation = client.get(path+'/runs/'+operation_id,headers=headers).json()['operation']
        if operation['status'] in ('completed','failed','cancelled'): break
        time.sleep(.01)
    assert operation['status'] == 'completed', operation
    content = client.get(path,headers=headers).json()['versions'][-1]['content']
    assert content['claims'][0]['citations'][0]['source_id'] == source_id
    assert len(captured) == 2 and all(x[2]['max_tokens'] == 1000 for x in captured)
    journal = api.RESEARCH_RUN_INPUTS.get(operation_id)
    diagnostics = journal['private_validation_diagnostics']
    from app.model_protocol import canonical_json_dumps
    from hashlib import sha256
    manifests = {row['wire_manifest']['request_sha256']: row for row in diagnostics.values() if 'wire_manifest' in row}
    for _, _, wire in captured:
        body = canonical_json_dumps(wire).encode()
        manifest = manifests[sha256(body).hexdigest()]['wire_manifest']
        assert manifest['wire_bytes'] == len(body)
        assert sum(manifest['value_bytes'].values()) + manifest['serialization_overhead_bytes'] == len(body)
    assert any(entry['tool_call_id'] == '1' and source_id in entry['references'].get('source_id', [])
               for row in diagnostics.values() for entry in row.get('tool_evidence_receipts', []))
    assert any(entry['tool_call_id'] == '1' and source_id in entry['references'].get('source_id', [])
               for row in diagnostics.values() for entry in row.get('input_tool_references', []))
    deliveries = [row['delivery'] for row in diagnostics.values() if 'delivery' in row]
    assert len(deliveries) == 1 and deliveries[0]['status'] == 'completed'
    assert any(source_id in claim['references'].get('source_id', []) for claim in deliveries[0]['claims'])
    assert any(part['status'] == 'answered' and part['references'].get('claim_id') for part in deliveries[0]['parts'])
    assert 'private_validation_diagnostics' not in json.dumps(operation)
    assert 'Revenue was 100 USD.' not in json.dumps(diagnostics)
    assert any(row.get('provider_error', {}).get('status_code') == 200 and row['provider_error'].get('request_id') == 'req-fixture' for row in diagnostics.values())
    assert captured[0][1]['Authorization'] == 'Bearer fixture-secret-1234'
    if platform:
        ledger = client.get(path+'/runs/'+operation_id+'/settlement', headers=headers).json()['model_ledger']
        assert ledger['settled_calls'] == 2 and ledger['accounted_tokens'] == 60
    assert captured[1][2]['messages'][-1]['content'][0]['tool_use_id'] == '1'
