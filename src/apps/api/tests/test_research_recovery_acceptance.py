"""Focused recovery acceptance using only local providers and memory storage."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

from tests.research_prompt_helpers import decode_research_prompt
import pytest
from fastapi.testclient import TestClient

import app.main as api
from app.research_recovery import RecoveryJournal
from app.storage import _drop_legacy_null_fields
from app.research_verification import REVIEW_SYSTEM_PROMPT


def _terminal(client, path, headers, operation):
    for _ in range(300):
        response = client.get(f'{path}/runs/{operation}', headers=headers)
        assert response.status_code == 200, response.text
        status = response.json()['operation']['status']
        if status in {'completed', 'failed', 'cancelled'}:
            return status
        time.sleep(.01)
    pytest.fail('Operation did not reach a terminal state')


def _setup(monkeypatch):
    monkeypatch.setattr(api, '_reserve_model_call', lambda *args, **kwargs: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *args, **kwargs: None)
    client = TestClient(api.app)
    registration = client.post('/v1/auth/local/register', json={
        'email': f'recovery-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'})
    assert registration.status_code == 201, registration.text
    headers = {'Authorization': 'Bearer ' + registration.json()['access_token']}
    source = client.post('/v2/research/sources', headers=headers, json={
        'title': 'Report', 'text': 'Revenue increased. Costs increased.',
        'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}})
    assert source.status_code == 201, source.text
    source_id = source.json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={
        'question': 'Assess the company.', 'source_ids': [source_id]})
    assert topic.status_code == 201, topic.text
    path = '/v2/research/topics/' + topic.json()['topic_id']
    return client, headers, source_id, path, topic.json()['versions'][-1]['version_id']


def test_repeated_continuation_preserves_committed_stages_and_original_budget(monkeypatch):
    client, headers, source_id, path, parent = _setup(monkeypatch)
    calls = []

    class Provider:
        name = 'local-recovery-acceptance'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=1500)

        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            payload = decode_research_prompt(prompt)
            if system == REVIEW_SYSTEM_PROMPT:
                calls.append('support_review')
                return {'reviews': [{'claim_index': i, 'verdict': 'supports'}
                                    for i in range(len(payload['claims']))]}, 40
            if 'counterpoint_request' in payload:
                calls.append('counterpoint')
                return {'counterarguments': []}, 30
            if 'Untrusted prior subquestions' in payload.get('question', ''):
                calls.append('gap_followup')
                return {'claims': [{'text': 'Costs increased.', 'citations': [
                    {'source_id': source_id, 'quotation': 'Costs increased.'}]}]}, 35
            calls.append('analysis')
            return {'claims': [
                {'text': 'Revenue increased.', 'citations': [{'source_id': source_id, 'quotation': 'Revenue increased.'}]},
                {'text': 'Margins rose.', 'citations': []}]}, 25

    monkeypatch.setattr(api, 'challenge_provider', Provider())
    original = RecoveryJournal.commit_checkpoint
    interrupted = set()
    observed = []

    def interrupt_after_committed_snapshot(self, snapshot, *, committed_call_ids):
        original(self, snapshot, committed_call_ids=committed_call_ids)
        stages = set(snapshot.plan.get('stage_results', {}))
        boundary = next((name for name in ('support_review', 'gap_followup', 'counterpoint') if name in stages), 'analysis')
        observed.append((boundary, {name: value['status'] for name, value in snapshot.plan.get('stage_results', {}).items()}))
        if boundary not in interrupted:
            interrupted.add(boundary)
            raise RuntimeError('Simulated interruption after durable checkpoint')

    monkeypatch.setattr(RecoveryJournal, 'commit_checkpoint', interrupt_after_committed_snapshot)
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'initial-recovery-run'}, json={
        'parent_version_id': parent, 'evidence_strategy': 'coordinator', 'plan_research': False, 'counterpoint_followup': True, 'gap_followup': True,
        'support_review': True, 'max_model_calls': 4, 'max_total_tokens': 20000,
        'max_search_calls': 0, 'max_elapsed_seconds': 180})
    assert response.status_code == 202, response.text
    operation = response.json()['operation']['operation_id']
    original_deadline = None
    previous_counts = (0, 0, 0)
    previous_actions = 0
    for attempt in range(4):
        assert _terminal(client, path, headers, operation) == 'failed'
        inputs = api.RESEARCH_RUN_INPUTS.get(operation)
        snapshot = inputs['recovery_snapshot']
        if attempt == 0:
            from copy import deepcopy
            from app.research_recovery import RecoverySnapshot, restore_stage_state
            topic = api.research_topics.get(inputs['owner_id'], inputs['topic_id'])
            with_hints = deepcopy(snapshot)
            with_hints['plan']['planning_result']['retrieval_hints'] = ['revenue', 'profit']
            restored = restore_stage_state(snapshot=RecoverySnapshot.model_validate(with_hints), inputs=inputs, topic=topic)
            assert list(restored.planning_result.retrieval_hints) == ['revenue', 'profit']
            del with_hints['plan']['planning_result']['retrieval_hints']
            old = restore_stage_state(snapshot=RecoverySnapshot.model_validate(with_hints), inputs=inputs, topic=topic)
            assert list(old.planning_result.retrieval_hints) == []
        budget = snapshot['budget']
        counts = (budget['model_calls'], budget['search_calls'], budget['accounted_tokens'])
        assert all(after >= before for before, after in zip(previous_counts, counts))
        previous_counts = counts
        actions = snapshot['plan']['coordinator']['actions_used']
        assert actions > previous_actions
        previous_actions = actions
        original_deadline = original_deadline or budget['deadline_at']
        assert budget['deadline_at'] == original_deadline
        status = client.get(f'{path}/runs/{operation}/recovery-status', headers=headers)
        assert snapshot['checkpoint_version_id'] == client.get(path, headers=headers).json()['versions'][-1]['version_id']
        assert _drop_legacy_null_fields(snapshot['source_fingerprints']) == _drop_legacy_null_fields(
            api._research_source_fingerprints([api.research_sources.get(inputs['owner_id'], source_id)]))
        assert status.status_code == 200 and status.json()['can_continue'], (
            status.text, snapshot['checkpoint_version_id'],
            client.get(path, headers=headers).json()['versions'][-1]['version_id'],
            snapshot['source_fingerprints'],
            api._research_source_fingerprints([api.research_sources.get(inputs['owner_id'], source_id)]))
        key = f'recovery-attempt-{attempt}'
        resumed = client.post(f'{path}/runs/{operation}/continue', headers={**headers, 'Idempotency-Key': key}, json={})
        assert resumed.status_code == 202, resumed.text
        replay = client.post(f'{path}/runs/{operation}/continue', headers={**headers, 'Idempotency-Key': key}, json={})
        assert replay.status_code == 202, replay.text
        assert replay.json()['operation']['operation_id'] == resumed.json()['operation']['operation_id']
        other = client.post(f'{path}/runs/{operation}/continue', headers={**headers, 'Idempotency-Key': key + '-other'}, json={})
        assert other.status_code == 409, other.text
        operation = resumed.json()['operation']['operation_id']
    assert _terminal(client, path, headers, operation) == 'completed'
    # The initial prompt already covered the entire short document; the gap
    # stage should not pay for a repeated analysis without new evidence.
    assert calls == ['analysis', 'counterpoint', 'support_review'], (calls, observed)
    assert interrupted == {'analysis', 'counterpoint', 'gap_followup', 'support_review'}
    result = client.get(path, headers=headers).json()['versions'][-1]['content']
    assert result['run']['model_calls'] == 3
    assert result['run']['total_tokens'] == 95
    assert previous_counts == (3, 0, 95)


def test_unresolved_external_call_blocks_replay_but_preserves_partial_delivery(monkeypatch):
    client, headers, source_id, path, parent = _setup(monkeypatch)
    entered, release = Event(), Event()
    calls = []

    class Provider:
        name = 'local-unknown-call'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            calls.append(system)
            if len(calls) == 1:
                return {'claims': [{'text': 'Revenue increased.', 'citations': [
                    {'source_id': source_id, 'quotation': 'Revenue increased.'}]}]}, 25
            entered.set()
            release.wait(5)
            return {'counterarguments': []}, 30

    monkeypatch.setattr(api, 'challenge_provider', Provider())
    started = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'unknown-call-start'}, json={
        'parent_version_id': parent, 'evidence_strategy': 'coordinator', 'counterpoint_followup': True, 'max_model_calls': 2,
        'max_search_calls': 0, 'max_total_tokens': 10000, 'max_elapsed_seconds': 60})
    assert started.status_code == 202, started.text
    operation = started.json()['operation']
    try:
        assert entered.wait(5)
        cancelled = client.post('/v1/operations/' + operation['operation_id'] + '/cancel', headers={
            **headers, 'Idempotency-Key': 'unknown-call-cancel'}, json={'cancel_token': operation['cancel_token']})
        assert cancelled.status_code == 200, cancelled.text
        status = client.get(f"{path}/runs/{operation['operation_id']}/recovery-status", headers=headers)
        assert status.status_code == 200 and status.json()['reason'] == 'unknown_external', status.text
        retry = client.post(f"{path}/runs/{operation['operation_id']}/continue", headers={
            **headers, 'Idempotency-Key': 'unknown-call-continue'}, json={})
        assert retry.status_code == 409, retry.text
        delivered = client.post(f"{path}/runs/{operation['operation_id']}/recover", headers={
            **headers, 'Idempotency-Key': 'unknown-call-deliver'}, json={})
        assert delivered.status_code == 202, delivered.text
        assert _terminal(client, path, headers, delivered.json()['operation']['operation_id']) == 'completed'
        version = client.get(path, headers=headers).json()['versions'][-1]
        result = version['content']
        assert result['run']['status'] == 'partial' and result['claims']
        coverage = result['run']['requirement_coverage']
        assert coverage['requirements'][0]['relation_status'] == 'not_assessed'
        exported = client.get(f"{path}/versions/{version['version_id']}/export", headers=headers)
        assert exported.status_code == 200, exported.text
        assert exported.json()['version']['content']['run']['requirement_coverage'] == coverage
        assert len(calls) == 2
    finally:
        release.set()


def test_checkpoint_is_account_scoped_and_user_revision_blocks_continuation(monkeypatch):
    client, headers, source_id, path, parent = _setup(monkeypatch)
    class Provider:
        name = 'local-recovery-ownership'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            return {'claims': [{'text': 'Revenue increased.', 'citations': [
                {'source_id': source_id, 'quotation': 'Revenue increased.'}]}]}, 25
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    original = RecoveryJournal.commit_checkpoint
    def interrupt(self, snapshot, *, committed_call_ids):
        original(self, snapshot, committed_call_ids=committed_call_ids)
        raise RuntimeError('Simulated interruption after committed analysis')
    monkeypatch.setattr(RecoveryJournal, 'commit_checkpoint', interrupt)
    started = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'owner-run'}, json={
        'parent_version_id': parent, 'evidence_strategy': 'coordinator', 'require_counterarguments': False, 'max_search_calls': 0,
        'max_total_tokens': 10000, 'max_elapsed_seconds': 60})
    assert started.status_code == 202, started.text
    operation = started.json()['operation']['operation_id']
    assert _terminal(client, path, headers, operation) == 'failed'
    other = client.post('/v1/auth/local/register', json={
        'email': f'other-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'})
    foreign = {'Authorization': 'Bearer ' + other.json()['access_token']}
    assert client.get(f'{path}/runs/{operation}/recovery-status', headers=foreign).status_code == 404
    assert client.post(f'{path}/runs/{operation}/continue', headers={**foreign, 'Idempotency-Key': 'foreign-attempt'}, json={}).status_code == 404
    current = client.get(path, headers=headers).json()['versions'][-1]
    revised = client.post(path + '/versions', headers=headers, json={
        'parent_version_id': current['version_id'], 'content': current['content'], 'review': 'confirmed'})
    assert revised.status_code == 201, revised.text
    status = client.get(f'{path}/runs/{operation}/recovery-status', headers=headers)
    assert status.json()['reason'] == 'changed' and not status.json()['can_continue']
    assert client.post(f'{path}/runs/{operation}/continue', headers={**headers, 'Idempotency-Key': 'stale-attempt'}, json={}).status_code == 409


def test_restored_budget_keeps_model_search_token_and_time_limits():
    from app.research_recovery import Continuation, RecoveryBudget, RecoverySnapshot, continuation_context
    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    snapshot = RecoverySnapshot(checkpoint_version_id='v1', next_stage='support_review',
        completed_stages=['analysis'], analysis={}, claims=[], planning_steps=[], source_ids=[],
        budget=RecoveryBudget(max_model_calls=3, max_search_calls=1, max_total_tokens=1000,
            model_calls=2, search_calls=1, accounted_tokens=900, reported_tokens=900, deadline_at=deadline),
        plan={}, source_fingerprints=[])
    context = continuation_context(Continuation(snapshot, 30, False), ensure_running=lambda: None,
        cancel_event=Event, reserve=lambda *_: None, settle=lambda *_: None, reserve_search=lambda: None)
    assert context.calls == 2 and context.accounted_tokens == 900
    assert context.search_exhaustion() == 'search_budget'
    assert context.model_exhaustion(101) == 'token_budget'
    assert context.model_exhaustion(1, required_calls=2) == 'call_budget'
    assert 0 < context.budget.deadline - context.now() <= 30


@pytest.mark.skipif(not os.getenv('FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN'), reason='isolated PostgreSQL not configured')
def test_postgres_two_processes_cannot_admit_same_topic_concurrently():
    script = r'''
import json, os, sys, time
from app.operation_store import PostgresOperationStore
from app.operations import ApiProblem, OperationManager
from app.research_runs import ResearchRunResult
from app.research_topics import ResearchContent
store = PostgresOperationStore(os.environ['FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN'])
manager = OperationManager(store=store)
topic = os.environ['RECOVERY_TEST_TOPIC']
sys.stdin.readline()
def build(operation_id):
    time.sleep(2)
    return ResearchRunResult(operation_id=operation_id, topic_id=topic, parent_version_id='v0',
        status='partial', content=ResearchContent()), topic
try:
    receipt, replay = manager.submit_async(endpoint=f'/v1/research/topics/{topic}/runs',
        idempotency_key=sys.argv[1], owner_subject='account:isolated-test', request_payload={},
        result_field='research', build_result=build, session_id=topic)
    print(json.dumps({'status':'accepted','operation_id':receipt.operation.operation_id}), flush=True)
except ApiProblem as error:
    print(json.dumps({'status':error.error_code,'http':error.status_code}), flush=True)
'''
    env = dict(os.environ, RECOVERY_TEST_TOPIC='topic_' + uuid4().hex)
    workers = [subprocess.Popen([sys.executable, '-c', script, f'concurrent-{i}'], env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
               for i in range(2)]
    try:
        for worker in workers:
            worker.stdin.write('start\n')
            worker.stdin.flush()
        results = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=15)
            assert worker.returncode == 0, stderr
            results.append(json.loads(stdout))
        assert sorted(item['status'] for item in results) == ['accepted', 'lifecycle_conflict']
        assert next(item for item in results if item['status'] == 'lifecycle_conflict')['http'] == 409
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait()


def test_settlement_is_account_scoped_and_separates_ledger_from_provider_report(monkeypatch):
    client, headers, source_id, path, parent = _setup(monkeypatch)

    class Provider:
        name = 'local-settlement'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=1500)

        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            return {'claims': [{'text': 'Revenue increased.', 'citations': [
                {'source_id': source_id, 'quotation': 'Revenue increased.'}]}]}, 25

    monkeypatch.setattr(api, 'challenge_provider', Provider())
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'settlement-run'},
                           json={'parent_version_id': parent, 'evidence_strategy': 'coordinator', 'max_search_calls': 0,
                                 'plan_research': False, 'gap_followup': False, 'support_review': False})
    assert response.status_code == 202, response.text
    operation = response.json()['operation']['operation_id']
    assert _terminal(client, path, headers, operation) == 'completed'
    owner = api.RESEARCH_RUN_INPUTS.get(operation)['owner_id']
    api.billing_ledger.reserve(owner, f'model:{operation}', 100)
    api.billing_ledger.settle(f'model:{operation}', 60)
    api.billing_ledger.reserve(owner, f'model:{operation}:next', 100)
    result = client.get(f'{path}/runs/{operation}/settlement', headers=headers)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body['model_ledger'] == {'reserved_calls': 1, 'settled_calls': 1, 'released_calls': 0, 'accounted_tokens': 60}
    assert body['reported_tokens'] == 25 and body['cost_status'] == 'unknown'
    assert body['checkpoint_version_id']
    other = client.post('/v1/auth/local/register', json={
        'email': f'other-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'})
    other_headers = {'Authorization': 'Bearer ' + other.json()['access_token']}
    assert client.get(f'{path}/runs/{operation}/settlement', headers=other_headers).status_code == 404


def test_direction_proof_rejected_when_forged_and_invalidated_by_source_or_location_edits(monkeypatch):
    from copy import deepcopy
    from app.research_calculations import verified_direction_proof

    client, headers, _, _, _ = _setup(monkeypatch)
    sentence = 'In 2024, output declined 2.5 percent.'
    source_ids = []
    for title in ('First', 'Second'):
        response = client.post('/v2/research/sources', headers=headers, json={'title': title, 'text': sentence,
            'permissions': {'store': True, 'display': True, 'model_input': True}})
        assert response.status_code == 201, response.text
        source_ids.append(response.json()['source_id'])
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Output?', 'source_ids': source_ids}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    fact = {'fact_id': 'f', 'label': 'Output', 'value': '-2.5', 'unit': 'percent', 'period': '2024',
            'source_id': source_ids[0], 'quotation': sentence,
            'direction_derivation': {'method': 'source_direction_percent_v1', 'fact_sha256': 'forged'}}
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id': topic['versions'][-1]['version_id'],
                                                                   'content': {'facts': [fact]}})
    assert saved.status_code == 201, saved.text
    assert 'direction_derivation' not in saved.json()['versions'][-1]['content']['facts'][0]

    class Provider:
        name = 'direction-proof-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            return {'facts': [{'fact_id': 'f', 'label': 'Output', 'value': '-2.5', 'unit': 'percent', 'period': '2024',
                'citation': {'source_id': source_ids[0], 'quotation': sentence}}]}, 10

    monkeypatch.setattr(api, 'challenge_provider', Provider())
    parent = saved.json()['versions'][-1]['version_id']
    started = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'direction-proof-run'},
                          json={'parent_version_id': parent, 'evidence_strategy': 'coordinator', 'selected_source_ids': [source_ids[0]], 'max_search_calls': 0})
    assert started.status_code == 202, started.text
    operation = started.json()['operation']['operation_id']
    assert _terminal(client, path, headers, operation) == 'completed'
    version = client.get(path, headers=headers).json()['versions'][-1]
    assert verified_direction_proof(version['content']['facts'][0])
    edited = deepcopy(version['content'])
    edited['facts'][0]['source_id'] = source_ids[1]
    changed = client.post(path + '/versions', headers=headers, json={'parent_version_id': version['version_id'], 'content': edited})
    assert changed.status_code == 201, changed.text
    next_version = changed.json()['versions'][-1]
    assert 'direction_derivation' not in next_version['content']['facts'][0]
    altered = deepcopy(version['content'])
    altered['facts'][0]['location']['character_start'] += 1
    changed_location = client.post(path + '/versions', headers=headers,
        json={'parent_version_id': next_version['version_id'], 'content': altered})
    assert changed_location.status_code == 201, changed_location.text
    assert 'direction_derivation' not in changed_location.json()['versions'][-1]['content']['facts'][0]


@pytest.mark.parametrize('review_outcome', ['invalid_schema', 'timeout'])
def test_review_settlement_is_separate_from_result_validation(monkeypatch, review_outcome):
    from app.phase3 import ProviderFailure
    client, headers, source_id, path, parent = _setup(monkeypatch)
    calls, settlements = [], []
    monkeypatch.setattr(api, '_settle_model_call', lambda request, call_id, usage: settlements.append((call_id, usage)))

    class Provider:
        name = 'local-review-settlement'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            if system == REVIEW_SYSTEM_PROMPT:
                calls.append('review')
                if review_outcome == 'timeout':
                    raise ProviderFailure('provider_timeout', 'Local simulated timeout.', retryable=True)
                return {'reviews': 'invalid schema'}, 18
            calls.append('analysis')
            return {'claims': [{'text': 'Revenue increased.', 'citations': [
                {'source_id': source_id, 'quotation': 'Revenue increased.'}]}]}, 20

    monkeypatch.setattr(api, 'challenge_provider', Provider())
    if review_outcome == 'invalid_schema':
        commit = RecoveryJournal.commit_checkpoint
        def stop_after_committed_review(self, snapshot, *, committed_call_ids):
            commit(self, snapshot, committed_call_ids=committed_call_ids)
            if 'support_review' in snapshot.plan.get('stage_results', {}):
                raise RuntimeError('Simulated crash after the settled review checkpoint')
        monkeypatch.setattr(RecoveryJournal, 'commit_checkpoint', stop_after_committed_review)
    started = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'review-settlement'},
        json={'parent_version_id': parent, 'evidence_strategy': 'coordinator', 'support_review': True, 'max_model_calls': 2,
              'max_total_tokens': 12000, 'max_search_calls': 0})
    assert started.status_code == 202, started.text
    operation = started.json()['operation']['operation_id']
    assert _terminal(client, path, headers, operation) == ('failed' if review_outcome == 'invalid_schema' else 'completed')
    record = api.RESEARCH_RUN_INPUTS.get(operation)
    assert calls == ['analysis', 'review']
    assert settlements[-1][1] == (18 if review_outcome == 'invalid_schema' else None)
    if review_outcome == 'invalid_schema':
        assert record['recovery_pending'] == []
        snapshot = record['recovery_snapshot']
        assert 'support_review' in snapshot['completed_stages']
        assert snapshot['plan']['stage_results']['support_review']['error'] == 'review_invalid_output'
        assert snapshot['budget']['model_calls'] == 2
        monkeypatch.setattr(RecoveryJournal, 'commit_checkpoint', commit)
        status = client.get(f'{path}/runs/{operation}/recovery-status', headers=headers)
        assert status.json()['can_continue'], status.text
        resumed = client.post(f'{path}/runs/{operation}/continue', headers={**headers, 'Idempotency-Key': 'known-resume'}, json={})
        assert resumed.status_code == 202, resumed.text
        assert _terminal(client, path, headers, resumed.json()['operation']['operation_id']) == 'completed'
        result = client.get(path, headers=headers).json()['versions'][-1]['content']['run']
        assert result['support_review_status'] == 'failed' and result['status'] == 'partial'
        assert result['support_review_error'] == 'review_invalid_output'
        assert calls == ['analysis', 'review']
    else:
        result = client.get(path, headers=headers).json()['versions'][-1]['content']['run']
        assert result['support_review_status'] == 'failed' and result['status'] == 'partial'
        assert result['support_review_error'] == 'provider_timeout'
        assert [item['stage'] for item in record['recovery_pending']] == ['support_review']
        status = client.get(f'{path}/runs/{operation}/recovery-status', headers=headers)
        assert not status.json()['can_continue']
        resumed = client.post(f'{path}/runs/{operation}/continue', headers={**headers, 'Idempotency-Key': 'timeout-resume'}, json={})
        assert resumed.status_code == 409
        assert calls == ['analysis', 'review']


@pytest.mark.parametrize('stage', ['planning', 'analysis', 'gap_followup', 'support_review'])
@pytest.mark.parametrize('outcome', ['invalid_contract', 'provider_timeout', 'settlement_failed'])
def test_model_call_receipts_separate_response_settlement_and_stage_validation(stage, outcome):
    """Across stages, only a returned and durably settled call can clear its marker."""
    from app.research_budget import ResearchExecutionBudget
    from app.research_coordinator import ResearchExecutionContext
    from app.research_recovery import model_receipts_confirmed, stage_result_settled
    from app.research_workflow import ResearchWorkflow
    from app.phase3 import ProviderFailure
    from time import monotonic

    def settle(_call_id, _tokens):
        if outcome == 'settlement_failed':
            raise RuntimeError('simulated storage failure')

    context = ResearchExecutionContext(ResearchExecutionBudget(3, 0, 10000, monotonic()+60), False,
        lambda: None, Event, lambda *_: None, settle, lambda: None)
    workflow = ResearchWorkflow(context)
    call_id = 'test:' + stage
    with workflow.stage(stage) as active:
        before = context.start_model(call_id, 1000)
        tokens = None
        try:
            def provider():
                if outcome == 'provider_timeout':
                    raise ProviderFailure('provider_timeout', 'simulated', retryable=True)
                return {'invalid': 'contract'}, 17
            _, tokens = context.invoke_model(call_id, provider)
        except ProviderFailure:
            pass
        finally:
            try:
                context.finish_model(call_id, tokens, before)
            except RuntimeError:
                pass
        if outcome == 'invalid_contract':
            context.mark_model_processing_error(call_id, 'contract_validation')
        active.status = 'failed'
    receipt = workflow.receipts[-1]
    assert model_receipts_confirmed(receipt) is (outcome == 'invalid_contract')
    call = receipt.model_call_receipts[0]
    if outcome == 'invalid_contract':
        assert call['provider'] == 'returned' and call['settlement'] == 'settled'
        assert call['processing_error'] == 'contract_validation'
    elif outcome == 'provider_timeout':
        assert call['provider'] == 'unknown'
    else:
        assert call['provider'] == 'returned' and call['settlement'] == 'unknown'
    if stage in ('gap_followup', 'support_review'):
        result = SimpleNamespace(status='failed', error='validation_error', tokens=tokens,
                                 search_status='not_requested', isolated_records=[])
        assert stage_result_settled(stage=stage, receipt=receipt, result=result, search_receipts=[]) is (outcome == 'invalid_contract')


def test_assertion_links_recovery_edit_and_export_permissions(monkeypatch):
    """Recovered model suggestions cannot be forged by edits or bypass source export rights."""
    client, headers, source_id, path, parent = _setup(monkeypatch)
    calls = []
    class Provider:
        name = 'local-assertion-recovery'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            calls.append(system)
            return {'claims': [{'text': 'Revenue and costs increased.',
                'citations': [{'source_id': source_id, 'quotation': 'Revenue increased.'}],
                'assertions': [{'text': 'Revenue increased.', 'citation_indexes': [0]},
                               {'text': 'Costs increased.', 'citation_indexes': [1]}]}]}, 25
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    original = RecoveryJournal.commit_checkpoint
    def stop_after_analysis(self, snapshot, *, committed_call_ids):
        original(self, snapshot, committed_call_ids=committed_call_ids)
        raise RuntimeError('simulated interruption after durable snapshot')
    monkeypatch.setattr(RecoveryJournal, 'commit_checkpoint', stop_after_analysis)
    started = client.post(path + '/runs', headers={**headers, 'Idempotency-Key':'assertion-start'}, json={
        'parent_version_id':parent, 'evidence_strategy':'coordinator', 'require_counterarguments':False, 'max_search_calls':0,
        'max_model_calls':1, 'max_total_tokens':10000, 'max_elapsed_seconds':60})
    assert started.status_code == 202, started.text
    operation = started.json()['operation']['operation_id']
    assert _terminal(client, path, headers, operation) == 'failed'
    saved = api.RESEARCH_RUN_INPUTS.get(operation)['recovery_snapshot']
    assertions = saved['analysis']['claims'][0]['assertion_suggestions']
    assert assertions[0]['citation_indexes'] == [0] and assertions[1]['unresolved_reference'] is True
    monkeypatch.setattr(RecoveryJournal, 'commit_checkpoint', original)
    continued = client.post(f'{path}/runs/{operation}/continue', headers={**headers, 'Idempotency-Key':'assertion-continue'}, json={})
    assert continued.status_code == 202, continued.text
    assert _terminal(client, path, headers, continued.json()['operation']['operation_id']) == 'completed'
    version = client.get(path, headers=headers).json()['versions'][-1]
    assert version['content']['claims'][0]['assertion_suggestions'] == assertions
    run = version['content']['run']
    assert run['evidence_links']['claims'][0]['assertions'][1]['citation_indexes'] == []
    assert run['completion_assessment']['assertion_link_gaps']
    assert run['status'] == 'partial'
    exported = client.get(f"{path}/versions/{version['version_id']}/export", headers=headers)
    assert exported.status_code == 200, exported.text
    assert exported.json()['version']['content']['claims'][0]['assertion_suggestions'] == assertions
    from copy import deepcopy
    edited = deepcopy(version['content'])
    edited['claims'][0]['assertion_suggestions'][0]['text'] = 'forged support'
    changed = client.post(path + '/versions', headers=headers, json={'parent_version_id':version['version_id'],
        'content':edited,'review':'confirmed'})
    assert changed.status_code == 201, changed.text
    new = changed.json()['versions'][-1]
    assert not new['content']['claims'][0].get('assertion_suggestions')
    assert len(calls) == 1
    restricted = client.post('/v2/research/sources', headers=headers, json={'title':'Restricted',
        'text':'Additional record.', 'permissions':{'store':True,'model_input':True,'display':True,'export':False}})
    assert restricted.status_code == 201, restricted.text
    attached = client.post(path + '/sources', headers=headers, json={'parent_version_id':new['version_id'],
        'source_ids':[restricted.json()['source_id']]})
    assert attached.status_code == 201, attached.text
    blocked = attached.json()['versions'][-1]
    assert client.get(f"{path}/versions/{blocked['version_id']}/export", headers=headers).status_code == 403


def test_pdf_diagnostics_snapshot_preserves_new_and_legacy_unknown():
    from dataclasses import asdict
    from app.research_analysis import AnalysisResult
    from app.research_recovery import AnalysisSnapshot
    raw = asdict(AnalysisResult([], [], [], [], None))
    raw['pdf_structure_diagnostics'] = [{'source_id':'s', 'geometry_status':'unavailable',
        'mapped_line_count':None, 'unmatched_line_count':None, 'group_count':None,
        'reading_order':'not_independently_verified'}]
    assert AnalysisSnapshot.model_validate(raw).pdf_structure_diagnostics == raw['pdf_structure_diagnostics']
    raw.pop('pdf_structure_diagnostics')
    assert AnalysisSnapshot.model_validate(raw).pdf_structure_diagnostics == []


@pytest.mark.parametrize('failure', ['analysis_schema', 'review_schema', 'review_index', 'provider_parse'])
def test_private_validation_diagnostics_are_bounded_and_not_exported(monkeypatch, failure):
    from app.phase3 import ProviderFailure
    client, headers, source_id, path, parent = _setup(monkeypatch)
    secret = 'SECRET_MODEL_CONTENT_83f4'
    class Provider:
        name = 'private-diagnostics-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds, diagnostic_callback):
            diagnostic_callback({'submission_state':'response_received', 'stages':[{'stage':'tcp', 'elapsed_ms':2, 'result':'ok', 'body':secret}]})
            if failure == 'provider_parse':
                from app.openai_compatible import OpenAICompatibleLlmProvider
                return OpenAICompatibleLlmProvider._completion_payload(json.dumps({'choices':[{'message':{'content':secret}}]})), 0
            if system == REVIEW_SYSTEM_PROMPT:
                if failure == 'review_schema':
                    return {'reviews': [{'claim_index':secret, 'verdict':'supports', secret:secret}]}, 15
                return {'reviews': [{'claim_index':99, 'verdict':'supports'}]}, 15
            if failure == 'analysis_schema':
                from app.research_diagnostics import set_finish_reason
                set_finish_reason('length')
                return {'claims': [{'text':42, secret:secret}]}, 20
            return {'claims':[{'text':'Revenue increased.','citations':[{'source_id':source_id,'quotation':'Revenue increased.'}]}]},20
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    request = {'parent_version_id':parent, 'evidence_strategy':'coordinator', 'max_search_calls':0, 'max_model_calls':2,
               'max_total_tokens':20000, 'require_counterarguments':False,
               'support_review':failure.startswith('review_')}
    started = client.post(path+'/runs', headers={**headers,'Idempotency-Key':'private-diagnostic'},json=request)
    assert started.status_code == 202, started.text
    op=started.json()['operation']['operation_id']
    assert _terminal(client,path,headers,op) == 'completed'
    record=api.RESEARCH_RUN_INPUTS.get(op)
    diagnostics=record['private_validation_diagnostics']
    assert diagnostics and all('transport' in item for item in diagnostics.values())
    item=next(item for item in diagnostics.values() if 'category' in item)
    expected={'analysis_schema':'contract_validation','review_schema':'contract_validation',
              'review_index':'index_constraint','provider_parse':'content_parse'}[failure]
    assert item['transport'] == {'submission_state':'response_received', 'stages':[{'stage':'tcp', 'elapsed_ms':2, 'result':'ok'}]}
    assert item['category']==expected and item['finish']==('provider_length' if failure=='analysis_schema' else 'unknown')
    if failure=='analysis_schema': assert any(x['path'][:2]==['claims',0] for x in item['errors'])
    if failure=='review_index': assert item['errors'][0]['type']=='index_constraint'
    assert secret not in json.dumps(diagnostics)
    exposed=client.get(f'{path}/runs/{op}/inputs',headers=headers)
    assert exposed.status_code==200 and 'private_validation_diagnostics' not in exposed.json()
    latest=client.get(path,headers=headers).json()['versions'][-1]
    exported=client.get(f"{path}/versions/{latest['version_id']}/export",headers=headers)
    assert exported.status_code==200 and secret not in exported.text and 'private_validation_diagnostics' not in exported.text


def test_structured_partial_survives_recovery_and_export_without_rejected_content(monkeypatch):
    from copy import deepcopy
    from app.research_output_contract import EXAMPLE
    client, headers, source_id, path, parent = _setup(monkeypatch)
    class Provider:
        name='local-structured-partial'
        policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=6000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds,contract=None):
            assert contract is not None
            source_ref=decode_research_prompt(prompt)['sources'][0]['source_id']
            draft=deepcopy(EXAMPLE)
            draft.update(evidence=[{'id':'e1','source_id':source_ref,'quotation':'Revenue increased.','source_page':None}],
                facts=[],calculations=[],counterarguments=[],research_questions=[],evidence_needs=[],absence_observations=[],requirement_links=[],limitations=[])
            good={'id':'good','text':'Revenue increased.','scope_declaration':'other',
                  'assertions':[{'text':'Revenue increased.','evidence_ids':['e1'],'calculation_ids':[]}], 'responds_to':[]}
            bad={'id':'bad','text':'HIDDEN_RAW_REJECTED_TEXT','scope_declaration':'other',
                 'assertions':[{'text':'HIDDEN_RAW_REJECTED_TEXT','evidence_ids':['missing'],'calculation_ids':[]}], 'responds_to':[]}
            draft['claims']=[good,bad]
            return draft,25
    monkeypatch.setattr(api,'challenge_provider',Provider())
    original=RecoveryJournal.commit_checkpoint
    def stop(self,snapshot,*,committed_call_ids):
        original(self,snapshot,committed_call_ids=committed_call_ids)
        raise RuntimeError('stop after checkpoint')
    monkeypatch.setattr(RecoveryJournal,'commit_checkpoint',stop)
    started=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'structured-partial-start'},json={
        'parent_version_id':parent,'evidence_strategy':'coordinator','require_counterarguments':False,
        'max_search_calls':0,'max_model_calls':1,'max_total_tokens':10000,'max_elapsed_seconds':60})
    assert started.status_code==202,started.text
    operation=started.json()['operation']['operation_id']
    assert _terminal(client,path,headers,operation)=='failed'
    snapshot=api.RESEARCH_RUN_INPUTS.get(operation)['recovery_snapshot']
    assert snapshot['analysis']['structured_output_status']=='partial'
    assert snapshot['analysis']['structured_output_ids']['claims']=={'good':0}
    assert 'HIDDEN_RAW_REJECTED_TEXT' not in json.dumps(snapshot)
    monkeypatch.setattr(RecoveryJournal,'commit_checkpoint',original)
    resumed=client.post(f'{path}/runs/{operation}/continue',headers={**headers,'Idempotency-Key':'structured-partial-continue'},json={})
    assert resumed.status_code==202,resumed.text
    assert _terminal(client,path,headers,resumed.json()['operation']['operation_id'])=='completed'
    version=client.get(path,headers=headers).json()['versions'][-1]
    content=version['content']
    assert content['run']['status']=='partial' and content['run']['structured_output_status']=='partial'
    assert content['run']['structured_output_ids']['claims']=={'good':0}
    assert len(content['claims'])==1 and 'HIDDEN_RAW_REJECTED_TEXT' not in json.dumps(content)
    exported=client.get(f"{path}/versions/{version['version_id']}/export",headers=headers)
    assert exported.status_code==200 and 'HIDDEN_RAW_REJECTED_TEXT' not in exported.text
    assert exported.json()['version']['content']['run']['structured_output_ids']['claims']=={'good':0}


def test_structured_all_invalid_discloses_no_valid_research_result(monkeypatch):
    from copy import deepcopy
    from app.research_output_contract import EXAMPLE
    client,headers,source_id,path,parent=_setup(monkeypatch)
    class Provider:
        name='local-structured-empty'
        policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=6000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds,contract=None):
            draft=deepcopy(EXAMPLE)
            draft.update(evidence=[],facts=[],calculations=[],counterarguments=[],research_questions=[],evidence_needs=[],absence_observations=[],requirement_links=[],limitations=[])
            draft['claims']=[{'id':'bad','text':'Excluded','scope_declaration':'other','assertions':[{'text':'Excluded','evidence_ids':['missing'],'calculation_ids':[]}],'responds_to':[]}]
            return draft,25
    monkeypatch.setattr(api,'challenge_provider',Provider())
    started=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'structured-all-invalid'},json={
        'parent_version_id':parent,'evidence_strategy':'coordinator','require_counterarguments':False,
        'max_search_calls':0,'max_model_calls':1,'max_total_tokens':10000,'max_elapsed_seconds':60})
    assert started.status_code==202,started.text
    assert _terminal(client,path,headers,started.json()['operation']['operation_id'])=='completed'
    content=client.get(path,headers=headers).json()['versions'][-1]['content']
    assert content['run']['status']=='partial' and not content['claims']
    assert any('No valid research results' in text for text in content['limitations'])


@pytest.mark.parametrize("interrupt", [False, True])
def test_bad_envelope_repair_is_separate_checkpointed_call(monkeypatch, interrupt):
    from copy import deepcopy
    from app.research_output_contract import ANALYSIS_CONTRACT
    client, headers, source_id, path, parent = _setup(monkeypatch)
    calls = []
    class Provider:
        name = 'local-repair-acceptance'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds, contract=None):
            calls.append(prompt)
            if len(calls) == 1:
                return {}, 20
            draft = deepcopy(ANALYSIS_CONTRACT.example)
            for key, value in draft.items():
                if isinstance(value, list): draft[key] = []
            draft['evidence'] = [{'id': 'e1', 'source_id': source_id, 'quotation': 'Revenue increased.', 'source_page': None}]
            draft['claims'] = [{'id': 'c1', 'text': 'Revenue increased.', 'scope_declaration': 'other',
                'assertions': [{'text': 'Revenue increased.', 'evidence_ids': ['e1'], 'calculation_ids': []}], 'responds_to': []}]
            return draft, 25
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    saved = []
    original = RecoveryJournal.commit_checkpoint
    def record(self, snapshot, *, committed_call_ids):
        original(self, snapshot, committed_call_ids=committed_call_ids)
        saved.append(snapshot.model_dump(mode='json'))
        if interrupt and snapshot.plan.get('generation_continuation'):
            raise RuntimeError('simulated process interruption after checkpoint')
    monkeypatch.setattr(RecoveryJournal, 'commit_checkpoint', record)
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'repair-acceptance'}, json={
        'parent_version_id': parent, 'evidence_strategy': 'coordinator', 'plan_research': False,
        'counterpoint_followup': False, 'gap_followup': False, 'support_review': False,
        'max_model_calls': 2, 'max_search_calls': 0, 'max_elapsed_seconds': 180})
    assert response.status_code == 202, response.text
    operation = response.json()['operation']['operation_id']
    assert _terminal(client, path, headers, operation) == ('failed' if interrupt else 'completed')
    if interrupt:
        assert len(calls) == 1
        monkeypatch.setattr(RecoveryJournal, 'commit_checkpoint', original)
        resumed = client.post(f'{path}/runs/{operation}/continue', headers={**headers, 'Idempotency-Key': 'repair-resume'}, json={})
        assert resumed.status_code == 202, resumed.text
        assert _terminal(client, path, headers, resumed.json()['operation']['operation_id']) == 'completed'
    assert len(calls) == 2
    assert any(item['plan'].get('generation_continuation') for item in saved)
    if not interrupt:
        assert any(item['analysis']['structured_output_status'] == 'complete' for item in saved)


def test_truncated_analysis_splits_into_distinct_checkpointed_calls(monkeypatch):
    from copy import deepcopy
    from app.phase3 import ProviderFailure
    from app.research_output_contract import ANALYSIS_CONTRACT
    client, headers, source_id, _, _ = _setup(monkeypatch)
    second = client.post('/v2/research/sources', headers=headers, json={
        'title': 'Second report', 'text': 'Demand declined during the year.',
        'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}})
    assert second.status_code == 201
    topic = client.post('/v2/research/topics', headers=headers, json={
        'question': 'Compare the two reports.', 'source_ids': [source_id, second.json()['source_id']]})
    assert topic.status_code == 201
    path = '/v2/research/topics/' + topic.json()['topic_id']
    calls = []
    class Provider:
        name = 'local-split-acceptance'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds, contract=None):
            calls.append(json.loads(prompt))
            if len(calls) == 1:
                error = ProviderFailure('provider_output_truncated', 'safe', retryable=False, reported_tokens=20)
                error.provider_response_received = True
                raise error
            draft = deepcopy(ANALYSIS_CONTRACT.example)
            for key, value in draft.items():
                if isinstance(value, list): draft[key] = []
            return draft, 25
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    snapshots = []
    original = RecoveryJournal.commit_checkpoint
    def record(self, snapshot, *, committed_call_ids):
        original(self, snapshot, committed_call_ids=committed_call_ids)
        snapshots.append(snapshot.model_dump(mode='json'))
    monkeypatch.setattr(RecoveryJournal, 'commit_checkpoint', record)
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'split-acceptance'}, json={
        'parent_version_id': topic.json()['versions'][-1]['version_id'], 'evidence_strategy': 'coordinator',
        'plan_research': False, 'counterpoint_followup': False, 'gap_followup': False, 'support_review': False,
        'max_model_calls': 3, 'max_search_calls': 0, 'max_elapsed_seconds': 180})
    assert response.status_code == 202, response.text
    assert _terminal(client, path, headers, response.json()['operation']['operation_id']) == 'completed'
    assert len(calls) == 3, {'source_counts': [len(c['sources']) for c in calls], 'status': [(s['plan']['generation_status'], s['plan']['unavailable_reason'], s['analysis']['limitations'][-2:], s['plan']['workflow_receipts'][-1]) for s in snapshots]}
    assert len(calls[0]['sources']) == 2 and all(len(call['sources']) == 1 for call in calls[1:])
    assert sum(bool(item['plan'].get('generation_continuation')) for item in snapshots) >= 2
    assert snapshots[-1]['analysis']['structured_output_status'] == 'partial'


@pytest.mark.parametrize('failure_code', ['provider_output_truncated', 'provider_context_limit', 'incomplete_model_response'])
def test_repeated_truncation_circuit_breaker_blocks_recovery_status_and_continue(monkeypatch, failure_code):
    from app.model_protocol import ModelResponse
    from app.phase3 import ProviderFailure
    from tests.test_research_runtime import turn

    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRIES', '0')
    monkeypatch.setattr(api, '_reserve_model_call', lambda *args, **kwargs: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *args, **kwargs: None)

    client = TestClient(api.app)
    registration = client.post('/v1/auth/local/register', json={
        'email': f'circuit-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!',
    })
    assert registration.status_code == 201
    headers = {'Authorization': 'Bearer ' + registration.json()['access_token']}
    long_text = 'Revenue increased to 100 USD. ' + ('Detailed financial footnote. ' * 200)
    source = client.post('/v2/research/sources', headers=headers, json={
        'title': '10-K Report', 'text': long_text,
        'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True},
    })
    assert source.status_code == 201
    source_id = source.json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={
        'question': 'Summarize revenue.', 'source_ids': [source_id],
    })
    assert topic.status_code == 201
    path = '/v2/research/topics/' + topic.json()['topic_id']

    observed_input_sizes: list[int] = []

    class AlwaysTruncatingProvider:
        name = 'always-truncating-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1500)

        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            observed_input_sizes.append(len(json.dumps(messages, ensure_ascii=False).encode('utf-8')))
            if self.calls == 1:
                return turn('read_sources', {'reads': [{'source_id': source_id}], 'rationale': 'Read source.'}, 'read1')
            if failure_code == 'incomplete_model_response' and self.calls == 2:
                return turn('search_source', {'source_id': source_id, 'query': 'Revenue', 'rationale': 'Locate.'}, 'search1')
            if failure_code == 'incomplete_model_response':
                return ModelResponse({'role': 'assistant', 'content': 'Partial'}, (), None, 'max_tokens', 'truncated')
            err = ProviderFailure(failure_code, f'Simulated {failure_code}.', retryable=False, reported_tokens=1500)
            err.provider_response_received = True
            raise err

    provider = AlwaysTruncatingProvider()
    monkeypatch.setattr(api, 'challenge_provider', provider)

    started = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={
        'parent_version_id': topic.json()['versions'][-1]['version_id'],
        'discover_sources': False,
    })
    assert started.status_code == 202, started.text
    op1 = started.json()['operation']['operation_id']
    assert _terminal(client, path, headers, op1) == 'completed'

    status1 = client.get(f'{path}/runs/{op1}/recovery-status', headers=headers)
    assert status1.status_code == 200
    assert status1.json()['can_continue'] is True
    assert status1.json()['can_deliver'] is False
    assert status1.json()['reason'] is None

    cont1 = client.post(f'{path}/runs/{op1}/continue', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={})
    assert cont1.status_code == 202, cont1.text
    op2 = cont1.json()['operation']['operation_id']
    assert _terminal(client, path, headers, op2) == 'completed'
    expected_calls = 4 if failure_code == 'incomplete_model_response' else 3
    assert provider.calls == expected_calls
    assert observed_input_sizes[-1] < observed_input_sizes[-2]
    cp2 = api.RESEARCH_RUN_INPUTS.get(op2)['runtime_checkpoint']
    assert cp2['continuation_count'] == 1
    assert cp2['consecutive_truncation_continuations'] == 1
    assert len(cp2['failed_attempts']) == 2

    status2 = client.get(f'{path}/runs/{op2}/recovery-status', headers=headers)
    assert status2.status_code == 200
    assert status2.json()['can_continue'] is False
    assert status2.json()['can_deliver'] is False
    assert status2.json()['reason'] == 'repeated_truncation'

    blocked = client.post(f'{path}/runs/{op2}/continue', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={})
    assert blocked.status_code == 409, blocked.text
    body = blocked.json()
    assert body['error_code'] == 'lifecycle_conflict'
    assert provider.calls == expected_calls


def test_runtime_continuation_policy_and_recovery_status_edge_cases():
    from app.research_recovery import (
        MAX_TRUNCATION_CONTINUATIONS,
        prepare_runtime_continuation,
        recovery_status,
        runtime_truncation_loop_blocked,
    )

    assert MAX_TRUNCATION_CONTINUATIONS == 2

    # 1. no_submission compacts with keep_recent=1 when small, escalating to keep_recent=0 above threshold.
    small_cp = {
        'user': '{}',
        'history': [
            {'assistant': {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1', 'name': 'read_sources', 'arguments': {}}]},
             'results': [{'tool_call_id': 'c1', 'content': json.dumps({'results': [{'text': 'A' * 2500}]}), 'is_error': False}]},
            {'assistant': {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c2', 'name': 'read_sources', 'arguments': {}}]},
             'results': [{'tool_call_id': 'c2', 'content': json.dumps({'results': [{'text': 'B' * 2500}]}), 'is_error': False}]},
        ],
        'failed_attempts': [{'reason': 'no_submission'}],
        'read_ranges': [{'source_id': 's1', 'start': 0, 'end': 2500}],
        'facts': [],
    }
    prepared_small = prepare_runtime_continuation(small_cp)
    assert prepared_small['continuation_count'] == 1
    assert prepared_small['consecutive_truncation_continuations'] == 0
    assert json.loads(prepared_small['history'][0]['results'][0]['content'])['cleared'] is True
    assert 'B' * 2500 in prepared_small['history'][1]['results'][0]['content']
    assert prepared_small['read_ranges'] == [{'source_id': 's1', 'start': 0, 'end': 2500}]

    # Above tight max_history_chars -> escalates to keep_recent=0
    prepared_tight = prepare_runtime_continuation(small_cp, max_history_chars=2_000)
    assert json.loads(prepared_tight['history'][1]['results'][0]['content'])['cleared'] is True

    # 2. Transient provider_transport_error after truncation does not trip repeated_truncation,
    #    but a second truncation after recovery does trip it.
    transport_cp = {
        'failed_attempts': [
            {'reason': 'provider_output_truncated'},
            {'reason': 'provider_transport_error'},
        ],
        'continuation_count': 1,
        'consecutive_truncation_continuations': 1,
    }
    assert runtime_truncation_loop_blocked(transport_cp) is False
    second_trunc_cp = {
        'failed_attempts': [
            {'reason': 'provider_output_truncated'},
            {'reason': 'provider_transport_error'},
            {'reason': 'provider_output_truncated'},
        ],
        'continuation_count': 2,
        'consecutive_truncation_continuations': 1,
    }
    assert runtime_truncation_loop_blocked(second_trunc_cp) is True

    # 3. Completed submission waiting for delivery is never blocked by repeated_truncation.
    submitted_cp = {
        **second_trunc_cp,
        'submission': {'status': 'partial', 'report': '', 'claims': [], 'gaps': [], 'stopping_reason': 'ok'},
    }
    assert runtime_truncation_loop_blocked(submitted_cp) is False

    # 4. recovery_status reports repeated_truncation when runtime_checkpoint hits the circuit breaker.
    now = datetime.now(timezone.utc)
    topic = SimpleNamespace(
        topic_id='t1',
        owner_id='owner1',
        versions=[SimpleNamespace(
            version_id='v1',
            created_by='agent',
            review='draft',
            content=SimpleNamespace(run={'operation_id': 'op1', 'checkpoint': 'runtime'}),
        )],
    )
    status = recovery_status(
        topic=topic,
        operation={'operation_id': 'op1', 'owner_subject': 'account:owner1', 'endpoint': '/v1/research/topics/t1/runs', 'status': 'failed'},
        inputs={'owner_id': 'owner1', 'topic_id': 't1', 'operation_id': 'op1', 'runtime_checkpoint': second_trunc_cp},
        owner_id='owner1',
        operation_id='op1',
        now=now,
        source_fingerprints=[],
        provider_matches=True,
    )
    assert status == {
        'can_continue': False,
        'can_deliver': False,
        'reason': 'repeated_truncation',
        'checkpoint': 'runtime',
        'version_id': 'v1',
    }
