from app.research_planning import parse_plan, parse_plan_requirements
from app.research_requirements import validate_requirement_links, unresolved_requirement_indexes


def test_plan_dimensions_are_optional_unverified_and_keep_question_identity():
    raw = {'subquestions': [{'question': 'Compare margins', 'dimensions': {'entities': ['A', 'B'], 'periods': ['2024']}}]}
    assert parse_plan(raw) == ['Compare margins']
    result = parse_plan_requirements(raw)
    assert result[0]['question'] == 'Compare margins'
    assert result[0]['dimensions']['entities'] == ['A', 'B']
    assert result[0]['dimensions']['metrics'] == []
    assert parse_plan_requirements({'subquestions': [{'question': 'Legacy'}]})[0]['dimensions']['units'] == []


def test_requirement_links_only_reference_selected_unique_facts_and_settled_calculations():
    facts = [
        {'fact_id': 'a', 'source_id': 's1', 'location': {'character_start': 3, 'character_end': 8}},
        {'fact_id': 'b', 'source_id': 's1', 'location': {'character_start': 10, 'character_end': 14}},
        {'fact_id': 'outside', 'source_id': 's2', 'location': {'character_start': 1, 'character_end': 4}},
        {'fact_id': 'ambiguous', 'source_id': 's1', 'location': {'status': 'ambiguous'}},
    ]
    calculations = [{'inputs': [{'fact_id': 'a'}, {'fact_id': 'b'}]},
                    {'inputs': [{'fact_id': 'a'}, {'fact_id': 'outside'}]}]
    proposals = [{'question_index': 0, 'fact_ids': ['a', 'outside', 'ambiguous'],
                  'calculation_indexes': [0, 1, True, 2], 'source_ids': ['ref1', 'ref2']},
                 {'question_index': None, 'fact_ids': [], 'calculation_indexes': [], 'source_ids': []},
                 {'question_index': 9, 'fact_ids': [], 'calculation_indexes': [], 'source_ids': []}]
    links = validate_requirement_links(proposals, [{'question': 'Compare'}], facts, calculations,
        [{'source_id': 's1', 'character_start': 0, 'character_end': 20}], {'ref1': 's1', 'ref2': 's2'}, {0: 0, 1: 1})
    assert len(links) == 2
    assert links[0]['question'] == 'Compare' and links[0]['fact_ids'] == ['a']
    assert links[0]['calculation_indexes'] == [0] and links[0]['source_ids'] == ['s1']
    assert links[0]['semantic_status'] == 'not_independently_verified'
    assert links[1]['question_index'] is None


def test_duplicate_fact_identifiers_cannot_bind_to_a_requirement():
    fact = {'fact_id': 'a', 'source_id': 's1', 'location': {'character_start': 1, 'character_end': 2}}
    links = validate_requirement_links([{'question_index': None, 'fact_ids': ['a']}], [], [fact, fact], [],
        [{'source_id': 's1', 'character_start': 0, 'character_end': 3}], {}, {})
    assert links[0]['fact_ids'] == []


