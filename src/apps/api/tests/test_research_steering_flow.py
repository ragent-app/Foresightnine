"""Acceptance through public entry points, with the model paused at a real decision boundary."""
import threading
from uuid import uuid4

from tests.test_research_multiturn import Scripted, env, read_all, submit
from tests.test_research_runtime import turn


def test_user_can_redirect_a_live_run_without_starting_a_second_turn(env):
    started = threading.Event()
    release = threading.Event()
    question = 'Check the two figures separately.'
    next_request = threading.Event()
    next_response = threading.Event()
    observed = []

    def before_correction(messages, kwargs):
        started.set()
        assert release.wait(10)
        return turn('submit_research', {'report': 'Stale answer', 'claims': [], 'limitations': [],
                                       'stopping_reason': 'finished', 'status': 'partial'}, 'old-submit')

    def after_correction(messages, kwargs):
        observed.extend(messages)
        next_request.set()
        assert next_response.wait(12)
        return read_all(env.source_id)(messages, kwargs)

    env.install(Scripted(before_correction, after_correction, submit(env.source_id)))
    created = env.start()
    run = created.json()['operation']['operation_id']
    turn_id = created.json()['turn_id']
    uri = env.path + '/turns/' + turn_id + '/instructions'
    headers = {**env.headers, 'Idempotency-Key': uuid4().hex}
    try:
        assert started.wait(10)
        owner = env.client.post(uri, json={'text': question}, headers=headers)
        assert owner.status_code == 201, owner.text
        assert owner.json()['status'] == 'received'
        assert env.client.post(uri, json={'text': question}, headers=headers).json() == owner.json()
        conflict = env.client.post(uri, json={'text': 'A different request'}, headers=headers)
        assert conflict.status_code == 409
        progress = env.client.get(f'{env.path}/runs/{run}/progress', headers=env.headers).json()
        assert progress['turn']['turn_id'] == turn_id
        assert progress['logical_status'] == 'running' and progress['instructions'] == [owner.json()]
        release.set()
        assert next_request.wait(10)
        awaiting = env.client.get(f'{env.path}/runs/{run}/progress', headers=env.headers).json()
        assert awaiting['instructions'][0]['status'] == 'awaiting_receipt'
    finally:
        release.set()
        next_response.set()
    assert env.wait(run)['status'] == 'completed'
    final = env.latest()
    assert len(final['content']['turns']) == 1
    answer = final['content']['turns'][0]
    assert answer['turn_id'] == turn_id and answer['report'] != 'Stale answer'
    assert [r['status'] for r in answer['user_instructions']] == ['included']
    assert any(message['role'] == 'user' and question in message['content'] for message in observed)
    assert env.client.get(f'{env.path}/runs/{run}/progress', headers=env.headers).json()['logical_status'] == 'completed'
    assert env.client.post(uri, json={'text': question}, headers=headers).status_code == 200
    assert env.client.post(uri, json={'text': 'late'}, headers={**headers, 'Idempotency-Key': uuid4().hex}).status_code == 409


def test_stopping_the_logical_turn_prevents_follow_on_calls(env):
    started = threading.Event()
    release = threading.Event()

    def slow_model(messages, kwargs):
        started.set()
        assert release.wait(10)
        return read_all(env.source_id)(messages, kwargs)

    model = Scripted(slow_model)
    env.install(model)
    created = env.start().json()
    turn_path = f"{env.path}/turns/{created['turn_id']}"
    try:
        assert started.wait(10)
        response = env.client.post(turn_path + '/stop', json={}, headers={**env.headers, 'Idempotency-Key': uuid4().hex})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'stopped'
        assert env.client.post(turn_path + '/instructions', headers={**env.headers, 'Idempotency-Key': uuid4().hex},
                               json={'text': 'late change'}).status_code == 409
    finally:
        release.set()
    run = created['operation']['operation_id']
    assert env.wait(run)['status'] == 'cancelled'
    progress = env.client.get(f'{env.path}/runs/{run}/progress', headers=env.headers).json()
    assert progress['logical_status'] == 'stopped' and model.calls == 1
