"""Controlled end-to-end runtime scenarios; no network or model calls."""
import json
from hashlib import sha256

from app.model_protocol import ModelResponse, ModelUsage
from app.research_runtime import run_research_agent
from tests.test_research_runtime_attention import _ctx, _source, _tool_response, NativeTurnRecorder


def last_result(turn):
    return json.loads(turn.history[-1].results[-1].content)


def test_counter_search_does_not_exhaust_discovery_for_the_core_question():
    question = 'How did Example market capitalization change?'
    searches = []

    def discover(query):
        searches.append(query)
        return []

    submit = {'report': 'No dated capitalization figures found.', 'claims': [], 'limitations': ['Data unavailable.'],
              'stopping_reason': 'Data unavailable.', 'status': 'partial'}

    def follow_core(turn, _):
        followup = last_result(turn)['next_action']
        assert followup['tool'] == 'news_search'
        return _tool_response([('news_search', followup['arguments'], 'core')])

    counter = {'query': 'Example tariff risk', 'rationale': 'Test an alternative explanation.',
               'counter_search': {'target': 'Example share price', 'hypothesis': 'Tariffs reduced demand.'}}
    model = NativeTurnRecorder([_tool_response([('news_search', counter, 'counter')]),
                                _tool_response([('submit_research', submit, 'draft')]), follow_core,
                                _tool_response([('submit_research', submit, 'final')])])
    result = run_research_agent(question=question, sources=[], model=model, context=_ctx(), discover=discover,
                                sync_instructions=lambda state, boundary: False)
    assert result.reason == 'submitted'
    assert searches == [counter['query'], question]


def test_early_draft_proceeds_through_retrieval_facts_and_calculation():
    text = 'Revenue was 100 USD. Revenue was 80 USD.'
    src = _source(text=text)
    src.content_sha256 = sha256(text.encode()).hexdigest()
    fact_ids = []
    def search(turn, _):
        candidate = last_result(turn)
        assert candidate['reason'] == 'core_evidence_path_available'
        return _tool_response([(candidate['next_action']['tool'], candidate['next_action']['arguments'], 'search')])
    def read(turn, _):
        hit = last_result(turn)['hits'][0]['read_sources']
        return _tool_response([('read_sources', {'reads': [{'source_id': hit['source_id'], 'start': 0, 'end': len(text)}], 'rationale': 'read the full located passage'}, 'read')])
    def record(turn, _):
        return _tool_response([('record_facts', {'facts': [
            {'label': 'revenue', 'value': 100, 'unit': 'USD', 'currency': 'USD', 'period': '2025', 'source_id': 's1', 'quotation': 'Revenue was 100 USD.'},
            {'label': 'revenue', 'value': 80, 'unit': 'USD', 'currency': 'USD', 'period': '2024', 'source_id': 's1', 'quotation': 'Revenue was 80 USD.'}], 'rationale': 'record both periods'}, 'facts')])
    def calculate(turn, _):
        fact_ids.extend(row['fact_id'] for row in last_result(turn)['facts'])
        return _tool_response([('calculate_financials', {'calculations': [{'formula': 'growth_percent', 'first_fact_id': fact_ids[0], 'second_fact_id': fact_ids[1]}], 'rationale': 'calculate growth'}, 'calc')])
    def submit(turn, _):
        assert last_result(turn)['calculations'][0]['value'] == '25.00'
        return _tool_response([('submit_research', {'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': 's1', 'quotation': 'Revenue was 100 USD.', 'role': 'support'}], 'limitations': ['Calculation context requires review.'], 'stopping_reason': 'reviewed', 'status': 'partial'}, 'final')])
    model = NativeTurnRecorder([
        _tool_response([('submit_research', {'report': 'No evidence yet.', 'claims': [], 'limitations': ['Evidence not read.'], 'stopping_reason': 'incomplete', 'status': 'partial'}, 'early')]),
        search, read, record, calculate, submit,
    ])
    saved = []
    result = run_research_agent(question='What was revenue growth from 2024 to 2025?', sources=[src], model=model,
                                context=_ctx(), sync_instructions=lambda state, boundary: False,
                                checkpoint=lambda state: saved.append(json.loads(json.dumps(state))))
    assert result.reason == 'submitted'
    assert result.calculations[0]['value'] == '25.00'
    assert saved[-1]['submission_drafts'] and saved[-1]['submission']
    assert [e['name'] for e in result.events if e.get('kind') == 'tool'] == ['submit_research', 'search_library', 'read_sources', 'record_facts', 'calculate_financials']


