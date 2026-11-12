from pathlib import Path
from hashlib import sha256
from uuid import UUID,uuid4
from unittest.mock import patch
import base64,gzip,json,os,subprocess,sys,time
p=Path(__file__).resolve().parent;root=p.parents[3];sys.path.insert(0,str(root/'apps/api'))
h=lambda b:sha256(b).hexdigest();m=json.loads((p/'manifest.json').read_text());task=json.loads((p/'task.json').read_text())
for f,k in [('task.json','task_sha256'),('prepare.py','prepare_sha256'),('run.py','run_sha256')]:assert h((p/f).read_bytes())==m[k]
subprocess.run(['git','diff','--quiet',m['implementation_commit'],'--','apps'],cwd=root,check=True)
raw=gzip.decompress((root/'docs/product/phase-a/60-reserved-full-review/cisco-2024-sec.html.gz').read_bytes());assert h(raw)==m['source_hash']
for line in json.loads(subprocess.check_output(['docker','inspect','--format','{{json .Config.Env}}','docker-api-1'])):
 key,sep,value=line.partition('=')
 if sep and (key.startswith('FORESIGHTNINE_LLM_') or key=='FORESIGHTNINE_GLM_CODING_API_KEY'):os.environ[key]=value
os.environ.update(FORESIGHTNINE_STORAGE_MODE='memory',FORESIGHTNINE_LOCAL_AUTH_MODE='mock',FORESIGHTNINE_LLM_TIMEOUT_SECONDS='60',FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS='6000')
import app.main as api
from fastapi.testclient import TestClient
assert h(api.RESEARCH_SYSTEM_PROMPT.encode())==m['system_sha256']
api.challenge_provider=api.challenge_provider.for_research();provider=api.challenge_provider
assert provider.name=='openai-compatible' and provider.settings.model==m['model']
assert provider.policy.max_input_tokens==6000 and provider.policy.max_output_tokens==4000
params=json.loads(provider._request_body(None,'probe',system='probe'));params.pop('messages')
assert params=={'do_sample':False,'max_tokens':4000,'model':'glm-5.3-flash','reasoning_effort':'low','response_format':{'type':'json_object'},'thinking':{'type':'enabled'}}
(p/'run-started.json').open('x').write(json.dumps({'manifest_sha256':h((p/'manifest.json').read_bytes()),'status':'started_once'})+'\n')
original=provider.generate_structured;calls=[]
def capture(prompt,*,cancel_event,system,timeout_seconds):
 assert system==api.RESEARCH_SYSTEM_PROMPT and not calls and h(prompt.encode())==m['prompt']['sha256']
 calls.append(True);result,used=original(prompt,cancel_event=cancel_event,system=system,timeout_seconds=timeout_seconds)
 out=json.dumps({'payload':result,'reported_tokens':used,'prompt_sha256':h(prompt.encode())},ensure_ascii=False,indent=2)
 for key in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY'):
  if os.environ.get(key):out=out.replace(os.environ[key],'[REDACTED]')
 (p/'provider.json').write_text(out+'\n');return result,used
provider.generate_structured=capture
client=TestClient(api.app);session=client.post('/v1/auth/local/register',json={'email':uuid4().hex+'@example.test','password':'correct horse battery staple!'}).json();headers={'Authorization':'Bearer '+session['access_token']}
with patch('app.research_sources.uuid4',return_value=UUID(task['source_id'].removeprefix('source_'))):
 r=client.post('/v2/research/sources/html',headers=headers,json={'title':task['title'],'html_base64':base64.b64encode(raw).decode(),
  'permissions':{'store':True,'display':True,'model_input':True,'export':True}})
assert r.status_code==201 and r.json()['source_id']==task['source_id'] and r.json()['content_sha256']==m['text_hash']
topic=client.post('/v2/research/topics',headers=headers,json={'question':task['question'],'source_ids':[task['source_id']]}).json();path='/v2/research/topics/'+topic['topic_id']
r=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'html-company-pilot'},json={'parent_version_id':topic['versions'][0]['version_id'],
 'max_search_calls':0,'max_model_calls':1,'support_review':False,'require_counterarguments':True,'max_input_tokens_per_call':6000,'max_total_tokens':10000,'max_elapsed_seconds':120})
assert r.status_code==202;op=r.json()['operation']['operation_id'];(p/'receipt.json').write_text(json.dumps({'operation_id':op})+'\n')
start=time.monotonic();status='unknown'
while time.monotonic()-start<150:
 r=client.get(path+'/runs/'+op,headers=headers);assert r.status_code==200
 status=r.json()['operation']['status']
 if status in ('completed','failed','cancelled'):break
 time.sleep(2)
out={'status':status,'elapsed_seconds':round(time.monotonic()-start,3),'cost_usd':'unknown'}
if status in ('completed','failed','cancelled'):out['content']=client.get(path,headers=headers).json()['versions'][-1]['content']
encoded=json.dumps(out,ensure_ascii=False,indent=2)
for key in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY'):
 if os.environ.get(key):encoded=encoded.replace(os.environ[key],'[REDACTED]')
(p/'result.json').write_text(encoded+'\n')
print(json.dumps({'status':status,'calls':len(calls),'tokens':out.get('content',{}).get('run',{}).get('total_tokens')}))
