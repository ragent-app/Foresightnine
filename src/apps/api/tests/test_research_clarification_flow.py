"""Offline clarification and same-turn continuation; no external calls."""
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as api
from tests.test_research_runtime import turn
from tests.test_research_turn_supervisor import _create, _wait


def test_clarification_answer_resumes_original_turn_and_is_private(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'offline-clarification'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)
        def __init__(self): self.calls = 0
        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return turn('request_user_clarification', {'question': 'When is the payment due?',
                    'why_it_matters': 'The date determines liquidity needs.', 'requirement_id': 'payment'}, 'ask')
            return turn('submit_research', {'report': 'The payment date is a relevant condition.', 'claims': [],
                'limitations': ['No product terms were verified.'], 'stopping_reason': 'No sources available.',
                'status': 'partial'}, 'submit')
    provider = Provider()
    monkeypatch.setattr(api, 'challenge_provider', provider)
    client = TestClient(api.app)
    headers, _, topic = _create(client)
    path = '/v2/research/topics/' + topic['topic_id']
    start_key = uuid4().hex
    payload = {'parent_version_id': topic['versions'][0]['version_id'], 'discover_sources': False, 'max_model_calls': 4}
    start = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': start_key}, json=payload)
    assert start.status_code == 202, start.text
    operation_id = start.json()['operation']['operation_id']
    turn_id = start.json()['turn_id']
    assert _wait(client, path, headers, operation_id) == 'completed'
    progress = client.get(path + '/runs/' + operation_id + '/progress', headers=headers).json()
    assert progress['logical_status'] == 'awaiting_user'
    assert progress['clarification']['question'] == 'When is the payment due?'
    other_headers, _, _ = _create(client)
    assert client.get(path + '/runs/' + operation_id + '/progress', headers=other_headers).status_code == 404
    answer = client.post(path + '/turns/' + turn_id + '/instructions', headers={**headers, 'Idempotency-Key': 'answer-key'}, json={'text': 'Next June'})
    assert answer.status_code == 201, answer.text
    replay_answer = client.post(path + '/turns/' + turn_id + '/instructions', headers={**headers, 'Idempotency-Key': 'answer-key'}, json={'text': 'Next June'})
    assert replay_answer.status_code == 200 and replay_answer.json()['instruction_id'] == answer.json()['instruction_id']
    resumed = client.post(path + '/runs/' + operation_id + '/continue', headers={**headers, 'Idempotency-Key': 'resume-key'}, json={})
    assert resumed.status_code == 202, resumed.text
    next_id = resumed.json()['operation']['operation_id']
    status = _wait(client, path, headers, next_id)
    cp = api.RESEARCH_RUN_INPUTS[next_id].get('runtime_checkpoint', {})
    assert status == 'completed', (cp.get('pending'), [(e.get('name'), e.get('tool_call_id')) for e in cp.get('events', []) if e.get('kind') == 'tool'])
    saved = client.get(path, headers=headers).json()['versions'][-1]['content']['turns'][-1]
    assert saved['turn_id'] == turn_id
    assert saved['operation_ids'] == [operation_id, next_id]
    assert saved['user_instructions'][0]['text'] == 'Next June'
    replay = client.post(path + '/runs/' + operation_id + '/continue', headers={**headers, 'Idempotency-Key': 'resume-key'}, json={})
    assert replay.status_code == 202 and replay.json()['operation']['operation_id'] == next_id
    assert len(saved['user_instructions']) == 1


def test_stopping_waiting_turn_prevents_queued_answer_from_resuming(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'offline-stop-clarification'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)
        def complete(self, messages, tools, **kwargs):
            return turn('request_user_clarification', {'question': 'When is the payment due?',
                'why_it_matters': 'It changes liquidity.', 'requirement_id': 'payment'}, 'ask')
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    headers, _, topic = _create(client)
    path = '/v2/research/topics/' + topic['topic_id']
    start = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex},
        json={'parent_version_id': topic['versions'][0]['version_id'], 'discover_sources': False, 'max_model_calls': 4})
    assert start.status_code == 202, start.text
    op, turn_id = start.json()['operation']['operation_id'], start.json()['turn_id']
    assert _wait(client, path, headers, op) == 'completed'
    answer = client.post(path + '/turns/' + turn_id + '/instructions', headers={**headers, 'Idempotency-Key': 'answer-key'}, json={'text': 'Next June'})
    assert answer.status_code == 201, answer.text
    stop = client.post(path + '/turns/' + turn_id + '/stop', headers={**headers, 'Idempotency-Key': 'stop-key'}, json={})
    assert stop.status_code == 200, stop.text
    resume = client.post(path + '/runs/' + op + '/continue', headers={**headers, 'Idempotency-Key': 'resume-key'}, json={})
    assert resume.status_code == 409, resume.text
    assert api.turn_instructions.read(topic['owner_id'], topic['topic_id'], turn_id)['status'] == 'stopped'
