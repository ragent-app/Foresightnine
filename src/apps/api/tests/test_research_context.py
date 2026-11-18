from app.research_context import evidence_units, rank_units


def test_sentence_units_keep_source_offsets_and_are_not_window_fragments():
    text = 'First fact is complete. Second fact has a distinct period!'
    units = evidence_units(text)
    assert [part for unit in units if unit.kind == 'sentence' for _, part in unit.parts] == [
        'First fact is complete.', 'Second fact has a distinct period!']
    assert all(text[start:start + len(part)] == part for unit in units for start, part in unit.parts)
    assert rank_units([unit for unit in units if unit.kind == 'sentence'], [(30, text[30:38])])[0].parts[0][1].startswith('Second')


def test_table_row_requires_header_and_preserves_separate_source_offsets():
    text = 'Notes.\n| Item | 2025 USD |\n| --- | --- |\n| Revenue | 40 |\n| Cost | 12 |\n'
    rows = [unit for unit in evidence_units(text) if unit.kind == 'table']
    assert len(rows) == 2
    assert rows[0].parts[0][1].startswith('| Item | 2025 USD |')
    assert rows[0].parts[1][1] == '| Revenue | 40 |'
    assert all(text[start:start + len(part)] == part for unit in rows for start, part in unit.parts)
    assert not evidence_units('| Item | Value |\n| Revenue | 40 |\n')


def test_unspaced_chinese_sentences_stay_complete():
    text = '第一期收入增长。第二期收入下降！'
    units = evidence_units(text)
    assert [unit.parts[0][1] for unit in units if unit.kind == 'sentence'] == ['第一期收入增长。', '第二期收入下降！']
    assert all(text[start:start + len(part)] == part for unit in units for start, part in unit.parts)


def test_period_header_without_separator_is_kept_with_each_row():
    text = '| Metric | 2024 USD |\n| Revenue | 10 |\n'
    units = evidence_units(text)
    assert len(units) == 1 and units[0].kind == 'table'
    assert [part for _, part in units[0].parts] == ['| Metric | 2024 USD |', '| Revenue | 10 |']