def test_empty_external_discovery_paraphrases_cannot_loop():
    queries = []
    def discover(query):
        queries.append(query)
        return []
    def alternate(turn, index):
        return _tool_response([('news_search', {'query': f'different wording {index}', 'research_target': 'unknown issuer',
                                               'rationale': 'try again'}, f'search{index}')])
    model = NativeTurnRecorder([alternate] * 5)
    result = run_research_agent(question='Research the unknown issuer.', sources=[], model=model, context=_ctx(),
                                discover=discover, sync_instructions=lambda state, boundary: False)
    assert result.reason == 'no_progress'
    assert len(queries) == 1


def test_different_authorized_discovery_scope_is_still_available():
    queries = []
    def discover(query):
        queries.append(query)
        return []
    model = NativeTurnRecorder([
        _tool_response([('news_search', {'query': 'filing', 'research_target': 'filing', 'languages': ['en'], 'rationale': 'english'}, 'a')]),
        _tool_response([('news_search', {'query': 'issuer disclosure', 'research_target': 'filing', 'languages': ['en'], 'rationale': 'paraphrase'}, 'b')]),
        _tool_response([('news_search', {'query': 'disclosure', 'research_target': 'filing', 'languages': ['zh'], 'rationale': 'different language'}, 'c')]),
        _tool_response([('news_search', {'query': 'filing', 'research_target': 'filing', 'languages': ['zh'], 'rationale': 'paraphrase'}, 'd')]),
        _tool_response([('news_search', {'query': 'report', 'research_target': 'filing', 'languages': ['zh'], 'rationale': 'paraphrase again'}, 'e')]),
    ])
    result = run_research_agent(question='Find the filing.', sources=[], model=model, context=_ctx(),
                                discover=discover, sync_instructions=lambda state, boundary: False)
    assert result.reason == 'no_progress'
    assert len(queries) == 2


def test_distinct_entities_remain_searchable_after_empty_result_and_rephrase_is_suppressed():
    queries = []
    def discover(query):
        queries.append(query)
        return []
    def submit(turn, _):
        assert last_result(turn)['reason'] == 'discovery_scope_no_progress'
        return _tool_response([('submit_research', {'report': 'No filings were located.', 'claims': [],
            'limitations': ['The authorized searches found no source.'],
            'stopping_reason': 'sources unavailable', 'status': 'partial'}, 'submit')])
    model = NativeTurnRecorder([
        _tool_response([('news_search', {'query': 'Aster annual filing', 'research_target': 'Aster', 'rationale': 'first company'}, 'a')]),
        _tool_response([('news_search', {'query': 'Bramble annual filing', 'research_target': 'Bramble', 'rationale': 'second company'}, 'b')]),
        _tool_response([('news_search', {'query': 'Aster yearly report', 'research_target': 'Aster', 'rationale': 'same path reworded'}, 'a2')]),
        submit,
    ])
    result = run_research_agent(question='Compare Aster and Bramble annual filings.', sources=[], model=model, context=_ctx(),
                                discover=discover, sync_instructions=lambda state, boundary: False)
    assert result.reason == 'submitted'
    assert queries == ['Aster annual filing', 'Bramble annual filing']


def test_unidentified_discovery_targets_do_not_lock_other_queries():
    queries = []
    def discover(query):
        queries.append(query)
        return []
    model = NativeTurnRecorder([
        _tool_response([('news_search', {'query': 'first entity filing', 'rationale': 'first'}, 'a')]),
        _tool_response([('news_search', {'query': 'second entity filing', 'research_target': 'unknown subtask', 'rationale': 'second'}, 'b')]),
        _tool_response([('submit_research', {'report': 'No filings were located.', 'claims': [],
            'limitations': ['The authorized searches found no source.'], 'stopping_reason': 'sources unavailable',
            'status': 'partial'}, 'submit')]),
    ])
    result = run_research_agent(question='Compare the two companies.', sources=[], model=model, context=_ctx(),
                                discover=discover, sync_instructions=lambda state, boundary: False)
    assert result.reason == 'submitted'
    assert queries == ['first entity filing', 'second entity filing']


