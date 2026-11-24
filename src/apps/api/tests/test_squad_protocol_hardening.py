"""Wire-protocol layer hardening tests across Anthropic Messages, OpenAI Chat Completions, and OpenAI Responses."""
from __future__ import annotations

import json
from threading import Event
from types import SimpleNamespace

import pytest

from app import openai_compatible, openai_responses
from app.anthropic_messages import AnthropicMessagesProvider
from app.chat_research_adapter import ChatResearchAdapter
from app.model_protocol import (
    ModelAssistantContinuation,
    ModelToolDefinition,
    ModelToolResult,
    ModelTurnExchange,
    ModelTurnInput,
    extract_assistant_text,
)
from app.openai_compatible import (
    HttpResponse,
    OpenAICompatibleLlmProvider,
    OpenAICompatibleSettings,
)
from app.openai_responses import OpenAIResponsesProvider
from app.phase3 import ProviderFailure, ProviderPolicy


SAMPLE_SCHEMA = {
    'type': 'object',
    'properties': {
        'tasks': {'type': 'array', 'items': {'type': 'string'}},
        'rationale': {'type': 'string', 'default': 'none'},
    },
    'required': ['tasks'],
    'additionalProperties': False,
}

STRICT_SCHEMA = {
    'type': 'object',
    'properties': {
        'ok': {'type': 'boolean'},
    },
    'required': ['ok'],
    'additionalProperties': False,
}


def test_extract_assistant_text_handles_all_wire_formats() -> None:
    assert extract_assistant_text(None) is None
    assert extract_assistant_text({'role': 'assistant', 'content': None}) is None
    assert extract_assistant_text({'role': 'assistant', 'content': ''}) is None
    assert extract_assistant_text({'role': 'assistant', 'content': '{"ok": true}'}) == '{"ok": true}'

    # Anthropic Messages content block list (thinking + text, never leaks thinking)
    anthropic_msg = {
        'role': 'assistant',
        'content': [
            {'type': 'thinking', 'thinking': 'SECRET_CHAIN_OF_THOUGHT', 'signature': 'sig-1'},
            {'type': 'text', 'text': '{"tasks": ["a"]}'},
        ],
    }
    assert extract_assistant_text(anthropic_msg) == '{"tasks": ["a"]}'

    # Anthropic tool_use-only turn (no text block)
    assert extract_assistant_text({
        'role': 'assistant',
        'content': [
            {'type': 'thinking', 'thinking': 'hidden', 'signature': 'sig-2'},
            {'type': 'tool_use', 'id': 'tu_1', 'name': 'read_sources', 'input': {}},
        ],
    }) is None

    # Multi-part OpenAI / Responses text blocks
    assert extract_assistant_text({
        'role': 'assistant',
        'content': [
            {'type': 'text', 'text': 'line 1'},
            {'type': 'output_text', 'text': 'line 2'},
            {'type': 'text', 'text': {'value': 'line 3'}},
        ],
    }) == 'line 1\nline 2\nline 3'

    # Responses output envelope fallback
    assert extract_assistant_text({
        'role': 'assistant',
        'output': [
            {'type': 'reasoning', 'id': 'rs1', 'summary': []},
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '{"ok": true}'}]},
        ],
    }) == '{"ok": true}'


def _anthropic_provider(transport=None, *, max_tokens: int = 1024, timeout_seconds: float = 30.0) -> AnthropicMessagesProvider:
    settings = SimpleNamespace(
        base_url='https://example.com/api/anthropic',
        model='claude-test',
        api_key='sk-ant-test',
        allow_private_network=False,
    )
    policy = ProviderPolicy(timeout_seconds=timeout_seconds, max_input_tokens=None, max_output_tokens=max_tokens)
    return AnthropicMessagesProvider(
        settings,
        policy,
        anthropic_version='2023-06-01',
        auth_scheme='x-api-key',
        max_tokens=max_tokens,
        transport=transport,
    )


