from app.research_calculations import CalculationRequest, calculate
from app.research_provenance import derive_evidence_links


def test_calculation_sources_require_unique_unchanged_facts_and_links():
    first = dict(fact_id='a', value='12', unit='widgets', period='2024', currency=None)
    second = dict(fact_id='b', value='10', unit='widgets', period='2023', currency=None)
    calc = calculate(CalculationRequest.model_validate({'formula': 'difference', 'first': first, 'second': second}))
    facts = [dict(first, source_id='s', location={'character_start': 0, 'character_end': 8}),
             dict(second, source_id='t', location={'character_start': 10, 'character_end': 18})]
    claim = {'claim_id': 'c', 'suggested_calculations': [{'calculation_index': 0}, {'calculation_index': 42}]}
    links = derive_evidence_links([claim], facts, [calc])
    assert links['claims'][0]['candidate_calculation_indexes'] == [0]
    assert [row['source_id'] for row in links['calculations'][0]['inputs']] == ['s', 't']
    assert links['calculations'][0]['inputs'][0]['source_context_status'] == 'not_independently_verified'
    facts[1]['location'] = None
    assert derive_evidence_links([claim], facts, [calc])['calculations'][0]['inputs'][1]['source_id'] is None
    facts[1]['value'] = '11'
    links = derive_evidence_links([claim], facts, [calc])
    assert links['claims'][0]['candidate_calculation_indexes'] == []
    assert all(row['source_id'] is None for row in links['calculations'][0]['inputs'])


def test_suggested_assertion_links_never_infer_or_accept_invalid_indexes():
    from app.research_coverage import derive_completion_assessment
    claim = {'claim_id': 'claim', 'citations': [{'source_id': 's', 'location': {'character_start': 3, 'character_end': 9}}],
             'assertion_suggestions': [
                 {'text': 'first assertion', 'citation_indexes': [0], 'calculation_indexes': []},
                 {'text': 'another assertion', 'citation_indexes': [True, 7], 'calculation_indexes': []},
                 {'text': 'unlinked assertion', 'citation_indexes': [], 'calculation_indexes': []},
             ]}
    links = derive_evidence_links([claim], [], [])
    assertions = links['claims'][0]['assertions']
    assert [item['link_status'] for item in assertions] == ['linked_suggestion', 'invalid_or_unlocated', 'unlinked']
    assert assertions[1]['citation_indexes'] == []
    assert all(item['semantic_status'] == 'not_independently_verified' for item in assertions)
    outcome = derive_completion_assessment(run={'evidence_links': links}, claims=[claim])
    assert outcome['assertion_link_gaps'] == ['claim'] and outcome['execution_status'] == 'known_gap'
    claim.pop('assertion_suggestions')
    links = derive_evidence_links([claim], [], [])
    assert links['claims'][0]['assertion_coverage'] == 'unknown'
    assert derive_completion_assessment(run={'evidence_links': links}, claims=[claim])['assertion_link_gaps'] == []
