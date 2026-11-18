"""Capacity/budget rules with local providers only; no network."""
import json
from threading import Event
from time import monotonic
from app.abuse import ModelCallQuota
from app.phase3 import ProviderPolicy
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext
from app.openai_compatible import OpenAICompatibleLlmProvider, OpenAICompatibleSettings, HttpResponse


def context(max_tokens=None, entitlement=None):
    reservations=[]
    ctx=ResearchExecutionContext(ResearchExecutionBudget(None,None,max_tokens,monotonic()+30),False,
        lambda:None,Event,lambda call_id,amount:reservations.append(amount),lambda *_:None,lambda:None,
        entitlement_remaining=lambda:entitlement)
    return ctx,reservations


def test_unknown_capacity_and_unlimited_user_budget_admit_without_reservation():
    ctx, reservations=context()
    p=type('P',(),{'policy':ProviderPolicy()})()
    amount, output=ctx.prepare_model_call(p,'hello','system')
    assert amount is None and output is None
    before=ctx.start_model('a',amount)
    ctx.invoke_model('a',lambda: ({}, 8))
    ctx.finish_model('a',8,before)
    assert reservations == [None] and ctx.usage==8 and ctx.accounted_tokens==8


def test_account_quota_gates_bounded_call_without_changing_output():
    ctx, reservations=context(entitlement=5000)
    p=type('P',(),{'policy':ProviderPolicy(max_output_tokens=1000)})()
    amount, output=ctx.prepare_model_call(p,'hello','system')
    assert output == 1000
    assert amount == 1000 + len(b'hello') + len(b'system') + 192
    before=ctx.start_model('a',amount)
    ctx.finish_model('a',100,before)
    assert reservations == [amount]


def test_account_quota_does_not_become_output_limit():
    import pytest
    from app.phase3 import ProviderFailure
    ctx,_=context(entitlement=2500)
    with pytest.raises(ProviderFailure, match='bounded per-call'):
        ctx.prepare_model_call(type('P',(),{'policy':ProviderPolicy()})(),'hello','system')
    ctx,_=context(entitlement=500)
    with pytest.raises(ProviderFailure, match='planned call'):
        ctx.prepare_model_call(type('P',(),{'policy':ProviderPolicy(max_output_tokens=1000)})(),'hello','system')


def test_explicit_task_limit_can_narrow_output():
    ctx,_=context(max_tokens=500, entitlement=5000)
    amount, output=ctx.prepare_model_call(type('P',(),{'policy':ProviderPolicy(max_output_tokens=1000)})(),'hello','system')
    assert amount == 500 and output == 500-(len(b'hello')+len(b'system')+192)


def test_unknown_response_retains_unknown_without_phantom_usage():
    ctx,_=context()
    before=ctx.start_model('a',None)
    ctx.finish_model('a',None,before)
    assert ctx.usage is None and ctx.accounted_tokens is None


def test_daily_model_quota_unset_zero_and_explicit():
    assert ModelCallQuota(limit=None).reserve('x',['a']).allowed
    assert not ModelCallQuota(limit=0).reserve('x',['a']).allowed
    q=ModelCallQuota(limit=1)
    assert q.reserve('x',['a']).allowed and not q.reserve('y',['a']).allowed


def test_context_limit_uses_declared_counter_and_not_account_balance():
    from types import SimpleNamespace
    import pytest
    from app.phase3 import ProviderFailure
    provider = type('P', (), {'policy': ProviderPolicy(max_output_tokens=1000),
        'research_capacity': lambda self: SimpleNamespace(output_tokens=1000, context_tokens=800, input_counter='utf8_byte_upper_bound')})()
    ctx, _ = context(entitlement=10000)
    reservation, output = ctx.prepare_model_call(provider, 'hello', 'system')
    assert reservation == 800 and output == 800 - (5 + 6 + 192)
    provider.research_capacity = lambda: SimpleNamespace(output_tokens=1000, context_tokens=800, input_counter=None)
    with pytest.raises(ProviderFailure, match='Cannot verify context fit'):
        ctx.prepare_model_call(provider, 'hello', 'system')
