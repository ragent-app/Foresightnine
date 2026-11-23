import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.storage import InMemoryDocumentRepository
from app.research_run_instructions import ResearchRunInstructions, InstructionConflict, InstructionNotFound
from app.research_context_manager import build_model_turn_input
from app.model_protocol import ModelAssistantContinuation, ModelToolResult, ModelTurnExchange
from app.chat_research_adapter import ChatResearchAdapter
from app.openai_responses import OpenAIResponsesProvider
from app.anthropic_messages import AnthropicMessagesProvider


def test_instruction_idempotency_fencing_and_seal():
    store = ResearchRunInstructions(InMemoryDocumentRepository())
    store.begin('a', 'topic', 'turn', 'op', budget={'calls': 0})
    receipt, created = store.append('a', 'topic', 'turn', 'Do not use averages', 'key')
    assert created and receipt['operation_id'] == 'op' and 'idempotency_digest' not in receipt
    assert store.append('a', 'topic', 'turn', 'Do not use averages', 'key') == (receipt, False)
    with pytest.raises(InstructionConflict):
        store.append('a', 'topic', 'turn', 'Different', 'key')
    with pytest.raises(InstructionNotFound):
        store.read('other', 'topic', 'turn')
    assert not store.seal('a', 'topic', 'turn', 0, operation_id='op')
    assert not store.claim_action('a', 'topic', 'turn', 'op', 'call', 0)
    assert store.claim_action('a', 'topic', 'turn', 'op', 'call', 1)
    assert store.action_status('a', 'topic', 'turn', 'op', 'call', 1) == 'claimed_unknown'
    assert not store.claim_action('a', 'topic', 'turn', 'op', 'call', 1)
    store.begin('a', 'topic', 'turn', 'op2', previous_operation_id='op')
    assert store.action_status('a', 'topic', 'turn', 'op2', 'call', 1) == 'claimed_unknown'
    with pytest.raises(InstructionConflict):
        store.fenced('a', 'topic', 'turn', 'op', lambda: None)
    store.ack('a', 'topic', 'turn', 'op2', 1)
    writes = []
    assert store.seal('a', 'topic', 'turn', 1, operation_id='op2', persist=lambda: writes.append(1))
    assert writes == [1]
    assert store.append('a', 'topic', 'turn', 'Do not use averages', 'key') == (receipt, False)
    with pytest.raises(InstructionConflict):
        store.append('a', 'topic', 'turn', 'New', 'new')
    assert store.read('a', 'topic', 'turn')['budget'] == {'calls': 0}
    assert all(row.get('account_id') == 'a' for _, row in store.repository.items())


def test_concurrent_append_and_seal_are_serialized():
    store = ResearchRunInstructions(InMemoryDocumentRepository())
    store.begin('a', 't', 'r', 'op')
    def append():
        try:
            return store.append('a', 't', 'r', 'new', 'key')[1]
        except InstructionConflict:
            return False
    with ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(append)
        b = executor.submit(store.seal, 'a', 't', 'r', 0, operation_id='op')
        appended, sealed = a.result(), b.result()
    assert appended != sealed


def test_clarification_is_account_scoped_and_resumes_same_turn():
    store = ResearchRunInstructions(InMemoryDocumentRepository())
    store.begin('a', 't', 'r', 'op')
    clarification = {'question': 'When is payment due?', 'why_it_matters': 'It changes liquidity needs.', 'requirement_id': 'cashflow'}
    assert store.wait_for_user('a', 't', 'r', 'op', 0, clarification)
    with pytest.raises(InstructionNotFound):
        store.read('other', 't', 'r')
    receipt, _ = store.append('a', 't', 'r', 'Next June', 'answer')
    assert receipt['sequence'] == 1
    with pytest.raises(InstructionConflict):
        store.accept_findings('a', 't', 'r', 'op', 'v')
    reopened = store.begin('a', 't', 'r', 'op2', previous_operation_id='op', allow_reopen=True)
    assert reopened['status'] == 'open' and 'clarification' not in reopened
    assert store.read('a', 't', 'r')['instructions'][0]['text'] == 'Next June'


