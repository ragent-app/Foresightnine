"""Local-only integration checks for gap-source acquisition and resumption."""
from types import SimpleNamespace

import pytest
import app.main as api
import app.research_gap_stage as gap_stage
from app.research_discovery import DiscoveryResult
from app.research_official_retrieval import OfficialRetrievalResult
from app.research_recovery import external_results_confirmed
from app.research_sources import SourceInput, SourcePermissions
from tests.research_prompt_helpers import decode_research_prompt
from tests.test_research_recovery_acceptance import _setup, _terminal


def test_external_outcomes_require_confirmed_return_or_non_admission():
    assert external_results_confirmed([{'status': 'failed', 'external_state': 'returned'}])
    assert external_results_confirmed([{'status': 'failed', 'external_state': 'not_started'}])
    assert not external_results_confirmed([{'status': 'failed', 'external_state': 'unknown'}])
    assert not external_results_confirmed([{'status': 'failed'}])


@pytest.mark.parametrize('field,value', [
    ('auto_fetch_official', False), ('gap_search', False), ('plan_research', False),
    ('max_total_tokens', 0), ('max_cost_usd', None), ('selected_source_ids', []),
])
def test_same_idempotency_key_cannot_change_presence_or_permissions(monkeypatch, field, value):
    client, headers, _, path, parent = _setup(monkeypatch)
    base = {'parent_version_id': parent, 'max_model_calls': 0, 'max_search_calls': 0}
    key_headers = {**headers, 'Idempotency-Key': 'presence-protection-test'}
    initial = client.post(path + '/runs', headers=key_headers, json=base)
    assert initial.status_code == 202, initial.text
    replay = client.post(path + '/runs', headers=key_headers, json=base)
    assert replay.status_code == 202, replay.text
    assert replay.json()['operation']['operation_id'] == initial.json()['operation']['operation_id']
    changed = client.post(path + '/runs', headers=key_headers, json={**base, field: value})
    assert changed.status_code == 409, changed.text


def test_gap_search_retrieves_source_and_analyzes_it_in_same_run(monkeypatch):
    client, headers, initial_id, path, parent = _setup(monkeypatch)
    model_inputs = []
    source_id = 'source_test_gap_acquired'
    class Provider:
        name = 'local-round-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            payload = decode_research_prompt(prompt)
            model_inputs.append(payload)
            if len(model_inputs) == 1:
                return {'claims': [{'text': 'Unconfirmed costs.', 'citations': []}]}, 25
            return {'claims': [{'text': 'Costs increased.', 'citations': [
                {'source_id': source_id, 'quotation': 'Costs increased.'}]}]}, 30
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    monkeypatch.setattr(api, 'evidence_provider', SimpleNamespace(supports_project_search=True, name='local-search'))
    def initial_search(**kwargs):
        return DiscoveryResult('completed', [], attempted=False)
    candidate = {'title': 'Official report', 'url': 'https://www.bea.gov/report',
                 'status': 'unverified', 'published_at': None, 'retrieved_at': '2026-11-15T00:00:00+00:00'}
    def gap_search(**kwargs):
        kwargs['context'].start_search()
        return DiscoveryResult('completed', [dict(candidate)], attempted=True, returned_count=1)
    def fetch(**kwargs):
        if kwargs.get('batch_id') is None:
            return OfficialRetrievalResult('no_eligible_source')
        source = kwargs['store'].create(kwargs['owner_id'], SourceInput(
            title='Official report', text='Costs increased.', url=candidate['url'], kind='official',
            permissions=SourcePermissions(store=True, display=True, model_input=True, export=True)), source_id=source_id)
        return OfficialRetrievalResult('completed', 1, [], source, 0, [(source, 0)],
                                       [{'candidate_index': 0, 'status': 'retrieved', 'source_id': source_id}])
    monkeypatch.setattr(api, 'execute_source_search', initial_search)
    monkeypatch.setattr(gap_stage, 'execute_source_search', gap_search)
    monkeypatch.setattr(api, 'execute_official_retrieval', fetch)
    monkeypatch.setattr(api, '_reserve_search_call', lambda *args, **kwargs: None, raising=False)
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'gap-source-round-test'}, json={
        'parent_version_id': parent, 'plan_research': False, 'gap_followup': True, 'gap_search': True,
        'support_review': False, 'auto_fetch_official': True, 'max_model_calls': 2, 'max_search_calls': 2,
        'require_counterarguments': False})
    assert response.status_code == 202, response.text
    operation = response.json()['operation']['operation_id']
    assert _terminal(client, path, headers, operation) == 'completed'
    content = client.get(path, headers=headers).json()['versions'][-1]['content']
    assert len(model_inputs) == 2
    assert 'Costs increased.' in str(model_inputs[1])
    assert source_id in {source['source_id'] for source in content['sources']}
    assert content['run']['evidence_rounds'][0]['new_source_ids'] == [source_id]
    assert any(citation['source_id'] == source_id for claim in content['claims'] for citation in claim.get('citations', []))