def test_correction_remains_visible_after_compaction_and_drives_calculation(monkeypatch):
    import app.research_runtime as runtime
    monkeypatch.setattr(runtime, '_l1_compaction_trigger_chars', lambda *args, **kwargs: 1)
    text = 'Revenue was 100 USD. Revenue was 80 USD.'
    src = _source(text=text)
    src.content_sha256 = sha256(text.encode()).hexdigest()
    fact_ids = []
    def early_submit(turn, _):
        assert [row['sequence'] for row in turn.user_instructions] == [1]
        fact_ids.extend(row['fact_id'] for row in last_result(turn)['facts'])
        return _tool_response([('submit_research', {
            'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': 's1',
                'quotation': 'Revenue was 100 USD.', 'role': 'support'}], 'limitations': [], 'stopping_reason': 'answered',
            'next_actions': [{'tool': 'calculate_financials', 'arguments': {'calculations': [{'formula': 'growth_percent',
                'first_fact_id': fact_ids[0], 'second_fact_id': fact_ids[1]}], 'rationale': 'calculate requested change'},
                'requirement': 'Calculate the year-over-year change.', 'expected_evidence': 'The percentage change.'}],
        }, 'early')])
    def calculate(turn, _):
        result = last_result(turn)
        assert result['reason'] == 'core_evidence_path_available'
        assert result['next_action']['tool'] == 'calculate_financials'
        return _tool_response([('calculate_financials', result['next_action']['arguments'], 'calc')])
    def final(turn, _):
        row = last_result(turn)['calculations'][0]
        assert row['value'] == '25.00'
        return _tool_response([('submit_research', {
            'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': 's1',
                'quotation': 'Revenue was 100 USD.', 'role': 'support'}], 'limitations': [], 'stopping_reason': 'answered',
            'instruction_results': [{'sequence': 1, 'status': 'answered', 'calculation_ids': [row['calculation_id']]}],
        }, 'final')])
    model = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'read')]),
        _tool_response([('record_facts', {'facts': [
            {'label': 'revenue', 'value': 100, 'unit': 'USD', 'currency': 'USD', 'period': '2025', 'source_id': 's1', 'quotation': 'Revenue was 100 USD.'},
            {'label': 'revenue', 'value': 80, 'unit': 'USD', 'currency': 'USD', 'period': '2024', 'source_id': 's1', 'quotation': 'Revenue was 80 USD.'}], 'rationale': 'record'}, 'facts')]),
        early_submit, calculate, final,
    ])
    saved = []
    def sync(state, boundary):
        if boundary == 'model' and any(event.get('name') == 'record_facts' for event in state['events']):
            state['user_instructions'] = [{'sequence': 1, 'text': 'Also calculate year-over-year change.'}]
        return bool(state.get('user_instructions')) and state.get('model_instruction_sequence', 0) < 1
    result = run_research_agent(question='What is revenue?', sources=[src], model=model, context=_ctx(),
                                sync_instructions=sync,
                                checkpoint=lambda state: saved.append(json.loads(json.dumps(state))))
    assert result.reason == 'submitted'
    assert saved[-1]['submission']['instruction_reviews'][0]['references'][0]['calculation_id']
    assert saved[-1]['acknowledged_instruction_sequence'] == 1


