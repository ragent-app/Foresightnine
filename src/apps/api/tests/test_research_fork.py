"""Forking a research into a new topic, up to a chosen turn."""
import time
from uuid import uuid4

from tests.test_research_multiturn import Q1, Q2, Scripted, env, read_all, submit  # noqa: F401  (env is a fixture)


def _two_turns(env):
    sid = env.source_id
    env.install(Scripted(read_all(sid), submit(sid), read_all(sid), submit(sid, quotation='Revenue was 80 USD.')))
    env.run()
    env.run({'question_override': Q2})
    return env.latest()


def _fork(env, body):
    return env.client.post(env.path + '/fork', headers=env.headers, json=body)


def test_fork_copies_the_research_with_its_own_version_chain(env):
    head = _two_turns(env)
    response = _fork(env, {'version_id': head['version_id']})
    assert response.status_code == 201, response.text
    fork = response.json()
    assert fork['topic_id'] != env.topic['topic_id']
    assert fork['title'] == f'{Q1}（分支）'
    assert fork['forked_from'] == {'topic_id': env.topic['topic_id'], 'version_id': head['version_id'],
                                   'turn_id': head['content']['turns'][-1]['turn_id'], 'turn_index': 2}
    assert fork['request'] == env.topic['request']
    versions = fork['versions']
    assert len(versions) == 3 and versions[0]['content']['turns'] == []
    assert all(child['parent_version_id'] == parent['version_id'] for parent, child in zip(versions, versions[1:]))
    # Every copied turn points at the fork's own version before it.
    assert [t['parent_version_id'] for t in versions[-1]['content']['turns']] == [versions[0]['version_id'], versions[1]['version_id']]
    assert [t['turn_id'] for t in versions[1]['content']['turns']] == [head['content']['turns'][0]['turn_id']]
    assert versions[-1]['content']['claims'] == head['content']['claims']
    assert versions[-1]['note'].startswith('分叉自《')
    # The original is untouched and both appear in the list.
    assert env.latest() == head
    listed = {item['topic_id'] for item in env.client.get('/v2/research/topics', headers=env.headers).json()}
    assert {env.topic['topic_id'], fork['topic_id']} <= listed


def test_fork_up_to_an_earlier_turn_keeps_only_that_turns_records(env):
    head = _two_turns(env)
    first = head['content']['turns'][0]
    fork = _fork(env, {'version_id': head['version_id'], 'turn_id': first['turn_id'], 'title': 'Revenue only'}).json()
    content = fork['versions'][-1]['content']
    assert fork['title'] == 'Revenue only' and fork['forked_from']['turn_index'] == 1
    assert [t['turn_id'] for t in content['turns']] == [first['turn_id']]
    assert {c['turn_id'] for c in content['claims']} == {first['turn_id']}
    assert content['gaps'] == first['gaps'] and content['summary'] == ''


def test_the_forks_latest_answer_can_be_regenerated(env):
    head = _two_turns(env)
    fork = _fork(env, {'version_id': head['version_id']}).json()
    path = '/v2/research/topics/' + fork['topic_id']
    sid = env.source_id
    env.install(Scripted(read_all(sid), submit(sid, quotation='Costs were 60 USD.')))
    last = fork['versions'][-1]
    response = env.client.post(path + '/runs', headers={**env.headers, 'Idempotency-Key': uuid4().hex}, json={
        'parent_version_id': last['version_id'], 'discover_sources': False, 'replace_turn_id': last['content']['turns'][-1]['turn_id']})
    assert response.status_code == 202, response.text
    operation_id = response.json()['operation']['operation_id']
    for _ in range(300):
        status = env.client.get(f'{path}/runs/{operation_id}', headers=env.headers).json()['operation']['status']
        if status in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    assert status == 'completed'
    turns = env.client.get(path, headers=env.headers).json()['versions'][-1]['content']['turns']
    assert turns[-1]['replaces_turn_id'] == last['content']['turns'][-1]['turn_id'] and turns[-1]['index'] == 2


def test_fork_rejects_unknown_turns_versions_and_other_accounts(env):
    head = _two_turns(env)
    assert _fork(env, {'version_id': head['version_id'], 'turn_id': 'turn-missing'}).status_code == 422
    assert _fork(env, {'version_id': 'version_missing'}).status_code == 404
    other = env.client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test',
                            'password': 'correct horse battery staple!'}).json()
    response = env.client.post(env.path + '/fork', headers={'Authorization': 'Bearer ' + other['access_token']},
                               json={'version_id': head['version_id']})
    assert response.status_code == 404
