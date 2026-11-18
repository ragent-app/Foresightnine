from datetime import datetime, timezone
from hashlib import sha256

from app.research_analysis import analyze
from app.research_context import unprocessed_source_ranges
from app.research_sources import ResearchSource, SourcePermissions


def source(text):
    return ResearchSource(source_id='s', owner_id='o', title='Source', text=text, kind='user_supplied',
        obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True))


def test_known_capacity_can_include_complete_source_above_old_threshold():
    text = 'Evidence: ' + ('x' * 10000) + '.'
    prompts = []
    result = analyze('Evidence?', [source(text)], None, lambda p: (prompts.append(p) or {}, 1),
        output_contract=None, prompt_fits=lambda p: len(p.encode()) <= 20000)
    assert prompts and result.segments[0]['character_end'] == len(text)
    assert result.unprocessed_ranges == []


def test_unknown_capacity_does_not_silently_apply_legacy_byte_ceiling():
    text = 'Evidence: ' + ('x' * 9000) + '.'
    prompts = []
    result = analyze('Evidence?', [source(text)], None, lambda p: (prompts.append(p) or {}, 1),
        output_contract=None, prompt_fits=lambda _: None)
    assert prompts and len(prompts[0].encode()) > 6000
    assert result.segments[0]['character_end'] == len(text)


def test_explicit_byte_budget_still_blocks_oversized_complete_unit():
    text = 'Evidence: ' + ('x' * 9000) + '.'
    prompts = []
    result = analyze('Evidence?', [source(text)], None, lambda p: (prompts.append(p) or {}, 1),
        output_contract=None, max_prompt_chars=6000)
    assert not prompts
    assert result.unprocessed_ranges == [{'source_id':'s', 'character_start':0, 'character_end':len(text), 'kind':'unprocessed'}]


def test_range_complement_keeps_original_coordinates_and_ignores_foreign_ranges():
    assert unprocessed_source_ranges({'s':20}, [
        {'source_id':'s','character_start':2,'character_end':8},
        {'source_id':'s','character_start':6,'character_end':10},
        {'source_id':'other','character_start':0,'character_end':20},
        {'source_id':'s','character_start':-1,'character_end':3},
    ]) == [{'source_id':'s','character_start':0,'character_end':2},
           {'source_id':'s','character_start':10,'character_end':20}]


def test_candidate_work_page_is_resumable_without_reusing_prefix(monkeypatch):
    import app.research_analysis as module
    monkeypatch.setattr(module, 'MAX_SELECTION_CANDIDATES', 2)
    text = 'Revenue first 10 percent. Revenue second 20 percent. Revenue third 30 percent.'
    s = source(text)
    first = analyze('Revenue?', [s], None, lambda _: ({}, 1), output_contract=None,
                    prompt_fits=lambda _: None)
    assert first.work_page['has_more']
    later = analyze('Revenue?', [s], None, lambda _: ({}, 1), output_contract=None,
                    prompt_fits=lambda _: None, candidate_cursor=first.work_page['next'], prior_segments=first.segments)
    assert later.work_page['start'] == first.work_page['next']
    assert later.work_page['source_hashes'] == first.work_page['source_hashes']


def test_later_page_scores_terms_beyond_truncated_index(monkeypatch):
    import app.research_analysis as analysis_module
    import app.research_retrieval as retrieval_module
    monkeypatch.setattr(analysis_module, 'MAX_SELECTION_CANDIDATES', 2)
    monkeypatch.setattr(analysis_module, 'MAX_SELECTION_GROUPS', 1)
    monkeypatch.setattr(retrieval_module, 'MAX_CHUNKS', 1)
    # Each paragraph remains a distinct original unit; the relevant term is
    # absent from the bounded index prefix and earlier candidate work page.
    text = '\n\n'.join([f'Unrelated passage {i}: ' + 'z' * 1400 + '.' for i in range(5)] +
                      ['TARGET_METRIC 2024 was 27 percent.'])
    s = source(text)
    cursor = 0
    prior = []
    seen = []
    while True:
        result = analyze('TARGET_METRIC 2024?', [s], None, lambda _: ({}, 1),
            output_contract=None, prompt_fits=lambda _: None, candidate_cursor=cursor,
            prior_segments=prior if prior else None)
        seen.extend(result.segments)
        prior.extend(result.segments)
        if not result.work_page.get('has_more'):
            break
        assert result.work_page['next'] > cursor
        cursor = result.work_page['next']
    assert any('TARGET_METRIC' in text[item['character_start']:item['character_end']] for item in seen)