def test_anthropic_non_tool_followup_turns_never_emit_empty_content(monkeypatch: pytest.MonkeyPatch) -> None:
    sent_wires: list[dict] = []

    def transport(method, url, headers, body, timeout):
        sent_wires.append(json.loads(body))
        return HttpResponse(200, json.dumps({
            'role': 'assistant',
            'content': [{'type': 'text', 'text': '{"tasks": ["worker_segments"]}'}],
            'stop_reason': 'end_turn',
            'usage': {'input_tokens': 15, 'output_tokens': 5},
        }))

    provider = _anthropic_provider(transport)
    ex1 = ModelTurnExchange(
        ModelAssistantContinuation({'role': 'assistant', 'content': [{'type': 'text', 'text': '{"worker_tasks": []}'}]}),
        (),
        user_followup='Validation failed: rename worker_tasks to tasks.',
    )
    ex2 = ModelTurnExchange(
        ModelAssistantContinuation({'role': 'assistant', 'content': [{'type': 'text', 'text': '{"tasks": []}'}]}),
        (),
        user_followup='Validation failed: tasks must have at least 1 item.',
    )
    turn = ModelTurnInput(
        system='Plan tasks.',
        user='Analyze NVDA Q2.',
        tools=(),
        history=(ex1, ex2),
        tail_note='Keep JSON compact.',
        response_schema=SAMPLE_SCHEMA,
        response_schema_name='ResearchDispatchPlan',
    )
    resp = provider.complete_turn(turn)
    assert extract_assistant_text(resp.message) == '{"tasks": ["worker_segments"]}'

    wire = sent_wires[-1]
    assert [m['role'] for m in wire['messages']] == ['user', 'assistant', 'user', 'assistant', 'user']
    for msg in wire['messages']:
        assert msg['content'], f'empty content emitted on {msg}'
        if isinstance(msg['content'], list):
            assert len(msg['content']) > 0
    assert wire['messages'][2] == {'role': 'user', 'content': 'Validation failed: rename worker_tasks to tasks.'}
    assert wire['messages'][4] == {
        'role': 'user',
        'content': [
            {'type': 'text', 'text': 'Validation failed: tasks must have at least 1 item.'},
            {'type': 'text', 'text': 'Keep JSON compact.'},
        ],
    }
    assert 'ResearchDispatchPlan' in wire['system']
    assert 'JSON Schema:' in wire['system']

    # Also test explicit_cache_control=True + empty results without user_followup -> still zero content: []
    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://example.com/api/anthropic/v1/messages',
        'model': 'claude-test',
        'version': 'v1',
        'mode': 'json_object',
        'explicit_cache_control': True,
    }))
    ex_no_results = ModelTurnExchange(
        ModelAssistantContinuation({'role': 'assistant', 'content': [{'type': 'text', 'text': 'draft'}]}),
        (),
    )
    cached_wire = provider._wire(ModelTurnInput(
        'Plan.', 'Question.', (), (ex_no_results,), tail_note='Wrap up.', response_schema=SAMPLE_SCHEMA, response_schema_name='Plan',
    ))
    assert [m['role'] for m in cached_wire['messages']] == ['user', 'assistant', 'user']
    assert cached_wire['messages'][-1] == {'role': 'user', 'content': [{'type': 'text', 'text': 'Wrap up.'}]}
    assert isinstance(cached_wire['system'], list) and cached_wire['system'][0]['cache_control'] == {'type': 'ephemeral'}


@pytest.mark.parametrize(('status_code', 'expected_code', 'expected_retryable'), [
    (529, 'provider_unavailable', True),
    (503, 'provider_unavailable', True),
    (502, 'provider_unavailable', True),
    (500, 'provider_unavailable', True),
    (429, 'provider_rate_limited', True),
    (400, 'provider_request_rejected', False),
])
def test_anthropic_http_status_retryability(status_code: int, expected_code: str, expected_retryable: bool) -> None:
    provider = _anthropic_provider(lambda *a: HttpResponse(status_code, json.dumps({'error': {'type': 'overloaded_error'}})))
    with pytest.raises(ProviderFailure) as caught:
        provider.complete_turn(ModelTurnInput('sys', 'user', (), ()))
    assert caught.value.code == expected_code
    assert caught.value.retryable is expected_retryable


