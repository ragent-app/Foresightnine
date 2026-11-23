"""Unit and end-to-end SSE tests for L3 native model streaming and draft preview."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import socket
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn

import app.main as api
import app.openai_compatible as openai_compatible
from app.anthropic_messages import AnthropicMessagesProvider
from app.chat_research_adapter import ChatResearchAdapter
from app.model_protocol import ModelToolDefinition, ModelTurnInput
from app.openai_compatible import (
    HttpResponse,
    OpenAICompatibleLlmProvider,
    OpenAICompatibleSettings,
    _SseLineParser,
)
from app.phase3 import ProviderFailure, ProviderPolicy
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext
from app.research_live_stream import DeltaCoalescer
from app.research_runtime import run_research_agent
from app.research_sources import ResearchSource, SourcePermissions
from app.research_stream_extract import TopLevelReportExtractor
from tests.test_research_live_stream import _parse_sse_frames, _read_sse_frames, live_server  # noqa: F401
from tests.test_research_multiturn import TEXT, env  # noqa: F401


def _set_profile(monkeypatch, *, endpoint: str, model: str, stream: bool | None = True,
                 usage: bool | None = True, native_tools: bool = True,
                 continuation: bool | None = None, continuation_fields: list[str] | None = None,
                 generation_parameters: list[str] | None = None) -> None:
    profile: dict[str, object] = {
        'endpoint': endpoint,
        'model': model,
        'version': 'v1',
        'mode': 'json_object',
        'native_tools': native_tools,
    }
    if stream is not None:
        profile['stream'] = stream
    if usage is not None:
        profile['usage'] = usage
    if continuation is not None:
        profile['continuation'] = continuation
    if continuation_fields is not None:
        profile['continuation_fields'] = continuation_fields
    if generation_parameters is not None:
        profile['generation_parameters'] = generation_parameters
    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps(profile))


def _sample_turn() -> ModelTurnInput:
    return ModelTurnInput(
        'SYSTEM SECRET INSTRUCTION',
        'Analyze revenue.',
        (
            ModelToolDefinition('read_sources', 'Read', {'type': 'object'}),
            ModelToolDefinition('submit_research', 'Submit', {'type': 'object'}),
        ),
        (),
    )


def _research_source() -> ResearchSource:
    return ResearchSource(
        source_id='s1',
        owner_id='owner',
        title='Annual Report',
        text=TEXT,
        url=None,
        kind='user_supplied',
        obtained_at=datetime.now(timezone.utc),
        content_sha256='hash-s1',
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
    )


def _runtime_context(*, op_id: str = 'op-l3') -> ResearchExecutionContext:
    ctx = ResearchExecutionContext(
        ResearchExecutionBudget(None, None, None, float('inf')),
        False,
        lambda: None,
        lambda: None,
        lambda *_: None,
        lambda *_: None,
        lambda: None,
    )
    ctx.diagnostic_context['operation_id'] = op_id
    return ctx


# ---------------------------------------------------------------------------
# 1. Incremental JSON "report" extractor tests
# ---------------------------------------------------------------------------

def test_report_extractor_handles_splits_escapes_surrogates_and_nested_keys():
    payload = {
        'rationale': 'drafting',
        'status': 'completed',
        'claims': [{'text': 'c1', 'report': 'NESTED_BEFORE_MUST_NOT_EMIT'}],
        'report': 'Line 1 "quoted" \\ slash / \b\f\n\r\t 中文 😀 end.',
        'limitations': ['No guidance'],
        'stopping_reason': 'answered',
        'extra': {'report': 'NESTED_AFTER_MUST_NOT_EMIT'},
    }
    raw = json.dumps(payload, ensure_ascii=True)
    assert '\\ud83d\\ude00' in raw

    # Feed 1 character at a time (worst-case split across every key, escape, \uXXXX, and surrogate pair).
    ext = TopLevelReportExtractor()
    emitted: list[str] = []
    for ch in raw:
        piece = ext.feed(ch)
        if piece:
            piece.encode('utf-8')
            emitted.append(piece)
    assert ''.join(emitted) == payload['report']
    assert ext.finished is True
    assert ext.failed is False


def test_report_extractor_supports_escaped_key_and_non_string_values():
    raw_escaped_key = ' {"\\u0072eport": "ok \\"here\\"", "claims": [{"report": "ignore"}]}'
    ext = TopLevelReportExtractor()
    out = ''.join(ext.feed(raw_escaped_key[i:i + 2]) for i in range(0, len(raw_escaped_key), 2))
    assert out == 'ok "here"'
    assert ext.finished is True

    for non_str in ('{"report": null}', '{"report": 123}', '{"report": {"report": "nested"}}', '{"report": ["a"]}', '{"report": ""}'):
        e2 = TopLevelReportExtractor()
        assert e2.feed(non_str) == ''
        assert e2.finished is True
        assert e2.failed is False


@pytest.mark.parametrize('bad_json', [
    '["not an object"]',
    '{"report": "bad \\q escape"}',
    '{"report": "\\ud83d plain text"}',
    '{"report": "\\ud83d"}',
    '{"report": "\\udc00"}',
    '{"report": "\\u12zz"}',
    '{"report": "raw\x01ctrl"}',
    '{"claims": [}',
])
def test_report_extractor_stops_silently_on_malformed_json_without_surrogates(bad_json):
    ext = TopLevelReportExtractor()
    pieces = [ext.feed(ch) for ch in bad_json]
    joined = ''.join(pieces)
    joined.encode('utf-8')
    assert ext.failed is True
    assert ext.feed('more text') == ''


# ---------------------------------------------------------------------------
# 2. SSE line parser & _default_stream_transport safety tests
# ---------------------------------------------------------------------------

def test_sse_line_parser_normalizes_crlf_cr_lf_and_pings():
    parser = _SseLineParser()
    items = []
    items.extend(parser.feed_text(': keepalive\r\nevent: content_block_delta\rdata: {"a":'))
    items.extend(parser.feed_text('1}\r\n\r\ndata: line1\ndata: line2\n\n'))
    items.extend(parser.finish())
    assert items == [
        ('ping', '', ''),
        ('frame', 'content_block_delta', '{"a":1}'),
        ('frame', '', 'line1\nline2'),
    ]


def test_stream_transport_preserves_presend_not_started_and_post_send_invariants(monkeypatch):
    endpoint = 'https://example.com/v1/chat/completions'
    _set_profile(monkeypatch, endpoint=endpoint, model='m', stream=True, usage=True)
    monkeypatch.setenv('FORESIGHTNINE_LLM_BASE_URL', 'https://example.com/v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_MODEL', 'm')
    monkeypatch.setenv('FORESIGHTNINE_LLM_API_KEY', 'secret-token')
    monkeypatch.setenv('FORESIGHTNINE_LLM_ALLOWED_HOSTS', 'example.com')

    # Pre-send DNS failure stays not_started -> retryable before billing
    monkeypatch.setattr(openai_compatible, '_validated_addresses', lambda *a, **k: (_ for _ in ()).throw(openai_compatible._TransportFailure()))
    snaps = []
    with pytest.raises(ProviderFailure) as caught:
        OpenAICompatibleLlmProvider.from_environment().complete(
            [{'role': 'user', 'content': 'hi'}],
            [],
            on_stream_event=lambda _ev: None,
            diagnostic_callback=snaps.append,
        )
    assert caught.value.code == 'provider_transport_error'
    assert getattr(caught.value, 'provider_response_received', False) is False
    assert snaps[0]['submission_state'] == 'not_started'

    # Invalid stream idle timeout fails before submission
    monkeypatch.setenv('FORESIGHTNINE_LLM_STREAM_IDLE_TIMEOUT_SECONDS', 'nan')
    snaps.clear()
    with pytest.raises(ProviderFailure) as bad_cfg:
        OpenAICompatibleLlmProvider.from_environment().complete(
            [{'role': 'user', 'content': 'hi'}],
            [],
            on_stream_event=lambda _ev: None,
            diagnostic_callback=snaps.append,
        )
    assert bad_cfg.value.code == 'provider_configuration_invalid'
    assert getattr(bad_cfg.value, 'provider_response_received', False) is False
    assert snaps[0]['submission_state'] == 'not_started'

    # Mid-stream idle timeout after frame 1 sets provider_response_received=True and calls sock.shutdown
    monkeypatch.setenv('FORESIGHTNINE_LLM_STREAM_IDLE_TIMEOUT_SECONDS', '0.05')
    monkeypatch.setattr(openai_compatible, '_validated_addresses', lambda *a, **k: ('1.1.1.1',))
    shutdown_called = threading.Event()
    unblock = threading.Event()

    class IdleSock:
        def settimeout(self, value): pass
        def shutdown(self, how):
            shutdown_called.set()
            unblock.set()

    class IdleResponse:
        status = 200
        headers = {'Content-Type': 'text/event-stream'}
        length = None
        def __init__(self): self.calls = 0
        def read1(self, n):
            self.calls += 1
            if self.calls == 1:
                return b'data: {"choices":[{"delta":{"role":"assistant","content":"hi"}}]}\n\n'
            unblock.wait(2.0)
            raise OSError('closed')
        def read(self, n): return b''

    class IdleConn:
        def __init__(self, *a): self.sock = IdleSock()
        def connect(self): pass
        def request(self, *a, **k): pass
        def getresponse(self): return IdleResponse()
        def close(self): unblock.set()

    monkeypatch.setattr(openai_compatible, '_PinnedHTTPSConnection', IdleConn)
    snaps.clear()
    events = []
    try:
        with pytest.raises(ProviderFailure) as idle_exc:
            OpenAICompatibleLlmProvider.from_environment().complete(
                [{'role': 'user', 'content': 'hi'}],
                [],
                on_stream_event=events.append,
                diagnostic_callback=snaps.append,
            )
    finally:
        unblock.set()
    assert idle_exc.value.code == 'provider_timeout'
    assert idle_exc.value.provider_response_received is True
    assert shutdown_called.is_set()
    assert snaps[0]['submission_state'] == 'response_received'
    assert events == [{'channel': 'text', 'delta': 'hi'}]

    # Fast mid-stream cancel calls sock.shutdown within <0.25s and marks provider_response_received=True
    monkeypatch.setenv('FORESIGHTNINE_LLM_STREAM_IDLE_TIMEOUT_SECONDS', '10')
    cancel_ev = threading.Event()
    cancel_shutdown = threading.Event()
    cancel_unblock = threading.Event()

    class CancelSock:
        def settimeout(self, value): pass
        def shutdown(self, how):
            cancel_shutdown.set()
            cancel_unblock.set()

    class CancelResponse:
        status = 200
        headers = {'Content-Type': 'text/event-stream'}
        length = None
        def __init__(self): self.calls = 0
        def read1(self, n):
            self.calls += 1
            if self.calls == 1:
                return b'data: {"choices":[{"delta":{"role":"assistant","content":"part"}}]}\n\n'
            cancel_ev.set()
            cancel_unblock.wait(2.0)
            raise OSError('cancelled')
        def read(self, n): return b''

    class CancelConn:
        def __init__(self, *a): self.sock = CancelSock()
        def connect(self): pass
        def request(self, *a, **k): pass
        def getresponse(self): return CancelResponse()
        def close(self): cancel_unblock.set()

    monkeypatch.setattr(openai_compatible, '_PinnedHTTPSConnection', CancelConn)
    t0 = time.monotonic()
    try:
        with pytest.raises(ProviderFailure) as cancel_exc:
            OpenAICompatibleLlmProvider.from_environment().complete(
                [{'role': 'user', 'content': 'hi'}],
                [],
                cancel_event=cancel_ev,
                on_stream_event=lambda _ev: None,
            )
    finally:
        cancel_unblock.set()
    assert time.monotonic() - t0 < 0.5
    assert cancel_exc.value.code == 'provider_cancelled'
    assert cancel_exc.value.provider_response_received is True
    assert cancel_shutdown.is_set()

    # Non-2xx response bounds error body <= 64 KB and never leaks error text; 2MB raw wire cap triggers failure.
    monkeypatch.delenv('FORESIGHTNINE_LLM_STREAM_IDLE_TIMEOUT_SECONDS', raising=False)

    class ErrorResponse:
        status = 400
        headers = {'Content-Type': 'application/json'}
        def read(self, n): return json.dumps({'error': {'message': 'TOP_SECRET_ERR', 'code': 'invalid_request'}}).encode()

    class ErrConn:
        def __init__(self, *a): self.sock = type('S', (), {'settimeout': lambda *_: None, 'shutdown': lambda *_: None})()
        def connect(self): pass
        def request(self, *a, **k): pass
        def getresponse(self): return ErrorResponse()
        def close(self): pass

    monkeypatch.setattr(openai_compatible, '_PinnedHTTPSConnection', ErrConn)
    err_snaps = []
    with pytest.raises(ProviderFailure) as err_exc:
        OpenAICompatibleLlmProvider.from_environment().complete(
            [{'role': 'user', 'content': 'hi'}],
            [],
            on_stream_event=lambda _ev: None,
            diagnostic_callback=err_snaps.append,
        )
    assert err_exc.value.code == 'provider_request_rejected'
    assert err_exc.value.provider_response_received is True
    assert 'TOP_SECRET_ERR' not in repr(err_snaps)

    # Wire byte cap > 2 MB
    big_chunk = b'data: {"choices":[{"delta":{"role":"assistant","content":"x"}}]}\n\n' * 35_000
    assert len(big_chunk) > openai_compatible._MAX_STREAM_BYTES

    class HugeResponse:
        status = 200
        headers = {'Content-Type': 'text/event-stream'}
        length = None
        def __init__(self): self.offset = 0
        def read1(self, n):
            chunk = big_chunk[self.offset:self.offset + 8192]
            self.offset += len(chunk)
            return chunk
        def read(self, n): return b''

    class HugeConn(ErrConn):
        def getresponse(self): return HugeResponse()

    monkeypatch.setattr(openai_compatible, '_PinnedHTTPSConnection', HugeConn)
    with pytest.raises(ProviderFailure) as huge_exc:
        OpenAICompatibleLlmProvider.from_environment().complete(
            [{'role': 'user', 'content': 'hi'}],
            [],
            on_stream_event=lambda _ev: None,
        )
    assert huge_exc.value.provider_response_received is True


# ---------------------------------------------------------------------------
# 3. Anthropic Messages SSE Streaming Tests
# ---------------------------------------------------------------------------

def _anthropic_settings():
    return SimpleNamespace(base_url='https://example.com/anthropic', model='m-anth', api_key='sk-test', allow_private_network=False)


def test_anthropic_capability_gating_keeps_non_stream_when_stream_false_or_unset(monkeypatch):
    sent = []
    def non_stream_transport(method, url, headers, body, timeout):
        sent.append((headers, json.loads(body)))
        return HttpResponse(200, json.dumps({
            'role': 'assistant',
            'content': [{'type': 'text', 'text': 'hello'}],
            'stop_reason': 'end_turn',
            'usage': {'input_tokens': 5, 'output_tokens': 2},
        }))

    provider = AnthropicMessagesProvider(
        _anthropic_settings(), SimpleNamespace(timeout_seconds=10, max_input_tokens=None),
        anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=200,
        transport=non_stream_transport,
    )
    events = []
    # No profile configured
    res1 = provider.complete_turn(_sample_turn(), on_stream_event=events.append)
    assert res1.message['content'] == [{'type': 'text', 'text': 'hello'}]
    assert 'stream' not in sent[-1][1]
    assert sent[-1][0]['Accept'] == 'application/json'
    assert events == []

    # Profile with stream: False
    _set_profile(monkeypatch, endpoint='https://example.com/anthropic/v1/messages', model='m-anth', stream=False)
    provider.complete_turn(_sample_turn(), on_stream_event=events.append)
    assert 'stream' not in sent[-1][1]
    assert sent[-1][0]['Accept'] == 'application/json'
    assert events == []


def test_anthropic_streaming_reassembly_equivalence_and_privacy(monkeypatch):
    _set_profile(monkeypatch, endpoint='https://example.com/anthropic/v1/messages', model='m-anth', stream=True)
    submit_args = {
        'rationale': 'submit final',
        'status': 'completed',
        'claims': [{'text': 'Revenue 100', 'report': 'NESTED_NO_LEAK', 'source_id': 's1', 'quotation': 'Revenue was 100 USD.'}],
        'report': 'Final summary: "100 USD"\nLine2 😀.',
        'limitations': [],
        'stopping_reason': 'answered',
    }
    expected_blocks = [
        {'type': 'thinking', 'thinking': 'step 1; step 2', 'signature': 'PRIVATE_SIG_999'},
        {'type': 'redacted_thinking', 'data': 'ENCRYPTED_REDACTED_BLOB'},
        {'type': 'text', 'text': 'Checking disclosure now.'},
        {'type': 'tool_use', 'id': 'call_read', 'name': 'read_sources', 'input': {'reads': [{'source_id': 's1'}], 'rationale': 'SECRET_READ_ARG'}},
        {'type': 'tool_use', 'id': 'call_sub', 'name': 'submit_research', 'input': submit_args},
    ]
    non_stream_body = {
        'role': 'assistant',
        'content': expected_blocks,
        'stop_reason': 'tool_use',
        'usage': {'input_tokens': 10, 'output_tokens': 15, 'cache_creation_input_tokens': 3, 'cache_read_input_tokens': 4},
    }
    ns_provider = AnthropicMessagesProvider(
        _anthropic_settings(), SimpleNamespace(timeout_seconds=10, max_input_tokens=None),
        anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=200,
        transport=lambda *_: HttpResponse(200, json.dumps(non_stream_body)),
    )
    expected = ns_provider.complete_turn(_sample_turn())

    submit_json = json.dumps(submit_args, ensure_ascii=True)
    read_json = json.dumps(expected_blocks[3]['input'])
    frames = [
        ('message_start', {'type': 'message_start', 'message': {'role': 'assistant', 'content': [], 'usage': {'input_tokens': 10, 'cache_creation_input_tokens': 3, 'cache_read_input_tokens': 4, 'output_tokens': 1}}}),
        ('ping', {'type': 'ping'}),
        ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}}),
        ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': 'step 1; '}}),
        ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': 'step 2'}}),
        ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'PRIVATE_'}}),
        ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'SIG_999'}}),
        ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
        ('content_block_start', {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'redacted_thinking', 'data': 'ENCRYPTED_REDACTED_BLOB'}}),
        ('content_block_stop', {'type': 'content_block_stop', 'index': 1}),
        ('content_block_start', {'type': 'content_block_start', 'index': 2, 'content_block': {'type': 'text', 'text': ''}}),
        ('content_block_delta', {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'text_delta', 'text': 'Checking disclosure'}}),
        ('content_block_delta', {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'text_delta', 'text': ' now.'}}),
        ('content_block_stop', {'type': 'content_block_stop', 'index': 2}),
        ('content_block_start', {'type': 'content_block_start', 'index': 3, 'content_block': {'type': 'tool_use', 'id': 'call_read', 'name': 'read_sources', 'input': {}}}),
        ('content_block_delta', {'type': 'content_block_delta', 'index': 3, 'delta': {'type': 'input_json_delta', 'partial_json': read_json[:10]}}),
        ('content_block_delta', {'type': 'content_block_delta', 'index': 3, 'delta': {'type': 'input_json_delta', 'partial_json': read_json[10:]}}),
        ('content_block_stop', {'type': 'content_block_stop', 'index': 3}),
        ('content_block_start', {'type': 'content_block_start', 'index': 4, 'content_block': {'type': 'tool_use', 'id': 'call_sub', 'name': 'submit_research', 'input': {}}}),
        *[('content_block_delta', {'type': 'content_block_delta', 'index': 4, 'delta': {'type': 'input_json_delta', 'partial_json': submit_json[i:i + 7]}})
          for i in range(0, len(submit_json), 7)],
        ('content_block_stop', {'type': 'content_block_stop', 'index': 4}),
        ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 15}}),
        ('message_stop', {'type': 'message_stop'}),
    ]

    seen_wire = []
    def mock_stream(method, url, headers, body, timeout, on_frame):
        seen_wire.append((headers, json.loads(body)))
        for ev, payload in frames:
            on_frame(ev, json.dumps(payload))
        return None

    emitted: list[dict] = []
    s_provider = AnthropicMessagesProvider(
        _anthropic_settings(), SimpleNamespace(timeout_seconds=10, max_input_tokens=None),
        anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=200,
        stream_transport=mock_stream,
    )
    actual = s_provider.complete_turn(_sample_turn(), on_stream_event=emitted.append)

    assert seen_wire[0][1]['stream'] is True
    assert seen_wire[0][0]['Accept'] == 'text/event-stream'
    assert actual == expected

    # Strict privacy check on emitted stream events
    serialized_emitted = json.dumps(emitted, ensure_ascii=False)
    for forbidden in ('PRIVATE_SIG_999', 'ENCRYPTED_REDACTED_BLOB', 'SECRET_READ_ARG', 'NESTED_NO_LEAK', 'SYSTEM SECRET INSTRUCTION'):
        assert forbidden not in serialized_emitted

    by_chan = {c: ''.join(e['delta'] for e in emitted if e['channel'] == c) for c in ('thinking', 'text', 'draft_report')}
    assert by_chan['thinking'] == 'step 1; step 2'
    assert by_chan['text'] == 'Checking disclosure now.'
    assert by_chan['draft_report'] == submit_args['report']
    assert [e['delta'] for e in emitted if e['channel'] == 'tool_status'] == [
        '正在准备工具参数：read_sources',
        '正在起草研究报告并整理引文，完成后将自动核验原文位置…',
    ]

    # HTTP 200 application/json fallback works seamlessly
    fallback_provider = AnthropicMessagesProvider(
        _anthropic_settings(), SimpleNamespace(timeout_seconds=10, max_input_tokens=None),
        anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=200,
        stream_transport=lambda *a, **k: HttpResponse(200, json.dumps(non_stream_body), {'content-type': 'application/json'}),
    )
    assert fallback_provider.complete_turn(_sample_turn(), on_stream_event=lambda _ev: None) == expected


@pytest.mark.parametrize('mode,expected_code', [
    ('eof', 'provider_transport_error'),
    ('error_frame', 'provider_unavailable'),
    ('max_tokens', 'provider_output_truncated'),
    ('refusal', 'provider_output_refused'),
])
def test_anthropic_streaming_midstream_failures_mark_received_and_partial_usage(monkeypatch, mode, expected_code):
    _set_profile(monkeypatch, endpoint='https://example.com/anthropic/v1/messages', model='m-anth', stream=True)

    def mock_stream(method, url, headers, body, timeout, on_frame):
        on_frame('message_start', json.dumps({'type': 'message_start', 'message': {'role': 'assistant', 'content': [], 'usage': {'input_tokens': 12, 'output_tokens': 2}}}))
        on_frame('content_block_start', json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': 'hi'}}))
        if mode == 'eof':
            return None
        if mode == 'error_frame':
            on_frame('error', json.dumps({'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'SECRET_MSG'}}))
            return None
        on_frame('content_block_stop', json.dumps({'type': 'content_block_stop', 'index': 0}))
        on_frame('message_delta', json.dumps({'type': 'message_delta', 'delta': {'stop_reason': mode}, 'usage': {'output_tokens': 9}}))
        on_frame('message_stop', json.dumps({'type': 'message_stop'}))
        return None

    provider = AnthropicMessagesProvider(
        _anthropic_settings(), SimpleNamespace(timeout_seconds=10, max_input_tokens=None),
        anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=200,
        stream_transport=mock_stream,
    )
    with pytest.raises(ProviderFailure) as caught:
        provider.complete_turn(_sample_turn(), on_stream_event=lambda _ev: None)
    assert caught.value.code == expected_code
    assert caught.value.provider_response_received is True
    assert caught.value.reported_tokens == (21 if mode in ('max_tokens', 'refusal') else 14)


# ---------------------------------------------------------------------------
# 4. OpenAI Chat Completions SSE Streaming Tests
# ---------------------------------------------------------------------------

def _openai_settings():
    return OpenAICompatibleSettings.from_values(base_url='https://example.com/v1', model='m-oai', api_key='sk-test', allowlist='example.com')


def test_openai_streaming_gating_stream_options_continuation_and_fallback(monkeypatch):
    endpoint = 'https://example.com/v1/chat/completions'

    # 1. stream: False -> non-stream
    _set_profile(monkeypatch, endpoint=endpoint, model='m-oai', stream=False, usage=True)
    sent_ns = []
    ns_body = {
        'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'done'}}],
        'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6},
    }
    p_ns = OpenAICompatibleLlmProvider(
        _openai_settings(),
        transport=lambda m, u, h, b, t: (sent_ns.append(json.loads(b)), HttpResponse(200, json.dumps(ns_body)))[1],
    )
    Adapter = ChatResearchAdapter(p_ns)
    Adapter.complete_turn(_sample_turn(), on_stream_event=lambda _ev: None)
    assert 'stream' not in sent_ns[-1]

    # 2. stream: True, usage: None -> stream=True WITHOUT stream_options
    _set_profile(monkeypatch, endpoint=endpoint, model='m-oai', stream=True, usage=None)
    sent_no_usage = []
    p_no_usage = OpenAICompatibleLlmProvider(
        _openai_settings(),
        stream_transport=lambda m, u, h, b, t, on_frame: (
            sent_no_usage.append(json.loads(b)),
            on_frame('', json.dumps({'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]})),
            on_frame('', '[DONE]'),
        )[-1] and None,
    )
    res_nu = ChatResearchAdapter(p_no_usage).complete_turn(_sample_turn(), on_stream_event=lambda _ev: None)
    assert sent_no_usage[0]['stream'] is True
    assert 'stream_options' not in sent_no_usage[0]
    assert res_nu.message['content'] == 'ok'

    # 3. Unapproved stream_options via generation_parameters rejected before submission
    _set_profile(monkeypatch, endpoint=endpoint, model='m-oai', stream=True, usage=True, generation_parameters=['temperature'])
    monkeypatch.setenv('FORESIGHTNINE_LLM_GENERATION_PARAMETERS', json.dumps({'stream_options': {'include_usage': False}}))
    with pytest.raises(ProviderFailure) as cfg_err:
        p_no_usage.complete([{'role': 'user', 'content': 'hi'}], [], on_stream_event=lambda _ev: None)
    assert cfg_err.value.code == 'provider_configuration_invalid'
    monkeypatch.delenv('FORESIGHTNINE_LLM_GENERATION_PARAMETERS')

    # 4. Full tool_calls + reasoning_content + continuation gating + tail usage
    _set_profile(monkeypatch, endpoint=endpoint, model='m-oai', stream=True, usage=True, continuation=True, continuation_fields=['reasoning_content'])
    submit_payload = {'rationale': 'r', 'status': 'completed', 'claims': [], 'report': 'Draft line 1\nLine 2', 'limitations': [], 'stopping_reason': 'done'}
    submit_str = json.dumps(submit_payload)
    sse_chunks = [
        {'choices': [{'index': 0, 'delta': {'role': 'assistant', 'reasoning_content': 'think A '}}]},
        {'choices': [{'index': 0, 'delta': {'reasoning_content': 'think B', 'content': 'process '}}]},
        {'choices': [{'index': 0, 'delta': {'content': 'note', 'tool_calls': [{'index': 0, 'id': 'tc_read', 'type': 'function', 'function': {'name': 'read_sources', 'arguments': '{"reads":[{"source_id":"s1"}]}'}}]}}]},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 1, 'id': 'tc_sub', 'type': 'function', 'function': {'name': 'submit_research', 'arguments': ''}}]}}]},
        *[{'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 1, 'function': {'arguments': submit_str[i:i + 9]}}]}}]}
          for i in range(0, len(submit_str), 9)],
        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]},
        {'choices': [], 'usage': {'prompt_tokens': 20, 'completion_tokens': 30, 'total_tokens': 50}},
    ]
    sent_full = []
    emitted = []
    p_full = OpenAICompatibleLlmProvider(
        _openai_settings(),
        stream_transport=lambda m, u, h, b, t, on_frame: (
            sent_full.append((h, json.loads(b))),
            [on_frame('', json.dumps(c)) for c in sse_chunks],
            on_frame('', '[DONE]'),
        )[-1] and None,
    )
    res_full = ChatResearchAdapter(p_full).complete_turn(_sample_turn(), on_stream_event=emitted.append)
    assert sent_full[0][1]['stream'] is True
    assert sent_full[0][1]['stream_options'] == {'include_usage': True}
    assert res_full.message['reasoning_content'] == 'think A think B'
    assert res_full.message['content'] == 'process note'
    assert [(c.id, c.name, c.arguments) for c in res_full.tool_calls] == [
        ('tc_read', 'read_sources', {'reads': [{'source_id': 's1'}]}),
        ('tc_sub', 'submit_research', submit_payload),
    ]
    assert res_full.usage.total_tokens == 50
    assert res_full.stop_class == 'tool_calls'
    assert ''.join(e['delta'] for e in emitted if e['channel'] == 'thinking') == 'think A think B'
    assert ''.join(e['delta'] for e in emitted if e['channel'] == 'text') == 'process note'
    assert ''.join(e['delta'] for e in emitted if e['channel'] == 'draft_report') == 'Draft line 1\nLine 2'

    # If continuation does NOT list reasoning_content, it must NOT remain on message
    _set_profile(monkeypatch, endpoint=endpoint, model='m-oai', stream=True, usage=True, continuation=False)
    res_stripped = ChatResearchAdapter(p_full).complete_turn(_sample_turn(), on_stream_event=lambda _ev: None)
    assert 'reasoning_content' not in res_stripped.message

    # JSON HTTP 200 fallback works identically
    p_json_fb = OpenAICompatibleLlmProvider(
        _openai_settings(),
        stream_transport=lambda *a, **k: HttpResponse(200, json.dumps(ns_body), {'content-type': 'application/json'}),
    )
    res_fb = p_json_fb.complete([{'role': 'user', 'content': 'hi'}], [], on_stream_event=lambda _ev: None)
    assert res_fb.message['content'] == 'done'
    assert res_fb.usage.total_tokens == 6

    # Truncated stream without [DONE] sets provider_response_received=True
    p_eof = OpenAICompatibleLlmProvider(
        _openai_settings(),
        stream_transport=lambda m, u, h, b, t, on_frame: (
            on_frame('', json.dumps({'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'half'}}]})),
            None,
        )[-1],
    )
    with pytest.raises(ProviderFailure) as eof_err:
        p_eof.complete([{'role': 'user', 'content': 'hi'}], [], on_stream_event=lambda _ev: None)
    assert eof_err.value.code == 'provider_transport_error'
    assert eof_err.value.provider_response_received is True


# ---------------------------------------------------------------------------
# 5. Coalescer, Pre-send vs Post-send Runtime Retries, and E2E Loopback SSE
# ---------------------------------------------------------------------------

def test_delta_coalescer_batches_by_chars_time_and_channel():
    emitted: list[dict] = []
    coalescer = DeltaCoalescer(call_seq=3, sink=emitted.append, flush_interval_seconds=0.05, flush_chars=10)
    coalescer.push_event({'channel': 'thinking', 'delta': 'aaa'})
    coalescer.push_event({'channel': 'thinking', 'delta': 'bbb'})
    assert emitted == []  # <10 chars, held in buffer
    time.sleep(0.08)
    assert emitted == [{'type': 'delta', 'call_seq': 3, 'channel': 'thinking', 'delta': 'aaabbb', 'seq': 1}]

    coalescer.push_event({'channel': 'text', 'delta': '12345'})
    # Switching to draft_report flushes text immediately
    coalescer.push_event({'channel': 'draft_report', 'delta': '0123456789'})
    # >= 10 chars triggers immediate flush too
    assert emitted[1:] == [
        {'type': 'delta', 'call_seq': 3, 'channel': 'text', 'delta': '12345', 'seq': 2},
        {'type': 'delta', 'call_seq': 3, 'channel': 'draft_report', 'delta': '0123456789', 'seq': 3},
    ]
    coalescer.close()


def test_runtime_emits_retrying_on_presend_and_never_retries_post_send_stream_failure(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRIES', '1')
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRY_BASE_SECONDS', '0.001')

    class PresendThenOkModel:
        def __init__(self): self.calls = 0
        def complete_turn(self, turn, *, cancel_event=None, timeout_seconds=None, max_output_tokens=None, on_submission=None, on_stream_event=None):
            self.calls += 1
            if self.calls == 1:
                ctx_diag = ctx.private_diagnostics[next(reversed(ctx.private_diagnostics))]
                ctx_diag['transport'] = {'submission_state': 'not_started', 'stages': [{'stage': 'dns', 'result': 'error', 'elapsed_ms': 1}]}
                err = ProviderFailure('provider_transport_error', 'dns', retryable=True)
                err.provider_response_received = False
                raise err
            if on_stream_event:
                on_stream_event({'channel': 'thinking', 'delta': 'x' * 70})
            from app.model_protocol import ModelResponse, ModelToolCall, ModelUsage
            args = {'status': 'completed', 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': 's1', 'quotation': 'Revenue was 100 USD.', 'role': 'support'}], 'report': 'Ans', 'limitations': [], 'stopping_reason': 'done'}
            return ModelResponse({'role': 'assistant', 'content': None}, (ModelToolCall('c1', 'submit_research', args),), ModelUsage(1, 1, 2), 'tool_calls', 'tool_calls')

    src = ResearchSource(
        source_id='s1', owner_id='owner', title='Doc', text=TEXT, url=None, kind='user_supplied',
        obtained_at=datetime.now(timezone.utc), content_sha256='h',
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
    )
    ctx = _runtime_context(op_id='op-retry')
    model_ok = PresendThenOkModel()
    events_seen = []
    res = run_research_agent(
        question='Revenue?', sources=[src], model=model_ok, context=ctx,
        initial_state={'user': '{}', 'history': [], 'events': [], 'read_ranges': [{'source_id': 's1', 'start': 0, 'end': len(TEXT)}], 'facts': [], 'calculations': [], 'leads': [], 'acquired_source_ids': [], 'pending': None, 'focus_ranges': []},
        on_stream_event=events_seen.append,
    )
    assert model_ok.calls == 2
    assert res.status == 'completed'
    assert any(e['type'] == 'reset' and e['call_seq'] == 1 and e['reason'] == 'retrying' for e in events_seen)
    assert any(e['type'] == 'delta' and e['call_seq'] == 2 and e['channel'] == 'thinking' for e in events_seen)

    # Post-send mid-stream failure MUST NOT retry!
    class PostsendFailModel:
        def __init__(self): self.calls = 0
        def complete_turn(self, turn, *, cancel_event=None, timeout_seconds=None, max_output_tokens=None, on_submission=None, on_stream_event=None):
            self.calls += 1
            if on_stream_event:
                on_stream_event({'channel': 'thinking', 'delta': 'started streaming'})
            err = ProviderFailure('provider_transport_error', 'midstream cut', retryable=True)
            err.provider_response_received = True
            err.reported_tokens = 11
            raise err

    ctx2 = _runtime_context(op_id='op-noretry')
    model_fail = PostsendFailModel()
    events_fail = []
    res2 = run_research_agent(question='Q', sources=[src], model=model_fail, context=ctx2, on_stream_event=events_fail.append)
    assert model_fail.calls == 1
    assert res2.status == 'partial'
    assert any(e['type'] == 'reset' and e['call_seq'] == 1 and e['reason'] == 'failed' for e in events_fail)


def _install_streaming_anthropic_provider(monkeypatch, env, stream_rounds):
    _set_profile(monkeypatch, endpoint='https://example.com/anthropic/v1/messages', model='m-l3', stream=True)
    idx = {'n': 0}

    def stream_transport(method, url, headers, body, timeout, on_frame):
        round_frames = stream_rounds[idx['n']]
        idx['n'] += 1
        for ev, payload in round_frames(body):
            on_frame(ev, json.dumps(payload))
        return None

    settings = SimpleNamespace(base_url='https://example.com/anthropic', model='m-l3', api_key='sk-test', allow_private_network=False)
    policy = ProviderPolicy(timeout_seconds=10, max_input_tokens=None, max_output_tokens=1000)
    provider = AnthropicMessagesProvider(settings, policy, anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=500, stream_transport=stream_transport)
    env.install(provider)


def _anthropic_round(thinking: str, tool_id: str, tool_name: str, tool_args: dict, *, start_gate: threading.Event | None = None):
    def build(_body):
        if start_gate is not None:
            start_gate.wait(10)
        arg_json = json.dumps(tool_args, ensure_ascii=False)
        frames = [
            ('message_start', {'type': 'message_start', 'message': {'role': 'assistant', 'content': [], 'usage': {'input_tokens': 5, 'output_tokens': 1}}}),
            ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}}),
            ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': thinking}}),
            ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'SIG_HIDDEN'}}),
            ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
            ('content_block_start', {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': tool_id, 'name': tool_name, 'input': {}}}),
        ]
        for i in range(0, len(arg_json), 12):
            frames.append(('content_block_delta', {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': arg_json[i:i + 12]}}))
        frames.extend([
            ('content_block_stop', {'type': 'content_block_stop', 'index': 1}),
            ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 10}}),
            ('message_stop', {'type': 'message_stop'}),
        ])
        return frames
    return build


def test_e2e_l3_live_sse_draft_rejection_and_verified_completion(monkeypatch, env, live_server):
    sid = env.source_id
    release = threading.Event()
    rounds = [
        _anthropic_round('Need to read disclosure first.', 'c1', 'read_sources', {'reads': [{'source_id': sid}], 'rationale': 'read doc'}, start_gate=release),
        _anthropic_round('Drafting initial report...', 'c2', 'submit_research', {
            'status': 'completed',
            'report': 'WRONG DRAFT TO RETRACT',
            'claims': [{'text': 'Bad quote', 'source_id': sid, 'quotation': 'DOES_NOT_EXIST_IN_SOURCE', 'role': 'support'}],
            'limitations': [],
            'stopping_reason': 'done',
        }),
        _anthropic_round('Fixing quotation...', 'c3', 'submit_research', {
            'status': 'completed',
            'report': 'Revenue was 100 USD.',
            'claims': [{'text': 'Revenue was 100 USD.', 'source_id': sid, 'quotation': 'Revenue was 100 USD.', 'role': 'support'}],
            'limitations': [],
            'stopping_reason': 'done',
        }),
    ]
    _install_streaming_anthropic_provider(monkeypatch, env, rounds)
    start_resp = env.start()
    assert start_resp.status_code == 202
    op_id = start_resp.json()['operation']['operation_id']

    async def scenario():
        async with httpx.AsyncClient(base_url=live_server, timeout=10.0) as client:
            async with client.stream('GET', f'{env.path}/runs/{op_id}/events', headers=env.headers) as resp:
                assert resp.status_code == 200
                release.set()
                return await _read_sse_frames(resp)

    try:
        frames = asyncio.run(scenario())
    finally:
        release.set()
    assert env.wait(op_id)['status'] == 'completed'

    deltas = [f['data'] for f in frames if f.get('event') == 'stream.delta']
    resets = [f['data'] for f in frames if f.get('event') == 'stream.reset']
    assert any(d['channel'] == 'thinking' and 'disclosure' in d['delta'] for d in deltas)
    assert any(d['channel'] == 'draft_report' and d['delta'] == 'WRONG DRAFT TO RETRACT' for d in deltas)
    assert any(d['channel'] == 'draft_report' and d['delta'] == 'Revenue was 100 USD.' for d in deltas)
    assert 'SIG_HIDDEN' not in json.dumps(frames)

    assert ('tool_started') in [r['reason'] for r in resets]
    assert ('submission_rejected') in [r['reason'] for r in resets]
    assert ('call_completed') in [r['reason'] for r in resets]

    # DB/checkpoint NEVER persists transient stream deltas or thinking leaks
    stored = api.RESEARCH_RUN_INPUTS.get(op_id)
    checkpoint = stored.get('runtime_checkpoint', {})
    assert all(ev.get('kind') in ('model', 'tool', 'submission') for ev in checkpoint.get('events', []))
    events_dump = json.dumps(checkpoint.get('events', []), ensure_ascii=False)
    assert 'stream.delta' not in json.dumps(stored)
    assert 'Need to read disclosure first.' not in events_dump
    assert 'SIG_HIDDEN' not in events_dump
    assert env.latest()['content']['turns'][-1]['report'] == 'Revenue was 100 USD.'
    assert all(ev.get('kind') in ('model', 'tool', 'submission') for ev in env.latest()['content']['turns'][-1]['events'])


def test_e2e_l3_live_sse_partial_submission_retracts_draft(monkeypatch, env, live_server):
    sid = env.source_id
    release = threading.Event()
    rounds = [
        _anthropic_round('Reading first.', 'c1', 'read_sources', {'reads': [{'source_id': sid}], 'rationale': 'read'}, start_gate=release),
        _anthropic_round('Submitting partial findings.', 'c2', 'submit_research', {
            'status': 'partial',
            'report': 'UNVERIFIED DRAFT ON PARTIAL',
            'claims': [],
            'limitations': ['Insufficient evidence'],
            'stopping_reason': 'evidence_gap',
        }),
    ]
    _install_streaming_anthropic_provider(monkeypatch, env, rounds)
    start_resp = env.start()
    assert start_resp.status_code == 202
    op_id = start_resp.json()['operation']['operation_id']

    async def scenario():
        async with httpx.AsyncClient(base_url=live_server, timeout=10.0) as client:
            async with client.stream('GET', f'{env.path}/runs/{op_id}/events', headers=env.headers) as resp:
                assert resp.status_code == 200
                release.set()
                return await _read_sse_frames(resp)

    try:
        frames = asyncio.run(scenario())
    finally:
        release.set()
    assert env.wait(op_id)['status'] == 'completed'

    deltas = [f['data'] for f in frames if f.get('event') == 'stream.delta']
    reset_reasons = [f['data']['reason'] for f in frames if f.get('event') == 'stream.reset']
    assert any(d['channel'] == 'draft_report' and d['delta'] == 'UNVERIFIED DRAFT ON PARTIAL' for d in deltas)
    assert 'submission_partial' in reset_reasons
    assert reset_reasons.index('submission_partial') < len(frames)
    assert frames[-1]['event'] == 'run.terminal'
    assert frames[-1]['data']['operation_status'] == 'completed'
    assert env.latest()['content']['turns'][-1]['status'] == 'partial'
    assert env.latest()['content']['turns'][-1]['report'] == ''


def test_e2e_l3_live_sse_openai_streaming_provider(monkeypatch, env, live_server):
    sid = env.source_id
    endpoint = 'https://example.com/v1/chat/completions'
    _set_profile(monkeypatch, endpoint=endpoint, model='m-oai', stream=True, usage=True)
    release = threading.Event()
    round_idx = {'n': 0}

    def oai_round_stream(method, url, headers, body, timeout, on_frame):
        n = round_idx['n']
        round_idx['n'] += 1
        if n == 0:
            release.wait(10)
            on_frame('', json.dumps({'choices': [{'index': 0, 'delta': {'role': 'assistant', 'reasoning_content': 'Plan read.'}}]}))
            on_frame('', json.dumps({'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'call_1', 'type': 'function', 'function': {'name': 'read_sources', 'arguments': json.dumps({'reads': [{'source_id': sid}], 'rationale': 'read'})}}]}, 'finish_reason': 'tool_calls'}]}))
            on_frame('', json.dumps({'choices': [], 'usage': {'prompt_tokens': 5, 'completion_tokens': 5, 'total_tokens': 10}}))
            on_frame('', '[DONE]')
            return None
        submit_args = json.dumps({
            'status': 'completed',
            'report': 'Reported revenue was 100 USD.',
            'claims': [{'text': 'Revenue 100', 'source_id': sid, 'quotation': 'Revenue was 100 USD.', 'role': 'support'}],
            'limitations': [],
            'stopping_reason': 'done',
        })
        on_frame('', json.dumps({'choices': [{'index': 0, 'delta': {'role': 'assistant', 'reasoning_content': 'Now submitting.'}}]}))
        on_frame('', json.dumps({'choices': [{'index': 0, 'delta': {'content': 'Final step note'}}]}))
        on_frame('', json.dumps({'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'call_2', 'type': 'function', 'function': {'name': 'submit_research', 'arguments': ''}}]}}]}))
        for i in range(0, len(submit_args), 11):
            on_frame('', json.dumps({'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': submit_args[i:i + 11]}}]}}]}))
        on_frame('', json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]}))
        on_frame('', json.dumps({'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 15, 'total_tokens': 25}}))
        on_frame('', '[DONE]')
        return None

    provider = OpenAICompatibleLlmProvider(
        _openai_settings(),
        policy=ProviderPolicy(timeout_seconds=10, max_output_tokens=500),
        stream_transport=oai_round_stream,
    )
    env.install(provider)
    op_id = env.start().json()['operation']['operation_id']

    async def scenario():
        async with httpx.AsyncClient(base_url=live_server, timeout=10.0) as client:
            async with client.stream('GET', f'{env.path}/runs/{op_id}/events', headers=env.headers) as resp:
                assert resp.status_code == 200
                release.set()
                return await _read_sse_frames(resp)

    try:
        frames = asyncio.run(scenario())
    finally:
        release.set()
    assert env.wait(op_id)['status'] == 'completed'

    deltas = [f['data'] for f in frames if f.get('event') == 'stream.delta']
    assert any(d['channel'] == 'thinking' and 'Plan read.' in d['delta'] for d in deltas)
    assert any(d['channel'] == 'text' and 'Final step note' in d['delta'] for d in deltas)
    assert any(d['channel'] == 'draft_report' and d['delta'] == 'Reported revenue was 100 USD.' for d in deltas)
    assert frames[-1]['event'] == 'run.terminal'
    assert env.latest()['content']['turns'][-1]['report'] == 'Reported revenue was 100 USD.'


def test_messages_stream_env_flag_gating_and_capacity_key(monkeypatch):
    monkeypatch.delenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', raising=False)
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS', '1000')
    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_VERSION', '2023-06-01')
    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_AUTH_SCHEME', 'x-api-key')
    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_NATIVE_TOOLS', 'true')
    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_CONTINUATION', 'true')
    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_CAPABILITY_BASE_URL', 'https://example.com/anthropic')
    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_CAPABILITY_MODEL', 'm-anth')
    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_CAPABILITY_VERSION', '2023-06-01')
    monkeypatch.setattr(openai_compatible, '_require_public_dns', lambda *_a, **_k: None)

    settings = OpenAICompatibleSettings.from_values(
        base_url='https://example.com/anthropic',
        model='m-anth',
        api_key='sk-test',
        allowlist='example.com',
    )

    monkeypatch.delenv('FORESIGHTNINE_MESSAGES_STREAM', raising=False)
    p_off = api._messages_research_provider(settings)
    assert p_off.stream_enabled is False

    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_STREAM', 'false')
    p_false = api._messages_research_provider(settings)
    assert p_false.stream_enabled is False

    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_STREAM', 'true')
    p_on = api._messages_research_provider(settings)
    assert p_on.stream_enabled is True
    assert p_on.research_capacity_key() != p_off.research_capacity_key()

    # Exact capability mismatch still fails even when FORESIGHTNINE_MESSAGES_STREAM='true'
    monkeypatch.setenv('FORESIGHTNINE_MESSAGES_CAPABILITY_MODEL', 'other-model')
    with pytest.raises(api.ApiProblem) as mismatch_err:
        api._messages_research_provider(settings)
    assert mismatch_err.value.status_code == 503

    # Non-bool stream_enabled fails configuration check cleanly
    with pytest.raises(ProviderFailure) as bad_bool:
        AnthropicMessagesProvider(
            _anthropic_settings(), SimpleNamespace(timeout_seconds=10, max_input_tokens=None),
            anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=100,
            stream_enabled='true',  # type: ignore[arg-type]
        )
    assert bad_bool.value.code == 'provider_configuration_invalid'

    # stream_enabled=True streams even without FORESIGHTNINE_LLM_CAPABILITY_PROFILE
    seen = []
    p_on.stream_transport = lambda m, u, h, b, t, on_frame: (
        seen.append((h, json.loads(b))),
        on_frame('message_start', json.dumps({'type': 'message_start', 'message': {'role': 'assistant', 'content': [], 'usage': {'input_tokens': 2, 'output_tokens': 1}}})),
        on_frame('content_block_start', json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})),
        on_frame('content_block_delta', json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'hi'}})),
        on_frame('content_block_stop', json.dumps({'type': 'content_block_stop', 'index': 0})),
        on_frame('message_delta', json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 2}})),
        on_frame('message_stop', json.dumps({'type': 'message_stop'})),
    )[-1] and None
    emitted = []
    res = p_on.complete_turn(_sample_turn(), on_stream_event=emitted.append)
    assert seen[0][0]['Accept'] == 'text/event-stream'
    assert seen[0][1]['stream'] is True
    assert emitted == [{'channel': 'text', 'delta': 'hi'}]
    assert res.message['content'] == [{'type': 'text', 'text': 'hi'}]
