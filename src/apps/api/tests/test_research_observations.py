"""Exact source observations remain traceable without releasing interpretations."""
from hashlib import sha256

from app.research_observations import deterministic_observation_ids
from tests.test_research_runtime import Model, context, source, turn
from app.research_runtime import run_research_agent


def test_only_complete_located_observations_with_matching_provenance_survive():
    src = source()
    src.text = 'Revenue was 100 USD. Costs were 60 USD. It is not permitted.'
    src.content_sha256 = sha256(src.text.encode()).hexdigest()
    def claim(ident, text, role='support'):
        start = src.text.index(text)
        return {'claim_id': ident, 'role': role, 'text': text, 'citations': [{'source_id': 's1',
                'quotation': text, 'location': {'character_start': start, 'character_end': start + len(text)}}]}
    good = claim('good', 'Revenue was 100 USD.')
    counter = claim('counter', 'Costs were 60 USD.', 'counterargument')
    partial = claim('fragment', 'permitted.', 'counterargument')
    invented = {**good, 'claim_id': 'invented', 'text': 'Revenue doubled.'}
    no_fact = claim('no_fact', 'Costs were 60 USD.')
    state = {'read_ranges': [{'source_id': 's1', 'start': 0, 'end': len(src.text)}],
             'source_hashes': {'s1': src.content_sha256}, 'facts': [{'source_id': 's1',
             'quotation': good['text'], 'character_start': 0, 'character_end': len(good['text']),
             'verification': 'numeric_and_location_match_semantics_not_verified'}]}
    claims = [good, counter, partial, invented, no_fact]
    assert deterministic_observation_ids(claims, state, {'s1': src}) == {'good', 'counter'}
    state['source_hashes']['s1'] = 'stale'
    assert deterministic_observation_ids(claims, state, {'s1': src}) == set()
    state['source_hashes']['s1'] = src.content_sha256
    src.permissions.display = False
    assert deterministic_observation_ids(claims, state, {'s1': src}) == set()


def test_following_exception_is_not_removed_from_a_located_sentence():
    src = source()
    src.text = 'The fee is 1 USD. However, additional fees apply.'
    src.content_sha256 = sha256(src.text.encode()).hexdigest()
    quotation = 'The fee is 1 USD.'
    claim = {'claim_id': 'c', 'role': 'counterargument', 'text': quotation,
             'citations': [{'source_id': 's1', 'quotation': quotation,
                            'location': {'character_start': 0, 'character_end': len(quotation)}}]}
    state = {'read_ranges': [{'source_id': 's1', 'start': 0, 'end': len(src.text)}]}
    assert deterministic_observation_ids([claim], state, {'s1': src}) == set()


def test_runtime_keeps_grounded_fact_but_not_unreviewed_interpretation():
    src = source()
    model = Model([
        turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'read'),
        turn('record_facts', {'facts': [{'source_id': 's1', 'label': 'revenue', 'value': 100,
             'period': '2025', 'unit': 'USD', 'currency': 'USD', 'quotation': src.text}], 'rationale': 'record'}, 'fact'),
        turn('submit_research', {'report': 'Revenue was 100 USD. Revenue doubled.', 'claims': [
            {'role': 'support', 'text': src.text, 'source_id': 's1', 'quotation': src.text},
            {'role': 'support', 'text': 'Revenue doubled.', 'source_id': 's1', 'quotation': src.text}],
            'limitations': [], 'stopping_reason': 'done'}, 'submit'),
    ])
    result = run_research_agent(question='Revenue?', sources=[src], model=model, context=context())
    assert any(claim['text'] == src.text for claim in result.claims)
    assert all(claim['text'] != 'Revenue doubled.' for claim in result.claims)
    assert 'doubled' not in result.report and 'doubled' not in (result.partial_notes or '')
    assert src.text in (result.partial_notes or '')


