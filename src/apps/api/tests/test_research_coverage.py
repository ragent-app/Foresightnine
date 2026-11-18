"""Task relationships are traceable leads, never automatic semantic proof."""
from app.research_coverage import derive_requirement_coverage, initial_source_coverage
from app.research_quality import research_gaps


def test_requirement_coverage_keeps_original_request_unknown_and_links_bounded_to_evidence():
    run = {
        'planning_requested': True, 'planning_status': 'failed',
        'question_links': {'questions': [{'question_index': 0, 'finding_ids': ['c1', 'absent'],
                                           'source_ids': ['s1'], 'counterpoints': []}]},
        'evidence_links': {'claims': [{'claim_id': 'c1', 'citations': [
            {'same_passage_fact_ids': ['f1']}], 'candidate_calculation_indexes': [0]}]},
    }
    result = derive_requirement_coverage(run=run, has_user_question=True)
    original, suggestion = result['requirements']
    assert original['relation_status'] == 'not_assessed'
    assert original['gaps'] == ['request_decomposition_not_verified']
    assert suggestion['finding_ids'] == ['c1']
    assert suggestion['candidate_fact_ids'] == ['f1']
    assert suggestion['candidate_calculation_indexes'] == [0]
    assert suggestion['semantic_status'] == 'not_independently_verified'
    assert result['requested_actions'] == [{'action': 'planning', 'execution_status': 'failed', 'requirement_status': 'unmet'}]


def test_source_ranges_and_missing_fact_dimensions_remain_distinct():
    segments = [{'source_id': 's', 'character_start': 0, 'character_end': 5},
                {'source_id': 's', 'character_start': 3, 'character_end': 8}]
    assert initial_source_coverage('s', 10, segments)['selected_characters'] == 8
    gaps = research_gaps(claims=[], calculations=[], sources=[], calculation_differences=[],
                         amount_differences=[], run={}, facts=[{'unit': '', 'period': '2025'}])
    assert any(row['code'] == 'fact_unit_or_period_missing' and row['indexes'] == [0]
               for row in gaps['items'])


def test_scope_and_needs_distinguish_selected_ranges_from_absence_and_relevance():
    from app.research_coverage import derive_scope_assessment, derive_evidence_needs, derive_absence_assessment, has_unresolved_task_gap
    run = {'operation_id': 'op', 'source_processing': [
        {'source_id': 's1', 'provided': True, 'selection_status': 'partial', 'analysis_status': 'produced'},
        {'source_id': 's2', 'provided': True, 'selection_status': 'not_selected', 'analysis_status': 'not_started'}],
        'research_questions': [], 'model_evidence_needs': []}
    claims = [{'text': 'The document contains no figure.'}]
    scope = derive_scope_assessment(run=run, claims=claims, limitations=['No figure found.'])
    assert scope['scope'] == 'partial_or_unknown'
    assert scope['absence_conclusions'] == 'not_independently_verified'
    needs = derive_evidence_needs(run=run, scope=scope)
    assert needs['items'][0]['action'] == 'inspect_or_supply_relevant_material'
    absences = derive_absence_assessment(run=run, scope=scope)
    assert not has_unresolved_task_gap(needs=needs, absences=absences, claims=claims)
    # An explicit observation is not upgraded to a source-wide fact.
    run['model_absence_observations'] = [{'text': claims[0]['text'], 'source_ids': ['s1', 's2', 'foreign'], 'scope': 'full_source'}]
    absences = derive_absence_assessment(run=run, scope=scope)
    assert absences['observations'][0]['source_ids'] == ['s1']
    assert absences['observations'][0]['declared_scope'] == 'full_source'
    assert absences['observations'][0]['assessment'] == 'not_established'
    assert has_unresolved_task_gap(needs=needs, absences=absences, claims=claims)
    legacy = derive_scope_assessment(run={'operation_id': 'old'}, claims=[], limitations=[])
    assert legacy['scope'] == 'unknown'


