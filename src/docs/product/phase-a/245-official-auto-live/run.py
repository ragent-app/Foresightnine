from pathlib import Path
from hashlib import sha256
from datetime import date
from uuid import uuid4
import json, os, subprocess, sys, time
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[3];sys.path.insert(0,str(ROOT/'apps/api'))
sha=lambda data:sha256(data).hexdigest()
mb=(HERE/'manifest.json').read_bytes();m=json.loads(mb);mh=sha(mb)
assert all(sha((HERE/f).read_bytes())==v for f,v in m['files'].items())
assert sha((HERE/'source.html').read_bytes())==m['source_sha256'] and sha((HERE/'source.txt').read_bytes())==m['text_sha256']
subprocess.run(['git','diff','--quiet',m['implementation_commit'],'--','apps'],cwd=ROOT,check=True)
for line in json.loads(subprocess.check_output(['docker','inspect','--format','{{json .Config.Env}}','docker-api-1'])):
 key,sep,value=line.partition('=')
 if sep and (key.startswith('FORESIGHTNINE_LLM_') or key=='FORESIGHTNINE_GLM_CODING_API_KEY'):os.environ[key]=value
os.environ.update(FORESIGHTNINE_STORAGE_MODE='memory',FORESIGHTNINE_LOCAL_AUTH_MODE='mock',FORESIGHTNINE_LLM_TIMEOUT_SECONDS='60',FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS='6000')
import app.main as api
from app.glm_mcp_search import SearchCandidate
from app.research_fetch import FetchFailure
from fastapi.testclient import TestClient
assert sha(api.RESEARCH_SYSTEM_PROMPT.encode())==m['system_sha256'] and api.RESEARCH_SELECTION_METHOD==m['selection_method']
api.challenge_provider=api.challenge_provider.for_research();provider=api.challenge_provider
assert provider.name=='openai-compatible' and provider.settings.model==m['model']
assert provider.policy.max_input_tokens==6000 and provider.policy.max_output_tokens==4000
params=json.loads(provider._request_body(None,'probe',system='probe'));params.pop('messages');assert params==m['generation_parameters']
class FixedSearch:
 name='preselected-test-candidate';supports_project_search=True
 def search(self,query,*,limit,deadline,cancel_event):
  assert deadline>time.monotonic() and limit==5
  return [SearchCandidate(title='BEA advance estimate',url=m['source_url'],excerpt='Search lead only',publisher='BEA',published_at=date(2024,1,25),independently_reviewable=True)]
api.evidence_provider=FixedSearch();original_fetch=api.fetch_official_text;original_generate=provider.generate_structured
calls={'fetch':0,'model':0}
with (HERE/'run-started.json').open('x') as handle:json.dump({'manifest_sha256':mh,'status':'started_once'},handle,indent=2)
def checked_fetch(request,*,timeout_seconds):
 calls['fetch']+=1;assert calls['fetch']==1 and timeout_seconds<=15
 result=original_fetch(request,timeout_seconds=timeout_seconds)
 if result.response_sha256!=m['source_sha256'] or sha(result.text.encode())!=m['text_sha256']:
  raise FetchFailure('source_snapshot_changed')
 return result
def checked_generate(prompt,*,cancel_event,system,timeout_seconds):
 assert calls['model']==0 and system==api.RESEARCH_SYSTEM_PROMPT and sha(prompt.encode())==m['prompt_sha256']
 calls['model']+=1
 raw,used=original_generate(prompt,cancel_event=cancel_event,system=system,timeout_seconds=timeout_seconds)
 value=json.dumps({'manifest_sha256':mh,'payload':raw,'reported_tokens':used,'prompt_sha256':sha(prompt.encode())},ensure_ascii=False,indent=2)
 for key in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY'):
  if os.environ.get(key):value=value.replace(os.environ[key],'[REDACTED]')
 (HERE/'provider.json').write_text(value+'\n')
 return raw,used
api.fetch_official_text=checked_fetch;provider.generate_structured=checked_generate
client=TestClient(api.app);started=time.monotonic();operation_id=None
try:
 auth=client.post('/v1/auth/local/register',json={'email':'auto-live-'+uuid4().hex+'@example.test','password':'correct horse battery staple!'})
 assert auth.status_code==201;headers={'Authorization':'Bearer '+auth.json()['access_token']}
 topic=client.post('/v2/research/topics',headers=headers,json={'question':m['question']});assert topic.status_code==201
 path='/v2/research/topics/'+topic.json()['topic_id']
 receipt=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'auto-live-one'},json={
   'parent_version_id':topic.json()['versions'][0]['version_id'],'auto_fetch_official':True,'require_counterarguments':False,
   'max_search_calls':1,'max_model_calls':1,'max_input_tokens_per_call':6000,'max_total_tokens':10000,'max_elapsed_seconds':120})
 assert receipt.status_code==202;operation_id=receipt.json()['operation']['operation_id']
 (HERE/'receipt.json').write_text(json.dumps({'manifest_sha256':mh,'operation_id':operation_id},indent=2)+'\n')
 status='unknown';errors=[]
 while time.monotonic()-started<150:
  response=client.get(path+'/runs/'+operation_id,headers=headers)
  if response.status_code==200:
   status=response.json()['operation']['status']
   if status in ('completed','failed','cancelled'):break
  else:
   errors.append(response.status_code)
   if response.status_code not in (429,503):break
  time.sleep(2)
 out={'status':status,'operation_id':operation_id,'manifest_sha256':mh,'poll_errors':errors,'cost_usd':'unknown'}
 if status in ('completed','failed','cancelled'):
  updated=client.get(path,headers=headers);assert updated.status_code==200
  current=updated.json()['versions'][-1];out['content']=current['content'];out['source_ids']=current.get('source_ids',[])
  if out['source_ids']:
   source_id=out['source_ids'][0];original=client.get('/v2/research/sources/'+source_id+'/original',headers=headers)
   out['original_download_status']=original.status_code
   if original.status_code==200:out['original_download_sha256']=sha(original.content)
   export=client.get(path+'/versions/'+current['version_id']+'/export',headers=headers);out['export_status']=export.status_code
except Exception as error:
 out={'status':'local_failure','operation_id':operation_id,'manifest_sha256':mh,'error':type(error).__name__,'cost_usd':'unknown'}
out['calls']=calls;out['elapsed_seconds']=round(time.monotonic()-started,3)
serialized=json.dumps(out,ensure_ascii=False,indent=2)
for key in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY'):
 if os.environ.get(key):serialized=serialized.replace(os.environ[key],'[REDACTED]')
(HERE/'result.json').write_text(serialized+'\n')
print(json.dumps({'status':out['status'],'calls':calls,'reported_tokens':out.get('content',{}).get('run',{}).get('total_tokens')}),flush=True)
