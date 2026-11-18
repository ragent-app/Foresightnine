from copy import deepcopy

import pytest

from app.research_output_contract import ANALYSIS_CONTRACT, ResearchDraft, convert_draft


def test_example_is_valid_and_references_are_translated():
    ResearchDraft.model_validate(ANALYSIS_CONTRACT.example)
    legacy, problems, ids = convert_draft(deepcopy(ANALYSIS_CONTRACT.example))
    assert problems == []
    assert ids['claims'] == ['c1']
    assert legacy['claims'][0]['assertions'][0]['citation_indexes'] == [0]
    assert legacy['claims'][0]['calculation_indexes'] == [0]
    assert legacy['counterarguments'][0]['responds_to_claim_indexes'] == [0]
    assert legacy['requirement_links'][0]['calculation_indexes'] == [0]


def test_bad_objects_and_colliding_fact_ids_do_not_rebind_dependencies():
    raw = deepcopy(ANALYSIS_CONTRACT.example)
    raw['evidence'][0]['unexpected'] = 'do not discard'
    legacy, problems, _ = convert_draft(raw, {'f1'})
    assert problems
    assert legacy['facts'] == []
    assert legacy['calculations'] == []
    assert legacy['claims'] == []
    assert legacy['counterarguments'] == []
    assert legacy['requirement_links'] == []


def test_wrong_version_and_unknown_envelope_are_rejected():
    raw = deepcopy(ANALYSIS_CONTRACT.example)
    raw['version'] = 'other'
    with pytest.raises(ValueError):
        convert_draft(raw)
    raw = deepcopy(ANALYSIS_CONTRACT.example)
    raw['unknown'] = 1
    with pytest.raises(ValueError):
        convert_draft(raw)


def test_default_contract_runs_selection_and_retains_grounded_finding():
    from datetime import datetime, timezone
    from hashlib import sha256
    from app.research_analysis import analyze
    from app.research_sources import ResearchSource, SourcePermissions

    text = 'Revenue was $10 in 2024.'
    source = ResearchSource(source_id='s1', owner_id='o', title='Report', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    draft = deepcopy(ANALYSIS_CONTRACT.example)
    draft['calculations'] = []
    draft['claims'][0]['assertions'][0]['calculation_ids'] = []
    draft['requirement_links'][0]['calculation_ids'] = []
    seen = []
    result = analyze('What was revenue in 2024?', [source], None,
                     lambda prompt: (seen.append(prompt) or draft, 1), max_prompt_chars=6000)
    assert seen and len(seen[0].encode()) <= 6000
    assert result.claims and result.segments
    assert result.output_contract_version == ANALYSIS_CONTRACT.version
    assert result.structured_output_ids['claims']['c1'] == 0


def test_counterpoint_reuses_new_contract_context_and_rejects_changed_source():
    from datetime import datetime, timezone
    from hashlib import sha256
    from types import SimpleNamespace
    from app.research_analysis import analyze
    from app.research_artifacts import prepare_run_claims
    from app.research_counterpoint_stage import execute_counterpoint
    from app.research_sources import ResearchSource, SourcePermissions

    text = 'Revenue was $10 in 2024. Demand declined during 2024.'
    source = ResearchSource(source_id='s1', owner_id='o', title='Report', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    draft = deepcopy(ANALYSIS_CONTRACT.example)
    draft['calculations'] = []
    draft['claims'][0]['assertions'][0]['calculation_ids'] = []
    draft['counterarguments'] = []
    draft['requirement_links'][0]['calculation_ids'] = []
    captured = []
    analysis = analyze('What was revenue in 2024?', [source], None,
        lambda prompt: (captured.append(prompt) or draft, 1), max_prompt_chars=6000)
    class Context:
        calls = [1]
        budget = SimpleNamespace(deadline=100, max_total_tokens=None)
        now = staticmethod(lambda: 0)
        model_exhaustion = staticmethod(lambda *_: None)
        ensure_running = staticmethod(lambda: None)
        start_model = staticmethod(lambda *_: None)
        finish_model = staticmethod(lambda *_: None)
        cancel_event = staticmethod(lambda: None)
        mark_model_processing_error = staticmethod(lambda *_: None)
        invoke_model = staticmethod(lambda _id, generate, prompt, **kwargs: generate(prompt))
    provider = SimpleNamespace(policy=SimpleNamespace(max_input_tokens=10000, max_output_tokens=1000, timeout_seconds=10))
    request = SimpleNamespace(counterpoint_followup=True, require_counterarguments=True)
    payload = {'counterarguments':[{'text':'Demand declined during 2024.',
        'citations':[{'source_id':'s1','quotation':'Demand declined during 2024.'}],
        'responds_to_claim_indexes':[0]}]}
    def run(current_source):
        return execute_counterpoint(request=request, analysis=analysis,
            claims=prepare_run_claims(analysis, [source], None, []), leads=[], question='What was revenue in 2024?',
            scope='', sources=[current_source], as_of=None, focus=None, planning_steps=[], base_prompt=captured[0],
            max_prompt_chars=6000, provider=provider, generate=lambda _: (payload, 1), context=Context(),
            call_id='c', checkpoint=lambda _: None)
    unchanged = run(source)
    assert unchanged.status in {'completed', 'needs_review'}
    assert unchanged.analysis.counterarguments
    changed = source.model_copy(update={'text': text.replace('declined', 'improved')})
    assert run(changed).status == 'failed'


@pytest.mark.parametrize(('mutation', 'expected'), [
    (lambda value: value.pop('version'), {'type': 'missing_version'}),
    (lambda value: value.update(version='private-version-value'), {'type': 'version_mismatch'}),
    (lambda value: value.update(version=['private-value']), {'type': 'version_type', 'actual_type': 'array'}),
    (lambda value: value.pop('facts'), {'type': 'missing_fields', 'fields': ['facts'], 'count': 1}),
    (lambda value: value.update(private_unknown_field='private-value'), {'type': 'extra_fields', 'count': 1}),
    (lambda value: value.update(facts={'private-value': 1}), {'type': 'collection_type', 'field': 'facts', 'actual_type': 'object'}),
])
def test_draft_contract_diagnostics_are_bounded_and_do_not_expose_values(mutation, expected):
    from app.research_output_contract import DraftContractError
    draft = deepcopy(ANALYSIS_CONTRACT.example)
    mutation(draft)
    with pytest.raises(DraftContractError) as caught:
        convert_draft(draft)
    assert expected in caught.value.errors
    assert 'private' not in str(caught.value.errors)


def test_draft_contract_non_object_has_only_type_diagnostic():
    from app.research_output_contract import DraftContractError
    with pytest.raises(DraftContractError) as caught:
        convert_draft(['private-value'])
    assert caught.value.errors == [{'type': 'envelope_type', 'actual_type': 'array'}]


def test_analysis_preserves_safe_envelope_diagnostic():
    from datetime import datetime, timezone
    from hashlib import sha256
    from app.research_analysis import InvalidAnalysis, analyze
    from app.research_sources import ResearchSource, SourcePermissions
    text = 'Revenue was $10 in 2024.'
    source = ResearchSource(source_id='s1', owner_id='o', title='Report', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    draft = deepcopy(ANALYSIS_CONTRACT.example)
    del draft['version']
    draft['private_key'] = 'private_value'
    with pytest.raises(InvalidAnalysis) as caught:
        analyze('What was revenue?', [source], None, lambda _: (draft, 1), max_prompt_chars=6000)
    assert caught.value.diagnostic == {'category': 'contract_validation', 'errors': [
        {'type': 'missing_version'}, {'type': 'extra_fields', 'count': 1}]}