def test_schema3_resumes_second_evidence_round_without_replaying_calls(monkeypatch):
    from dataclasses import replace
    from app.research_gap_stage import GapStageResult
    from app.research_recovery import RecoveryJournal
    client, headers, initial_id, path, parent = _setup(monkeypatch)
    calls, fetches = [], []
    source_id = 'source_test_resume_rounds'

    class Provider:
        name = 'local-resume-round-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            calls.append('initial')
            return {'claims': [{'text': 'Unconfirmed costs.', 'citations': []}]}, 25
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    monkeypatch.setattr(api, 'evidence_provider', SimpleNamespace(supports_project_search=True, name='local-search'))
    monkeypatch.setattr(api, 'execute_source_search', lambda **kwargs: DiscoveryResult('completed', [], attempted=False))
    def fetch(**kwargs):
        if kwargs.get('batch_id') is None:
            return OfficialRetrievalResult('no_eligible_source')
        fetches.append(kwargs['batch_id'])
        source = kwargs['store'].create(kwargs['owner_id'], SourceInput(
            title='Official report', text='Costs rose; margins changed.', url='https://www.bea.gov/report',
            kind='official', permissions=SourcePermissions(store=True, display=True, model_input=True, export=True)),
            source_id=source_id)
        return OfficialRetrievalResult('completed', 1, [], source, 0, [(source, 0)],
            [{'candidate_index': 0, 'status': 'retrieved', 'source_id': source_id, 'external_state': 'returned'}])
    monkeypatch.setattr(api, 'execute_official_retrieval', fetch)
    monkeypatch.setattr(api, '_reserve_search_call', lambda *args, **kwargs: None)

    def gap(**kwargs):
        index = int(kwargs['round_id'].strip(':'))
        analysis = kwargs['analysis']
        if index == 1:
            candidate = {'title': 'Official report', 'url': 'https://www.bea.gov/report', 'status': 'unverified'}
            kwargs['discovered'].append(candidate)
            kwargs['retrieve_candidates']([candidate])
        if index > 2:
            return GapStageResult(analysis, kwargs['claims'], status='no_evidence', search_status='not_requested')
        context = kwargs['context']
        call_id = kwargs['operation_id'] + ':simulated-gap:' + str(index)
        before = context.start_model(call_id, 7500)
        try:
            def local_result(*args, **kw):
                calls.append('gap-' + str(index))
                return {}, 10
            context.invoke_model(call_id, local_result)
        finally:
            context.finish_model(call_id, 10, before)
        segment = {'source_id': source_id, 'character_start': 0 if index == 1 else 5,
                   'character_end': 5 if index == 1 else 10}
        updated = replace(analysis, source_ids=[*analysis.source_ids, source_id], segments=[*analysis.segments, segment])
        return GapStageResult(updated, kwargs['claims'], status='completed', search_status='not_requested', tokens=10,
                              segments=[segment])
    monkeypatch.setattr(api, 'execute_gap_followup', gap)
    original = RecoveryJournal.commit_checkpoint
    stopped = []
    def interrupt(self, snapshot, *, committed_call_ids):
        original(self, snapshot, committed_call_ids=committed_call_ids)
        if snapshot.schema_version == '3' and snapshot.plan['coordinator'].get('repeat_gap') and not stopped:
            stopped.append(True)
            raise RuntimeError('simulated interruption after first saved evidence round')
    monkeypatch.setattr(RecoveryJournal, 'commit_checkpoint', interrupt)
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'schema3-initial'}, json={
        'parent_version_id': parent, 'plan_research': False, 'gap_followup': True, 'gap_search': False,
        'support_review': False, 'auto_fetch_official': True, 'max_model_calls': 3, 'max_search_calls': 0,
        'require_counterarguments': False})
    assert response.status_code == 202, response.text
    operation = response.json()['operation']['operation_id']
    assert _terminal(client, path, headers, operation) == 'failed'
    saved = api.RESEARCH_RUN_INPUTS.get(operation)['recovery_snapshot']
    assert saved['schema_version'] == '3' and saved['next_stage'] == 'gap_followup'
    assert saved['budget']['model_calls'] == 2 and saved['budget']['reported_tokens'] == 35
    assert client.get(f'{path}/runs/{operation}/recovery-status', headers=headers).json()['can_continue']
    resumed = client.post(f'{path}/runs/{operation}/continue', headers={**headers, 'Idempotency-Key': 'schema3-continue'}, json={})
    assert resumed.status_code == 202, resumed.text
    new_operation = resumed.json()['operation']['operation_id']
    assert _terminal(client, path, headers, new_operation) == 'completed'
    final = client.get(path, headers=headers).json()['versions'][-1]['content']
    assert calls == ['initial', 'gap-1', 'gap-2']
    assert fetches == ['gap-1']
    assert final['run']['model_calls'] == 3 and final['run']['total_tokens'] == 45
    rounds = final['run']['evidence_rounds']
    assert [item['round'] for item in rounds] == [1, 2]
    assert rounds[0]['new_ranges'] and rounds[1]['new_ranges']