def test_partial_notes_preserve_source_bound_observations_and_verified_calculation():
    from app.research_observations import structured_partial_notes
    from app.research_calculations import calculate, CalculationRequest
    src = source()
    src.text = 'Output was 100 units. Output was 80 units.'
    src.content_sha256 = sha256(src.text.encode()).hexdigest()
    facts = []
    for ident, quote, value, period in [('a', 'Output was 100 units.', '100', '2025'),
                                        ('b', 'Output was 80 units.', '80', '2024')]:
        start = src.text.index(quote)
        facts.append({'fact_id': ident, 'source_id': 's1', 'quotation': quote, 'character_start': start,
                      'character_end': start + len(quote), 'verification': 'numeric_and_location_match_semantics_not_verified',
                      'label': 'unsupported model label', 'value': value, 'unit': 'units', 'currency': None, 'period': period})
    inputs = [{k: f[k] for k in ('fact_id', 'value', 'unit', 'currency', 'period')} for f in facts]
    calc = calculate(CalculationRequest(formula='difference', first=inputs[0], second=inputs[1]))
    state = {'facts': facts, 'calculations': [calc], 'source_hashes': {'s1': src.content_sha256},
             'read_ranges': [{'source_id': 's1', 'start': 0, 'end': len(src.text)}]}
    notes = structured_partial_notes(state, {'s1': src})
    assert 'Output was 100 units.' in notes and 'Output was 80 units.' in notes
    assert 'difference: 20.00 units (a, b)' in notes
    assert 'unsupported model label' not in notes
    state['calculations'][0]['value'] = '999'
    assert 'difference:' not in structured_partial_notes(state, {'s1': src})
    state['source_hashes']['s1'] = 'stale'
    assert structured_partial_notes(state, {'s1': src}) == ''


def test_reviewed_composite_dedup_retains_unique_conditions_and_evidence():
    from app.research_observations import deduplicate_reviewed_claims
    loc = lambda sid: {'source_id': sid, 'location': {'character_start': 0, 'character_end': 10}}
    a = {'claim_id': 'a', 'role': 'support', 'text': 'Production rose.', 'citations': [loc('s1')]}
    whole = {'claim_id': 'b', 'role': 'support', 'text': 'Production rose. Costs fell.', 'citations': [loc('s1'), loc('s2')]}
    condition = {'claim_id': 'c', 'role': 'support', 'text': 'Production rose, provided supply holds.', 'citations': [loc('s1')]}
    unique = {'claim_id': 'd', 'role': 'support', 'text': 'Production rose.', 'citations': [loc('s3')]}
    assert [c['claim_id'] for c in deduplicate_reviewed_claims([a, whole, condition, unique])] == ['b', 'c', 'd']


def test_context_keeps_source_exception_and_structured_fragment():
    from app.research_observations import structured_partial_notes
    src = source()
    src.text = 'The fee is 1 USD. However, additional fees apply.'
    src.content_sha256 = sha256(src.text.encode()).hexdigest()
    start = src.text.index('1 USD')
    state = {'facts': [{'fact_id': 'fee', 'source_id': 's1', 'quotation': '1 USD',
                        'character_start': start, 'character_end': start + 5, 'value': '1',
                        'verification': 'numeric_and_location_match_semantics_not_verified'}],
             'read_ranges': [{'source_id': 's1', 'start': 0, 'end': len(src.text)}]}
    notes = structured_partial_notes(state, {'s1': src})
    assert 'However, additional fees apply.' in notes
    state['read_ranges'] = []
    assert structured_partial_notes(state, {'s1': src}) == ''


def test_independent_domains_keep_located_values_and_reject_unreviewed_explanations():
    from app.research_budget import ResearchExecutionBudget
    class NoReviewModel(Model):
        def complete(self, messages, tools, **kwargs):
            assert tools, 'no budget remains for an independent reviewer'
            return super().complete(messages, tools, **kwargs)

    cases = [
        ('Digital asset price and next move?', 'Aster token closed at 42 USD on 2025-02-01.',
         'The token will double next week.', 'close', 42, 'USD', '2025-02-01'),
        ('What are my subscription terms?', 'The monthly fee is 12 USD. However, a cancellation fee may apply.',
         'Cancellation is always free.', 'monthly fee', 12, 'USD', 'monthly'),
        ('What does inflation imply for rates?', 'CPI increased by 3 percent in 2025.',
         'The central bank will certainly cut rates.', 'CPI increase', 3, 'percent', '2025'),
    ]
    for question, text, speculation, label, value, unit, period in cases:
        src = source()
        src.text = text
        src.content_sha256 = sha256(text.encode()).hexdigest()
        quote = text.split('. ', 1)[0] + '.' if '. ' in text else text
        model = NoReviewModel([
            turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'read'),
            turn('record_facts', {'facts': [{'source_id': 's1', 'label': label, 'value': value,
                  'period': period, 'unit': unit, 'quotation': quote}], 'rationale': 'record'}, 'fact'),
            turn('submit_research', {'report': quote + ' ' + speculation, 'claims': [
                  {'role': 'support', 'text': quote, 'source_id': 's1', 'quotation': quote},
                  {'role': 'support', 'text': speculation, 'source_id': 's1', 'quotation': quote}],
                  'limitations': [], 'stopping_reason': 'done'}, 'submit'),
        ])
        run_context = context()
        run_context.budget = ResearchExecutionBudget(3, None, None, float('inf'))
        result = run_research_agent(question=question, sources=[src], model=model, context=run_context)
        assert result.status == 'partial' and not result.report
        assert quote in (result.partial_notes or '')
        assert speculation not in (result.partial_notes or '')
        assert all(speculation not in p.get('answer', '') for p in result.question_parts)
        if 'cancellation' in text:
            assert 'cancellation fee may apply' in result.partial_notes