def test_model_receives_table_group_with_original_separate_ranges_and_budget():
    import json
    from datetime import datetime, timezone
    from app.research_analysis import analyze
    from app.research_sources import ResearchSource, SourcePermissions
    text = '| Metric | 2024 USD |\n| --- | --- |\n| Revenue | 40 |\n' + ('Other content. ' * 700)
    source = ResearchSource(source_id='s', owner_id='o', title='Report', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256='x',
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    prompts = []
    result = analyze('Revenue 2024?', [source], None,
                     lambda prompt: (prompts.append(prompt) or {}, 0), max_prompt_chars=4000, output_contract=None)
    assert len(prompts[0].encode('utf-8')) <= 4000
    payload = json.loads(prompts[0])
    assert payload['source_input_version'] == '2'
    items = [passage for source_item in payload['sources'] for passage in source_item['passages']]
    header = next(item for item in items if '| Metric |' in item['text'])
    row = next(item for item in items if '| Revenue |' in item['text'])
    assert set(header['context_unit_ids']) & set(row['context_unit_ids'])
    segments = result.segments
    assert all(text[item['character_start']:item['character_end']] for item in segments)
    assert len({item['text'] for item in items}) == len(items)


def test_user_focus_is_not_excluded_by_lexical_mismatch():
    from datetime import datetime, timezone
    from app.research_analysis import analyze
    from app.research_sources import ResearchSource, SourcePermissions
    selected = 'A complete passage has a selected detail.'
    text = selected + (' Unrelated material.' * 700)
    source = ResearchSource(source_id='s', owner_id='o', title='Report', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256='x',
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    result = analyze('Unmatched query words', [source], None, lambda _: ({}, 0),
                     max_prompt_chars=4000, focus=('s', 0, len(selected)), output_contract=None)
    assert any(item['character_start'] == 0 and item['character_end'] >= len(selected) for item in result.segments)


def test_short_supplied_sources_are_sent_whole_even_without_lexical_match():
    import json
    from datetime import datetime, timezone
    from app.research_analysis import analyze
    from app.research_sources import ResearchSource, SourcePermissions
    def make(sid, text):
        return ResearchSource(source_id=sid, owner_id='o', title='Report', text=text,
            kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256='x',
            permissions=SourcePermissions(store=True, model_input=True, display=True))
    sources = [make('first', '营业收入上升。'), make('second', '经营成本下降。')]
    for selected in (sources[:1], sources):
        sent = []
        result = analyze('What changed?', selected, None,
            lambda prompt: (sent.append(prompt) or {}, 0), max_prompt_chars=4000, output_contract=None)
        assert len(sent[0].encode()) <= 4000
        items = json.loads(sent[0])['sources']
        assert {item['source_id']: item['passages'][0]['text'] for item in items} == {s.source_id: s.text for s in selected}
        assert set(result.source_ids) == {s.source_id for s in selected}


def test_optional_planning_hints_guide_existing_retrieval_without_changing_question():
    import json
    from datetime import datetime, timezone
    from app.research_analysis import analyze
    from app.research_planning import parse_plan_hints, plan_prompt
    from app.research_source_policy import source_language_hints
    from app.research_sources import ResearchSource, SourcePermissions
    old = {'subquestions': [{'question': 'What changed?'}]}
    assert parse_plan_hints(old) == ()
    assert parse_plan_hints({**old, 'retrieval_hints': ['Revenue']}) == ('Revenue',)
    text = ('Other material. ' * 600) + 'Revenue increased in the period.'
    source = ResearchSource(source_id='s', owner_id='o', title='Report', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256='x',
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    languages = source_language_hints([source], None)
    assert languages == ('latin',) and 'Revenue' not in plan_prompt('收入变化', '', languages)
    prompts = []
    result = analyze('收入变化', [source], None, lambda p: (prompts.append(json.loads(p)) or {}, 0),
                     max_prompt_chars=4000, retrieval_hints=('Revenue',), output_contract=None)
    assert prompts[0]['question'] == '收入变化'
    assert any('Revenue increased' in passage['text'] for item in prompts[0]['sources'] for passage in item['passages'])
    assert result.source_ids == ['s']


def test_multiline_table_heading_and_row_are_one_group_with_original_offsets():
    text = ('| Organization | Report | Report |\n'
            '| Period | 2024 | 2023 |\n'
            '| Unit | USD million | USD million |\n'
            '| Revenue | 240 | 210 |\n')
    rows = [unit for unit in evidence_units(text) if unit.kind == 'table']
    assert len(rows) == 1
    assert 'Organization' in rows[0].parts[0][1]
    assert '2024' in rows[0].parts[0][1] and 'USD million' in rows[0].parts[0][1]
    assert 'Revenue' in rows[0].parts[1][1]
    assert all(text[start:start + len(part)] == part for start, part in rows[0].parts)


def test_retrieval_prioritizes_uncovered_terms_within_same_source():
    from app.research_context import EvidenceUnit
    first = EvidenceUnit('sentence', ((0, 'Revenue increased in the period.'),))
    second = EvidenceUnit('sentence', ((50, 'Profit decreased in the period.'),))
    ranked = rank_units([first, second], [], 'revenue and profit',
                        ['revenue and profit'], covered_text=first.parts[0][1])
    assert ranked[0] == second


def test_source_processing_only_reports_recorded_selection_hints():
    from datetime import datetime, timezone
    from app.research_source_policy import summarize_source_processing
    from app.research_sources import ResearchSource, SourcePermissions
    source = ResearchSource(source_id='s', owner_id='o', title='Report', text='Evidence.',
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256='x',
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    segments = [{'source_id':'s', 'character_start':0, 'character_end':9,
                 'context_unit_ids':['u0'], 'retrieval_facet_indexes':[1]}]
    row = summarize_source_processing([source], None, segments, generation_status='completed')[0]
    assert row['selection_details']['selected_unit_count'] == 1
    assert row['selection_details']['matched_facet_indexes'] == [1]
    assert row['selection_details']['unmatched_requirement_indexes'] is None
    old = summarize_source_processing([source], None, [{'source_id':'s','character_start':0,'character_end':9}])[0]
    assert 'selection_details' not in old


def test_model_gap_and_absence_are_attributed_and_not_established():
    from datetime import datetime, timezone
    from app.research_analysis import analyze
    from app.research_sources import ResearchSource, SourcePermissions
    source = ResearchSource(source_id='s', owner_id='o', title='Report', text='One source sentence.',
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256='x',
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    def model(_):
        return ({'evidence_needs':[{'question_index':None, 'requested_fact':'another measure',
                  'reason':'not located', 'source_kind':'official report', 'action':'find source'}],
                 'absence_observations':[{'text':'Not seen', 'source_ids':['s','unowned'],
                                          'scope':'full_source'}]}, 1)
    result = analyze('What happened?', [source], None, model, output_contract=None)
    assert result.evidence_needs[0]['origin'] == 'model_suggested'
    assert result.absence_observations[0]['source_ids'] == ['s']
    assert result.absence_observations[0]['assessment'] == 'not_established'


def test_v2_keeps_source_groups_and_offsets_isolated_with_actual_budget():
    import json
    from datetime import datetime, timezone
    from app.research_analysis import analyze
    from app.research_sources import ResearchSource, SourcePermissions
    def make(sid, number):
        text = f'| Metric | 2024 USD |\n| --- | --- |\n| Revenue | {number} |\n' + ('Extra matter. ' * 700)
        return ResearchSource(source_id=sid, owner_id='o', title=sid, text=text,
            kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256='x',
            permissions=SourcePermissions(store=True, model_input=True, display=True))
    sources = [make('one', 40), make('two', 80)]
    prompts = []
    result = analyze('Compare 2024 revenue.', sources, None,
                     lambda p: (prompts.append(p) or {}, 1), max_prompt_chars=5000, output_contract=None)
    assert len(prompts[0].encode('utf-8')) <= 5000
    data = json.loads(prompts[0])
    assert data['source_input_version'] == '2'
    by_id = {item['source_id']: item for item in data['sources']}
    assert set(by_id) == {'one', 'two'}
    for sid, number in (('one', 40), ('two', 80)):
        item = by_id[sid]
        assert item['title'] == sid
        header = next(p for p in item['passages'] if '2024 USD' in p['text'])
        row = next(p for p in item['passages'] if f'Revenue | {number}' in p['text'])
        assert set(header['context_unit_ids']) & set(row['context_unit_ids'])
        assert all('source_id' not in passage and 'title' not in passage for passage in item['passages'])
    for segment in result.segments:
        src = next(source for source in sources if source.source_id == segment['source_id'])
        assert src.text[segment['character_start']:segment['character_end']]


def test_bounded_selection_preserves_distinct_requirements_without_exceeding_budget():
    import json
    from datetime import datetime, timezone
    from app.research_analysis import analyze
    from app.research_sources import ResearchSource, SourcePermissions
    text = ('Temperature data are stable. ' * 250 +
            'Rainfall decreased during the later period. ' +
            'Temperature notes are repeated. ' * 250)
    source = ResearchSource(source_id='weather', owner_id='o', title='Weather', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256='x',
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    sent = []
    result = analyze('Compare temperature and rainfall.', [source], None,
                     lambda p: (sent.append(p) or {}, 1), max_prompt_chars=3000,
                     planning_steps=['Describe temperature.', 'Describe rainfall.'], output_contract=None)
    assert len(sent[0].encode()) <= 3000
    passages = json.loads(sent[0])['sources'][0]['passages']
    assert any('Temperature data' in part['text'] for part in passages)
    assert any('Rainfall decreased' in part['text'] for part in passages)
    assert all(text[segment['character_start']:segment['character_end']] for segment in result.segments)


def test_index_idf_and_hints_are_available_to_unit_ranking():
    from app.research_retrieval import build_passage_index
    from app.research_context import EvidenceUnit
    text = ('common observation. ' * 180) + 'rarefigure indicator. ' + ('common observation. ' * 100)
    facets = [f'common requirement {i}' for i in range(6)] + ['rarefigure indicator']
    index = build_passage_index(text, 'common rarefigure', facets=facets)
    assert index is not None
    weights = dict(index.term_weights)
    assert weights['rarefigure'] > weights['common']
    assert len(index.facet_terms) == 7 and 'rarefigure' in index.facet_terms[6]
    common = EvidenceUnit('sentence', ((0, 'common observation.'),))
    rare_at = text.index('rarefigure indicator.')
    rare = EvidenceUnit('sentence', ((rare_at, 'rarefigure indicator.'),))
    ranked = rank_units([common, rare], [], 'common rarefigure', facets,
                        term_weights=weights, indexed_facets=index.facet_terms)
    assert ranked[0] == rare


def test_pdf_unresolved_text_columns_are_not_marked_complete():
    from datetime import datetime, timezone
    from app.research_analysis import analyze
    from app.research_sources import ResearchSource, SourcePermissions
    clean = 'The indicator increased during the period.'
    columns = 'Left indicator description          Right unrelated explanation\n'
    text = clean + '\n\n' + columns + ('Other material. ' * 700)
    source = ResearchSource(source_id='s', owner_id='o', title='Report', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256='x',
        permissions=SourcePermissions(store=True, model_input=True, display=True), retrieval_method='user_pdf')
    result = analyze('What happened to the indicator?', [source], None, lambda _: ({}, 0), max_prompt_chars=4000, output_contract=None)
    assert any(clean in text[segment['character_start']:segment['character_end']] for segment in result.segments)
    assert all(columns.strip() not in text[segment['character_start']:segment['character_end']] for segment in result.segments)
    assert any('column or table structure is uncertain' in note for note in result.limitations)


def test_global_score_apportions_shared_words_and_preserves_source_signal():
    from app.research_context import allocation_score
    facets = [{'common', 'alpha'}, {'common', 'beta'}, {'common', 'gamma'}]
    weights = {'one': {'common': 1.0, 'alpha': 5.0},
               'two': {'common': 1.0, 'beta': 2.0}}
    shared = allocation_score({'one': {'common'}}, weights, facets, 2)
    rare = allocation_score({'one': {'alpha'}}, weights, facets, 2)
    both_sources = allocation_score({'one': {'common'}, 'two': {'common'}}, weights, facets, 2)
    assert abs(shared - 0.375) < 1e-9
    assert rare > shared
    assert abs(both_sources - 0.75) < 1e-9
