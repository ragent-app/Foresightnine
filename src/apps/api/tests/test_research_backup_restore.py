"""Opt-in backup drill; creates and removes only uniquely named disposable containers."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest


@pytest.mark.skipif(os.getenv("FORESIGHTNINE_TEST_DISPOSABLE_DOCKER_BACKUP") != "1",
                    reason="disposable Docker backup drill not enabled")
def test_research_backup_restores_versions_ownership_and_idempotency(tmp_path):
    root = Path(__file__).resolve().parents[3]
    names = ["foresightnine-backup-drill-" + uuid4().hex for _ in range(2)]
    created = []

    def docker(*args, **kwargs):
        return subprocess.run(["docker", *args], check=True, capture_output=True, **kwargs)

    try:
        ports = []
        for name in names:
            docker("run", "--rm", "-d", "--name", name, "-e", "POSTGRES_PASSWORD=disposable-only",
                   "-e", "POSTGRES_DB=research_test", "-p", "127.0.0.1::5432", "postgres:16-alpine")
            created.append(name)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres", "-d", "research_test"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                    break
                time.sleep(0.25)
            else:
                pytest.fail("Disposable PostgreSQL did not become ready")
            port = docker("port", name, "5432/tcp", text=True).stdout.strip().rsplit(":", 1)[1]
            ports.append(int(port))
        for name in ("001_research_state.sql", "002_operation_state.sql"):
            sql = (root / "infra/migrations" / name).read_bytes()
            docker("exec", "-i", names[0], "psql", "-U", "postgres", "-d", "research_test", "-v", "ON_ERROR_STOP=1", input=sql)

        state = tmp_path / "state.json"
        base_env = dict(os.environ, FORESIGHTNINE_STORAGE_MODE="postgres", FORESIGHTNINE_LOCAL_AUTH_MODE="mock",
                        STAGEB_BACKUP_TEST_STATE=str(state))
        seed = r'''
import json, os, time
from types import SimpleNamespace
from uuid import uuid4
import app.main as api
from fastapi.testclient import TestClient
client = TestClient(api.app)
a = client.post('/v1/auth/local/register', json={'email':f'backup-a-{uuid4().hex}@example.test','password':'correct horse battery staple!'})
b = client.post('/v1/auth/local/register', json={'email':f'backup-b-{uuid4().hex}@example.test','password':'correct horse battery staple!'})
assert a.status_code == b.status_code == 201
headers={'Authorization':'Bearer '+a.json()['access_token']}
source=client.post('/v2/research/sources',headers=headers,json={'title':'Synthetic report','text':'Revenue was 100 million in 2024. Goods had a $36.7 billion decrease. Services increased $44.9 billion.','permissions':{'store':True,'display':True,'model_input':True,'export':True}})
assert source.status_code==201
sid=source.json()['source_id']
topic=client.post('/v2/research/topics',headers=headers,json={'question':'Revenue?','source_ids':[sid]})
assert topic.status_code==201
value=topic.json();path='/v2/research/topics/'+value['topic_id']; payload={'parent_version_id':value['versions'][0]['version_id']}
class Provider:
 name='backup-drill-stub'
 policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
 def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
  c={'source_id':sid,'quotation':'Revenue was 100 million in 2024.'}
  facts=[{'fact_id':'goods','label':'Goods change','value':'-36.7','unit':'billion USD','currency':'USD','period':'2024',
          'citation':{'source_id':sid,'quotation':'a $36.7 billion decrease'}},
         {'fact_id':'services','label':'Services change','value':'44.9','unit':'billion USD','currency':'USD','period':'2024',
          'citation':{'source_id':sid,'quotation':'increased $44.9 billion'}}]
  return {'claims':[{'text':'Revenue was 100 million.','citations':[c]}], 'counterarguments':[{'text':'Revenue alone does not establish profit.','citations':[c]}], 'facts':facts, 'calculations':[], 'limitations':[]}, 40
api.challenge_provider=Provider()
r=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'backup-drill-idempotency'},json=payload);assert r.status_code==202, r.text
op=r.json()['operation']['operation_id']
for _ in range(200):
 status=client.get(path+'/runs/'+op,headers=headers).json()['operation']['status']
 if status in ('completed','failed','cancelled'):break
 time.sleep(.05)
assert status=='completed', status
value=client.get(path,headers=headers).json();assert len(value['versions'])==2
r=client.post(path+'/facts/confirm-direction',headers=headers,json={'parent_version_id':value['versions'][-1]['version_id'],'fact_id':'goods'})
assert r.status_code==201,r.text
value=r.json()
r=client.post(path+'/calculations',headers=headers,json={'parent_version_id':value['versions'][-1]['version_id'],
  'formula':'difference','first_fact_id':'services','second_fact_id':'goods'})
assert r.status_code==201,r.text
value=r.json();assert value['versions'][-1]['content']['calculations'][0]['value']=='81.60'
r=client.post(path+'/versions',headers=headers,json={'parent_version_id':value['versions'][-1]['version_id'],'content':value['versions'][-1]['content'],'review':'confirmed','note':'Synthetic confirmation'})
assert r.status_code==201, r.text
state={'token':a.json()['access_token'],'other':b.json()['access_token'],'source_id':sid,'path':path,'operation_id':op,'payload':payload,'topic':r.json()}
with open(os.environ['STAGEB_BACKUP_TEST_STATE'],'w') as f: json.dump(state,f)
'''
        verify = r'''
import json, os
from types import SimpleNamespace
import app.main as api
from fastapi.testclient import TestClient
class MustNotCall:
 name='backup-replay-stub'
 policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
 def generate_structured(self,*a,**kw): raise AssertionError('Restored replay must not call provider')
api.challenge_provider=MustNotCall()
s=json.load(open(os.environ['STAGEB_BACKUP_TEST_STATE']));client=TestClient(api.app)
h={'Authorization':'Bearer '+s['token']};other={'Authorization':'Bearer '+s['other']};path=s['path']
r=client.get(path,headers=h);assert r.status_code==200 and r.json()==s['topic']
assert r.json()['versions'][-1]['review']=='confirmed'
assert r.json()['versions'][-1]['content']['claims'][0]['citations'][0]['quotation']=='Revenue was 100 million in 2024.'
fact=next(f for f in r.json()['versions'][-1]['content']['facts'] if f['fact_id']=='goods')
assert fact['direction_confirmation']['confirmed_by']=='user' and api.direction_user_confirmed(fact)
assert r.json()['versions'][-1]['content']['calculations'][0]['value']=='81.60'
assert client.get('/v2/research/sources/'+s['source_id'],headers=h).status_code==200
assert client.get(path,headers=other).status_code==404
assert client.get('/v2/research/sources/'+s['source_id'],headers=other).status_code==404
assert client.get(path+'/runs/'+s['operation_id']+'/inputs',headers=h).status_code==200
r=client.post(path+'/runs',headers={**h,'Idempotency-Key':'backup-drill-idempotency'},json=s['payload'])
assert r.status_code==202 and r.headers.get('Idempotency-Replayed')=='true' and r.json()['operation']['operation_id']==s['operation_id']
'''
        def run_api(code, port):
            env = dict(base_env, FORESIGHTNINE_POSTGRES_DSN=f"postgresql://postgres:disposable-only@127.0.0.1:{port}/research_test",
                       PYTHONPATH=str(root / "apps/api"))
            result = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stderr

        run_api(seed, ports[0])
        dump = docker("exec", names[0], "pg_dump", "-U", "postgres", "-d", "research_test", "--format=custom", "--no-owner", "--no-acl").stdout
        assert dump.startswith(b"PGDMP")
        docker("exec", "-i", names[1], "pg_restore", "-U", "postgres", "-d", "research_test", "--no-owner", "--no-acl", "--exit-on-error", input=dump)
        run_api(verify, ports[1])
    finally:
        for name in reversed(created):
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
