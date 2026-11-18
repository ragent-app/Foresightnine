import pytest

from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext


def context(settle=lambda call_id, tokens: None):
    return ResearchExecutionContext(ResearchExecutionBudget(4, 0, 1000, 100), False,
        lambda: None, lambda: None, lambda call_id, tokens: None, settle, lambda: None, now=lambda: 0)


def test_returned_and_settled_requires_known_usage_and_successful_settlement():
    ctx = context()
    before = ctx.start_model('a', 100)
    assert ctx.invoke_model('a', lambda: ({}, 12)) == ({}, 12)
    ctx.finish_model('a', 12, before)
    assert ctx.model_call_receipts['a'] == {'provider': 'returned', 'settlement': 'settled', 'processing_error': None}
    ctx.mark_model_processing_error('a', 'contract_validation')
    assert ctx.model_call_receipts['a']['processing_error'] == 'contract_validation'
    before = ctx.start_model('b', 100)
    ctx.invoke_model('b', lambda: ({}, None))
    ctx.finish_model('b', None, before)
    assert ctx.model_call_receipts['b']['settlement'] == 'unknown'


def test_provider_exception_and_settlement_failure_stay_unknown():
    ctx = context()
    before = ctx.start_model('a', 100)
    with pytest.raises(RuntimeError):
        ctx.invoke_model('a', lambda: (_ for _ in ()).throw(RuntimeError('private details')))
    ctx.finish_model('a', 12, before)
    assert ctx.model_call_receipts['a']['provider'] == 'unknown'
    assert ctx.model_call_receipts['a']['settlement'] == 'unknown'
    assert 'private details' not in repr(ctx.model_call_receipts)
    def failing_settlement(call_id, tokens):
        raise RuntimeError('ledger unavailable')
    ctx = context(failing_settlement)
    before = ctx.start_model('b', 100)
    ctx.invoke_model('b', lambda: ({}, 12))
    with pytest.raises(RuntimeError):
        ctx.finish_model('b', 12, before)
    assert ctx.model_call_receipts['b']['settlement'] == 'unknown'
    assert ctx.accounted_tokens == 100


def test_transport_diagnostics_are_sanitized_merged_and_persisted_per_call():
    ctx = context()
    writes = []
    ctx.persist_private_diagnostics = lambda value: writes.append(value.copy())
    ctx.start_model('a', 100)
    def generate(*, diagnostic_callback):
        diagnostic_callback({'submission_state': 'response_received', 'secret': 'never save', 'stages': [
            {'stage': 'tcp', 'result': 'ok', 'elapsed_ms': 7, 'headers': 'never save'},
            {'stage': 'secret', 'result': 'error', 'elapsed_ms': 1},
        ]})
        return {}, 1
    ctx.invoke_model('a', generate)
    ctx.record_validation_diagnostic('a', {'category': 'index_constraint', 'errors': []})
    assert ctx.private_diagnostics['a']['transport']['stages'] == [{'stage': 'tcp', 'result': 'ok', 'elapsed_ms': 7}]
    assert ctx.private_diagnostics['a']['category'] == 'index_constraint'
    assert 'never save' not in repr(writes)
    assert any(write.get('a', {}).get('transport', {}).get('submission_state') == 'response_received' for write in writes)
    assert writes[-1]['a']['category'] == 'index_constraint'
    assert writes[-1]['a']['errors'] == []


def test_diagnostic_storage_failure_does_not_change_provider_outcome():
    ctx = context()
    ctx.persist_private_diagnostics = lambda _: (_ for _ in ()).throw(RuntimeError('storage error'))
    before = ctx.start_model('a', 100)
    def generate(*, diagnostic_callback):
        diagnostic_callback({'submission_state': 'response_received', 'stages': []})
        return {}, 1
    assert ctx.invoke_model('a', generate) == ({}, 1)
    ctx.finish_model('a', 1, before)
    assert ctx.model_call_receipts['a']['provider'] == 'returned'
    assert ctx.model_call_receipts['a']['settlement'] == 'settled'


@pytest.mark.parametrize('code,expected,accounted', [
    ('invalid_provider_output','settled',12),
    ('provider_output_truncated','settled',12),
    ('provider_output_refused','settled',12),
    ('provider_timeout','unknown',100),
])
def test_response_failure_classification_preserves_only_observed_usage(code, expected, accounted):
    from app.phase3 import ProviderFailure
    ctx=context(); before=ctx.start_model('x',100)
    def generate():
        error=ProviderFailure(code,'safe',retryable=False,reported_tokens=12)
        if code != 'provider_timeout': error.provider_response_received=True
        raise error
    with pytest.raises(ProviderFailure) as exc:
        ctx.invoke_model('x',generate)
    ctx.finish_model('x',exc.value.reported_tokens,before)
    assert ctx.model_call_receipts['x']['settlement']==expected
    assert ctx.accounted_tokens==accounted