def test_anthropic_turn_runtime_fields_and_unsigned_thinking_block() -> None:
    seen: list[tuple[dict, float]] = []

    def transport(method, url, headers, body, timeout):
        seen.append((json.loads(body), timeout))
        return HttpResponse(200, json.dumps({
            'role': 'assistant',
            'content': [
                {'type': 'thinking', 'thinking': 'proxy stripped signature'},
                {'type': 'text', 'text': '{"ok": true}'},
            ],
            'stop_reason': 'end_turn',
            'usage': {'input_tokens': 5, 'output_tokens': 3},
        }))

    provider = _anthropic_provider(transport, max_tokens=2048, timeout_seconds=60.0)
    turn = ModelTurnInput('sys', 'q', (), (), max_output_tokens=333, timeout_seconds=12.5)
    resp = provider.complete_turn(turn)
    assert extract_assistant_text(resp.message) == '{"ok": true}'
    assert seen[-1][0]['max_tokens'] == 333
    assert seen[-1][1] == 12.5

    cancelled = Event()
    cancelled.set()
    with pytest.raises(ProviderFailure) as cancel_exc:
        provider.complete_turn(ModelTurnInput('sys', 'q', (), (), cancel_event=cancelled))
    assert cancel_exc.value.code == 'provider_cancelled'
    assert len(seen) == 1

    with pytest.raises(ProviderFailure) as budget_exc:
        provider.complete_turn(ModelTurnInput('sys', 'q', (), (), max_output_tokens=0))
    assert budget_exc.value.code == 'provider_budget_exhausted'


def test_openai_compatible_schema_followup_list_parts_and_personal_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_compatible, '_require_public_dns', lambda *_: None)
    personal_settings = OpenAICompatibleSettings.from_account_values(
        base_url='https://model.example.test/v1',
        model='deepseek-reasoner',
        api_key='sk-personal',
    )
    personal_settings = OpenAICompatibleSettings(
        base_url=personal_settings.base_url,
        model=personal_settings.model,
        allowed_hosts=personal_settings.allowed_hosts,
        api_key=personal_settings.api_key,
        personal=True,
        tools_verified=True,
    )

    sent: list[tuple[dict, float]] = []
    replies = iter([
        # Turn 0 (with tools): thinking model returns tool_calls + reasoning_content
        HttpResponse(200, json.dumps({
            'choices': [{
                'finish_reason': 'tool_calls',
                'message': {
                    'role': 'assistant',
                    'content': None,
                    'reasoning_content': 'step-by-step reasoning',
                    'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{"q":"NVDA"}'}}],
                },
            }],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 4, 'total_tokens': 14},
        })),
        # Turn 1 (no tools, structured response_schema): proxy returns list content parts + tool_calls: null
        HttpResponse(200, json.dumps({
            'choices': [{
                'finish_reason': 'stop',
                'message': {
                    'role': 'assistant',
                    'content': [{'type': 'text', 'text': '{"tasks": ["a"]}'}],
                    'tool_calls': None,
                },
            }],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 6, 'total_tokens': 18},
        })),
        # Turn 2 (non-tool repair follow-up)
        HttpResponse(200, json.dumps({
            'choices': [{
                'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': '{"tasks": ["a", "b"]}'},
            }],
            'usage': {'prompt_tokens': 15, 'completion_tokens': 7, 'total_tokens': 22},
        })),
    ])

    def transport(method, url, headers, body, timeout):
        sent.append((json.loads(body), timeout))
        return next(replies)

    adapter = ChatResearchAdapter(OpenAICompatibleLlmProvider(personal_settings, transport=transport))
    tool = ModelToolDefinition('lookup', 'Lookup', {'type': 'object', 'properties': {'q': {'type': 'string'}}})

    # Turn 0: tools non-empty -> MUST NOT send response_format, preserves reasoning_content in personal mode
    r0 = adapter.complete_turn(ModelTurnInput(
        'sys', 'q', (tool,), (), response_schema=SAMPLE_SCHEMA, response_schema_name='ResearchDispatchPlan',
        max_output_tokens=500, timeout_seconds=15.0,
    ))
    assert 'response_format' not in sent[0][0]
    assert sent[0][0]['max_tokens'] == 500
    assert sent[0][1] == 15.0
    assert r0.message['reasoning_content'] == 'step-by-step reasoning'

    # Turn 1: tools empty + response_schema -> sends response_format={"type": "json_object"} + schema fallback + tool_calls: null + list content
    ex_tool = ModelTurnExchange(ModelAssistantContinuation(r0.message), (ModelToolResult('c1', '{"ok":1}'),))
    r1 = adapter.complete_turn(ModelTurnInput(
        'sys', 'q', (), (ex_tool,), response_schema=SAMPLE_SCHEMA, response_schema_name='ResearchDispatchPlan',
    ))
    assert sent[1][0]['response_format'] == {'type': 'json_object'}
    assert 'JSON Schema:' in sent[1][0]['messages'][0]['content']
    assert sent[1][0]['messages'][2]['reasoning_content'] == 'step-by-step reasoning'
    assert extract_assistant_text(r1.message) == '{"tasks": ["a"]}'

    # Turn 2: non-tool repair follow-up turn
    ex_repair = ModelTurnExchange(
        ModelAssistantContinuation(r1.message),
        (),
        user_followup='Add second worker task.',
    )
    r2 = adapter.complete_turn(ModelTurnInput(
        'sys', 'q', (), (ex_tool, ex_repair), response_schema=SAMPLE_SCHEMA, response_schema_name='ResearchDispatchPlan',
    ))
    assert [m['role'] for m in sent[2][0]['messages']] == ['system', 'user', 'assistant', 'tool', 'assistant', 'user']
    assert sent[2][0]['messages'][-1] == {'role': 'user', 'content': 'Add second worker task.'}
    assert extract_assistant_text(r2.message) == '{"tasks": ["a", "b"]}'

    # Cancel event on turn prevents submission
    ev = Event()
    ev.set()
    with pytest.raises(ProviderFailure) as cancel_exc:
        adapter.complete_turn(ModelTurnInput('sys', 'q', (), (), cancel_event=ev))
    assert cancel_exc.value.code == 'provider_cancelled'
    assert len(sent) == 3


