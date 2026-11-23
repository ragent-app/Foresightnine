from app.research_delivery import assemble_parts, incomplete


def test_canonical_requirements_do_not_overwrite_missing_with_checkpoint_answer():
    state = {'facts': [{'fact_id': 'f', 'source_id': 's', 'label': 'Revenue', 'value': '100', 'unit': 'USD'}],
             'question_parts': [{'question': 'Revenue and EPS', 'answer': 'Revenue is 100 USD', 'status': 'answered',
                                 'reason': '', 'references': [{'fact_id': 'f', 'origin': 'current'}]}]}
    rows = [
        {'id': 'revenue', 'question': 'Revenue and EPS', 'core': True, 'status': 'satisfied', 'reason': '', 'references': [{'fact_id': 'f'}]},
        {'id': 'eps', 'question': 'Revenue and EPS', 'core': True, 'status': 'missing', 'reason': 'EPS missing', 'references': []},
        {'id': 'background', 'question': 'background', 'core': False, 'status': 'missing', 'reason': '', 'references': []},
    ]
    parts = assemble_parts('Revenue and EPS', state, {}, {'s'}, rows, [])
    assert [p['id'] for p in parts] == ['revenue', 'eps']
    assert parts[0]['status'] == 'answered' and parts[0]['references'][0]['fact_id'] == 'f'
    assert parts[1]['status'] == 'unresolved' and not parts[1]['answer']
    assert incomplete(rows)
    assert not incomplete([rows[0], rows[2]])


def test_qualitative_link_produces_answer_with_source_range():
    from types import SimpleNamespace
    ref = {'source_id': 's', 'character_start': 0, 'character_end': 10, 'origin': 'current'}
    state = {'question_parts': [{'question': 'What terms?', 'answer': 'Ten years.', 'status': 'answered', 'reason': '', 'references': [ref]}]}
    rows = [{'id': 'terms', 'question': 'What terms?', 'status': 'satisfied', 'reason': 'Review semantics.', 'references': [ref], 'core': True}]
    part = assemble_parts('What terms?', state, {'s': SimpleNamespace(text='Ten years.')}, {'s'}, rows, [])[0]
    assert part['answer'] == 'Ten years.' and part['status'] == 'answered'
    assert part['references'][0]['character_end'] == 10
