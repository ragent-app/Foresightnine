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
    for raw, expected in [('length', 'provider_length'), ('stop', 'provider_stop'),
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
    from app.research_diagnostics import safe_provider_error
    assert safe_provider_error('{"error":{"code":"secret","type":"private","message":"secret"}}') == {
        'shape': 'error_object', 'code': 'unknown', 'category': 'unknown'}
    assert safe_provider_error('secret') == {'shape': 'non_json', 'code': 'unknown', 'category': 'unknown'}
