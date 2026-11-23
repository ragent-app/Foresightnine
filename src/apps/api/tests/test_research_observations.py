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