def test_model_evidence_needs_remain_attributed_bounded_and_actionable():
    from app.research_coverage import derive_evidence_needs, has_unresolved_task_gap
    run = {'research_questions': [], 'model_evidence_needs': [{'question_index': None,
        'requested_fact': 'Monthly employment rate', 'reason': 'The requested value is missing',
        'source_kind': 'Labour statistics release', 'action': 'Obtain a relevant primary release'}]}
    needs = derive_evidence_needs(run=run, scope={'attempted': True, 'scope': 'complete_input_selected'})
    item = needs['items'][0]
    assert item['origin'] == 'model_suggested' and item['status'] == 'not_obtained_or_not_verified'
    assert item['requested_fact'] == 'Monthly employment rate' and item['action'] == 'Obtain a relevant primary release'
    assert has_unresolved_task_gap(needs=needs, absences={'observations': []}, claims=[])


def test_version_rebuild_marks_explicit_absence_unestablished_without_rewriting_text():
    from app.research_topics import ResearchTopics, ResearchInput, ResearchContent, VersionInput
    from app.storage import InMemoryDocumentRepository
    topics = ResearchTopics(InMemoryDocumentRepository())
    topic = topics.create('owner', ResearchInput(question='What is missing?'))
    content = ResearchContent(claims=[{'claim_id': 'c1', 'text': '資料沒有季度資料。', 'role': 'support', 'citations': [], 'model_absence_refs': ['absence_test']}],
        run={'operation_id': 'op', 'status': 'completed', 'source_processing': [
            {'source_id': 's', 'provided': True, 'selection_status': 'partial', 'initial_selection': 'partial',
             'eligibility': 'eligible', 'analysis_status': 'produced'}],
             'model_absence_observations': [{'text': '尚未找到季度數據。', 'source_ids': ['s'], 'scope': 'full_source', 'observation_ref': 'absence_test'}]})
    result = topics.add_version('owner', topic.topic_id, VersionInput(parent_version_id=topic.versions[0].version_id, content=content), created_by='agent')
    saved = result.versions[-1].content
    assert saved.claims[0]['text'] == '資料沒有季度資料。'
    assert saved.claims[0]['support_status'] == 'incomplete' and saved.claims[0]['absence_scope_status'] == 'not_established'
    assert saved.run['status'] == 'partial'
    assert saved.run['absence_assessment']['observations'][0]['assessment'] == 'not_established'
    assert saved.run['absence_assessment']['observations'][0]['claim_ids'] == [saved.claims[0]['claim_id']]
    assert any(item['code'] == 'absence_observation_unestablished' for item in saved.run['research_gaps']['items'])


def test_task_completion_does_not_assume_unselected_source_is_relevant():
    from time import monotonic
    from app.research_analysis import AnalysisResult
    from app.research_assessment import assess_research_outcome
    from app.research_runs import ResearchRunRequest
    claim = {'text': 'A documented result', 'role': 'support', 'citations': [{'source_id': 's', 'quotation': 'text'}]}
    analysis = AnalysisResult([claim], [], ['Source other: remaining text was not examined.'], ['s'], 1)
    request = ResearchRunRequest(parent_version_id='v', require_counterarguments=False, support_review=False)
    kwargs = dict(request=request, claims=[claim], source_states=[{'source_id': 's', 'eligibility': 'eligible',
        'initial_selection': 'partial', 'analysis_status': 'produced'}], discovery_notes=[], review_status='not_requested',
        counterpoint_status='not_requested', gap_status='not_requested', gap_search_status='not_requested',
        planning_status='not_requested', deadline=monotonic()+30, usage=1)
    assert assess_research_outcome(analysis=analysis, **kwargs).complete is True
    from dataclasses import replace
    need = {'question_index': None, 'requested_fact': 'The missing measure', 'reason': 'needed by the question',
            'source_kind': 'primary dataset', 'action': 'obtain a relevant release'}
    assert assess_research_outcome(analysis=replace(analysis, evidence_needs=[need]), **kwargs).complete is False