def test_structured_plan_candidate_is_saved_and_reaches_analysis(monkeypatch):
    import app.research_structured_connector as connector
    from app.research_planning import PLAN_SYSTEM_PROMPT
    client, headers, initial_id, path, parent = _setup(monkeypatch)
    source_id = 'source_test_structured_hook'
    seen, fetches = [], []
    proposal = {'source_kind': 'sec_company_concept', 'parameters': {
        'entity_query': 'Example', 'taxonomy': 'us-gaap', 'tag': 'Revenue', 'unit': 'USD',
        'period_start': '2023-01-01', 'period_end': '2023-12-31'}}
    class Provider:
        name = 'local-structured-hook'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            if system == PLAN_SYSTEM_PROMPT:
                return {'subquestions': [{'question': 'What was revenue?'}], 'suggested_actions': [], 'retrieval_hints': [],
                        'structured_queries': [proposal]}, 15
            payload = decode_research_prompt(prompt)
            seen.append(payload)
            return {'claims': [{'text': 'Revenue was 100.', 'citations': [
                {'source_id': source_id, 'quotation': 'Revenue was 100.'}]}]}, 20
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    monkeypatch.setattr(api, 'evidence_provider', SimpleNamespace(supports_project_search=True))
    monkeypatch.setattr(api, 'execute_source_search', lambda **kwargs: DiscoveryResult('completed', [], attempted=False))
    monkeypatch.setattr(api, 'execute_official_retrieval', lambda **kwargs: OfficialRetrievalResult('no_eligible_source'))
    def execute(query, **kwargs):
        assert kwargs['permitted'] and query.model_dump(mode='json') == proposal
        fetches.append(query)
        source = kwargs['store'].create(kwargs['owner_id'], SourceInput(
            title='Structured', text='Revenue was 100.', kind='official',
            permissions=SourcePermissions(store=True, display=True, model_input=True, export=True)), source_id=source_id)
        return connector.StructuredQueryResult('completed', source=source, calls=2, external_state='returned')
    monkeypatch.setattr(connector, 'execute_structured_query', execute)
    options = {'plan_research': True, 'gap_followup': False, 'support_review': False,
               'max_model_calls': 2, 'max_search_calls': 1, 'require_counterarguments': False}
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'structured-hook'},
                           json={'parent_version_id': parent, 'auto_fetch_official': True, **options})
    assert response.status_code == 202, response.text
    operation = response.json()['operation']['operation_id']
    assert _terminal(client, path, headers, operation) == 'completed'
    topic = client.get(path, headers=headers).json()
    content = topic['versions'][-1]['content']
    assert seen and 'Revenue was 100.' in str(seen[0]), (content['run'], len(fetches))
    assert source_id in {source['source_id'] for source in content['sources']}
    assert content['run']['structured_source_receipts'][0]['source_id'] == source_id
    assert len(fetches) == 1
    disabled = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'structured-disabled'},
                           json={'parent_version_id': topic['versions'][-1]['version_id'], 'auto_fetch_official': False, **options})
    assert disabled.status_code == 202, disabled.text
    assert _terminal(client, path, headers, disabled.json()['operation']['operation_id']) == 'completed'
    assert len(fetches) == 1
