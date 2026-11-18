from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import json
import pytest
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext, execute_generation
from app.research_output_contract import ANALYSIS_CONTRACT
from app.research_page_merge import merge_repair_patch
from app.research_sources import ResearchSource, SourcePermissions


def test_partial_repair_preserves_valid_evidence_and_fact_and_rejects_wrong_slots():
    text = 'Revenue was USD 10 in 2024. Revenue was USD 8 in 2023.'
    source = ResearchSource(source_id='s1', owner_id='owner', title='Report', text=text,
        kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256='test',
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    original = deepcopy(ANALYSIS_CONTRACT.example)
    for key in ('counterarguments', 'research_questions', 'evidence_needs', 'absence_observations', 'requirement_links', 'limitations'):
        original[key] = []
    original['evidence'][0]['quotation'] = 'Revenue was USD 10 in 2024.'
    second_evidence = deepcopy(original['evidence'][0])
    second_evidence.update(id='e2', quotation='Revenue was USD 8 in 2023.')
    original['evidence'].append(second_evidence)
    second_fact = deepcopy(original['facts'][0])
    second_fact.update(id='f2', value='8', period='2023', evidence_id='e2')
    original['facts'].append(second_fact)
    original['calculations'][0]['second_fact_id'] = 'f2'
    original['calculations'][0]['formula'] = 'not-a-formula'
    responses = [original]
    prompts = []
    def generate(prompt, *, contract, timeout_seconds, cancel_event, system):
        prompts.append(json.loads(prompt))
        return deepcopy(responses.pop(0)), 5
    provider = SimpleNamespace(policy=SimpleNamespace(max_input_tokens=None, max_output_tokens=None, timeout_seconds=10))
    ctx = ResearchExecutionContext(ResearchExecutionBudget(None, None, None, 100), False,
        lambda: None, lambda: None, lambda *_: None, lambda *_: None, lambda: None, now=lambda: 0)
    common = dict(question='What was revenue?', scope='', sources=[source], as_of=None, provider=provider,
        generate=generate, context=ctx, focus=None, planning_steps=[], require_counterarguments=False)
    initial = execute_generation(**common, call_id='first')
    assert initial.analysis.facts and not initial.analysis.claims
    continuation = initial.continuation
    assert continuation and continuation['version'] == 'generation-patch-v1'
    pending = continuation['pending'][0]
    assert {item['collection'] for item in pending['dependencies']} == {'facts', 'evidence'}
    assert {item['collection'] for item in pending['targets']} == {'calculations', 'claims'}
    repaired = deepcopy(ANALYSIS_CONTRACT.example)
    for key, value in repaired.items():
        if isinstance(value, list): repaired[key] = []
    calculation = deepcopy(ANALYSIS_CONTRACT.example['calculations'][0])
    calculation.update(id='calculations-0', first_fact_id='facts-0', second_fact_id='facts-1')
    claim = deepcopy(ANALYSIS_CONTRACT.example['claims'][0])
    claim['id'] = 'claims-0'
    claim['assertions'][0]['evidence_ids'] = ['evidence-0']
    claim['assertions'][0]['calculation_ids'] = ['calculations-0']
    repaired['calculations'] = [calculation]
    repaired['claims'] = [claim]
    responses.append(repaired)
    page = execute_generation(**common, call_id='repair', repair=continuation)
    assert len(prompts) == 2
    assert page.unavailable_reason is None and page.analysis.calculations and page.analysis.claims
    merged = merge_repair_patch(initial.analysis, page.analysis, page_id='repair',
        targets=pending['targets'], dependencies=pending['dependencies'])
    assert len(merged.facts) == 2 and len(merged.calculations) == 1 and len(merged.claims) == 1
    assert merged.claims[0]['citations'][0]['quotation'] == 'Revenue was USD 10 in 2024.'
    assert merged.claims[0]['assertion_suggestions'][0]['calculation_indexes'] == [0]
    bad = deepcopy(repaired)
    bad['claims'][0]['id'] = 'claims-other'
    responses.append(bad)
    rejected = execute_generation(**common, call_id='bad-repair', repair=continuation)
    assert rejected.unavailable_reason == 'invalid_model_output'
    with pytest.raises(ValueError):
        merge_repair_patch(initial.analysis, rejected.analysis, page_id='bad-repair',
            targets=pending['targets'], dependencies=pending['dependencies'])
