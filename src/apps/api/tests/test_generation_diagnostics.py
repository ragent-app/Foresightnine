import json

import pytest

from app.generation_outcome import GenerationOutcome, transport_from_submission
from app.research_diagnostics import (fingerprint, safe_diagnostic_context,
                                      safe_exception_diagnostic, set_finish_reason, get_finish_reason)


def test_outcomes_are_independent_and_serializable():
    outcome = GenerationOutcome(transport='responded', structure='invalid')
    assert json.loads(json.dumps(outcome.to_dict())) == {
        'transport': 'responded', 'structure': 'invalid',
        'evidence': 'not_checked', 'completion': 'not_checked'}
    assert transport_from_submission('started_unknown') == 'unknown'
    assert transport_from_submission('unavailable') == 'unknown'
    assert transport_from_submission('not_started') == 'not_sent'
    assert transport_from_submission('response_received') == 'responded'
    with pytest.raises(ValueError):
        GenerationOutcome(completion='success')


def test_diagnostics_drop_untrusted_values():
    safe = safe_diagnostic_context(request_id='request-1', model='model-1',
        input_hash=fingerprint('private input'), source_range_fingerprint='not-a-hash',
        parameters={'temperature': 0.2, 'secret': 'private', 'seed': float('nan')},
        prompt='private input')
    assert safe == {'request_id': 'request-1', 'model': 'model-1',
                    'input_hash': fingerprint('private input'), 'parameters': {'temperature': 0.2}}
    try:
        raise ValueError('private error text')
    except ValueError as error:
        diagnostic = safe_exception_diagnostic(error)
    assert diagnostic['exception_class'] == 'ValueError'
    assert 'private' not in json.dumps(diagnostic)


def test_finish_reasons_are_bounded():
    for raw, expected in [('length', 'provider_length'), ('max_tokens', 'provider_length'),
                          ('model_context_window_exceeded', 'provider_context_limit'),
                          ('stop', 'provider_stop'), ('end_turn', 'provider_stop'), ('stop_sequence', 'provider_stop'),
                          ('tool_use', 'provider_tool_calls'), ('tool_calls', 'provider_tool_calls'),
                          ('refusal', 'provider_refusal'), ('content_filter', 'provider_refusal'),
                          ('private', 'unknown')]:
        set_finish_reason(raw)
        assert get_finish_reason() == expected


def test_full_outcome_vocabulary_and_generation_controls():
    assert GenerationOutcome(structure='partial', evidence='contradicts', completion='blocked').to_dict()['completion'] == 'blocked'
    assert GenerationOutcome(evidence='uncertain', completion='cancelled').completion == 'cancelled'
    safe = safe_diagnostic_context(provider='openai_compatible', output_mode='json_schema',
        parameters={'thinking': 'enabled', 'thinking_enabled': True, 'reasoning_effort': 'low', 'do_sample': False})
    assert safe == {'provider': 'openai_compatible', 'output_mode': 'json_schema', 'parameters': {
        'thinking': 'enabled', 'thinking_enabled': True, 'reasoning_effort': 'low', 'do_sample': False}}
    assert safe_diagnostic_context(provider='https://secret.example', output_mode='private',
        parameters={'thinking': 'private', 'reasoning_effort': 'secret', 'do_sample': 'false'}) == {}


def test_logging_failure_does_not_escape(monkeypatch, capfd):
    import logging
    from app.research_diagnostics import report_diagnostic_write_failure
    logger = logging.getLogger('app.research_diagnostics')
    def broken_error(*args, **kwargs):
        raise RuntimeError('private logging failure')
    monkeypatch.setattr(logger, 'error', broken_error)
    report_diagnostic_write_failure(ValueError('private provider data'), operation_id='operation-1')
    captured = capfd.readouterr()
    assert 'diagnostic_write_failed' in captured.err
    assert 'private' not in captured.err