def test_analysis_emits_validated_links_to_its_actual_excerpt():
    from datetime import datetime, timezone
    from hashlib import sha256
    import json
    from app.research_analysis import analyze
    from app.research_sources import ResearchSource, SourcePermissions
    text = 'Revenue was 10 million dollars in 2024.'
    source = ResearchSource(source_id='source', owner_id='owner', title='Report', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    payload = {'research_questions': [{'question': 'What was revenue?', 'claim_indexes': []}],
        'facts': [{'fact_id': 'f1', 'label': 'Revenue', 'value': '10', 'unit': 'million dollars',
                   'currency': 'USD', 'period': '2024', 'citation': {'source_id': 'source', 'quotation': text}}],
        'requirement_links': [{'question_index': 0, 'fact_ids': ['f1', 'not-sent'], 'source_ids': ['source', 'other']}]}
    prompts = []
    result = analyze('What was revenue?', [source], None,
        lambda prompt: (prompts.append(json.loads(prompt)) or payload, 1), max_prompt_chars=6000,
        planning_steps=['What was revenue?'], planning_requirements=({'question': 'What was revenue?',
        'dimensions': {'metrics': ['revenue']}},))
    assert prompts[0]['requirement_dimensions_untrusted'][0]['question'] == 'What was revenue?'
    assert result.requirement_links[0]['fact_ids'] == ['f1']
    assert result.requirement_links[0]['source_ids'] == ['source']
    assert result.requirement_links[0]['semantic_status'] == 'not_independently_verified'


def test_task_dimensions_direct_actual_bounded_source_selection():
    from datetime import datetime, timezone
    from hashlib import sha256
    from app.research_analysis import analyze
    from app.research_sources import ResearchSource, SourcePermissions
    import json
    target = 'Selenium capacity was 17 units in 2022.'
    text = ('Background text without the requested detail.\n\n' * 200) + target
    source = ResearchSource(source_id='source', owner_id='owner', title='Report', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    prompts = []
    requirement = {'question': 'Describe the comparison', 'dimensions': {'metrics': ['selenium capacity'], 'periods': ['2022']}}
    result = analyze('Describe the comparison', [source], None,
        lambda prompt: (prompts.append(json.loads(prompt)) or {}, 1), max_prompt_chars=6000,
        planning_steps=['Describe the comparison'], planning_requirements=(requirement,))
    assert prompts and any(target in part['text'] for item in prompts[0]['sources'] for part in item['passages'])
    assert any(text[s['character_start']:s['character_end']] == target for s in result.segments)


def test_linked_claim_does_not_hide_missing_fact_or_requested_calculation():
    from types import SimpleNamespace
    from app.research_gap_followup import build_gap_focus
    question = 'Compare two periods'
    analysis = SimpleNamespace(research_questions=[{'question': question, 'claim_indexes': [0]}],
        requirement_links=[{'question': question, 'source_ids': ['s'], 'fact_ids': [], 'calculation_indexes': []}])
    requirements = ({'question': question, 'dimensions': {'metrics': ['capacity']}, 'calculation_requested': True},)
    gaps = unresolved_requirement_indexes(analysis, requirements)
    assert gaps == {0: ('no_linked_fact', 'no_linked_calculation')}
    focus = build_gap_focus('Original user objective', [{'text': 'A claim', 'citations': [{}]}],
                            analysis.research_questions, requirement_gaps=gaps)
    assert focus and focus.question_indexes == (0,) and 'Original user objective' in focus.question


def test_full_original_request_is_preserved_and_empty_research_can_be_followed_up():
    import json
    from app.research_planning import plan_prompt
    from app.research_gap_followup import build_gap_focus
    question = 'A' * 4000 + ' final requirement'
    assert json.loads(plan_prompt(question, 'scope'))['question'] == question
    focus = build_gap_focus(question, [], [])
    assert focus is not None and focus.question.startswith(question)


def test_followup_remaps_evidence_and_calculation_ids_without_cross_binding():
    from app.research_gap_followup import remap_followup_artifacts
    facts = [{'fact_id': 'a'}, {'fact_id': 'b'}]
    calculations = [{'inputs': [{'fact_id': 'a'}, {'fact_id': 'b'}]},
                    {'inputs': [{'fact_id': 'a'}, {'fact_id': 'missing'}]}]
    id_map, calc_map = {}, {}
    retained, computed, omitted = remap_followup_artifacts(facts, calculations, operation_id='op',
        existing_fact_ids=set(), existing_facts=[], max_facts=2, max_calculations=2,
        fact_id_map=id_map, calculation_index_map=calc_map)
    assert len(retained) == 2 and len(computed) == 1 and omitted == 1
    assert calc_map == {0: 0}
    assert [item['fact_id'] for item in computed[0]['inputs']] == [id_map['a'], id_map['b']]


def test_coordinator_may_follow_up_unanswered_user_request_without_claims():
    from types import SimpleNamespace
    from app.research_orchestrator import CoordinationObservation, ResearchOrchestrator
    request = SimpleNamespace(auto_fetch_official=False, max_search_calls=0, counterpoint_followup=False,
                              gap_followup=True, support_review=False, gap_search=False)
    context = SimpleNamespace(now=lambda: 0, budget=SimpleNamespace(deadline=10, max_model_calls=3),
                              calls=1, cost_limited=False)
    workflow = SimpleNamespace(record_decision=lambda *args: None)
    coordinator = ResearchOrchestrator(context=context, workflow=workflow, request=request,
        question=True, observe=lambda: None, handlers={}, attempted={'planning', 'analysis'},
        omissions={'source_discovery': 'not_requested', 'source_retrieval': 'not_requested'})
    state = CoordinationObservation(source_count=1, candidate_count=0, has_analysis=True,
        claim_count=0, has_counterpoint=False, has_gaps=True, generation_failed=False)
    assert coordinator._choose(state)[0] == 'gap_followup'


def test_default_provider_budget_keeps_a_small_complete_source():
    from datetime import datetime, timezone
    from hashlib import sha256
    from app.research_analysis import analyze, SYSTEM_PROMPT
    from app.research_coordinator import research_prompt_budget, research_prompt_fits
    from app.research_sources import ResearchSource, SourcePermissions
    text = ('Revenue rose to 100 units in the current period. Costs were 60 units for the same period. '
            'The prior period recorded revenue of 80 units and costs of 50 units. Both periods use the same stated reporting currency. '
            'Management noted that the change depends on demand.')
    source = ResearchSource(source_id='source_' + 'a' * 32, owner_id='owner', title='Report', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    prompts = []
    result = analyze('Compare revenue and cost across the two periods.', [source], None,
                     lambda prompt: (prompts.append(prompt) or {}, 1),
                     max_prompt_chars=research_prompt_budget(3000, SYSTEM_PROMPT))
    assert len(prompts) == 1 and research_prompt_fits(prompts[0], SYSTEM_PROMPT, 3000)
    assert any(item['character_start'] == 0 and item['character_end'] == len(text) for item in result.segments)


def test_nonfinancial_repeated_terms_still_allow_distinct_complete_units():
    from datetime import datetime, timezone
    from hashlib import sha256
    from app.research_analysis import analyze
    from app.research_sources import ResearchSource, SourcePermissions
    text = ('Background unrelated.\n\n' * 500 +
            'Orchid growth was slow during spring.\n\nOrchid growth was rapid during summer.')
    source = ResearchSource(source_id='garden', owner_id='owner', title='Observation', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    result = analyze('Describe orchid growth.', [source], None, lambda prompt: ({}, 1), max_prompt_chars=6000)
    passages = [text[s['character_start']:s['character_end']] for s in result.segments]
    assert any('spring' in item for item in passages)
    assert any('summer' in item for item in passages)
    assert any(row['phase'] == 'additional_relevant_unit' and row.get('selected') for row in result.selection_diagnostics)


def test_no_planning_analysis_subquestions_can_drive_existing_gap_followup():
    from datetime import datetime, timezone
    from hashlib import sha256
    from app.research_analysis import analyze
    from app.research_requirements import combined_requirements
    from app.research_gap_followup import build_gap_focus
    from app.research_sources import ResearchSource, SourcePermissions
    text = 'The greenhouse opened in spring.'
    source = ResearchSource(source_id='garden', owner_id='owner', title='Observation', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    raw = {'research_questions': [
        {'question': 'What happened in spring?', 'claim_indexes': [0], 'dimensions': {'periods': ['spring']}},
        {'question': 'What happened in winter?', 'claim_indexes': [], 'dimensions': {'periods': ['winter']}}],
        'claims': [{'text': 'It opened in spring.', 'citations': [{'source_id': 'garden', 'quotation': text}]}],
        'requirement_links': [{'question_index': 0, 'source_ids': ['garden']}]}
    result = analyze('Compare the greenhouse across seasons.', [source], None, lambda prompt: (raw, 1), max_prompt_chars=6000)
    requirements = combined_requirements((), result)
    gaps = unresolved_requirement_indexes(result, requirements)
    target = build_gap_focus('Compare the greenhouse across seasons.', result.claims, result.research_questions,
                             requirement_gaps=gaps)
    assert len(requirements) == 2 and target is not None and 1 in target.question_indexes
    assert 'winter' in target.question


def test_requirement_dimensions_and_cross_language_hints_are_bounded_and_fair():
    from app.research_requirements import requirement_retrieval_facets, followup_retrieval_hints
    requirements = ({'question': '比较两个对象', 'dimensions': {'entities': ['Aster', 'Birch'],
        'periods': ['2022', '2023']}, 'retrieval_hints': ['policy decision', 'target range']},
        {'question': '核查估计', 'dimensions': {'metrics': ['output']}, 'retrieval_hints': ['advance estimate']})
    facets = requirement_retrieval_facets(requirements)
    assert 'Aster' in facets[0] and 'output' in facets[1]
    assert 'Birch' in facets and '2022' in facets and '2023' in facets
    hints = followup_retrieval_hints(('global one', 'global two'), requirements)
    assert hints[:3] == ('global one', 'policy decision', 'advance estimate')
    assert len(hints) <= 6


def test_original_and_reordered_question_evidence_needs_do_not_silently_disappear():
    from types import SimpleNamespace
    from app.research_requirements import unresolved_original_requirement, evidence_need_details
    from app.research_gap_followup import build_gap_focus
    analysis = SimpleNamespace(research_questions=[{'question': 'first'}, {'question': 'second', 'claim_indexes': [0]}],
        requirement_links=[], evidence_needs=[{'question_index': 0, 'question': 'second', 'requested_fact': 'second period', 'reason': 'not in excerpts'},
                                              {'question_index': None, 'requested_fact': 'missing', 'source_kind': 'official release'}])
    gaps = unresolved_requirement_indexes(analysis, ())
    assert gaps == {1: ('model_suggested_evidence_need',)}
    assert unresolved_original_requirement(analysis)
    target = build_gap_focus('Original full request', [{'text': 'Existing', 'citations': [{}]}],
        analysis.research_questions, requirement_gaps=gaps, original_gap=True, need_details=evidence_need_details(analysis))
    assert target and 1 in target.question_indexes and 'original request' in target.question
    assert 'second period' in target.question and 'not in excerpts' in target.question
    assert 'official release' in target.question and 'Untrusted' in target.question
    assert 'official release' not in target.search_query
    analysis.evidence_needs = [{'question_index': None, 'question_origin': 'unlinked_followup'}]
    assert not unresolved_original_requirement(analysis)


def test_assertion_references_only_map_to_unique_actual_citations():
    from datetime import datetime, timezone
    from hashlib import sha256
    from app.research_analysis import analyze
    from app.research_sources import ResearchSource, SourcePermissions
    text = 'Revenue was 10 units. Costs were 6 units.'
    source = ResearchSource(source_id='source', owner_id='owner', title='Report', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    result = analyze('Describe both.', [source], None, lambda prompt: ({'claims': [{
        'text': 'Revenue was 10 and costs were 6.',
        'citations': [{'source_id': 'source', 'quotation': 'Revenue was 10 units.'}],
        'assertions': [{'text': 'Revenue was 10.', 'citation_indexes': [0]},
                       {'text': 'Costs were 6.', 'citation_indexes': [2]}]}]}, 1), max_prompt_chars=6000)
    first, second = result.claims[0]['assertion_suggestions']
    assert first['citation_indexes'] == [0] and first['unresolved_reference'] is False
    assert second['citation_indexes'] == [] and second['unresolved_reference'] is True
    assert first['semantic_status'] == 'not_independently_verified'


def test_formal_input_limit_leaves_space_for_multiple_complete_passages():
    from datetime import datetime, timezone
    from hashlib import sha256
    from app.research_analysis import analyze, SYSTEM_PROMPT
    from app.research_coordinator import research_prompt_budget, research_prompt_fits
    from app.research_sources import ResearchSource, SourcePermissions
    text = ' '.join(f'Observation {index} describes a distinct authorized measurement and its reporting period.'
                    for index in range(16))
    source = ResearchSource(source_id='source_' + 'a' * 32, owner_id='owner', title='Report', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    prompts = []
    result = analyze('Describe the observations.', [source], None,
        lambda prompt: (prompts.append(prompt) or {}, 1),
        max_prompt_chars=research_prompt_budget(6000, SYSTEM_PROMPT))
    assert prompts and research_prompt_fits(prompts[0], SYSTEM_PROMPT, 6000)
    assert any(item['character_start'] == 0 and item['character_end'] == len(text) for item in result.segments)


def test_gap_incremental_selection_adds_unseen_evidence_and_preserves_needed_context():
    from datetime import datetime, timezone
    from hashlib import sha256
    from app.research_analysis import analyze
    from app.research_sources import ResearchSource, SourcePermissions
    from app.research_gap_followup import GapFocus, gap_context_refs
    from types import SimpleNamespace
    spring = 'Garden spring bloom count was 10.'
    summer = 'Garden summer bloom count was 20.'
    text = spring + '\n\n' + summer + '\n\nGarden autumn bloom count was 30.'
    source = ResearchSource(source_id='garden', owner_id='owner', title='Observations', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    prior = [{'source_id': 'garden', 'character_start': 0, 'character_end': len(spring)}]
    analysis = SimpleNamespace(research_questions=[], requirement_links=[], facts=[])
    claims = [{'citations': [{'source_id': 'garden', 'location': {'character_start': 0, 'character_end': len(spring)}}]}]
    refs = gap_context_refs(GapFocus((0,), (), 'q', 'q'), analysis, claims)
    assert refs == prior
    for context_refs in (refs, []):
        sent = []
        result = analyze('Compare garden bloom counts in spring and summer.', [source], None,
            lambda prompt: (sent.append(prompt) or {}, 1), max_prompt_chars=3500,
            prior_segments=prior, context_refs=context_refs, incremental_focus=('garden summer bloom',), output_contract=None)
        assert sent and len(sent[0].encode('utf-8')) <= 3500
        excerpts = [text[item['character_start']:item['character_end']] for item in result.segments]
        assert summer in excerpts
        assert (spring in excerpts) is bool(context_refs)
        assert any(item.get('phase') == 'incremental_context' for item in result.selection_diagnostics)
