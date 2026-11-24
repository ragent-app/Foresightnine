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
    assert 'json' in json.dumps(sent[0][0]['input']).lower()
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
        {
            'role': 'user',
            'content': 'Missing required field tasks.\n\nRespond with a valid JSON object matching the schema in instructions.',
        },
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


def test_openai_responses_json_word_400_fallbacks_and_replay_pairing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_compatible, '_require_public_dns', lambda *_: None)
    from app.provider_capabilities import ChatGenerationParameters

    settings = OpenAICompatibleSettings(
        base_url='https://model.example.test/v1',
        model='gpt-5.6-terra',
        allowed_hosts=frozenset({('model.example.test', 443)}),
        api_key='sk-resp',
        personal=True,
        tools_verified=True,
        generation_parameters=ChatGenerationParameters(reasoning_effort='medium', temperature=0.2),
    )
    provider = OpenAIResponsesProvider(settings)

    # 1) Already has 'json' in input -> not duplicated; missing 'json' -> injected
    wire_has_json = provider._wire(ModelTurnInput(
        'sys', 'Return valid JSON for AAPL.', (), (),
        response_schema=SAMPLE_SCHEMA, response_schema_name='Plan',
    ))
    assert wire_has_json['input'] == [{'role': 'user', 'content': 'Return valid JSON for AAPL.'}]
    assert wire_has_json['text'] == {'format': {'type': 'json_object'}}

    # 2) Fallback cascade on 400: include rejected -> temperature rejected alongside reasoning -> text.format rejected
    sent_wires: list[dict] = []
    replies = iter([
        HttpResponse(400, json.dumps({'error': {'type': 'invalid_request_error', 'param': 'include', 'message': 'Unsupported include reasoning.encrypted_content'}})),
        HttpResponse(400, json.dumps({'error': {'type': 'invalid_request_error', 'param': 'temperature', 'message': 'temperature is not supported with reasoning.effort'}})),
        HttpResponse(400, json.dumps({'error': {'type': 'invalid_request_error', 'param': 'text.format', 'message': 'text.format json_object rejected'}})),
        HttpResponse(200, json.dumps({
            'status': 'completed',
            'output': [
                {'type': 'reasoning', 'id': 'rs_1', 'encrypted_content': 'enc-only-no-summary'},
                {'type': 'message', 'id': 'm_1', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '{"tasks": ["w1"]}'}]},
            ],
            'usage': {'input_tokens': 12, 'output_tokens': 6, 'total_tokens': 18},
        })),
    ])

    def transport(method, url, headers, body, timeout, **kwargs):
        sent_wires.append(json.loads(body))
        return next(replies)

    monkeypatch.setattr(openai_responses, '_default_transport', transport)
    resp = provider.complete_turn(ModelTurnInput(
        'sys', 'Analyze NVDA earnings.', (), (),
        response_schema=SAMPLE_SCHEMA, response_schema_name='Plan',
    ))
    assert extract_assistant_text(resp.message) == '{"tasks": ["w1"]}'
    assert len(sent_wires) == 4
    assert 'include' in sent_wires[0] and 'temperature' in sent_wires[0] and 'text' in sent_wires[0]
    assert 'include' not in sent_wires[1] and 'temperature' in sent_wires[1]
    assert 'temperature' not in sent_wires[2] and 'text' in sent_wires[2] and sent_wires[2]['reasoning'] == {'effort': 'medium'}
    assert 'text' not in sent_wires[3] and 'JSON Schema:' in sent_wires[3]['instructions']
    assert provider.include_refused is True
    # Missing summary on reasoning item defaulted to [] on parse so replay stays valid
    reasoning_items = [item for item in resp.message['output'] if item.get('type') == 'reasoning']
    assert reasoning_items and reasoning_items[0]['summary'] == []

    # 3) Unpaired function_call / orphan function_call_output stripped during multi-turn replay
    broken_ex = ModelTurnExchange(
        ModelAssistantContinuation({
            'role': 'assistant',
            'content': '',
            'output': [
                {'type': 'function_call', 'call_id': 'c_ok', 'name': 'lookup', 'arguments': '{}'},
                {'type': 'function_call', 'call_id': 'c_unpaired', 'name': 'lookup', 'arguments': '{}'},
            ],
        }),
        (
            ModelToolResult('c_ok', '{"x": 1}'),
            ModelToolResult('c_orphan_result', '{"x": 2}'),
        ),
    )
    repaired_wire = provider._wire(ModelTurnInput('sys', 'q', (), (broken_ex,)))
    call_ids = [item.get('call_id') for item in repaired_wire['input'] if item.get('type') == 'function_call']
    out_ids = [item.get('call_id') for item in repaired_wire['input'] if item.get('type') == 'function_call_output']
    assert call_ids == ['c_ok'] and out_ids == ['c_ok']

    # 4) json_schema 400 fallback injects schema note into instructions
    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://model.example.test/v1/responses',
        'model': 'gpt-5.6-terra',
        'version': 'v1',
        'mode': 'json_schema',
        'schema_dialect': 'required_nullable_v1',
        'native_tools': True,
    }))
    schema_wires: list[dict] = []
    schema_replies = iter([
        HttpResponse(400, json.dumps({'error': {'type': 'invalid_request_error', 'code': 'invalid_json_schema', 'param': 'text.format', 'message': 'Invalid schema'}})),
        HttpResponse(200, json.dumps({
            'status': 'completed',
            'output': [{'type': 'message', 'id': 'm_js', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '{"ok": true}'}]}],
        })),
    ])
    monkeypatch.setattr(
        openai_responses,
        '_default_transport',
        lambda m, u, h, b, t, **kw: (schema_wires.append(json.loads(b)), next(schema_replies))[1],
    )
    p_js = OpenAIResponsesProvider(OpenAICompatibleSettings(
        base_url='https://model.example.test/v1',
        model='gpt-5.6-terra',
        allowed_hosts=frozenset({('model.example.test', 443)}),
        api_key='sk-resp',
        personal=False,
        tools_verified=True,
    ))
    js_res = p_js.complete_turn(ModelTurnInput('sys', 'Check status.', (), (), response_schema=STRICT_SCHEMA, response_schema_name='OkOut'))
    assert extract_assistant_text(js_res.message) == '{"ok": true}'
    assert schema_wires[0]['text']['format']['type'] == 'json_schema'
    assert 'text' not in schema_wires[1] and 'JSON Schema:' in schema_wires[1]['instructions']