def test_http_error_is_received_without_fabricated_usage(monkeypatch):
    from app.openai_compatible import HttpResponse, OpenAICompatibleLlmProvider
    from app.phase3 import ProviderFailure, ProviderPolicy
    monkeypatch.setenv('FORESIGHTNINE_LLM_BASE_URL', 'https://api.example.com/v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_MODEL', 'unit-model')
    monkeypatch.setenv('FORESIGHTNINE_LLM_API_KEY', 'test-only')
    monkeypatch.setenv('FORESIGHTNINE_LLM_ALLOWED_HOSTS', 'api.example.com')
    provider = OpenAICompatibleLlmProvider.from_environment(policy=ProviderPolicy(timeout_seconds=1), transport=lambda *args: HttpResponse(429, 'sensitive'))
    with pytest.raises(ProviderFailure) as caught:
        provider.generate_structured('prompt')
    assert caught.value.provider_response_received is True
    assert getattr(caught.value, 'reported_tokens', None) is None


def test_diagnostic_cleaner_preserves_known_envelope_cause_without_unknown_fields():
    from app.research_diagnostics import sanitize_diagnostic_errors
    errors = sanitize_diagnostic_errors([
        {'type': 'missing_version'}, {'type': 'version_mismatch'},
        {'type': 'missing_fields', 'fields': ['facts', 'private_field'], 'count': 2},
        {'type': 'extra_fields', 'count': 1, 'value': 'private'},
        {'type': 'collection_type', 'field': 'facts', 'actual_type': 'object'},
    ])
    assert errors == [{'type': 'missing_version'}, {'type': 'version_mismatch'},
                      {'type': 'missing_fields', 'fields': ['facts'], 'count': 2},
                      {'type': 'extra_fields', 'count': 1},
                      {'type': 'collection_type', 'field': 'facts', 'actual_type': 'object'}]
    assert 'private' not in repr(errors)


def test_provider_metadata_records_actual_wire_and_safe_error(monkeypatch):
    from app.openai_compatible import HttpResponse, OpenAICompatibleLlmProvider
    from app.phase3 import ProviderFailure, ProviderPolicy
    from app.research_budget import ResearchExecutionBudget
    from app.research_coordinator import ResearchExecutionContext
    monkeypatch.setenv('FORESIGHTNINE_LLM_BASE_URL', 'https://api.example.com/v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_MODEL', 'unit-model')
    monkeypatch.setenv('FORESIGHTNINE_LLM_API_KEY', 'test-secret')
    monkeypatch.setenv('FORESIGHTNINE_LLM_ALLOWED_HOSTS', 'api.example.com')
    body = json.dumps({'error': {'type': 'invalid_request_error', 'code': 'context_length_exceeded',
                                  'message': 'private message', 'private_field': 'private'}})
    provider = OpenAICompatibleLlmProvider.from_environment(policy=ProviderPolicy(timeout_seconds=1),
        transport=lambda *args: HttpResponse(400, body))
    ctx = ResearchExecutionContext(ResearchExecutionBudget(1, 0, None, 100), False,
        lambda: None, lambda: None, lambda *_: None, lambda *_: None, lambda: None, now=lambda: 0)
    before = ctx.start_model('call-1', None)
    with pytest.raises(ProviderFailure) as caught:
        ctx.invoke_model('call-1', provider.generate_structured, 'private prompt', timeout_seconds=1, max_output_tokens=123)
    ctx.finish_model('call-1', None, before)
    item = ctx.private_diagnostics['call-1']
    assert item['context']['output_mode'] == 'json_object'
    assert item['context']['parameters']['max_tokens'] == 123
    assert item['provider_error'] == {'shape': 'error_object', 'code': 'context_length_exceeded', 'category': 'invalid_request_error'}
    assert caught.value.provider_response_received and ctx.model_call_receipts['call-1']['settlement'] == 'unknown'
    assert 'private' not in repr(item) and 'test-secret' not in repr(item)


