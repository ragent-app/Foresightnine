"""Regenerate / edit-and-resend the latest answer, and restore an earlier version.

The run reads the version before the replaced turn; its result is appended as the newest version,
so the replaced answer stays in history and the version chain stays linear.
"""
from uuid import uuid4

from app.phase3 import ProviderFailure
from tests.test_research_multiturn import Q1, Q2, Scripted, env, read_all, submit  # noqa: F401  (env is a fixture)


def _two_turns(env):
    sid = env.source_id
    provider = Scripted(read_all(sid), submit(sid), read_all(sid), submit(sid, quotation='Revenue was 80 USD.'))
    env.install(provider)
    env.run()
    env.run({'question_override': Q2})
    return env.latest()


def _chain_is_linear(versions):
    return all(child['parent_version_id'] == parent['version_id'] for parent, child in zip(versions, versions[1:]))


def test_regenerate_replaces_the_latest_answer_and_keeps_the_old_one_in_history(env):
    before = _two_turns(env)
    old_turn = before['content']['turns'][-1]
    sid = env.source_id
    seen = {}

    def submit_again(messages, kwargs):
        seen['question'] = messages[1]['content']
        return submit(sid, quotation='Costs were 60 USD.')(messages, kwargs)

    env.install(Scripted(read_all(sid), submit_again))
    env.run({'replace_turn_id': old_turn['turn_id']})
    topic = env.client.get(env.path, headers=env.headers).json()
    latest = topic['versions'][-1]
    turns = latest['content']['turns']
    assert [t['index'] for t in turns] == [1, 2]
    assert turns[0] == before['content']['turns'][0]
    new_turn = turns[-1]
    assert new_turn['turn_id'] != old_turn['turn_id'] and new_turn['replaces_turn_id'] == old_turn['turn_id']
    assert new_turn['question'] == Q2  # regenerate keeps the question
    assert Q2 in seen['question']
    # The replaced answer's claims are gone from the newest version but intact in the previous one.
    assert {c['turn_id'] for c in latest['content']['claims']} == {turns[0]['turn_id'], new_turn['turn_id']}
    assert topic['versions'][-2]['content']['turns'][-1] == old_turn
    assert _chain_is_linear(topic['versions'])


def test_edit_and_resend_uses_the_edited_question(env):
    before = _two_turns(env)
    sid = env.source_id
    env.install(Scripted(read_all(sid), submit(sid)))
    env.run({'replace_turn_id': before['content']['turns'][-1]['turn_id'], 'question_override': 'What were costs?'})
    turns = env.latest()['content']['turns']
    assert [t['question'] for t in turns] == [Q1, 'What were costs?']


def test_only_the_latest_answer_can_be_replaced(env):
    before = _two_turns(env)
    response = env.start({'replace_turn_id': before['content']['turns'][0]['turn_id']})
    assert response.status_code == 409
    assert 'latest answer' in response.json()['message']


def test_review_edits_after_the_answer_need_explicit_confirmation(env):
    before = _two_turns(env)
    content = {**before['content'], 'summary': 'My own judgement.'}
    saved = env.client.post(env.path + '/versions', headers=env.headers,
                            json={'parent_version_id': before['version_id'], 'content': content})
    assert saved.status_code == 201, saved.text
    turn_id = before['content']['turns'][-1]['turn_id']
    refused = env.start({'replace_turn_id': turn_id})
    assert refused.status_code == 409 and 'confirm' in refused.json()['message']
    sid = env.source_id
    env.install(Scripted(read_all(sid), submit(sid)))
    env.run({'replace_turn_id': turn_id, 'discard_review_edits': True})
    assert env.latest()['content']['turns'][-1]['replaces_turn_id'] == turn_id


def test_a_failed_regeneration_is_not_resumable_and_leaves_the_answer(env):
    before = _two_turns(env)

    def fail(messages, kwargs):
        raise ProviderFailure('provider_timeout', 'timeout', retryable=True)

    env.install(Scripted(fail))
    response = env.start({'replace_turn_id': before['content']['turns'][-1]['turn_id']})
    assert response.status_code == 202, response.text
    operation_id = response.json()['operation']['operation_id']
    assert env.wait(operation_id)['status'] == 'failed'
    assert env.latest() == before
    status = env.client.get(f'{env.path}/runs/{operation_id}/recovery-status', headers=env.headers).json()
    assert status['can_continue'] is False and status['can_deliver'] is False and status['reason'] == 'replaced_turn_run'
    resumed = env.client.post(f'{env.path}/runs/{operation_id}/continue',
                              headers={**env.headers, 'Idempotency-Key': uuid4().hex}, json={})
    assert resumed.status_code == 409


def test_restore_appends_a_copy_of_an_earlier_version(env):
    before = _two_turns(env)
    sid = env.source_id
    env.install(Scripted(read_all(sid), submit(sid, quotation='Costs were 60 USD.')))
    env.run({'replace_turn_id': before['content']['turns'][-1]['turn_id']})
    head = env.latest()
    stale = env.client.post(f"{env.path}/versions/{before['version_id']}/restore", headers=env.headers,
                            json={'parent_version_id': before['version_id']})
    assert stale.status_code == 409
    restored = env.client.post(f"{env.path}/versions/{before['version_id']}/restore", headers=env.headers,
                               json={'parent_version_id': head['version_id']})
    assert restored.status_code == 201, restored.text
    versions = restored.json()['versions']
    assert versions[-1]['content'] == before['content']
    assert versions[-1]['created_by'] == 'user' and versions[-1]['note'].startswith('恢复为 v')
    assert _chain_is_linear(versions)
    missing = env.client.post(f"{env.path}/versions/version_missing/restore", headers=env.headers,
                              json={'parent_version_id': versions[-1]['version_id']})
    assert missing.status_code == 404
