from app import main
from app.openai_compatible import OpenAICompatibleLlmProvider, OpenAICompatibleSettings, HttpResponse
from .helpers import CLIENT, confirm_standard_flow, idem


SYNTHETIC_MODEL_KEY = 'synthetic-not-a-real-key'
SYNTHETIC_ACCOUNT_KEY = 'synthetic-account-key'

def provider(transport):
    return OpenAICompatibleLlmProvider(OpenAICompatibleSettings(base_url='https://models.example.test/v1', model='test-model',api_key=SYNTHETIC_MODEL_KEY,allowed_hosts=frozenset({('models.example.test',443)})),transport=transport,sleeper=lambda _:None)


def test_live_worldline_route_must_not_return_mock_on_provider_failure(monkeypatch):
    _, confirmed=confirm_standard_flow()
    calls=[]
    live=provider(lambda *args: (calls.append(args),HttpResponse(503,'{}'))[1])
    monkeypatch.setattr(main,'challenge_provider',live)
    response=CLIENT.post('/v1/worldlines',json={'challenge_id':confirmed['challenge']['challenge_id'],'horizon_end':'2027-08-10'},headers=idem('live-worldline-provider-down'))
    assert response.status_code==503
    assert len(calls)==1
    assert not main.WORLDLINES

import json
from datetime import timedelta
import pytest
from app.phase4 import WorldlineGenerationInput
from app.phase4_live import LivePhase4Adapter
from app.providers import FixedClock
from app.services import content_hash


def valid_draft(baseline=None):
    day = baseline or main.lifecycle_clock.today()
    return {
        'premise_probability': .6, 'premise_interval': {'minimum': .5, 'maximum': .7},
        'branches': [
            {'kind': kind, 'premise_status':'assumed_true', 'title': f'{kind} infrastructure scenario',
             'events': [
                 {'event_date': day.isoformat(), 'reveal_date': day.isoformat(),
                  'event': 'Orders establish the baseline', 'indicator': 'Published order index', 'state_change': 'Baseline established'},
                 {'event_date': (day+timedelta(days=30)).isoformat(), 'reveal_date': (day+timedelta(days=31)).isoformat(),
                  'event': f'{kind} demand transmission', 'indicator': 'Dated delivery volumes', 'state_change': f'{kind} change'},
             ],
             'impacts': [{'asset_id': 'ETF:SPY', 'direction': 'mixed', 'severity': 'medium',
                          'reason': 'Orders affect earnings', 'second_order_reason': 'Earnings affect investment', 'already_priced_score': 2}],
             'conditional_probability': probability, 'conditional_interval': {'minimum': probability-.05, 'maximum': probability+.05},
             'reasons': ['Explicit uncalibrated scenario assumption'], 'invalidation_conditions': ['Observed orders reverse']}
            for kind,probability in [('weak',.2),('main',.5),('strong',.3)]
        ],
    }


def completion(draft, usage=1234):
    body={'choices':[{'message':{'content':json.dumps(draft)},'finish_reason':'stop'}]}
    if usage is not None: body['usage']={'total_tokens':usage}
    return HttpResponse(200,json.dumps(body))


def setup_live(monkeypatch, draft=None, usage=1234, headers=None):
    CLIENT.headers.update(headers or {})
    try:
        _,confirmed=confirm_standard_flow()
    finally:
        CLIENT.headers.pop('Authorization',None)
    calls=[]
    live=provider(lambda *args:(calls.append(args),completion(draft or valid_draft(),usage))[1])
    monkeypatch.setattr(main,'challenge_provider',live)
    return confirmed['challenge']['challenge_id'],calls,live


def submit(challenge_id,key='live-phase4-success',headers=None):
    return CLIENT.post('/v1/worldlines', json={'challenge_id':challenge_id,'horizon_end':'2027-08-10'}, headers={**idem(key),**(headers or {})})


def test_live_bundle_current_clock_canonical_and_idempotent(monkeypatch):
    cid,calls,live=setup_live(monkeypatch)
    result=submit(cid)
    assert result.status_code==201,result.text
    bundle=result.json()['bundle']
    assert [w['branch_kind'] for w in bundle['worldlines']]==['weak','main','strong']
    for w in bundle['worldlines']:
        assert w['baseline_date']==main.lifecycle_clock.today().isoformat()
        assert 'uncalibrated' in w['provenance'] and 'mock' not in w['provenance']
        assert w['content_hash']==content_hash({k:v for k,v in w.items() if k not in ('content_hash','created_at')})
        assert w['asset_impacts'][0]['instrument_id']=='ETF:SPY'
    assert bundle['content_hash']==content_hash({k:v for k,v in bundle.items() if k not in ('content_hash','created_at')})
    assert bundle['probability_assessments'][1]['joint_probability']==.3
    repeated=submit(cid)
    assert repeated.status_code==201 and repeated.json()==result.json()
    assert repeated.headers['Idempotency-Replayed']=='true'
    assert len(calls)==1
    body=json.loads(calls[0][3]); assert body['model']=='test-model' and body['max_tokens']==4000
    assert live.policy.max_output_tokens==1500  # Request scoped, no global policy mutation.
    assert 'synthetic-not-a-real-key' not in result.text