def test_openai_compatible_strict_json_schema_and_529_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://example.com/v1/chat/completions',
        'model': 'deployment',
        'version': 'v1',
        'mode': 'json_schema',
        'schema_dialect': 'required_nullable_v1',
        'native_tools': True,
    }))
    settings = OpenAICompatibleSettings.from_values(
        base_url='https://example.com/v1', model='deployment', api_key='sk-test', allowlist='example.com',
    )
    sent: list[dict] = []

    def transport(method, url, headers, body, timeout):
        sent.append(json.loads(body))
        return HttpResponse(200, json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{"ok": true}'}}]}))

    adapter = ChatResearchAdapter(OpenAICompatibleLlmProvider(settings, transport=transport))
    # Strict schema uses json_schema wire format without duplicating schema in system
    adapter.complete_turn(ModelTurnInput('sys', 'q', (), (), response_schema=STRICT_SCHEMA, response_schema_name='StrictCheck'))
    assert sent[-1]['response_format'] == {
        'type': 'json_schema',
        'json_schema': {'name': 'StrictCheck', 'strict': True, 'schema': STRICT_SCHEMA},
    }
    assert sent[-1]['messages'][0]['content'] == 'sys'

    # Non-strict schema (with non-null default) gracefully falls back to json_object + system schema instruction
    adapter.complete_turn(ModelTurnInput('sys', 'q', (), (), response_schema=SAMPLE_SCHEMA, response_schema_name='ResearchDispatchPlan'))
    assert sent[-1]['response_format'] == {'type': 'json_object'}
    assert 'JSON Schema:' in sent[-1]['messages'][0]['content']

    # HTTP 529 is retryable
    p_529 = OpenAICompatibleLlmProvider(settings, transport=lambda *a: HttpResponse(529, '{"error":{"type":"overloaded"}}'))
    with pytest.raises(ProviderFailure) as exc_529:
        ChatResearchAdapter(p_529).complete_turn(ModelTurnInput('sys', 'q', (), ()))
    assert exc_529.value.retryable is True


def test_openai_responses_reasoning_fallback_followup_schema_and_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_compatible, '_require_public_dns', lambda *_: None)
    settings = openai_compatible.OpenAICompatibleSettings.from_account_values(
        base_url='https://model.example.test/v1',
        model='gpt-5-test',
        api_key='sk-resp',
    )
    settings = OpenAICompatibleSettings(
        base_url=settings.base_url,
        model=settings.model,
        allowed_hosts=settings.allowed_hosts,
        api_key=settings.api_key,
        personal=True,
        tools_verified=True,
    )
    provider = OpenAIResponsesProvider(settings)

    sent: list[tuple[dict, float]] = []
    replies = iter([
        # Turn 0: reasoning WITHOUT encrypted_content + ancillary web_search_call + text output part
        HttpResponse(200, json.dumps({
            'status': 'completed',
            'output': [
                {'type': 'reasoning', 'id': 'rs_no_enc', 'summary': [{'type': 'summary_text', 'text': 'thought'}]},
                {'type': 'web_search_call', 'id': 'ws_1', 'status': 'completed'},
                {'type': 'message', 'id': 'm_1', 'role': 'assistant', 'content': [{'type': 'text', 'text': '{"bad": true}'}]},
            ],
            'usage': {'input_tokens': 20, 'output_tokens': 10, 'total_tokens': 30},
        })),
        # Turn 1: after user_followup repair turn, succeeds
        HttpResponse(200, json.dumps({
            'status': 'completed',
            'output': [
                {'type': 'reasoning', 'id': 'rs_enc', 'summary': [], 'encrypted_content': 'enc-blob'},
                {'type': 'message', 'id': 'm_2', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '{"tasks": ["a"]}'}]},
            ],
            'usage': {'input_tokens': 30, 'output_tokens': 12, 'total_tokens': 42},
        })),
    ])

    def transport(method, url, headers, body, timeout, **kwargs):
        sent.append((json.loads(body), timeout))
        return next(replies)

    monkeypatch.setattr(openai_responses, '_default_transport', transport)

    r0 = provider.complete_turn(ModelTurnInput(
        'sys', 'Analyze AAPL.', (), (),
        response_schema=SAMPLE_SCHEMA,
        response_schema_name='ResearchDispatchPlan',
        max_output_tokens=400,
        timeout_seconds=18.0,
    ))
    assert extract_assistant_text(r0.message) == '{"bad": true}'
    # Unencrypted reasoning item was omitted from replayable output so store=False Turn 2 never fails
    assert all(item.get('type') != 'reasoning' for item in r0.message['output'])
    assert sent[0][0]['text'] == {'format': {'type': 'json_object'}}
    assert sent[0][0]['max_output_tokens'] == 400
    assert sent[0][1] == 18.0
    assert 'JSON Schema:' in sent[0][0]['instructions']

    # Turn 1: non-tool user_followup repair turn
    ex = ModelTurnExchange(
        ModelAssistantContinuation(r0.message),
        (),
        user_followup='Missing required field tasks.',
    )
    r1 = provider.complete_turn(ModelTurnInput(
        'sys', 'Analyze AAPL.', (), (ex,),
        response_schema=SAMPLE_SCHEMA,
        response_schema_name='ResearchDispatchPlan',
    ))
    assert extract_assistant_text(r1.message) == '{"tasks": ["a"]}'
    assert any(item.get('encrypted_content') == 'enc-blob' for item in r1.message['output'])
    assert sent[1][0]['input'] == [
        {'role': 'user', 'content': 'Analyze AAPL.'},
        r0.message['output'][0],
        {'role': 'user', 'content': 'Missing required field tasks.'},
    ]

    # When tools are non-empty, text.format is omitted on wire while instructions preserve schema hint
    tool = ModelToolDefinition('lookup', 'Lookup', {'type': 'object'})
    wire_with_tools = provider._wire(ModelTurnInput(
        'sys', 'q', (tool,), (), response_schema=SAMPLE_SCHEMA, response_schema_name='ResearchDispatchPlan',
    ))
    assert 'text' not in wire_with_tools
    assert 'JSON Schema:' in wire_with_tools['instructions']

    # turn.cancel_event aborts cleanly before transport
    ev = Event()
    ev.set()
    with pytest.raises(ProviderFailure) as cancel_exc:
        provider.complete_turn(ModelTurnInput('sys', 'q', (), (), cancel_event=ev))
    assert cancel_exc.value.code == 'provider_cancelled'
    assert len(sent) == 2


