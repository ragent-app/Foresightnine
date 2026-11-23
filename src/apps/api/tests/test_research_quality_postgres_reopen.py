"""Native cancellation checkpoint survives a PostgreSQL process restart.

Requires a disposable, isolated PostgreSQL DSN. Each phase imports a fresh app
and the first interpreter exits before the second opens the same records.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(not os.getenv('FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN'), reason='disposable PostgreSQL not configured')
def test_native_cancel_checkpoint_reopens_once_and_export_preserves_permissions(tmp_path):
    env = dict(os.environ, FORESIGHTNINE_STORAGE_MODE='postgres', FORESIGHTNINE_LOCAL_AUTH_MODE='mock',
               FORESIGHTNINE_POSTGRES_DSN=os.environ['FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN'],
               QUALITY_REOPEN_STATE=str(tmp_path / 'state.json'))
    root = Path(__file__).resolve().parents[3]
    first = r'''
import json, os, sys
from threading import Event
from types import SimpleNamespace
from uuid import uuid4
sys.path.insert(0, 'apps/api')
import app.main as api
from fastapi.testclient import TestClient
from tests.test_research_runtime import turn
c = TestClient(api.app)
r = c.post('/v1/auth/local/register', json={'email': uuid4().hex+'@example.test', 'password':'correct horse battery staple!'})
assert r.status_code == 201, r.text
h = {'Authorization':'Bearer '+r.json()['access_token']}
def register_other():
    r = c.post('/v1/auth/local/register', json={'email':uuid4().hex+'@example.test','password':'correct horse battery staple!'})
    assert r.status_code == 201, r.text
    return 'Bearer ' + r.json()['access_token']
other = register_other()
text = 'Revenue was 120 USD in FY2025. Revenue was 100 USD in FY2024.'
r = c.post('/v2/research/sources', headers=h, json={'title':'Annual report','text':text,
    'permissions':{'store':True,'model_input':True,'display':True,'export':True}})
assert r.status_code == 201, r.text
sid = r.json()['source_id']
r = c.post('/v2/research/topics', headers=h, json={'question':'Compare revenue growth and diluted EPS.', 'source_ids':[sid]})
assert r.status_code == 201, r.text
topic = r.json(); path = '/v2/research/topics/' + topic['topic_id']; parent = topic['versions'][0]['version_id']
entered, release = Event(), Event()
class Provider:
    name = 'native-postgres-checkpoint'
    policy = SimpleNamespace(timeout_seconds=30, max_input_tokens=None, max_output_tokens=2048)
    calls = 0
    def complete(self, messages, tools, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return turn('read_sources', {'reads':[{'source_id':sid}], 'rationale':'Read disclosure.'}, 'read')
        if self.calls == 2:
            facts = [{'source_id':sid,'label':'revenue','value':value,'unit':'USD','currency':'USD',
                      'period':period,'quotation':quote} for value,period,quote in [
                     ('120','FY2025','Revenue was 120 USD in FY2025.'),('100','FY2024','Revenue was 100 USD in FY2024.')]]
            return turn('record_facts', {'facts':facts,'rationale':'Record two annual figures.'}, 'facts')
        if self.calls == 3:
            self.fact_ids = [x['fact_id'] for x in json.loads([m for m in messages if m.get('role')=='tool'][-1]['content'])['facts']]
            return turn('calculate_financials', {'calculations':[{'formula':'growth_percent','first_fact_id':self.fact_ids[0],
                        'second_fact_id':self.fact_ids[1]}], 'rationale':'Calculate growth.'}, 'calc')
        if self.calls == 4:
            return turn('checkpoint_question_part', {'question':'revenue growth','answer':'Revenue was 120 USD in FY2025.',
                        'fact_ids':[self.fact_ids[0]]}, 'part')
        entered.set(); release.wait(30)
        raise api.ProviderFailure('provider_cancelled','interrupted',retryable=False)
p = Provider(); api.challenge_provider = p
payload = {'parent_version_id':parent, 'discover_sources':False}
r = c.post(path+'/runs', headers={**h,'Idempotency-Key':'native-reopen-start'}, json=payload)
assert r.status_code == 202, r.text
operation = r.json()['operation']; assert entered.wait(20), 'checkpoint not reached'
control = '/v1/operations/'+operation['operation_id']+'/cancel'
for _ in range(2):
    cancelled = c.post(control, headers={**h,'Idempotency-Key':'native-reopen-cancel'}, json={'cancel_token':operation['cancel_token']})
    assert cancelled.status_code == 200 and cancelled.json()['operation']['status'] == 'cancelled', cancelled.text
versions = c.get(path, headers=h).json()['versions']; assert len(versions) == 2, versions
v = versions[-1]; content = v['content']; assert p.calls == 5
assert len(content['facts']) == 2 and len(content['calculations']) == 1, content
assert content['calculations'][0]['value'] == '20.00', content['calculations']
assert any(x.get('answer') and '120' in x['answer'] for x in content['turns'][-1]['question_parts'])
assert any('EPS' in x['question'] and x['status'] != 'answered' for x in content['turns'][-1]['question_parts'])
with open(os.environ['QUALITY_REOPEN_STATE'], 'w') as f:
    json.dump({'auth':h['Authorization'],'other':other,'path':path,'source':sid,'parent':parent,
               'version':v['version_id'],'operation':operation['operation_id'],'payload':payload,
               'fact_ids':[x['fact_id'] for x in content['facts']],
               'calculation_id':content['calculations'][0]['calculation_id']}, f)
release.set()
os._exit(0)
'''
    second = r'''
import json, os, sys
sys.path.insert(0, 'apps/api')
import app.main as api
from fastapi.testclient import TestClient
with open(os.environ['QUALITY_REOPEN_STATE']) as f: s = json.load(f)
c = TestClient(api.app); h = {'Authorization':s['auth']}; other = {'Authorization':s['other']}; path = s['path']
class NoReplay:
    def complete(self, *args, **kwargs): raise AssertionError('external model call was replayed')
api.challenge_provider = NoReplay()
r = c.get(path, headers=h); assert r.status_code == 200, r.text
versions = r.json()['versions']; assert len(versions) == 2 and versions[-1]['version_id'] == s['version'], versions
assert versions[-1]['parent_version_id'] == s['parent']
vpath = path + '/versions/' + s['version']
v = c.get(vpath, headers=h); assert v.status_code == 200, v.text
content = v.json()['content']; facts, calcs = content['facts'], content['calculations']
assert [x['fact_id'] for x in facts] == s['fact_ids'] and len(set(s['fact_ids'])) == 2
assert len(calcs) == 1 and calcs[0]['calculation_id'] == s['calculation_id'] and calcs[0]['value'] == '20.00'
assert {x['fact_id'] for x in calcs[0]['inputs']} == set(s['fact_ids'])
parts = content['turns'][-1]['question_parts']
assert any(x.get('answer') and '120' in x['answer'] for x in parts), parts
assert any('EPS' in x['question'] and x['status'] != 'answered' for x in parts), parts
export = c.get(vpath+'/export', headers=h); assert export.status_code == 200, export.text
assert export.json()['version']['content'] == content
assert c.get(vpath, headers=other).status_code == 404
assert c.get(vpath+'/export', headers=other).status_code == 404
replay = c.post(path+'/runs', headers={**h,'Idempotency-Key':'native-reopen-start'}, json=s['payload'])
assert replay.status_code == 202 and replay.headers['Idempotency-Replayed'] == 'true', replay.text
assert replay.json()['operation']['operation_id'] == s['operation']
stale = c.post(path+'/versions', headers=h, json={'parent_version_id':s['parent'],'content':content,'review':'confirmed'})
assert stale.status_code == 409, stale.text
assert len(c.get(path, headers=h).json()['versions']) == 2
assert c.delete('/v1/auth/local/account', headers=h).status_code == 200
assert c.delete('/v1/auth/local/account', headers=other).status_code == 200
'''
    for script in (first, second):
        result = subprocess.run([sys.executable, '-c', script], cwd=root, env=env, capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not os.getenv('FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN'), reason='disposable PostgreSQL not configured')
def test_sigkill_after_submission_checkpoint_recovers_without_external_call(tmp_path):
    """Kill a worker after durable submission, before the final version write."""
    import time
    state = tmp_path / 'kill-state.json'
    marker = tmp_path / 'save-entered'
    calls = tmp_path / 'provider-calls'
    env = dict(os.environ, FORESIGHTNINE_STORAGE_MODE='postgres', FORESIGHTNINE_LOCAL_AUTH_MODE='mock',
               FORESIGHTNINE_POSTGRES_DSN=os.environ['FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN'],
               QUALITY_REOPEN_STATE=str(state), QUALITY_SAVE_MARKER=str(marker), QUALITY_CALLS=str(calls))
    root = Path(__file__).resolve().parents[3]
    first = r'''
import json, os, sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4
sys.path.insert(0,'apps/api')
import app.main as api
from fastapi.testclient import TestClient
from tests.test_research_runtime import turn
c=TestClient(api.app)
r=c.post('/v1/auth/local/register',json={'email':uuid4().hex+'@example.test','password':'correct horse battery staple!'})
assert r.status_code==201,r.text
h={'Authorization':'Bearer '+r.json()['access_token']}
r=c.post('/v2/research/sources',headers=h,json={'title':'Report','text':'Revenue was 100 USD.',
  'permissions':{'store':True,'model_input':True,'display':True,'export':True}})
assert r.status_code==201,r.text
sid=r.json()['source_id'];r=c.post('/v2/research/topics',headers=h,json={'question':'Revenue?','source_ids':[sid]})
assert r.status_code==201,r.text
topic=r.json(); path='/v2/research/topics/'+topic['topic_id']; parent=topic['versions'][0]['version_id']
class Provider:
 name='native-kill-test';policy=SimpleNamespace(timeout_seconds=20,max_input_tokens=None,max_output_tokens=2048)
 def __init__(self): self.calls=0
 def complete(self,messages,tools,**kwargs):
  self.calls+=1
  with open(os.environ['QUALITY_CALLS'],'a') as f: f.write('called\n')
  if self.calls==1:return turn('read_sources',{'reads':[{'source_id':sid}],'rationale':'read'},'read')
  return turn('submit_research',{'report':'Revenue was 100 USD.','claims':[{'text':'Revenue was 100 USD.',
   'source_id':sid,'quotation':'Revenue was 100 USD.','role':'support'}],'limitations':[],'stopping_reason':'answered'},'submit')
api.challenge_provider=Provider()
def hold_before_save(self,parent,content):
 Path(os.environ['QUALITY_SAVE_MARKER']).write_text('entered')
 Event().wait(60)
 raise AssertionError('the parent must kill this process')
api.ResearchVersionChain.save=hold_before_save
payload={'parent_version_id':parent,'discover_sources':False,'max_model_calls':2}
r=c.post(path+'/runs',headers={**h,'Idempotency-Key':'kill-start'},json=payload)
assert r.status_code==202,r.text
with open(os.environ['QUALITY_REOPEN_STATE'],'w') as f:json.dump({'auth':h['Authorization'],'path':path,'parent':parent,
  'source':sid,'operation':r.json()['operation']['operation_id']},f)
# Keep the process alive until the parent kills it after the final-save hook.
Event().wait(60)
raise AssertionError('the parent must kill this process')
'''
    second = r'''
import json,os,sys,time
from datetime import timedelta
sys.path.insert(0,'apps/api')
import app.main as api
from fastapi.testclient import TestClient
with open(os.environ['QUALITY_REOPEN_STATE']) as f:s=json.load(f)
c=TestClient(api.app);h={'Authorization':s['auth']};path=s['path'];op=s['operation']
class NoReplay:
 name='native-kill-test'
 from types import SimpleNamespace
 policy=SimpleNamespace(timeout_seconds=20,max_input_tokens=None,max_output_tokens=2048)
 def complete(self,*args,**kwargs):
  with open(os.environ['QUALITY_CALLS'],'a') as f:f.write('unexpected\n')
  raise AssertionError('recovery must not call the provider')
api.challenge_provider=NoReplay()
record=api.RESEARCH_RUN_INPUTS.get(op);cp=record['runtime_checkpoint']
assert cp.get('submission') and cp['submission']['report']=='Revenue was 100 USD.',cp
assert api.operations.recover_expired_leases(now=api.lifecycle_clock.now()+timedelta(seconds=301))>=1
status=c.get(path+'/runs/'+op+'/recovery-status',headers=h)
assert status.status_code==200 and status.json()['can_deliver'],status.text
before=c.get(path,headers=h).json()['versions'];assert len(before)==1 and before[0]['version_id']==s['parent']
request_headers={**h,'Idempotency-Key':'kill-recover'}
r=c.post(path+'/runs/'+op+'/recover',headers=request_headers,json={});assert r.status_code==202,r.text
recovered=r.json()['operation']['operation_id']
for _ in range(400):
 current=c.get(path+'/runs/'+recovered,headers=h).json()['operation']['status']
 if current in ('completed','failed','cancelled'):break
 time.sleep(.05)
assert current=='completed',current
versions=c.get(path,headers=h).json()['versions'];assert len(versions)==2,versions
version=versions[-1];assert version['parent_version_id']==s['parent']
assert version['content']['turns'][-1]['report']=='Revenue was 100 USD.'
vpath=path+'/versions/'+version['version_id']
assert c.get(vpath+'/export',headers=h).json()['version']['version_id']==version['version_id']
replay=c.post(path+'/runs/'+op+'/recover',headers=request_headers,json={})
assert replay.status_code==202 and replay.headers['Idempotency-Replayed']=='true',replay.text
assert replay.json()['operation']['operation_id']==recovered
new=c.post(path+'/runs/'+op+'/recover',headers={**h,'Idempotency-Key':'kill-recover-new'},json={})
assert new.status_code==409 and new.json()['error_code']=='lifecycle_conflict',new.text
assert len(c.get(path,headers=h).json()['versions'])==2
assert c.get(vpath,headers=h).json()['version_id']==version['version_id']
with open(os.environ['QUALITY_CALLS']) as f:assert f.read().splitlines()==['called','called']
assert c.delete('/v1/auth/local/account',headers=h).status_code==200
'''
    process = subprocess.Popen([sys.executable, '-c', first], cwd=root, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and not (marker.exists() and state.exists()):
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(f'worker exited before fault point: {stdout}\n{stderr}')
            time.sleep(.05)
        assert marker.exists() and state.exists(), 'worker did not reach final-save fault point'
        process.kill()
        process.communicate(timeout=10)
        assert process.returncode < 0, process.returncode
        result = subprocess.run([sys.executable, '-c', second], cwd=root, env=env, capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