def test_unknown_provider_error_values_are_not_saved():
    from app.research_diagnostics import safe_provider_error, sanitize_provider_error
    parsed = safe_provider_error('{"error":{"code":"secret","type":"private","message":"secret"}}')
    assert parsed == {
        'shape': 'error_object', 'code': 'unknown', 'category': 'unknown',
        'messages_type': 'unrecognized', 'messages_code': 'unrecognized', 'messages_parameter': 'absent'}
    assert sanitize_provider_error(parsed) == {'shape': 'error_object', 'code': 'unknown', 'category': 'unknown'}
    assert 'secret' not in repr(parsed) and 'private' not in repr(parsed)
    assert safe_provider_error('secret') == {'shape': 'non_json', 'code': 'unknown', 'category': 'unknown'}


def test_stop_reason_usage_and_wire_metadata_sanitization():
    from app.model_protocol import ModelUsage
    from app.research_diagnostics import safe_stop_reason, safe_usage_diagnostics, safe_wire_metadata
    assert safe_stop_reason('max_tokens') == 'max_tokens'
    assert safe_stop_reason(None) == 'absent'
    assert safe_stop_reason('secret-reason') == 'unrecognized'
    assert safe_usage_diagnostics(ModelUsage(10, 20, 30, 5, None)) == {
        'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30, 'cache_creation_input_tokens': 5}
    assert safe_usage_diagnostics({'input_tokens': True, 'output_tokens': -1, 'total_tokens': 42, 'secret': 'LEAK'}) == {'total_tokens': 42}
    meta = safe_wire_metadata({'model': 'custom-model', 'max_tokens': 16384, 'stream': True, 'system': 'SECRET'}, wire_bytes=2048)
    assert meta['wire_bytes'] == 2048
    assert meta['stream'] is True
    assert meta['parameters'] == {'max_tokens': 16384}
    assert 'SECRET' not in repr(meta)


def test_sanitize_transport_diagnostic_allows_timeout_kind_and_600s_elapsed():
    from app.research_diagnostics import sanitize_transport_diagnostic

    plain = sanitize_transport_diagnostic({
        'submission_state': 'response_received',
        'stages': [{'stage': 'tcp', 'result': 'ok', 'elapsed_ms': 12}],
    })
    assert plain == {
        'submission_state': 'response_received',
        'stages': [{'stage': 'tcp', 'result': 'ok', 'elapsed_ms': 12}],
    }

    for kind in ('connect_timeout', 'header_timeout', 'stream_idle_timeout', 'stream_deadline_timeout', 'response_timeout'):
        sanitized = sanitize_transport_diagnostic({
            'submission_state': 'response_received',
            'timeout_kind': kind,
            'secret': 'SECRET_SHOULD_DROP',
            'stages': [
                {'stage': 'response_body', 'result': 'timeout', 'elapsed_ms': 300_000},
                {'stage': 'deadline', 'result': 'timeout', 'elapsed_ms': 600_000},
                {'stage': 'deadline', 'result': 'timeout', 'elapsed_ms': 600_001},
            ],
        })
        assert sanitized == {
            'submission_state': 'response_received',
            'timeout_kind': kind,
            'stages': [
                {'stage': 'response_body', 'result': 'timeout', 'elapsed_ms': 300_000},
                {'stage': 'deadline', 'result': 'timeout', 'elapsed_ms': 600_000},
            ],
        }
        assert 'SECRET_SHOULD_DROP' not in repr(sanitized)

    invalid_kind = sanitize_transport_diagnostic({
        'submission_state': 'started_unknown',
        'timeout_kind': 'SECRET_UNKNOWN_KIND',
        'stages': [],
    })
    assert invalid_kind == {'submission_state': 'started_unknown', 'stages': []}
    assert 'SECRET_UNKNOWN_KIND' not in repr(invalid_kind)


def test_warn_if_timeout_output_mismatch_helper(caplog):
    import logging
    from app.research_diagnostics import warn_if_timeout_output_mismatch

    with caplog.at_level(logging.WARNING, logger='app.research_diagnostics'):
        caplog.clear()
        assert warn_if_timeout_output_mismatch(8192, 239.9, streaming=True) is True
        assert warn_if_timeout_output_mismatch(16384, 120, streaming=False) is True
        assert len(caplog.records) == 2
        assert 'max_output_tokens=8192' in caplog.records[0].message
        assert 'streaming=true' in caplog.records[0].message

        caplog.clear()
        assert warn_if_timeout_output_mismatch(8192, 240) is False
        assert warn_if_timeout_output_mismatch(16384, 300) is False
        assert warn_if_timeout_output_mismatch(4096, 120) is False
        assert warn_if_timeout_output_mismatch(8192, 0) is False
        assert warn_if_timeout_output_mismatch(8192, float('nan')) is False
        assert warn_if_timeout_output_mismatch(True, 120) is False
        assert warn_if_timeout_output_mismatch(8192, True) is False
        assert caplog.records == []


def test_openai_compatible_timeout_attaches_metadata_and_600s_policy(monkeypatch):
    from app.openai_compatible import OpenAICompatibleLlmProvider, _TransportTimeout
    from app.phase3 import ProviderFailure, ProviderPolicy

    monkeypatch.setenv('FORESIGHTNINE_LLM_BASE_URL', 'https://api.example.com/v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_MODEL', 'unit-model')
    monkeypatch.setenv('FORESIGHTNINE_LLM_API_KEY', 'test-secret')
    monkeypatch.setenv('FORESIGHTNINE_LLM_ALLOWED_HOSTS', 'api.example.com')

    built_600 = OpenAICompatibleLlmProvider.from_environment(
        policy=ProviderPolicy(timeout_seconds=600, max_output_tokens=16384),
        transport=lambda *a: (_ for _ in ()).throw(_TransportTimeout(timeout_kind='header_timeout')),
    )
    with pytest.raises(ProviderFailure) as caught:
        built_600.generate_structured('private prompt', max_output_tokens=16384)
    assert caught.value.code == 'provider_timeout'
    assert caught.value.timeout_kind == 'header_timeout'
    assert type(caught.value.elapsed_ms) is int and caught.value.elapsed_ms >= 0
    assert type(caught.value.wire_bytes) is int and caught.value.wire_bytes > 0
    assert caught.value.streamed_bytes is None

    with pytest.raises(ValueError, match='between 0 and 600'):
        OpenAICompatibleLlmProvider.from_environment(policy=ProviderPolicy(timeout_seconds=601))


def test_sanitize_stream_metrics_bounds_and_drops_secrets():
    from app.research_diagnostics import sanitize_stream_metrics

    assert sanitize_stream_metrics('not-a-dict') == {}
    cleaned = sanitize_stream_metrics(
        {
            'frames_received': 12,
            'ttft_ms': 340,
            'last_frame_elapsed_ms': 1200,
            'reassembled_bytes': 512,
            'block_bytes_by_type': {
                'thinking': 400,
                'text': 100,
                'tool_use': 12,
                'redacted_thinking': True,
                'extra_secret_block': 999,
            },
            'active_tool_names': ['read_sources', 'SECRET_LEAK', 'read_sources', 'submit_research', 'bad name!'],
            'raw_payload': 'SECRET_RAW_PAYLOAD',
        },
        allowed_tool_names=frozenset({'read_sources', 'submit_research'}),
    )
    assert cleaned == {
        'frames_received': 12,
        'ttft_ms': 340,
        'last_frame_elapsed_ms': 1200,
        'reassembled_bytes': 512,
        'block_bytes_by_type': {
            'thinking': 400,
            'text': 100,
            'tool_use': 12,
            'redacted_thinking': 0,
        },
        'active_tool_names': ['read_sources', 'unrecognized', 'submit_research'],
    }
    assert 'SECRET' not in repr(cleaned)

    default_tools = sanitize_stream_metrics({
        'frames_received': True,
        'ttft_ms': -5,
        'last_frame_elapsed_ms': 4_000_000,
        'reassembled_bytes': -1,
        'active_tool_names': ['lookup', 'SECRET_NAME', 'calculate_financials'],
    })
    assert default_tools == {
        'frames_received': 0,
        'ttft_ms': None,
        'last_frame_elapsed_ms': None,
        'reassembled_bytes': 0,
        'block_bytes_by_type': {'thinking': 0, 'text': 0, 'tool_use': 0, 'redacted_thinking': 0},
        'active_tool_names': ['lookup', 'unrecognized', 'calculate_financials'],
    }