def test_more_than_500_prior_ranges_remain_covered():
    segments = [{'source_id':'s','character_start':i * 2,'character_end':i * 2 + 1} for i in range(501)]
    remaining = unprocessed_source_ranges({'s':1002}, segments)
    assert {'source_id':'s','character_start':1000,'character_end':1001} not in remaining


def test_source_and_artifact_totals_are_not_limited_to_one_work_page():
    from app.research_source_policy import source_eligibility
    from app.research_runs import ResearchRunRequest
    from app.research_topics import ResearchContent, ResearchInput
    sources = [source(f'Evidence {i}.').model_copy(update={'source_id':f's{i}'}) for i in range(101)]
    assert all(code == 'eligible' for _, code in source_eligibility(sources, None))
    ids = [item.source_id for item in sources]
    assert len(ResearchInput(question='Q', source_ids=ids).source_ids) == 101
    assert len(ResearchRunRequest(parent_version_id='v', source_ids=ids,
                                  max_input_tokens_per_call=50000).source_ids) == 101
    assert len(ResearchContent(sources=[{}] * 201, facts=[{}] * 2001).facts) == 2001


def test_cursor_binding_rejects_changed_source_without_model_call(monkeypatch):
    import app.research_analysis as module
    monkeypatch.setattr(module, 'MAX_SELECTION_CANDIDATES', 1)
    s = source('Revenue first 10 percent. Revenue second 20 percent.')
    first = analyze('Revenue?', [s], None, lambda _: ({}, 1), output_contract=None,
                    prompt_fits=lambda _: None, capacity_key='provider-profile')
    changed = s.model_copy(update={'text':'Revenue first 10 percent. Revenue second 30 percent.'})
    sent = []
    resumed = analyze('Revenue?', [changed], None, lambda p: (sent.append(p) or {}, 1), output_contract=None,
        prompt_fits=lambda _: None, candidate_cursor=first.work_page['next'],
        prior_segments=first.segments, capacity_key='provider-profile', expected_page_binding=first.work_page['binding'])
    assert not sent
    assert resumed.unprocessed_ranges
    assert any('context changed' in message for message in resumed.limitations)


def test_only_whitespace_gaps_do_not_count_as_unread_content():
    text = 'Alpha.\n\nBeta.'
    segments = [{'source_id':'s','character_start':0,'character_end':6},
                {'source_id':'s','character_start':8,'character_end':len(text)}]
    remaining = unprocessed_source_ranges({'s':len(text)}, segments, {'s':text})
    assert remaining == [{'source_id':'s','character_start':6,'character_end':8,'kind':'whitespace'}]
    table_text = 'Alpha.\n\n| unclear | 5 |'
    unknown = unprocessed_source_ranges({'s':len(table_text)}, segments[:1], {'s':table_text})
    assert unknown[0]['kind'] == 'unprocessed'


def test_acquisition_can_choose_delivery_with_unread_ranges():
    from types import SimpleNamespace
    from app.research_orchestrator import ResearchOrchestrator, CoordinationObservation
    state = CoordinationObservation(source_count=1, candidate_count=0, has_analysis=True,
        claim_count=1, has_counterpoint=False, has_gaps=True, generation_failed=False, analysis_pending=True)
    coordinator = ResearchOrchestrator(context=SimpleNamespace(), workflow=SimpleNamespace(), request=SimpleNamespace(),
        question=True, observe=lambda: state, handlers={}, acquisition_enabled=True,
        acquisition_due=True, acquisition_action=lambda: 'deliver_partial', attempted={'planning','analysis'})
    assert coordinator._choose(state)[0] == 'acquisition_decision'
    coordinator.acquisition_due = False
    assert coordinator._choose(state)[0] is None


def test_new_read_focus_resets_cursor_but_same_focus_resumes():
    from app.research_context import select_work_context
    first = source('First observation.').model_copy(update={'source_id':'one'})
    second = source('Second observation.').model_copy(update={'source_id':'two'})
    state = {'source_offset':0, 'cursor':0, 'binding':None, 'sequence':1, 'pending':True, 'started':True}
    assert select_work_context(state, ('one', 0, 5), [first, second]) == 'unchanged'
    state.update(source_offset=0, cursor=512, binding='original')
    assert select_work_context(state, ('one', 0, 5), [first, second]) == 'unchanged'
    assert state['cursor'] == 512 and state['binding'] == 'original'
    assert select_work_context(state, ('two', 0, 6), [second, first]) == 'reset'
    assert state['source_offset'] == 0 and state['cursor'] == 0 and state['binding'] is None
    assert state['sequence'] == 1 and state['started'] is True