def test_substantive_user_edit_does_not_retain_stale_model_absence_suggestions():
    from copy import deepcopy
    from app.research_topics import ResearchTopics, ResearchInput, ResearchContent, VersionInput
    from app.storage import InMemoryDocumentRepository
    topics = ResearchTopics(InMemoryDocumentRepository())
    topic = topics.create('owner', ResearchInput(question='What is missing?'))
    content = ResearchContent(claims=[{'claim_id': 'c', 'text': 'Not found.', 'role': 'support', 'citations': []}],
        run={'operation_id': 'op', 'status': 'partial', 'source_processing': [],
             'model_absence_observations': [{'text': 'Not found.', 'source_ids': [], 'scope': 'unknown'}]})
    old = topics.add_version('owner', topic.topic_id, VersionInput(parent_version_id=topic.versions[0].version_id, content=content), created_by='agent')
    changed = deepcopy(old.versions[-1].content)
    changed.claims[0]['text'] = 'User revised the conclusion.'
    latest = topics.add_version('owner', topic.topic_id, VersionInput(parent_version_id=old.versions[-1].version_id, content=changed), created_by='user')
    assert latest.versions[-1].content.run['absence_assessment']['observations'] == []
    assert latest.versions[-2].content.run['absence_assessment']['observations'][0]['assessment'] == 'not_established'