def test_chat_completions_json_word_response_format_max_completion_and_pairing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_compatible, '_require_public_dns', lambda *_: None)
    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://model.example.test/v1/chat/completions',
        'model': 'o-reasoning',
        'version': 'v1',
        'mode': 'json_schema',
        'schema_dialect': 'required_nullable_v1',
        'native_tools': True,
        'stream': False,
        'usage': True,
    }))
    sent: list[dict] = []
    replies = iter([
        HttpResponse(400, json.dumps({'error': {'type': 'invalid_request_error', 'param': 'max_tokens', 'message': "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens'."}})),
        HttpResponse(400, json.dumps({'error': {'type': 'invalid_request_error', 'param': 'response_format', 'message': 'response_format json_schema rejected'}})),
        HttpResponse(200, json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{"ok": true}'}}]})),
    ])

    def transport(m, u, h, b, t):
        sent.append(json.loads(b))
        return next(replies)

    settings = OpenAICompatibleSettings(
        base_url='https://model.example.test/v1',
        model='o-reasoning',
        allowed_hosts=frozenset({('model.example.test', 443)}),
        api_key='sk-chat',
        personal=False,
        tools_verified=True,
    )
    provider = OpenAICompatibleLlmProvider(settings, transport=transport)
    adapter = ChatResearchAdapter(provider)
    res = adapter.complete_turn(
        ModelTurnInput('sys', 'Run check.', (), (), response_schema=STRICT_SCHEMA, response_schema_name='Ok'),
        max_output_tokens=512,
    )
    assert extract_assistant_text(res.message) == '{"ok": true}'
    assert len(sent) == 3
    assert sent[0]['max_tokens'] == 512 and sent[0]['response_format']['type'] == 'json_schema'
    assert sent[1]['max_completion_tokens'] == 512 and 'max_tokens' not in sent[1]
    assert 'response_format' not in sent[2] and sent[2]['max_completion_tokens'] == 512
    assert 'JSON Schema:' in sent[2]['messages'][0]['content']
    assert provider._prefer_max_completion_tokens is True

    # Direct complete() with json_object and no 'json' word anywhere injects 'json' cleanly
    direct_sent: list[dict] = []
    p_direct = OpenAICompatibleLlmProvider(
        settings,
        transport=lambda m, u, h, b, t: (
            direct_sent.append(json.loads(b)),
            HttpResponse(200, json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{"ok": 1}'}}]})),
        )[1],
    )
    p_direct.complete([{'role': 'system', 'content': 'Reply strictly.'}, {'role': 'user', 'content': 'Compute AAPL.'}], [], response_format={'type': 'json_object'})
    assert 'json' in json.dumps(direct_sent[0]['messages']).lower()

    # Unpaired tool calls / tool results sanitized across ChatResearchAdapter and AnthropicMessagesProvider
    unpaired_chat = ModelTurnExchange(
        ModelAssistantContinuation({
            'role': 'assistant',
            'content': None,
            'tool_calls': [
                {'id': 'c1', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}},
                {'id': 'c_missing', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}},
            ],
        }),
        (ModelToolResult('c1', 'res1'), ModelToolResult('c_orphan', 'res2')),
    )
    chat_msgs, _ = ChatResearchAdapter._render(ModelTurnInput('sys', 'q', (), (unpaired_chat,)))
    assert [c['id'] for c in chat_msgs[2]['tool_calls']] == ['c1']
    assert [m['tool_call_id'] for m in chat_msgs if m['role'] == 'tool'] == ['c1']

    anth_wire = _anthropic_provider()._wire(ModelTurnInput('sys', 'q', (), (
        ModelTurnExchange(
            ModelAssistantContinuation({'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'tu_unpaired', 'name': 'lookup', 'input': {}}]}),
            (),
            user_followup='Retry without tools.',
        ),
    )))
    assert all(
        b.get('type') != 'tool_use'
        for msg in anth_wire['messages']
        if isinstance(msg.get('content'), list)
        for b in msg['content']
    )


def test_safe_rejection_details_redacts_secrets_without_polluting_diagnostics(caplog: pytest.LogCaptureFixture) -> None:
    from app.research_diagnostics import safe_provider_error, safe_rejection_details, sanitize_provider_error, set_rejection_details

    raw_body = json.dumps({
        'error': {
            'type': 'invalid_request_error',
            'code': 'invalid_value',
            'param': 'text.format',
            'message': 'Bad Authorization: Bearer sk-secretkey999999 and api_key=MY_KEY_123 at https://x.test?token=tok_secret on input[0].content',
        }
    })
    safe_err = safe_provider_error(raw_body)
    assert 'sk-secretkey' not in json.dumps(safe_err)
    assert sanitize_provider_error(safe_err)['shape'] == 'error_object'

    details = safe_rejection_details(raw_body, status_code=400, safe_error=safe_err)
    assert details['status_code'] == 400
    assert details['error_type'] == 'invalid_request_error'
    assert details['error_code'] == 'invalid_value'
    assert details['error_param'] == 'text.format'
    assert 'sk-secretkey' not in details['message']
    assert 'MY_KEY_123' not in details['message']
    assert 'tok_secret' not in details['message']
    assert len(details['message']) <= 240

    failure = ProviderFailure('provider_request_rejected', 'Provider rejected the request.', retryable=False)
    resp = HttpResponse(400, '', {}, safe_err, details)
    set_rejection_details(failure, resp)
    assert failure.upstream_status == 400
    assert failure.parameter == 'text.format'
    assert failure.rejection_details == details


