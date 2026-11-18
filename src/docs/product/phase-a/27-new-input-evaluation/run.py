"""One-shot evaluator; polling retries never resubmit a model request."""
from __future__ import annotations
import base64,hashlib,json,os,sys,time
from pathlib import Path
from uuid import uuid4
folder=Path(__file__).resolve().parent;root=folder.parents[3]
h=lambda b:hashlib.sha256(b).hexdigest()
mb=(folder/'manifest.json').read_bytes();manifest=json.loads(mb)
assert h((folder/'references.json').read_bytes())==manifest['references_sha256']
assert h((folder/'score.py').read_bytes())==manifest['scorer_sha256']
assert h((folder/'run.py').read_bytes())==manifest['runner_sha256']
for entry in manifest['inputs']:
 b=(folder/(entry['id']+'-input.json')).read_bytes();assert h(b)==entry['sha256']
 task=json.loads(b)
 if task['source_kind']=='original_pdf':assert h((folder/task['source_file']).read_bytes())==task['source_sha256']
assert manifest['max_calls_total']==len(manifest['inputs'])==2 and manifest['max_calls_per_task']==1
with (folder/'run-started.json').open('x') as f:json.dump({'manifest_sha256':h(mb),'status':'started_once','uncertain_calls_are_not_retried':True},f,indent=2)
os.environ.update(FORESIGHTNINE_LOCAL_AUTH_MODE='mock',FORESIGHTNINE_STORAGE_MODE='memory',FORESIGHTNINE_LLM_TIMEOUT_SECONDS='60')
sys.path.insert(0,str(root/'apps/api'))
import app.main as api
from fastapi.testclient import TestClient
provider=api.challenge_provider; original=provider.generate_structured;current_id=''
assert provider.name == 'openai-compatible' and provider.settings.model == manifest['model']
params=json.loads(provider._request_body(None,'probe',system='probe'));params.pop('messages')
assert params == manifest['generation_parameters'] and provider.policy.max_input_tokens == 3000 and provider.policy.timeout_seconds == 60

def capture(prompt,*,cancel_event,system,timeout_seconds):
 payload,used=original(prompt,cancel_event=cancel_event,system=system,timeout_seconds=timeout_seconds)
 serialized=json.dumps({'payload':payload,'reported_tokens':used},ensure_ascii=False,indent=2)
 for k in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY'):
  secret=os.environ.get(k)
  if secret:serialized=serialized.replace(secret,'[REDACTED]')
 (folder/(current_id+'-provider-output.json')).write_text(serialized+'\n')
 return payload,used
provider.generate_structured=capture
client=TestClient(api.app)
r=client.post('/v1/auth/local/register',json={'email':f'holdout-v3-{uuid4().hex}@example.test','password':'correct horse battery staple!'})
assert r.status_code==201
headers={'Authorization':'Bearer '+r.json()['access_token']}
for entry in manifest['inputs']:
 current_id=entry['id']; task=json.loads((folder/(current_id+'-input.json')).read_bytes());begin=time.monotonic();op=None;path=None;poll_errors=[]
 try:
  permissions={'store':True,'display':True,'model_input':True,'export':True}
  if task['source_kind']=='original_pdf':
   body={'title':task['source_title'],'pdf_base64':base64.b64encode((folder/task['source_file']).read_bytes()).decode(),
         'published_at':task['published_at'],'permissions':permissions};endpoint='/v2/research/sources/pdf'
  else:
   body={'title':task['source_title'],'text':task['source_text'],'url':task['source_url'],'permissions':permissions};endpoint='/v2/research/sources'
  r=client.post(endpoint,headers=headers,json=body)
  if r.status_code!=201:raise RuntimeError('source_setup_failed')
  source=r.json()['source_id'];r=client.post('/v2/research/topics',headers=headers,json={'question':task['question'],'source_ids':[source]})
  if r.status_code!=201:raise RuntimeError('topic_setup_failed')
  topic=r.json();path='/v2/research/topics/'+topic['topic_id']
  r=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'frozen-v3-'+current_id},json={'parent_version_id':topic['versions'][0]['version_id'],
      'max_search_calls':0,'max_model_calls':1,'support_review':False,'max_total_tokens':10000,'max_elapsed_seconds':120})
  if r.status_code!=202:raise RuntimeError('run_receipt_unconfirmed')
  op=r.json()['operation']['operation_id']
  (folder/(current_id+'-receipt.json')).write_text(json.dumps({'operation_id':op,'topic_id':topic['topic_id'],'manifest_sha256':h(mb)},indent=2)+'\n')
  status='unknown'
  while time.monotonic()-begin<150:
   try:
    check=client.get(path+'/runs/'+op,headers=headers)
    if check.status_code==200:
     status=check.json()['operation']['status']
     if status in ('completed','failed','cancelled'):break
    else:
     poll_errors.append(check.status_code)
     if check.status_code not in (429,503):break
   except Exception as e:
    poll_errors.append(type(e).__name__)
   time.sleep(2)
  if status not in ('completed','failed','cancelled'):
   result={'id':current_id,'operation_status':'unknown','manifest_sha256':h(mb),'operation_id':op,'poll_errors':poll_errors,'cost_usd':'unknown'}
  else:
   r=client.get(path,headers=headers)
   if r.status_code!=200:raise RuntimeError('result_fetch_failed')
   result={'id':current_id,'operation_status':status,'manifest_sha256':h(mb),'input_sha256':entry['sha256'],
      'elapsed_seconds_observed':round(time.monotonic()-begin,3),'poll_errors':poll_errors,'content':r.json()['versions'][-1]['content'],'cost_usd':'unknown'}
 except Exception as e:
  result={'id':current_id,'operation_status':'local_failure','error_type':type(e).__name__,'phase':str(e) if isinstance(e,RuntimeError) else 'unknown',
          'operation_id':op,'manifest_sha256':h(mb),'poll_errors':poll_errors,'cost_usd':'unknown'}
 (folder/(current_id+'-result.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({'id':current_id,'status':result['operation_status'],'model_calls':result.get('content',{}).get('run',{}).get('model_calls')},ensure_ascii=False),flush=True)
 if result['operation_status'] not in ('completed','failed','cancelled'):
  break