def test_absence_indexes_bind_only_retained_model_claims_and_old_records_stay_unlinked():
    from datetime import datetime, timezone
    from app.research_analysis import analyze
    from app.research_sources import ResearchSource, SourcePermissions
    report = ResearchSource(source_id='s', owner_id='owner', title='source', text='The source contains one observation.',
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256='test',
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    payload = {'claims': [
        {'text': 'Discard this unsupported finding.', 'citations': [{'source_id': 'missing', 'quotation': 'fabricated'}]},
        {'text': 'No later observation is supplied.', 'citations': []},
        {'text': 'A distinct finding.', 'citations': []}],
        'absence_observations': [{'text': 'The later period is missing.', 'source_ids': [], 'scope': 'unknown',
                                  'claim_indexes': [0, 1, True, 99, '2']}]}
    result = analyze('What was observed?', [report], None, lambda _: (payload, 1))
    assert [claim['text'] for claim in result.claims] == ['No later observation is supplied.', 'A distinct finding.']
    ref = result.absence_observations[0]['observation_ref']
    assert result.claims[0]['model_absence_refs'] == [ref]
    assert 'model_absence_refs' not in result.claims[1]
    assert any('invalid or omitted finding' in item for item in result.limitations)
    from app.research_coverage import derive_absence_assessment
    legacy = derive_absence_assessment(run={'model_absence_observations': [{'text': 'legacy'}]},
        scope={'sources': [], 'scope': 'unknown'}, claims=[{'claim_id': 'c', 'text': 'legacy'}])
    assert legacy['observations'][0]['claim_ids'] == []


def test_followup_absence_and_selected_ranges_merge_without_rebinding_old_claims():
    from app.research_analysis import AnalysisResult
    from app.research_gap_stage import _merge_followup_evidence
    from app.research_coverage import derive_absence_assessment
    first = {'source_id': 'a', 'character_start': 0, 'character_end': 10, 'source_sha256': 'a-hash'}
    second = {'source_id': 'b', 'character_start': 20, 'character_end': 30, 'source_sha256': 'b-hash'}
    original = AnalysisResult([{'text': 'Old finding'}], [], [], ['a'], 1, segments=[first])
    extra = AnalysisResult([{'text': 'New absence', 'model_absence_refs': ['absence_new']}], [], [], ['b'], 1,
        segments=[first.copy(), second], absence_observations=[{'text': 'Missing later period', 'source_ids': ['b'],
                                                               'observation_ref': 'absence_new'}])
    observations, segments = _merge_followup_evidence(original, extra)
    assert segments == [first, second]
    scope = {'sources': [{'source_id': 'a', 'selection_status': 'partial'}, {'source_id': 'b', 'selection_status': 'partial'}],
             'scope': 'partial_or_unknown'}
    findings = [{'claim_id': 'old', 'text': 'Old finding'},
                {'claim_id': 'new', **extra.claims[0]}]
    derived = derive_absence_assessment(run={'model_absence_observations': observations}, scope=scope, claims=findings)
    assert derived['observations'][0]['claim_ids'] == ['new']
    assert derived['observations'][0]['source_ids'] == ['b']


def test_run_ranges_preserve_initial_and_followup_as_separate_stages():
    from app.research_coverage import derive_run_source_processing, derive_scope_assessment
    run = {'operation_id': 'op', 'source_processing': [
        {'source_id': 's', 'eligibility': 'eligible', 'provided': True, 'text_characters': 20,
         'initial_selection': 'partial', 'selection_status': 'partial', 'analysis_status': 'produced'}],
        'selected_segments': [{'source_id': 's', 'character_start': 0, 'character_end': 10}],
        'gap_followup_requested': True, 'gap_followup_status': 'completed', 'gap_followup_segments': [
            {'source_id': 's', 'character_start': 10, 'character_end': 20},
            {'source_id': 's', 'character_start': 10, 'character_end': 20}]}
    run['run_source_processing'] = derive_run_source_processing(run)
    row = run['run_source_processing'][0]
    assert row['coverage'] == 'complete'
    assert row['stage_ranges'] == [{'stage': 'analysis', 'ranges': [{'start': 0, 'end': 10}]},
                                   {'stage': 'gap_followup', 'ranges': [{'start': 10, 'end': 20}]}]
    assert run['source_processing'][0]['initial_selection'] == 'partial'
    assert derive_scope_assessment(run=run, claims=[], limitations=[])['scope'] == 'partial_or_unknown'
    run['gap_followup_segments'] = []
    run['gap_followup_status'] = 'failed'
    assert derive_run_source_processing(run)[0]['coverage'] == 'unknown'


def test_followup_evidence_needs_keep_question_identity_or_remain_unlinked():
    from app.research_analysis import AnalysisResult
    from app.research_gap_stage import _merge_followup_needs
    from app.research_coverage import safe_model_evidence_needs, derive_evidence_needs, has_unresolved_task_gap
    original = AnalysisResult([], [], [], [], None)
    extra = AnalysisResult([], [], [], [], None, research_questions=[{'question': 'Subquestion'}],
        evidence_needs=[{'question_index': 0, 'requested_fact': 'One'},
                        {'question_index': None, 'requested_fact': 'Other'}])
    merged = _merge_followup_needs(original, extra, [{'question': 'Other'}, {'question': 'Subquestion'}])
    assert merged[0]['question_index'] == 1
    assert merged[1]['question_index'] is None and merged[1]['question_origin'] == 'unlinked_followup'
    sanitized = safe_model_evidence_needs(merged)
    needs = derive_evidence_needs(run={'research_questions': [{}, {}], 'model_evidence_needs': sanitized},
                                  scope={'attempted': False})
    assert not has_unresolved_task_gap(needs=needs, absences={'observations': []}, claims=[])


def test_requirement_links_rebind_unique_question_and_validate_selected_facts_and_calculation():
    from app.research_coverage import derive_requirement_coverage, derive_completion_assessment
    from app.research_calculations import CalculationRequest, calculate
    facts = [dict(fact_id='f1', value='10', unit='percent', currency=None, period='2022', source_id='s',
                  location={'character_start': 0, 'character_end': 5}),
             dict(fact_id='f2', value='12', unit='percent', currency=None, period='2023', source_id='s',
                  location={'character_start': 6, 'character_end': 10})]
    calc = calculate(CalculationRequest.model_validate({'formula': 'difference', 'first': {k: facts[0][k] for k in ('fact_id','value','unit','currency','period')},
                                                        'second': {k: facts[1][k] for k in ('fact_id','value','unit','currency','period')}, 'decimal_places': 2}))
    calc['result_status'] = 'matched'
    run = {'research_questions': [{'question': 'Other'}, {'question': 'Metric change'}],
           'question_links': {'questions': [{'question_index': 0, 'finding_ids': [], 'source_ids': [], 'counterpoints': []},
                                             {'question_index': 1, 'finding_ids': [], 'source_ids': [], 'counterpoints': []}]},
           'evidence_links': {'claims': []},
           'planning_requirements': [{'question': 'Metric change', 'dimensions': {'metrics': ['rate']}, 'calculation_requested': True}],
           'run_source_processing': [{'source_id': 's', 'stage_ranges': [{'stage': 'analysis', 'ranges': [{'start': 0, 'end': 10}]}]}],
           'model_requirement_links': [{'question_index': 0, 'question': 'Metric change', 'fact_ids': ['f1','f2','invented'],
                                        'calculation_indexes': [0, 7], 'source_ids': ['s','other']}]}
    coverage = derive_requirement_coverage(run=run, has_user_question=True, facts=facts, calculations=[calc])
    row = coverage['requirements'][2]
    assert row['candidate_fact_ids'] == ['f1','f2'] and row['candidate_calculation_indexes'] == [0] and row['source_ids'] == ['s']
    assert row['semantic_status'] == 'not_independently_verified' and row['dimensions_status'] == 'model_suggested_not_verified'
    facts[1]['value'] = '99'
    changed = derive_requirement_coverage(run=run, has_user_question=True, facts=facts, calculations=[calc])
    row = changed['requirements'][2]
    assert row['candidate_calculation_indexes'] == [] and 'suggested_calculation_not_linked' in row['gaps']
    run['requirement_coverage'] = changed
    assert derive_completion_assessment(run=run, claims=[])['execution_status'] == 'known_gap'
    run['research_questions'].append({'question': 'Metric change'})
    ambiguous = derive_requirement_coverage(run=run, has_user_question=True, facts=facts, calculations=[calc])
    assert not any(item.get('model_relation_status') for item in ambiguous['requirements'])


def test_version_delivery_records_suggested_requirement_gap_without_claiming_semantic_completion():
    from app.research_topics import ResearchTopics, ResearchInput, ResearchContent, VersionInput
    from app.storage import InMemoryDocumentRepository
    topics = ResearchTopics(InMemoryDocumentRepository())
    topic = topics.create('owner', ResearchInput(question='Compare the periods'))
    content = ResearchContent(claims=[], run={'operation_id': 'op', 'status': 'completed',
        'research_questions': [{'question': 'Compare periods', 'claim_indexes': []}],
        'planning_requirements': [{'question': 'Compare periods', 'dimensions': {'metrics': ['output']}, 'calculation_requested': True}],
        'model_requirement_links': [], 'source_processing': [], 'selected_segments': []})
    saved = topics.add_version('owner', topic.topic_id, VersionInput(parent_version_id=topic.versions[0].version_id, content=content), created_by='agent').versions[-1].content
    row = saved.run['requirement_coverage']['requirements'][1]
    assert 'suggested_requirement_without_evidence_link' in row['gaps']
    assert 'suggested_calculation_not_linked' in row['gaps']
    assert row['semantic_status'] == 'not_independently_verified'
    assert saved.run['status'] == 'partial'
    assert saved.run['completion_assessment']['suggested_requirement_gaps'] == [row['requirement_id']]
    assert {item['code'] for item in saved.run['research_gaps']['items']} >= {'suggested_requirement_unlinked', 'suggested_calculation_unlinked'}


def test_recovery_snapshot_accepts_old_and_new_requirement_links():
    from app.research_recovery import AnalysisSnapshot
    base = {'claims': [], 'counterarguments': [], 'limitations': [], 'source_ids': [], 'facts': [],
            'calculations': [], 'prompt_characters': 0, 'prompt_bytes': 0, 'segments': [],
            'research_questions': [], 'available_fact_ids': []}
    assert AnalysisSnapshot.model_validate(base).requirement_links == []
    links = [{'question_index': None, 'question': None, 'fact_ids': ['f']}]
    assert AnalysisSnapshot.model_validate({**base, 'requirement_links': links}).requirement_links == links


def test_claim_scope_declarations_never_treat_undeclared_or_other_as_verified():
    from app.research_topics import ResearchTopics, ResearchInput, ResearchContent, VersionInput
    from app.storage import InMemoryDocumentRepository
    topics = ResearchTopics(InMemoryDocumentRepository())
    topic = topics.create('owner', ResearchInput(question='What is known?'))
    content = ResearchContent(claims=[
        {'text': 'No later figure exists.', 'scope_declaration': 'absence', 'role': 'support', 'citations': []},
        {'text': 'The reported value increased.', 'scope_declaration': 'other', 'role': 'support', 'citations': []},
        {'text': 'Another finding.', 'role': 'support', 'citations': []}],
        run={'operation_id': 'op', 'status': 'completed', 'source_processing': [], 'model_absence_observations': []})
    saved = topics.add_version('owner', topic.topic_id, VersionInput(parent_version_id=topic.versions[0].version_id,
        content=content), created_by='agent').versions[-1].content
    assert saved.claims[0]['absence_scope_status'] == 'not_established'
    assert saved.claims[0]['support_status'] == 'incomplete' and saved.run['status'] == 'partial'
    reviews = saved.run['scope_assessment']['claim_scope_declarations']
    assert [item['assessment'] for item in reviews] == ['not_established', 'not_independently_verified', 'unknown']
    assert saved.run['scope_assessment']['claim_scope_coverage'] == 'partial_model_declarations'
    assert all(not item['observation_linked'] for item in reviews)


def test_scope_review_disagreement_cannot_upgrade_absence_to_supported():
    from app.research_topics import ResearchTopics, ResearchInput, ResearchContent, VersionInput
    from app.storage import InMemoryDocumentRepository
    topics = ResearchTopics(InMemoryDocumentRepository())
    topic = topics.create('owner', ResearchInput(question='What is missing?'))
    content = ResearchContent(claims=[{'text': 'Later data are unavailable.', 'role': 'support', 'citations': [],
        'scope_declaration': 'other', 'support_review': {'verdict': 'supports', 'scope_declaration': 'absence',
        'claimed_scope': 'full_source', 'scope_evidence': 'cited_excerpt_only', 'independently_verified': False}}],
        run={'operation_id': 'op', 'status': 'completed', 'source_processing': [], 'support_review_requested': True})
    saved = topics.add_version('owner', topic.topic_id, VersionInput(parent_version_id=topic.versions[0].version_id,
        content=content), created_by='agent').versions[-1].content
    claim = saved.claims[0]; review = saved.run['scope_assessment']['claim_scope_declarations'][0]
    assert claim['support_status'] == 'incomplete' and claim['absence_scope_status'] == 'not_established'
    assert review['declaration_conflict'] is True and review['available_review_scope'] == 'cited_excerpt_only'
    assert review['assessment'] == 'not_established' and saved.run['status'] == 'partial'


def test_analysis_requirement_fills_only_unique_planning_gaps():
    from app.research_coverage import derive_requirement_coverage
    run = {
        'research_questions': [{'question': 'Compare periods'}],
        'question_links': {'questions': [{'question_index': 0, 'finding_ids': [], 'source_ids': [], 'counterpoints': []}]},
        'planning_requirements': [{'question': 'Compare periods', 'dimensions': {'metrics': ['sales']}}],
        'analysis_requirements': [{'question': 'Compare periods', 'dimensions': {'metrics': ['other'], 'periods': ['2023', '2024']}, 'calculation_requested': True}],
    }
    row = derive_requirement_coverage(run=run, has_user_question=True, facts=[], calculations=[])['requirements'][1]
    assert row['dimensions']['metrics'] == ['sales']
    assert row['dimensions']['periods'] == ['2023', '2024']
    assert row['calculation_requested'] is True
    assert row['semantic_status'] == 'not_independently_verified'
    run['research_questions'].append({'question': 'Compare periods'})
    rows = derive_requirement_coverage(run=run, has_user_question=True, facts=[], calculations=[])['requirements']
    assert all(not item.get('dimensions') for item in rows)


def test_evidence_needs_rebind_unique_question_text_and_never_old_index():
    from app.research_coverage import derive_evidence_needs, safe_model_evidence_needs
    items = safe_model_evidence_needs([
        {'question_index': 0, 'question': 'B', 'requested_fact': 'relevant fact'},
        {'question_index': 1, 'question': 'missing', 'requested_fact': 'unresolved fact'},
        {'question_index': None, 'requested_fact': 'root fact'},
    ])
    run = {'research_questions': [{'question': 'A', 'claim_ids': ['a']}, {'question': 'B', 'claim_ids': ['b']}], 'model_evidence_needs': items}
    needs = derive_evidence_needs(run=run, scope={})['items']
    assert needs[0]['model_question_index'] == 1
    assert needs[1]['model_question_index'] is None and needs[1]['question_origin'] == 'unlinked_followup'
    assert needs[2]['model_question_index'] is None and 'question_origin' not in needs[2]
    run['research_questions'].append({'question': 'B', 'claim_ids': ['c']})
    duplicate = derive_evidence_needs(run=run, scope={})['items'][0]
    assert duplicate['model_question_index'] is None and duplicate['question_origin'] == 'unlinked_followup'


def test_requirement_factual_conflicts_require_matching_identity_and_known_dimensions():
    from app.research_coverage import derive_requirement_coverage
    metadata = {'entity_id': 'entity', 'metric_id': 'metric'}
    facts = [dict(fact_id='a', source_id='s', location={'character_start': 0, 'character_end': 2},
                  value='10', unit='USD', currency='USD', period='2024', structured_metadata=metadata),
             dict(fact_id='b', source_id='t', location={'character_start': 0, 'character_end': 2},
                  value='11', unit='USD', currency='USD', period='2024', structured_metadata=metadata)]
    run = {'research_questions': [], 'question_links': {'questions': []}, 'evidence_links': {'claims': []},
           'run_source_processing': [{'source_id': source, 'stage_ranges': [{'ranges': [{'start': 0, 'end': 2}]}]} for source in ('s', 't')],
           'model_requirement_links': [{'question_index': None, 'question': None, 'fact_ids': ['a', 'b'], 'source_ids': ['s', 't'], 'calculation_indexes': []}]}
    row = derive_requirement_coverage(run=run, has_user_question=True, facts=facts, calculations=[])['requirements'][0]
    assert 'fact_value_conflict' in row['gaps']
    assert row['fact_conflicts'][0]['status'] == 'requires_revision_and_context_review'
    facts[1]['period'] = '2023'
    row = derive_requirement_coverage(run=run, has_user_question=True, facts=facts, calculations=[])['requirements'][0]
    assert 'fact_value_conflict' not in row['gaps']
    facts[1]['period'] = '2024'; facts[1]['unit'] = 'EUR'; facts[1]['currency'] = 'EUR'
    row = derive_requirement_coverage(run=run, has_user_question=True, facts=facts, calculations=[])['requirements'][0]
    assert 'fact_unit_conflict' in row['gaps'] and 'fact_currency_conflict' in row['gaps']
    facts[1].pop('structured_metadata')
    row = derive_requirement_coverage(run=run, has_user_question=True, facts=facts, calculations=[])['requirements'][0]
    assert 'fact_unit_conflict' not in row['gaps']


def test_requirement_calculation_input_conflict_is_recorded_not_silently_dropped():
    from app.research_coverage import derive_requirement_coverage, derive_completion_assessment
    fact = {'fact_id': 'f', 'source_id': 's', 'location': {'character_start': 0, 'character_end': 2},
            'value': '10', 'unit': 'percent', 'currency': None, 'period': '2024'}
    run = {'research_questions': [], 'question_links': {'questions': []}, 'evidence_links': {'claims': []},
           'run_source_processing': [{'source_id': 's', 'stage_ranges': [{'ranges': [{'start': 0, 'end': 2}]}]}],
           'model_requirement_links': [{'question_index': None, 'question': None, 'fact_ids': ['f'], 'source_ids': ['s'], 'calculation_indexes': [0]}]}
    calc = {'result_status': 'matched', 'inputs': [{'fact_id': 'f', 'value': '10', 'unit': 'percent', 'currency': None, 'period': '2023'}]}
    coverage = derive_requirement_coverage(run=run, has_user_question=True, facts=[fact], calculations=[calc])
    row = coverage['requirements'][0]
    assert row['candidate_calculation_indexes'] == []
    assert 'calculation_input_period_mismatch' in row['gaps']
    run['requirement_coverage'] = coverage
    assert derive_completion_assessment(run=run, claims=[])['execution_status'] == 'known_gap'
    calc['inputs'][0]['period'] = None
    row = derive_requirement_coverage(run=run, has_user_question=True, facts=[fact], calculations=[calc])['requirements'][0]
    assert 'calculation_input_period_mismatch' not in row['gaps']