def test_openai_compatible_sse_list_parts_and_personal_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_compatible, '_require_public_dns', lambda *_: None)
    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    # Personal mode has continuation=None; SSE stream with list delta content & reasoning_content must work.
    personal = OpenAICompatibleSettings(
        base_url='https://model.example.test/v1',
        model='glm-thinking',
        allowed_hosts=frozenset({('model.example.test', 443)}),
        api_key='sk-personal',
        personal=True,
        tools_verified=True,
    )
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://model.example.test/v1/chat/completions',
        'model': 'glm-thinking',
        'version': 'v1',
        'mode': 'json_object',
        'native_tools': True,
        'stream': True,
        'usage': True,
    }))
    # Note: personal=True uses research_profile which sets stream=None; test via explicit stream_transport + non-personal None continuation check,
    # and verify personal=True + capability profile stream=True behavior:
    chunks = [
        {'choices': [{'index': 0, 'delta': {'role': 'assistant', 'reasoning_content': 'step 1 '}}]},
        {'choices': [{'index': 0, 'delta': {'reasoning_content': 'step 2', 'content': [{'type': 'text', 'text': '{"tasks": ["w1"]}'}]}}]},
        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
        {'choices': [], 'usage': {'prompt_tokens': 8, 'completion_tokens': 4, 'total_tokens': 12}},
    ]

    def stream_transport(method, url, headers, body, timeout, *, on_frame):
        for chunk in chunks:
            on_frame('', json.dumps(chunk))
        on_frame('', '[DONE]')
        return None

    # Force stream=True by checking profile.stream on non-personal profile with continuation=None,
    # AND test personal=True with research_capacity monkeypatched to stream=True, continuation=None.
    p = OpenAICompatibleLlmProvider(personal, stream_transport=stream_transport)
    monkeypatch.setattr(
        p,
        'research_capacity',
        lambda: SimpleNamespace(
            mode='json_object',
            native_tools=True,
            stream=True,
            usage=True,
            continuation=None,
            continuation_fields=None,
            generation_parameters=None,
        ),
    )
    emitted: list[dict] = []
    res = ChatResearchAdapter(p).complete_turn(
        ModelTurnInput('sys', 'q', (), (), response_schema=SAMPLE_SCHEMA, response_schema_name='Plan'),
        on_stream_event=emitted.append,
    )
    assert extract_assistant_text(res.message) == '{"tasks": ["w1"]}'
    assert res.message['reasoning_content'] == 'step 1 step 2'
    assert ''.join(e['delta'] for e in emitted if e['channel'] == 'thinking') == 'step 1 step 2'


