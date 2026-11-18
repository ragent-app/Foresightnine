from app.research_generation_repair import make_continuation, validate_continuation, split_work_item


def test_continuation_is_bound_and_repeated_failure_stops():
    state = make_continuation(binding='a', parent_call_id='first', error='contract_validation')
    assert validate_continuation(state, 'a')
    assert not validate_continuation(state, 'b')
    assert make_continuation(binding='a', parent_call_id='second', error='contract_validation', previous=state) is None
    assert make_continuation(binding='a', parent_call_id='first', error='provider_output_refused') is None


def test_split_preserves_units_and_never_splits_a_single_unit():
    item = {'scope': {'evidence_units': [{'source_id': 'a', 'start': 0, 'end': 10},
                                        {'source_id': 'a', 'start': 10, 'end': 20}]}}
    parts = split_work_item(item)
    assert [part['scope']['evidence_units'] for part in parts] == [[item['scope']['evidence_units'][0]], [item['scope']['evidence_units'][1]]]
    assert split_work_item(parts[0]) == []


def test_slots_bind_by_server_id_and_dependencies_are_immutable():
    from app.research_generation_repair import fingerprint, validate_slot_response
    dependency = {'id': 'dependency-1', 'text': 'verified'}
    targets = [{'collection': 'claims', 'slot_id': 'slot-1'}, {'collection': 'claims', 'slot_id': 'slot-2'}]
    dependencies = [{'collection': 'claims', 'slot_id': 'dependency-1', 'dto': dependency, 'dto_digest': fingerprint(dependency)}]
    from app.research_output_contract import EXAMPLE
    response = {key: ([] if isinstance(value, list) else value) for key, value in EXAMPLE.items()}
    response['claims'] = [{'id': 'slot-2'}, {'id': 'slot-1'}]
    assert len(validate_slot_response(response, targets, dependencies)['claims']) == 3
    response['claims'].append({'id': 'dependency-1', 'text': 'changed'})
    assert validate_slot_response(response, targets, dependencies) is None


def test_allowed_units_limit_actual_prompt_and_validation_scope():
    from copy import deepcopy
    from datetime import datetime, timezone
    import json
    from app.research_analysis import analyze
    from app.research_output_contract import ANALYSIS_CONTRACT
    from app.research_sources import ResearchSource, SourcePermissions
    def source(sid, text):
        return ResearchSource(source_id=sid, owner_id='owner', title=sid, text=text,
            kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256='test',
            permissions=SourcePermissions(store=True, model_input=True, display=True))
    sources = [source('one', 'Revenue was 100 million in 2024.'),
               source('two', 'Revenue was 200 million in 2024.')]
    empty = deepcopy(ANALYSIS_CONTRACT.example)
    for key, value in empty.items():
        if isinstance(value, list): empty[key] = []
    parent_prompts = []
    parent = analyze('Compare revenue in 2024', sources, None,
                     lambda prompt: (parent_prompts.append(json.loads(prompt)) or empty, 1))
    assert len(parent.segments) >= 2
    allowed = [parent.segments[0]]
    child_prompts = []
    child = analyze('Compare revenue in 2024', sources, None,
                    lambda prompt: (child_prompts.append(json.loads(prompt)) or empty, 1),
                    allowed_units=allowed)
    assert child_prompts
    assert len(child.segments) < len(parent.segments)
    assert {(x['source_id'], x['character_start'], x['character_end']) for x in child.segments} <= {
        (x['source_id'], x['character_start'], x['character_end']) for x in allowed}
    assert len(child_prompts[0]['sources']) < len(parent_prompts[0]['sources'])
