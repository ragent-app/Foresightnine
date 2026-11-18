"""Offline classification of response validation versus unknown transport."""
from datetime import datetime, timezone
from threading import Event
from time import monotonic
from types import SimpleNamespace
import pytest
from app.phase3 import ProviderFailure
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext, execute_generation
from app.research_recovery import AnalysisSnapshot
from app.research_sources import ResearchSource, SourcePermissions
from dataclasses import asdict


@pytest.mark.parametrize('failure,expected_status,expected_reason,expected_receipt', [
    ('schema','failed','invalid_model_output','returned'),
    ('timeout','not_validated','provider_timeout','unknown'),
])
def test_structured_state_distinguishes_schema_failure_from_unknown(failure,expected_status,expected_reason,expected_receipt):
    source=ResearchSource(source_id='s',owner_id='test',title='Source',text='Revenue increased.',
        kind='user_supplied',obtained_at=datetime.now(timezone.utc),content_sha256='test',
        permissions=SourcePermissions(store=True,model_input=True,display=True,export=True))
    provider=SimpleNamespace(policy=SimpleNamespace(timeout_seconds=10,max_input_tokens=6000,max_output_tokens=1000))
    ctx=ResearchExecutionContext(ResearchExecutionBudget(1,0,7000,monotonic()+60),False,lambda:None,Event,
        lambda *_:None,lambda *_:None,lambda:None)
    def generate(prompt,*,cancel_event,system,timeout_seconds,contract=None):
        if failure=='timeout':
            raise ProviderFailure('provider_timeout','safe',retryable=True,reported_tokens=12)
        return {}, 17
    result=execute_generation(question='What happened to revenue?',scope='',sources=[source],as_of=None,
        provider=provider,generate=generate,context=ctx,call_id='call',focus=None,planning_steps=[],require_counterarguments=False)
    assert result.unavailable_reason==expected_reason
    assert result.generation_tokens == (17 if failure == 'schema' else None)
    assert result.analysis.structured_output_status==expected_status
    assert not result.analysis.claims
    assert ctx.model_call_receipts['call']['provider']==expected_receipt
    assert ctx.model_call_receipts['call']['settlement']==('settled' if failure=='schema' else 'unknown')
    restored=AnalysisSnapshot.model_validate(asdict(result.analysis))
    assert restored.structured_output_status==expected_status


def test_legacy_recovery_infers_only_nonempty_validated_analysis():
    from app.research_recovery import _legacy_analysis_status
    kw={'generation_status':'completed','prompt':'saved prompt','unavailable_reason':None,
        'workflow_receipts':[{'stage':'analysis','status':'completed','model_calls_admitted':1,
          'model_call_receipts':[{'provider':'returned','settlement':'settled','processing_error':None}]}]}
    assert _legacy_analysis_status({'claims':[{'text':'saved'}]},**kw)['structured_output_status']=='complete'
    assert 'structured_output_status' not in _legacy_analysis_status({'claims':[]},**kw)
    assert 'structured_output_status' not in _legacy_analysis_status({'claims':[{'text':'saved'}]},**{**kw,'workflow_receipts':[]})
    assert 'structured_output_status' not in _legacy_analysis_status({'claims':[{}]},**{**kw,'generation_status':'failed'})
    assert 'structured_output_status' not in _legacy_analysis_status({'claims':[{}]},**{**kw,'unavailable_reason':'invalid_model_output'})
    assert _legacy_analysis_status({'claims':[{}],'structured_output_status':'failed'},**kw)['structured_output_status']=='failed'