def test_clarification_fence_preserves_concurrent_instruction_and_stop():
    store = ResearchRunInstructions(InMemoryDocumentRepository())
    store.begin('a', 't', 'r', 'op')
    store.append('a', 't', 'r', 'New condition', 'k')
    writes = []
    assert not store.wait_for_user('a', 't', 'r', 'op', 0, {'question': 'Q'}, persist=lambda: writes.append(1))
    assert writes == []
    assert store.wait_for_user('a', 't', 'r', 'op', 1, {'question': 'Q'})
    store.stop('a', 't', 'r')
    with pytest.raises(InstructionConflict):
        store.fenced('a', 't', 'r', 'op', lambda: writes.append(1), allow_waiting=True)
    assert writes == []


def test_accept_findings_is_idempotent_without_model_resume():
    store = ResearchRunInstructions(InMemoryDocumentRepository())
    store.begin('a', 't', 'r', 'op')
    assert store.wait_for_user('a', 't', 'r', 'op', 0, {'question': 'Q'})
    assert store.accept_findings('a', 't', 'r', 'op', 'v')['status'] == 'sealed'
    assert store.accept_findings('a', 't', 'r', 'op', 'v')['final_version_id'] == 'v'
    with pytest.raises(InstructionConflict):
        store.append('a', 't', 'r', 'late', 'late')


@pytest.mark.parametrize('history', [False, True])
def test_three_wires_include_ordered_user_messages_after_tools_and_in_estimates(history):
    state = {'user': '{}', 'history': [], 'user_instructions': [
        {'instruction_id': 'i2', 'sequence': 2, 'text': '第二条'},
        {'instruction_id': 'i1', 'sequence': 1, 'text': 'first'}]}
    turn = build_model_turn_input(state=state, source_by_id={}, tools=())
    if history:
        from dataclasses import replace
        turn = replace(turn, history=(ModelTurnExchange(ModelAssistantContinuation({'role': 'assistant', 'tool_calls': [{'id': 'c'}], 'output': [{'type': 'function_call', 'call_id': 'c'}], 'content': [{'type': 'tool_use', 'id': 'c', 'name': 'read', 'input': {}}]}), (ModelToolResult('c', 'result'),)),))
    chat = ChatResearchAdapter(SimpleNamespace())
    messages, _ = chat._render(turn)
    assert [json.loads(m['content'])['sequence'] for m in messages[-2:]] == [1, 2]
    if history:
        assert messages[-3]['role'] == 'tool'
    settings = SimpleNamespace(model='m', generation_parameters=None, base_url='https://example.com')
    responses = OpenAIResponsesProvider(settings)
    wire = responses._wire(turn)
    assert [json.loads(m['content'])['sequence'] for m in wire['input'][-2:]] == [1, 2]
    assert responses.estimate_turn_input(turn) > responses.estimate_turn_input(build_model_turn_input(state={'user': '{}', 'history': []}, source_by_id={}, tools=()))
    anthropic = AnthropicMessagesProvider(settings, SimpleNamespace(timeout_seconds=1), anthropic_version='v1', auth_scheme='bearer', max_tokens=128)
    wire = anthropic._wire(turn, profile=SimpleNamespace(explicit_cache_control=False))
    blocks = wire['messages'][-1]['content']
    assert [json.loads(b['text'])['sequence'] for b in blocks[-2:]] == [1, 2]
    if history:
        assert blocks[0]['type'] == 'tool_result'


def test_explicit_reopen_requires_previous_operation_and_preserves_claims():
    store = ResearchRunInstructions(InMemoryDocumentRepository())
    store.begin('a', 't', 'r', 'op', budget={'calls': 0})
    assert store.claim_action('a', 't', 'r', 'op', 'pending', 0)
    store.stop('a', 't', 'r')
    with pytest.raises(InstructionConflict):
        store.begin('a', 't', 'r', 'next', previous_operation_id='op')
    with pytest.raises(InstructionConflict):
        store.begin('a', 't', 'r', 'next', previous_operation_id='wrong', allow_reopen=True)
    result = store.begin('a', 't', 'r', 'next', previous_operation_id='op', allow_reopen=True)
    assert result['status'] == 'open' and result['budget'] == {'calls': 0}
    assert store.action_status('a', 't', 'r', 'next', 'pending', 0) == 'claimed_unknown'
    assert store.seal('a', 't', 'r', 0, operation_id='next', final_version_id='v1')
    reopened = store.begin('a', 't', 'r', 'next2', previous_operation_id='next', allow_reopen=True)
    assert reopened['status'] == 'open' and reopened['final_version_id'] is None