def test_changed_previous_source_is_not_treated_as_new_work():
    from app.research_context import select_work_context
    first = source('Original.').model_copy(update={'source_id':'one'})
    state = {'source_offset':0, 'cursor':0, 'binding':None, 'sequence':0, 'pending':True, 'started':True}
    assert select_work_context(state, None, [first]) == 'unchanged'
    state.update(cursor=512, binding='old')
    changed = first.model_copy(update={'text':'Modified.'})
    assert select_work_context(state, None, [changed]) == 'source_changed'
    assert state['cursor'] == 512 and state['binding'] == 'old'


def test_api_delegated_two_reads_use_the_selected_source(monkeypatch):
    import json, time
    from types import SimpleNamespace
    from uuid import uuid4
    from fastapi.testclient import TestClient
    import app.main as api
    from app.research_acquisition import SYSTEM as ACQUISITION_SYSTEM
    from app.research_analysis import SYSTEM_PROMPT as ANALYSIS_SYSTEM
    from tests.research_prompt_helpers import decode_research_prompt

    class Provider:
        name = 'offline-two-read'
        policy = SimpleNamespace(timeout_seconds=10, max_input_tokens=6000, max_output_tokens=500)
        def __init__(self): self.reads = []; self.analyzed = []
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds, max_output_tokens=None):
            data = decode_research_prompt(prompt)
            if system == ACQUISITION_SYSTEM:
                target = data['target']['requirement_id']
                available = [row for row in data['ranges'] if row.get('source_id') not in self.reads]
                if available and len(self.reads) < 2:
                    ref = available[0]
                    self.reads.append(ref['source_id'])
                    return {'requirement_id':target, 'action':'read', 'source_id':ref['source_id'],
                            'character_start':ref['character_start'], 'character_end':ref['character_end'],
                            'expected_gap':'Need evidence', 'result_update':'Read', 'reason':'Source relevance'}, 10
                return {'requirement_id':target,'action':'deliver_partial','expected_gap':'Unknown',
                        'result_update':'Preserve','reason':'No further evidence'}, 10
            if system == ANALYSIS_SYSTEM:
                data = json.loads(prompt)
                sid = data['sources'][0]['source_id']
                text = data['sources'][0]['passages'][0]['text']
                self.analyzed.append(text[:40])
                if len(self.analyzed) == 1: return {}, 10
                quote = 'Second company revenue fell'
                return {'claims':[{'text':quote,'citations':[{'source_id':sid,'quotation':quote}]}]}, 10
            raise AssertionError('Unexpected local call')
    provider = Provider()
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE','mock')
    import app.research_source_policy as source_policy
    monkeypatch.setattr(source_policy, 'MAX_SOURCES', 1)
    monkeypatch.setattr(api, 'challenge_provider', provider)
    monkeypatch.setattr(api, '_reserve_model_call', lambda *a, **k: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *a, **k: None)
    client = TestClient(api.app)
    reg = client.post('/v1/auth/local/register',json={'email':f'two-read-{uuid4().hex}@example.test','password':'correct horse battery staple!'})
    headers = {'Authorization':'Bearer '+reg.json()['access_token']}
    ids = []
    for text in ('First company revenue rose.', 'Second company revenue fell.'):
        response = client.post('/v2/research/sources', headers=headers, json={'title':'Report','text':text,
            'permissions':{'store':True,'display':True,'model_input':True,'export':True}})
        assert response.status_code == 201
        ids.append(response.json()['source_id'])
    topic = client.post('/v2/research/topics', headers=headers, json={'question':'Compare revenue changes.','source_ids':ids}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    response = client.post(path+'/runs', headers={**headers,'Idempotency-Key':'two-reads'}, json={
        'parent_version_id':topic['versions'][-1]['version_id'], 'discover_sources':False,
        'auto_fetch_official':False, 'plan_research':False, 'gap_followup':True,
        'gap_search':False, 'support_review':False, 'require_counterarguments':False})
    assert response.status_code == 202
    op = response.json()['operation']['operation_id']
    for _ in range(500):
        status = client.get(path+'/runs/'+op, headers=headers).json()['operation']['status']
        if status in {'completed','failed','cancelled'}: break
        time.sleep(.01)
    assert status == 'completed'
    assert provider.reads[:2] == ids and len(provider.analyzed) >= 2
    assert provider.analyzed[0] != provider.analyzed[1]
