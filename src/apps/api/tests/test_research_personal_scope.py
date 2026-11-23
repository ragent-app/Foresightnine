"""Workflow checks for private user conditions, actual capabilities and versioned material."""
import json

import pytest

from app.research_answer_evidence import visible_answer_content
from app.research_public_query import public_query_problem
from app.research_requirement_plan import RequirementPlanInput, save_requirement_plan
from app.research_runtime import run_research_agent
from app.research_topics import ResearchContent
from tests.test_research_runtime_attention import NativeTurnRecorder, _ctx, _tool_response


def test_private_research_queries_stay_local_and_public_reformulation_reaches_provider():
    question = 'I need my $19,750 for tuition. My email is private@example.net. Compare redemption terms.'
    messages = []
    provider_queries = []

    def observe(turn, index):
        # Personal financial constraints are needed by the chosen model. Incidental identity is not.
        messages.append(turn.user)
        assert 'private@example.net' not in turn.user
        assert '$19,750' in turn.user
        if index == 0:
            calls = [('news_search', {'query': question, 'rationale': 'find terms'}, 'private'),
                     ('news_search', {'query': 'fund redemption for USD 19750', 'rationale': 'look up terms'}, 'amount'),
                     ('news_search', {'query': 'bond fund redemption settlement rules', 'rationale': 'public rules'}, 'public')]
            return _tool_response(calls)
        results = [json.loads(row.content) for row in turn.history[-1].results]
        assert [row.get('reason') for row in results[:2]] == ['private_query', 'private_query']
        return _tool_response([('submit_research', {'report': 'Redemption terms were unavailable.', 'status': 'partial',
            'claims': [], 'limitations': ['No eligible public documents.'], 'stopping_reason': 'No sources.'}, 'end')])

    recorder = NativeTurnRecorder([observe, observe])
    result = run_research_agent(question=question, sources=[], model=recorder, context=_ctx(),
                                discover=lambda query: provider_queries.append(query) or [])
    assert result.reason == 'submitted'
    assert provider_queries == ['bond fund redemption settlement rules']
    caps = json.loads(messages[0])['available_capabilities']
    tools = {row['tool'] for row in caps['evidence_routes']}
    assert 'news_search' in tools and 'market_data' not in tools


def test_requirements_keep_user_conditions_distinct_from_research_assumptions():
    question = 'Compare fund expenses. I need the proceeds next year.'
    req = {'source': {'sequence': 0, 'excerpt': 'Compare fund expenses'}, 'deliverable': 'Compare net proceeds',
           'evidence_needed': 'Fee schedule and redemption window', 'user_conditions': [
               {'sequence': 0, 'excerpt': 'I need the proceeds next year'}], 'assumptions': ['Assume an unchanged fee schedule']}
    request = RequirementPlanInput(requirements=[req])
    state = {}
    original = save_requirement_plan(question, state, request)[0]
    assert save_requirement_plan(question, state, request)[0] == original
    state['user_instructions'] = [{'sequence': 1, 'text': 'Actually, focus on the euro share class.'}]
    req['user_conditions'].append({'sequence': 1, 'excerpt': 'the euro share class'})
    current = save_requirement_plan(question, state, RequirementPlanInput(requirements=[req]))[0]
    assert current['requirement_id'] == original['requirement_id']
    assert current['revision'] == 2 and current['instruction_sequence'] == 1
    assert 'euro' not in str(state['requirement_plan_history'][0])
    req['user_conditions'][0]['excerpt'] = 'I can afford to lose money'
    with pytest.raises(ValueError, match='exactly'):
        save_requirement_plan(question, state, RequirementPlanInput(requirements=[req]))
    assert state['requirement_plan'][0] == current
    assert public_query_problem('查找私人姓名', ['我叫张明，想查费用']) is None
    assert public_query_problem('张明 债券持仓', ['我叫张明，想查费用']) == 'private_query'
    assert public_query_problem('investment plan for 80000', ['我的年收入为8万']) == 'private_query'
    assert public_query_problem('savings of $80k', ['My salary is 80,000']) == 'private_query'
    assert public_query_problem('Series I bonds redemption', ['I need investment income']) is None


def test_review_verdict_and_raw_quote_withdrawn_when_source_access_changes():
    review = {'requirement_id': 'goal1', 'claim_id': 'c', 'review_key': 'key',
              'verdict': 'supported', 'reason': 'Restricted passage',
              'evidence': [{'source_id': 's', 'content_sha256': 'a'*64, 'character_start': 1,
                            'character_end': 5, 'quotation': 'text'}]}
    turn = dict(turn_id='t', index=1, question='Terms?', question_origin='user', operation_ids=['op'],
                parent_version_id='v', started_at='2026-11-01T00:00:00Z', finished_at='2026-11-01T00:01:00Z',
                status='completed', reason='submitted', search_status='completed', official_fetch_status='completed',
                usage={'model_calls': 1, 'search_calls': 0, 'total_tokens': 0}, semantic_reviews=[review])
    content = ResearchContent(turns=[turn])
    visible = visible_answer_content(content, lambda sid: False)
    assert not visible.turns[0].semantic_reviews
    assert content.turns[0].semantic_reviews[0].evidence[0].quotation == 'text'


def test_multi_source_checkpoint_answer_can_survive_review_when_its_premises_alone_cannot():
    from app.model_protocol import ModelResponse, ModelUsage
    from tests.test_research_runtime_attention import _source

    question = 'Which account can fund the registration deadline?'
    early, late = 'Early account pays the same day.', 'Late account pays the next month.'
    answer = 'Early account is the suitable choice for a same-day deadline. Late account pays the next month.'
    citations = [{'source_id': 'early', 'quotation': early}, {'source_id': 'late', 'quotation': late}]

    class Model(NativeTurnRecorder):
        def complete_turn(self, request, **kwargs):
            if request.tools:
                return super().complete_turn(request, **kwargs)
            rows = []
            for item in json.loads(request.user)['candidates']:
                good = item['claim'] == answer
                if good:
                    assert {e['text'] for e in item['evidence_context']} >= {early, late}
                rows.append({**{k: item[k] for k in ('requirement_id', 'claim_id', 'review_key')},
                             'verdict': 'supported' if good else 'weaker',
                             'reason': 'Compared both payment times.' if good else 'Only one payment time.',
                             'condition': None, 'evidence_ids': [x['evidence_id'] for x in item['evidence_context']]})
            return ModelResponse({'content': json.dumps({'reviews': rows})}, (), ModelUsage(4, 4, 8), 'stop')

    provider = Model([
        _tool_response([('read_sources', {'reads': [{'source_id': c['source_id']} for c in citations], 'rationale': 'Read terms.'}, 'read')]),
        _tool_response([('checkpoint_question_part', {'question': question, 'answer': answer, 'citations': citations}, 'part')]),
        _tool_response([('submit_research', {'report': answer, 'claims': [{'text': early, 'role': 'support', **citations[0]}],
                                             'limitations': [], 'stopping_reason': 'answered'}, 'submit')])])
    result = run_research_agent(question=question, sources=[_source('early', early), _source('late', late)],
                                model=provider, context=_ctx())
    assert result.status == 'completed' and result.question_parts[0]['answer'] == answer
    assert len(result.claims) == 1 and result.claims[0]['text'] == answer
    assert {c['source_id'] for c in result.claims[0]['citations']} == {'early', 'late'}