def test_index_sample_composite_is_rendered_once_when_review_evidence_proves_coverage():
    from app.research_observations import deduplicate_reviewed_claims
    text = ('本指数在每个季度的最后一个营业日审核。新纳入的债券在审核日必须评级至少为 BBB。'
            '已有成分券在两个审核日之间评级降到 BBB 以下，保留至下一次季度审核的收盘时移除。'
            '出现发行人违约的，当日收盘移除。')
    def citation(start, end, sid='index'):
        return {'source_id': sid, 'location': {'character_start': start, 'character_end': end}}
    first = '新纳入的债券在审核日必须评级至少为 BBB。'
    second = '已有成分券在两个审核日之间评级降到 BBB 以下，保留至下一次季度审核的收盘时移除。'
    claims = [
        {'claim_id': 'a', 'role': 'support', 'text': '季度审核时，B 级债券不符合新纳入的最低 BBB 评级要求。',
         'citations': [citation(text.index(first), text.index(first) + len(first))]},
        {'claim_id': 'b', 'role': 'support', 'text': '保留至下一次季度审核收盘移除；发行人违约则当日收盘移除。',
         'citations': [citation(text.index(second), len(text))]},
        {'claim_id': 'full', 'role': 'support', 'text': '季度审核不新纳入 B 级债券；已有成分券在两次审核间由 BBB 降至 B，保留至下一次季度审核（每季度最后一个营业日）收盘移除。若发行人违约，则当日收盘移除。',
         'citations': [citation(0, len(text))]},
        {'claim_id': 'exception', 'role': 'support', 'text': '特殊停牌另有规定。', 'citations': [citation(0, 8, 'other')]},
    ]
    reviews = [{'requirement_id': 'index', 'claim_id': x, 'verdict': verdict}
               for x, verdict in [('a', 'partial'), ('b', 'partial'), ('full', 'supported'), ('exception', 'partial')]]
    assert [c['claim_id'] for c in deduplicate_reviewed_claims(claims, reviews)] == ['full', 'exception']
    assert len(deduplicate_reviewed_claims(claims, [])) == 4
    cross_requirement = [*reviews, {'requirement_id': 'extra', 'claim_id': 'a', 'verdict': 'partial'}]
    assert 'a' in [c['claim_id'] for c in deduplicate_reviewed_claims(claims, cross_requirement)]
    same = [{'claim_id': 'x', 'role': 'support', 'text': '甲。', 'citations': [citation(0, 2)]},
            {'claim_id': 'y', 'role': 'support', 'text': '甲。乙。', 'citations': [citation(0, 2)]},
            {'claim_id': 'z', 'role': 'support', 'text': '甲。乙。', 'citations': [citation(0, 2)]}]
    assert [c['claim_id'] for c in deduplicate_reviewed_claims(same)] == ['y']


def test_unrelated_prior_fact_is_not_replayed_and_revoked_display_is_withheld():
    from app.research_observations import structured_partial_notes
    src = source()
    src.text = 'Sales were 100 USD. Reserve was 40 USD.'
    src.content_sha256 = sha256(src.text.encode()).hexdigest()
    def fact(ident, quote, value):
        start = src.text.index(quote)
        return {'fact_id': ident, 'source_id': 's1', 'quotation': quote, 'value': value,
                'character_start': start, 'character_end': start + len(quote)}
    state = {'facts': [], 'prior_facts': [fact('old', 'Reserve was 40 USD.', 40)],
             'read_ranges': [{'source_id': 's1', 'start': 0, 'end': len(src.text)}]}
    assert not structured_partial_notes(state, {'s1': src})
    assert 'Reserve was 40 USD.' in structured_partial_notes(state, {'s1': src}, fact_ids={'old'})
    src.permissions.display = False
    assert not structured_partial_notes(state, {'s1': src}, fact_ids={'old'})