def test_anthropic_combined_tool_result_and_user_followup_and_minimal_chat_provider() -> None:
    # 1. Combined tool_result + user_followup on the same Anthropic exchange keeps valid user block list
    sent_anth: list[dict] = []
    provider = _anthropic_provider(lambda m, u, h, b, t: (
        sent_anth.append(json.loads(b)),
        HttpResponse(200, json.dumps({
            'role': 'assistant',
            'content': [{'type': 'text', 'text': '{"ok": true}'}],
            'stop_reason': 'end_turn',
            'usage': {'input_tokens': 5, 'output_tokens': 2},
        })),
    )[1])
    ex = ModelTurnExchange(
        ModelAssistantContinuation({'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'tu_1', 'name': 'lookup', 'input': {}}]}),
        (ModelToolResult('tu_1', '{"v": 1}'),),
        user_followup='Wrap up now without calling more tools.',
    )
    provider.complete_turn(ModelTurnInput('sys', 'q', (), (ex,), response_schema=STRICT_SCHEMA, response_schema_name='Final'))
    assert sent_anth[-1]['messages'] == [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'tu_1', 'name': 'lookup', 'input': {}}]},
        {
            'role': 'user',
            'content': [
                {'type': 'tool_result', 'tool_use_id': 'tu_1', 'content': '{"v": 1}', 'is_error': False},
                {'type': 'text', 'text': 'Wrap up now without calling more tools.'},
            ],
        },
    ]

    # 2. ChatResearchAdapter wrapping minimal provider (no kwargs) still enforces turn cancel/budget and injects schema
    captured_msgs: list[list[dict]] = []

    class MinimalProvider:
        def complete(self, messages, tools):
            captured_msgs.append(messages)
            return SimpleNamespace(
                message={'role': 'assistant', 'content': '{"ok": true}'},
                tool_calls=(),
                usage=None,
                finish_reason='stop',
            )

    adapter = ChatResearchAdapter(MinimalProvider())
    res = adapter.complete_turn(ModelTurnInput('sys', 'q', (), (), response_schema=STRICT_SCHEMA, response_schema_name='Minimal'))
    assert extract_assistant_text(res.message) == '{"ok": true}'
    assert 'JSON Schema:' in captured_msgs[0][0]['content']

    ev = Event()
    ev.set()
    with pytest.raises(ProviderFailure) as c_exc:
        adapter.complete_turn(ModelTurnInput('sys', 'q', (), (), cancel_event=ev))
    assert c_exc.value.code == 'provider_cancelled'

    with pytest.raises(ProviderFailure) as t_exc:
        adapter.complete_turn(ModelTurnInput('sys', 'q', (), (), timeout_seconds=0))
    assert t_exc.value.code == 'provider_budget_exhausted'

    with pytest.raises(ProviderFailure) as m_exc:
        adapter.complete_turn(ModelTurnInput('sys', 'q', (), (), max_output_tokens=0))
    assert m_exc.value.code == 'provider_budget_exhausted'

