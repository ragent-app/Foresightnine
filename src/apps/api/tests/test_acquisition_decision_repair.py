import json
from types import SimpleNamespace

from app.phase3 import ProviderFailure
from app.research_acquisition import ProposedAction, execute_decision, make_decision_continuation
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext


def setup_run(responses):
    receipts = []
    context = ResearchExecutionContext(ResearchExecutionBudget(None, None, None, float('inf')), False,
        lambda: None, lambda: None, lambda *_: None, lambda call, tokens: receipts.append((call, tokens)), lambda: None)
    provider = SimpleNamespace(name='fixture', policy=SimpleNamespace(max_input_tokens=10000, max_output_tokens=1000, timeout_seconds=5))
    prompts = []
    def generate(prompt, *, cancel_event, system, timeout_seconds):
        prompts.append(json.loads(prompt))
        value = responses.pop(0)
        if isinstance(value, Exception): raise value
        return value, 7
    args = dict(question='Research?', target={'requirement_id':'r1'}, requirements=[{'requirement_id':'r1'}],
        candidates=[{'candidate_id':'c1'}], ranges=[], feedback=[], capabilities=['review'],
        provider=provider, generate=generate, context=context)
    return args, prompts, receipts


def valid():
    return {'requirement_id':'r1', 'action':'review', 'expected_gap':'Need evidence',
            'result_update':'Review sources', 'reason':'Check the available evidence'}


def test_invalid_response_repaired_once_and_strict_binding():
    args, prompts, receipts = setup_run([{'requirement_id':'r1','action':'invalid'}, valid()])
    first = execute_decision(**args, call_id='first')
    assert first.status == 'failed' and first.continuation
    assert ProposedAction.model_validate(prompts[0]['example']).action == 'review'
    errors = first.continuation['pending'][0]['errors']
    assert any('action' in item['path'] for item in errors)
    changed = execute_decision(**{**args, 'question':'Different?'}, call_id='changed', repair=first.continuation)
    assert changed.reason == 'repair_binding_mismatch' and len(prompts) == 1
    second = execute_decision(**args, call_id='repair', repair=first.continuation)
    assert second.status == 'completed' and second.continuation is None
    assert len(prompts) == 2 and prompts[1]['repair_instruction']['errors'] == errors
    assert receipts == [('first',7), ('repair',7)]


def test_repeat_invalid_no_progress_and_unknown_or_refusal_do_not_retry():
    invalid = {'requirement_id':'r1','action':'invalid'}
    args, prompts, _ = setup_run([invalid, invalid])
    first = execute_decision(**args, call_id='a')
    second = execute_decision(**args, call_id='b', repair=first.continuation)
    assert second.status == 'failed' and second.continuation is None and len(prompts) == 2
    for code, responded in [('provider_timeout', False), ('provider_output_refused', True)]:
        error = ProviderFailure(code, 'safe', retryable=False)
        error.provider_response_received = responded
        args, _, _ = setup_run([error])
        assert execute_decision(**args, call_id='x').continuation is None


def test_semantic_gate_uses_same_repetition_guard():
    args, _, _ = setup_run([valid(), valid()])
    first = execute_decision(**args, call_id='a')
    state = make_decision_continuation(first, call_id='a', error='invalid_reference')
    assert state
    second = execute_decision(**args, call_id='b', repair=state)
    assert make_decision_continuation(second, call_id='b', error='invalid_reference', previous=state) is None