def test_late_format_correction_and_report_excerpt_are_auditable():
    source = _source(text='Revenue was 100 USD.')
    report = '## Revenue\n\nRevenue was 100 USD.'
    instructions = [{'sequence': 1, 'text': 'Use a bulleted list.'},
                    {'sequence': 2, 'text': 'Replace the list with a heading.'}]
    def final(turn, _):
        assert [row['sequence'] for row in turn.user_instructions] == [1, 2]
        skipped = last_result(turn)
        assert skipped['reason'] == 'user_instruction_updated'
        return _tool_response([('submit_research', {
            'report': report, 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': 's1',
                                          'quotation': 'Revenue was 100 USD.', 'role': 'support'}],
            'limitations': [], 'stopping_reason': 'answered',
            'instruction_results': [
                {'sequence': 1, 'status': 'superseded', 'superseded_by_sequence': 2,
                 'superseding_excerpt': 'Replace the list with a heading.'},
                {'sequence': 2, 'status': 'answered', 'report_excerpt': '## Revenue'},
            ],
        }, 'final')])
    model = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'read'),
                        ('search_source', {'source_id': 's1', 'query': 'Revenue'}, 'old')]),
        final,
    ])
    saved = []
    def sync(state, boundary):
        if boundary == 'tool' and any(event.get('tool_call_id') == 'read' for event in state['events']):
            state['user_instructions'] = instructions
        return bool(state.get('user_instructions')) and state.get('model_instruction_sequence', 0) < 2
    result = run_research_agent(question='Revenue?', sources=[source], model=model, context=_ctx(), sync_instructions=sync,
                                checkpoint=lambda state: saved.append(json.loads(json.dumps(state))))
    assert result.reason == 'submitted'
    submission = saved[-1]['submission']
    first, second = submission['instruction_reviews']
    assert first['status'] == 'supersession_needs_review'
    ref = first['references'][0]
    assert instructions[1]['text'][ref['start']:ref['end']] == ref['excerpt']
    assert second['status'] == 'linked_for_review'
    ref = second['references'][0]
    assert submission[ref['field']][ref['start']:ref['end']] == '## Revenue'
    assert any('补充指令 1' in row['message'] for row in result.gaps)


def test_unknown_and_duplicate_instruction_sequences_are_rejected():
    instruction = {'sequence': 1, 'text': 'Explain the limit.'}
    base = {'report': 'Evidence was unavailable.', 'claims': [], 'limitations': ['No source.'],
            'stopping_reason': 'unavailable', 'status': 'partial'}
    def duplicate(turn, _):
        assert last_result(turn)['status'] == 'invalid'
        return _tool_response([('submit_research', {**base, 'instruction_results': [
            {'sequence': 1, 'status': 'unresolved'}, {'sequence': 1, 'status': 'unresolved'}]}, 'duplicate')])
    def corrected(turn, _):
        assert last_result(turn)['status'] == 'invalid'
        return _tool_response([('submit_research', {**base, 'instruction_results': [
            {'sequence': 1, 'status': 'unresolved', 'explanation': 'No source was available.'}]}, 'corrected')])
    model = NativeTurnRecorder([
        _tool_response([('submit_research', {**base, 'instruction_results': [{'sequence': 9, 'status': 'unresolved'}]}, 'unknown')]),
        duplicate, corrected,
    ])
    def sync(state, boundary):
        state['user_instructions'] = [instruction]
        return False
    result = run_research_agent(question='Research the claim.', sources=[], model=model, context=_ctx(), sync_instructions=sync)
    assert result.reason == 'submitted'


def test_unlocated_format_and_model_only_supersession_stay_unresolved():
    model = NativeTurnRecorder([_tool_response([('submit_research', {
        'report': 'Evidence was unavailable.', 'claims': [], 'limitations': ['No source.'],
        'stopping_reason': 'unavailable', 'status': 'partial', 'instruction_results': [
            {'sequence': 1, 'status': 'superseded', 'explanation': 'The later message replaced it.'},
            {'sequence': 2, 'status': 'answered', 'report_excerpt': 'Text not in the report'},
        ],
    }, 'submit')])])
    saved = []
    def sync(state, boundary):
        state['user_instructions'] = [{'sequence': 1, 'text': 'Use a table.'},
                                      {'sequence': 2, 'text': 'Add a heading.'}]
        return False
    result = run_research_agent(question='Research the claim.', sources=[], model=model, context=_ctx(),
                                sync_instructions=sync,
                                checkpoint=lambda state: saved.append(json.loads(json.dumps(state))))
    reviews = saved[-1]['submission']['instruction_reviews']
    assert [row['status'] for row in reviews] == ['unresolved', 'unresolved']
    assert all(not row['references'] for row in reviews)
    assert sum('补充指令' in row['message'] for row in result.gaps) == 2
