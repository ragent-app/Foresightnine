"""Run the pre-registered bounded holdout once, using externally configured credentials.

No retry after an ambiguous external call. The reference file is only hashed here;
it is never included in a model prompt. Results do not assess semantic correctness.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

folder=Path(__file__).resolve().parent
root=folder.parents[3]
manifest_path=folder/'manifest.json'
manifest_bytes=manifest_path.read_bytes(); manifest=json.loads(manifest_bytes)
digest=lambda value: hashlib.sha256(value).hexdigest()
assert digest((folder/'references.json').read_bytes())==manifest['reference_sha256']
assert manifest['max_calls_total']==3 and manifest['max_calls_per_task']==1
for item in manifest['inputs']:
    assert digest((folder/(item['id']+'-input.json')).read_bytes())==item['sha256']
assert digest((root/'docs/product/phase-a/baseline/holdout-walmart-2024.json').read_bytes())==manifest['sources'][0]['input_sha256']
with (folder/'run-started.json').open('x') as f:
    json.dump({'manifest_sha256':digest(manifest_bytes),'status':'started_once','models_may_bill_even_if_response_lost':True},f,indent=2)
os.environ['FORESIGHTNINE_LOCAL_AUTH_MODE']='mock'
os.environ['FORESIGHTNINE_STORAGE_MODE']='memory'
os.environ['FORESIGHTNINE_LLM_TIMEOUT_SECONDS']='60'
sys.path.insert(0,str(root/'apps/api'))
import app.main as api
from fastapi.testclient import TestClient

provider=api.challenge_provider
original=provider.generate_structured
current_task=''
def capture(prompt,*,cancel_event,system,timeout_seconds):
    payload,usage=original(prompt,cancel_event=cancel_event,system=system,timeout_seconds=timeout_seconds)
    serialized=json.dumps({'payload':payload,'reported_tokens':usage},ensure_ascii=False,indent=2)
    for key in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY'):
        secret=os.environ.get(key)
        if secret: serialized=serialized.replace(secret,'[REDACTED]')
    (folder/(current_task+'-provider-output.json')).write_text(serialized+'\n')
    return payload,usage
provider.generate_structured=capture
client=TestClient(api.app)
r=client.post('/v1/auth/local/register',json={'email':f'holdout-{uuid4().hex}@example.test','password':'correct horse battery staple!'})
assert r.status_code==201,r.status_code
headers={'Authorization':'Bearer '+r.json()['access_token']}
for item in manifest['inputs']:
    current_task=item['id']; started=time.monotonic()
    try:
        task=json.loads((folder/(current_task+'-input.json')).read_text())
        r=client.post('/v2/research/sources',headers=headers,json={'title':task['source_title'],'text':task['source_text'],'url':task['source_url'],
            'permissions':{'store':True,'display':True,'model_input':True,'export':True}})
        assert r.status_code==201, f'source status {r.status_code}'
        source=r.json()['source_id']
        r=client.post('/v2/research/topics',headers=headers,json={'question':task['question'],'source_ids':[source]})
        assert r.status_code==201, f'topic status {r.status_code}'
        topic=r.json();path='/v2/research/topics/'+topic['topic_id']
        r=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'frozen-holdout-'+current_task},json={'parent_version_id':topic['versions'][0]['version_id'],
          'max_search_calls':0,'max_model_calls':1,'support_review':False,'max_total_tokens':10000,'max_elapsed_seconds':120})
        assert r.status_code==202, f'run status {r.status_code}'
        op=r.json()['operation']['operation_id'];status='unknown'
        while time.monotonic()-started<140:
            status=client.get(path+'/runs/'+op,headers=headers).json()['operation']['status']
            if status in ('completed','failed','cancelled'):break
            time.sleep(.5)
        content=client.get(path,headers=headers).json()['versions'][-1]['content']
        result={'id':current_task,'operation_status':status,'input_sha256':item['sha256'],'manifest_sha256':digest(manifest_bytes),
                'elapsed_seconds_observed':round(time.monotonic()-started,3),'content':content,'cost_usd':'unknown'}
    except Exception as error:
        result={'id':current_task,'operation_status':'local_failure','error_type':type(error).__name__,
                'manifest_sha256':digest(manifest_bytes),'cost_usd':'unknown'}
    (folder/(current_task+'-result.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'id':current_task,'status':result['operation_status'],'model_calls':result.get('content',{}).get('run',{}).get('model_calls')},ensure_ascii=False),flush=True)