@pytest.mark.parametrize('invalid', ['unknown_asset','duplicate_branch','mass','interval','future','reveal','extra','nan','event_order','past_baseline'])
def test_invalid_live_scenarios_fail_closed(monkeypatch,invalid):
    draft=valid_draft(); branch=draft['branches'][0]
    if invalid=='unknown_asset': branch['impacts'][0]['asset_id']='ETF:INVENTED'
    elif invalid=='duplicate_branch': branch['kind']='main'
    elif invalid=='mass': branch['conditional_probability']=.21
    elif invalid=='interval': branch['conditional_interval']['minimum']=.9
    elif invalid=='future': branch['events'][1]['event_date']='2040-01-01'
    elif invalid=='reveal': branch['events'][1]['reveal_date']=branch['events'][0]['event_date']
    elif invalid=='extra': branch['api_key']='untrusted-extra'
    elif invalid=='nan': draft['premise_probability']=float('nan')
    elif invalid=='event_order': branch['events'].reverse()
    elif invalid=='past_baseline': branch['events'][0]['event_date']='2026-01-01'
    cid,calls,_=setup_live(monkeypatch,draft)
    result=submit(cid)
    assert result.status_code==500,result.text
    assert not main.WORLDLINES and len(calls)==1
    assert 'untrusted-extra' not in result.text


