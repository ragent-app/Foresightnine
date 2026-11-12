"""One shot, two official PDFs; no retry after an uncertain external call."""
from pathlib import Path
from hashlib import sha256
from uuid import uuid4
import base64,json,os,sys,time
p=Path(__file__).resolve().parent;root=p.parents[3];h=lambda b:sha256(b).hexdigest()
mb=(p/'manifest.json').read_bytes();m=json.loads(mb)
for name,key in [('run.py','runner_sha256'),('score.py','scorer_sha256'),('input.json','input_sha256')]:assert h((p/name).read_bytes())==m[key]
for s in m['sources']:
 assert h((p/s['pdf']).read_bytes())==s['pdf_sha256'] and h((p/s['text']).read_bytes())==s['text_sha256']
with (p/'run-started.json').open('x') as f:json.dump({'manifest_sha256':h(mb),'status':'started_once','uncertain_call_is_not_retried':True},f,indent=2)
os.environ.update(FORESIGHTNINE_LOCAL_AUTH_MODE='mock',FORESIGHTNINE_STORAGE_MODE='memory',FORESIGHTNINE_LLM_TIMEOUT_SECONDS='60')
sys.path.insert(0,str(root/'apps/api'))
import app.main as api
from fastapi.testclient import TestClient
provider=api.challenge_provider;original=provider.generate_structured
assert provider.name=='openai-compatible' and provider.settings.model==m['model']
params=json.loads(provider._request_body(None,'probe',system='probe'));params.pop('messages')
assert params==m['generation_parameters'] and provider.policy.max_input_tokens==3000 and provider.policy.timeout_seconds==60
calls=0
def capture(prompt,*,cancel_event,system,timeout_seconds):
 global calls
 calls+=1
 if calls>1:raise AssertionError('Call limit exceeded')
 payload,used=original(prompt,cancel_event=cancel_event,system=system,timeout_seconds=timeout_seconds)
 serialized=json.dumps({'payload':payload,'reported_tokens':used},ensure_ascii=False,indent=2)
 for key in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY'):
  secret=os.environ.get(key)
  if secret:serialized=serialized.replace(secret,'[REDACTED]')
 (p/'provider-output.json').write_text(serialized+'\n')
 return payload,used
provider.generate_structured=capture; client=TestClient(api.app)
r=client.post('/v1/auth/local/register',json={'email':f'revision-{uuid4().hex}@example.test','password':'correct horse battery staple!'})
assert r.status_code==201
headers={'Authorization':'Bearer '+r.json()['access_token']};ids=[]
for s in m['sources']:
 body={'title':s['title'],'pdf_base64':base64.b64encode((p/s['pdf']).read_bytes()).decode(),'published_at':s['published_at'],
       'permissions':{'store':True,'display':True,'model_input':True,'export':True}}
 r=client.post('/v2/research/sources/pdf',headers=headers,json=body);assert r.status_code==201,r.text
 ids.append(r.json()['source_id'])
task=json.loads((p/'input.json').read_text());r=client.post('/v2/research/topics',headers=headers,json={'question':task['question'],'source_ids':ids})
assert r.status_code==201;topic=r.json();path='/v2/research/topics/'+topic['topic_id'];started=time.monotonic()
r=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'revision-eval-29'},json={'parent_version_id':topic['versions'][0]['version_id'],
 'max_search_calls':0,'max_model_calls':1,'support_review':False,'max_total_tokens':10000,'max_elapsed_seconds':120})
assert r.status_code==202;op=r.json()['operation']['operation_id']
(p/'receipt.json').write_text(json.dumps({'manifest_sha256':h(mb),'operation_id':op,'source_ids':ids},indent=2)+'\n')
status='unknown';errors=[]
while time.monotonic()-started<150:
 try:
  check=client.get(path+'/runs/'+op,headers=headers)
  if check.status_code==200:
   status=check.json()['operation']['status']
   if status in ('completed','failed','cancelled'):break
  else: errors.append(check.status_code)
 except Exception as error:errors.append(type(error).__name__)
 time.sleep(2)
result={'manifest_sha256':h(mb),'operation_status':status,'source_ids':ids,'elapsed_seconds_observed':round(time.monotonic()-started,3),'poll_errors':errors,'cost_usd':'unknown'}
if status in ('completed','failed','cancelled'):
 r=client.get(path,headers=headers);assert r.status_code==200
 result['content']=r.json()['versions'][-1]['content']
(p/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'status':status,'calls':calls},ensure_ascii=False),flush=True)