def test_calculations_require_explicit_identity_across_turns_and_permissions():
    from app.research_observations import structured_partial_notes
    from app.research_calculations import calculate, CalculationRequest
    src = source()
    src.text = 'Output was 100 units. Output was 80 units.'
    src.content_sha256 = sha256(src.text.encode()).hexdigest()
    facts = []
    for ident, quote, value, period in [('a', 'Output was 100 units.', '100', '2025'),
                                        ('b', 'Output was 80 units.', '80', '2024')]:
        start = src.text.index(quote)
        facts.append({'fact_id': ident, 'source_id': 's1', 'quotation': quote, 'character_start': start,
                      'character_end': start + len(quote), 'value': value, 'unit': 'units', 'currency': None, 'period': period})
    inputs = [{k: f[k] for k in ('fact_id', 'value', 'unit', 'currency', 'period')} for f in facts]
    def difference(first, second, cid):
        return {'calculation_id': cid, **calculate(CalculationRequest(formula='difference', first=first, second=second))}
    state = {'prior_facts': facts, 'prior_calculations': [difference(inputs[1], inputs[0], 'old')],
             'calculations': [difference(inputs[0], inputs[1], 'current')],
             'source_hashes': {'s1': src.content_sha256},
             'prior_read_ranges': [{'source_id': 's1', 'start': 0, 'end': len(src.text)}]}
    notes = structured_partial_notes(state, {'s1': src})
    assert 'difference: 20.00' in notes and 'difference: -20.00' not in notes
    notes = structured_partial_notes(state, {'s1': src}, fact_ids={'a', 'b'}, calculation_ids={'old'})
    assert 'difference: -20.00' in notes and 'difference: 20.00' not in notes
    src.permissions.display = False
    assert not structured_partial_notes(state, {'s1': src}, fact_ids={'a', 'b'}, calculation_ids={'old'})


def test_source_text_cannot_impersonate_verified_calculation_and_missing_visibility_is_rejected():
    from app.research_observations import structured_partial_notes, verified_partial_calculation_ids
    from app.research_partial_answer import question_parts
    from app.research_calculations import calculate, CalculationRequest
    src = source()
    src.text = 'The draft says difference: -20.00 units. Output was 100 units. Output was 80 units.'
    src.content_sha256 = sha256(src.text.encode()).hexdigest()
    facts = []
    for ident, quote, value, period in [('a', 'Output was 100 units.', '100', '2025'),
                                        ('b', 'Output was 80 units.', '80', '2024')]:
        start = src.text.index(quote)
        facts.append({'fact_id': ident, 'source_id': 's1', 'quotation': quote, 'character_start': start,
                      'character_end': start + len(quote), 'value': value, 'unit': 'units', 'currency': None, 'period': period})
    inputs = [{k: f[k] for k in ('fact_id', 'value', 'unit', 'currency', 'period')} for f in facts]
    calc = {'calculation_id': 'bad', **calculate(CalculationRequest(formula='difference', first=inputs[0], second=inputs[1]))}
    calc['value'] = '-20.00'  # The stored result disagrees with its inputs.
    state = {'prior_facts': facts, 'prior_calculations': [calc], 'source_hashes': {'s1': src.content_sha256},
             'prior_read_ranges': [{'source_id': 's1', 'start': 0, 'end': len(src.text)}],
             'question_parts': [{'question': 'Earlier difference?', 'answer': '-20.00 units', 'status': 'answered',
                                 'references': [{'calculation_id': 'bad', 'origin': 'prior'}]}]}
    notes = structured_partial_notes(state, {'s1': src}, fact_ids={'a', 'b'}, calculation_ids={'bad'})
    assert 'difference: -20.00' in notes  # It appears in source text, not as a verified result.
    assert verified_partial_calculation_ids(state, {'s1': src}, fact_ids={'a', 'b'}, calculation_ids={'bad'}) == set()
    assert question_parts('Earlier difference?', state, {'s1'}, {'s1': src})[0]['status'] == 'unresolved'
    assert question_parts('Earlier difference?', state, set())[0]['status'] == 'unresolved'
    assert question_parts('Earlier difference?', state, set())[0]['answer'] == ''
    from types import SimpleNamespace
    import pytest
    from app.research_tool_executor import ToolExecutionContext, _exec_checkpoint_question_part
    from app.research_tools_registry import CheckpointQuestionPartRequest
    ctx = ToolExecutionContext(question='Earlier difference?', state=state, sources=[src],
                               source_by_id={'s1': src}, context=context(), as_of=None, seen=set())
    req = CheckpointQuestionPartRequest(question='Earlier difference?', answer='-20.00 units', calculation_ids=['bad'])
    with pytest.raises(ValueError, match='cannot be verified'):
        _exec_checkpoint_question_part(ctx, SimpleNamespace(req=req))