def auth(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE','mock')
    response=CLIENT.post('/v1/auth/local/register',json={'email':'phase4@example.test','password':'Synthetic phase4 passphrase!'})
    assert response.status_code==201,response.text
    return {'Authorization':f"Bearer {response.json()['access_token']}"}


@pytest.mark.parametrize('usage,expected',[(1234,1234),(None,7000),(0,0)])
def test_live_platform_account_metering(monkeypatch,usage,expected):
    headers=auth(monkeypatch)
    cid,calls,_=setup_live(monkeypatch,usage=usage,headers=headers)
    before=CLIENT.get('/v1/account',headers=headers).json()['quota_consumed']
    result=submit(cid,headers=headers)
    assert result.status_code==201,result.text
    snapshot=CLIENT.get('/v1/account',headers=headers)
    assert snapshot.status_code==200,snapshot.text
    assert snapshot.json()['quota_consumed']==before+expected
    assert snapshot.json()['quota_reserved']==0
    assert submit(cid,headers=headers).status_code==201
    assert CLIENT.get('/v1/account',headers=headers).json()['quota_consumed']==before+expected
    assert len(calls)==1


def test_provider_failure_releases_reservation_and_does_not_retry_post(monkeypatch):
    headers=auth(monkeypatch)
    cid,calls,live=setup_live(monkeypatch,headers=headers)
    before=CLIENT.get('/v1/account',headers=headers).json()['quota_consumed']
    live.transport=lambda *args:(calls.append(args),HttpResponse(503,'{}'))[1]
    assert submit(cid,headers=headers).status_code==503
    state=CLIENT.get('/v1/account',headers=headers).json()
    assert state['quota_consumed']==before and state['quota_reserved']==0
    assert len(calls)==1 and not main.WORLDLINES


def test_invalid_auth_never_falls_back_to_anonymous_model(monkeypatch):
    cid,calls,_=setup_live(monkeypatch)
    assert submit(cid,headers={'Authorization':'Bearer invalid-credential'}).status_code==401
    assert not calls


def test_standalone_probability_uses_live_provider(monkeypatch):
    cid,calls,live=setup_live(monkeypatch)
    bundle=submit(cid).json()['bundle'];wid=bundle['worldlines'][0]['worldline_id']
    draft={'premise_status':'assumed_true','premise_probability':.6,'premise_interval':{'minimum':.5,'maximum':.7},'conditional_probability':.2,
           'conditional_interval':{'minimum':.15,'maximum':.25},'reasons':['Model estimate, not historical calibration']}
    live.transport=lambda *args:(calls.append(args),completion(draft))[1]
    result=CLIENT.post('/v1/probabilities/assess',json={'worldline_id':wid},headers=idem('live-probability-reassess'))
    assert result.status_code==200,result.text
    assert result.json()['probability']['premise_probability']==.6 and len(calls)==2
    prompt=json.loads(calls[-1][3])['messages'][1]['content']
    assert 'conditional scalar probabilities must sum' not in prompt


def test_personal_model_uses_account_settings_without_platform_charge(monkeypatch):
    import app.openai_compatible as compatible
    from app.model_credentials import AccountModelSettings,ModelCredentialCipher,ModelConfigUpdate
    from app.storage import InMemoryDocumentRepository
    headers=auth(monkeypatch)
    cid,calls,platform=setup_live(monkeypatch,headers=headers)
    account=CLIENT.get('/v1/account',headers=headers).json()
    aid=account['account']['account_id'];before=account['quota_consumed']
    settings=AccountModelSettings(InMemoryDocumentRepository(),ModelCredentialCipher(b'x'*32))
    monkeypatch.setattr(compatible,'_require_public_dns',lambda *a,**k:None)
    settings.update(aid,ModelConfigUpdate(mode='personal',base_url='https://models.example.test/v1',model='account-model',api_key=SYNTHETIC_ACCOUNT_KEY))
    monkeypatch.setattr(main,'model_settings',settings)
    personal_calls=[]
    def transport(*args,**kwargs):
        personal_calls.append(args)
        assert json.loads(args[3])['model']=='account-model'
        assert args[2]['Authorization']=='Bearer synthetic-account-key'
        return completion(valid_draft())
    monkeypatch.setattr(compatible,'_default_transport',transport)
    result=submit(cid,headers=headers)
    assert result.status_code==201
    state=CLIENT.get('/v1/account',headers=headers).json()
    assert state['quota_consumed']==before and state['quota_reserved']==0
    assert len(personal_calls)==1 and not calls
    assert main.challenge_provider is platform and platform.settings.model=='test-model'
    assert 'synthetic-account-key' not in result.text
    # Another account cannot read the owner's results or trigger its model.
    other=CLIENT.post('/v1/auth/local/register',json={'email':'other-phase4@example.test','password':'Other synthetic passphrase!'})
    foreign={'Authorization':f"Bearer {other.json()['access_token']}"}
    assert submit(cid,key='other-account-request',headers=foreign).status_code==404
    wid=result.json()['bundle']['worldlines'][0]['worldline_id']
    assert CLIENT.get('/v1/worldlines/'+wid,headers=foreign).status_code==404
    assert len(personal_calls)==1 and not calls


@pytest.mark.parametrize('fault',['invalid','timeout','commit'])
def test_no_billing_or_partial_worldline_after_failure(monkeypatch,fault):
    headers=auth(monkeypatch)
    cid,calls,live=setup_live(monkeypatch,headers=headers)
    before=CLIENT.get('/v1/account',headers=headers).json()['quota_consumed']
    if fault=='invalid':
        live.transport=lambda *args:completion({'branches':[]})
    elif fault=='timeout':
        def timeout(*args):
            calls.append(args)
            raise TimeoutError('synthetic-secret-must-not-leak')
        live.transport=timeout
    else:
        def fail(*args,**kwargs):raise RuntimeError('synthetic-secret-must-not-leak')
        monkeypatch.setattr(main.billing_ledger,'settle',fail)
    result=submit(cid,headers=headers)
    assert result.status_code in (500,503) and 'synthetic-secret' not in result.text
    state=CLIENT.get('/v1/account',headers=headers).json()
    assert state['quota_consumed']==before and state['quota_reserved']==0
    assert not main.WORLDLINES
    if fault=='timeout': assert len(calls)==1


def test_unsupported_live_model_never_returns_mock(monkeypatch):
    cid,calls,_=setup_live(monkeypatch)
    class Unsupported:
        name='unsupported-live-provider'
    monkeypatch.setattr(main,'challenge_provider',Unsupported())
    assert submit(cid).status_code==503
    assert not main.WORLDLINES and not calls


def test_cancelled_structured_call_discarded_without_second_attempt():
    from threading import Event
    from app.phase3 import ProviderFailure
    cancelled=Event();calls=[]
    def transport(*args):
        calls.append(args);cancelled.set();return completion(valid_draft())
    adapter=LivePhase4Adapter(provider(transport),cancel_event=cancelled)
    with pytest.raises(ProviderFailure) as error:
        adapter.provider.generate_structured('synthetic prompt',cancel_event=cancelled)
    assert error.value.code=='provider_cancelled' and len(calls)==1
