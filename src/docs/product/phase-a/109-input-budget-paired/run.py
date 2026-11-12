"""One run per frozen task; never retry an uncertain external call."""
from pathlib import Path
from hashlib import sha256
from uuid import UUID,uuid4
from unittest.mock import patch
import base64,json,os,subprocess,sys,time
from dataclasses import replace
p=Path(__file__).resolve().parent;root=p.parents[3];h=lambda b:sha256(b).hexdigest();mb=(p/'manifest.json').read_bytes();m=json.loads(mb)
for name,digest in m['script_sha256'].items():assert h((p/name).read_bytes())==digest
assert h((p/'references.json').read_bytes())==m['references_sha256']
for entry in m['tasks']:
 data=(p/(entry['id']+'-input.json')).read_bytes();assert h(data)==entry['input_sha256'];task=json.loads(data)
 for source in task['sources']:
  if source['kind']=='pdf':assert h((p/source['file']).read_bytes())==source['sha256'] and h((p/source['text_file']).read_bytes())==source['text_sha256']
assert m['max_calls_total']==len(m['tasks'])*m['max_calls_per_task']==2 and m['retry_external_calls']==0
for line in json.loads(subprocess.check_output(['docker','inspect','--format','{{json .Config.Env}}','docker-api-1'])):
 key,sep,value=line.partition('=')
 if sep and (key.startswith('FORESIGHTNINE_LLM_') or key=='FORESIGHTNINE_GLM_CODING_API_KEY'):os.environ[key]=value
os.environ.update(FORESIGHTNINE_LOCAL_AUTH_MODE='mock',FORESIGHTNINE_STORAGE_MODE='memory',FORESIGHTNINE_LLM_TIMEOUT_SECONDS='60')
sys.path.insert(0,str(root/'apps/api'));import app.main as api
from fastapi.testclient import TestClient
assert h(api.RESEARCH_SYSTEM_PROMPT.encode())==m['research_system_prompt_sha256'] and h(api.REVIEW_SYSTEM_PROMPT.encode())==m['review_system_prompt_sha256'] and api.REVIEW_METHOD==m['review_method']
provider=api.challenge_provider;provider.policy=replace(provider.policy,max_input_tokens=6000);original=provider.generate_structured;current=''
assert provider.name=='openai-compatible' and provider.settings.model==m['model']
params=json.loads(provider._request_body(None,'probe',system='probe'));params.pop('messages');assert params==m['generation_parameters'] and provider.policy.max_input_tokens==6000 and provider.policy.timeout_seconds==60
with (p/'run-started.json').open('x') as f:json.dump({'manifest_sha256':h(mb),'status':'started_once','uncertain_calls_are_not_retried':True},f,indent=2)
def capture(prompt,*,cancel_event,system,timeout_seconds):
 assert h(prompt.encode()) == m['prompt_sha256_by_task'][current] and system == api.RESEARCH_SYSTEM_PROMPT
 payload,used=original(prompt,cancel_event=cancel_event,system=system,timeout_seconds=timeout_seconds)
 kind='review' if system==api.REVIEW_SYSTEM_PROMPT else 'generation';assert system in (api.RESEARCH_SYSTEM_PROMPT,api.REVIEW_SYSTEM_PROMPT)
 data=json.dumps({'source_attribution':m['source_attributions'][current],'payload':payload,'reported_tokens':used,'prompt_sha256':h(prompt.encode()),'system_prompt_sha256':h(system.encode())},ensure_ascii=False,indent=2)
 for key in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY'):
  secret=os.environ.get(key)
  if secret:data=data.replace(secret,'[REDACTED]')
 (p/(current+'-'+kind+'-provider-output.json')).write_text(data+'\n');return payload,used
provider.generate_structured=capture;client=TestClient(api.app)
r=client.post('/v1/auth/local/register',json={'email':f'eval70-{uuid4().hex}@example.test','password':'correct horse battery staple!'});assert r.status_code==201
headers={'Authorization':'Bearer '+r.json()['access_token']}
for entry in m['tasks']:
 current=entry['id'];task=json.loads((p/(current+'-input.json')).read_text());begin=time.monotonic();op=None;errors=[]
 try:
  ids=[]
  for source in task['sources']:
   permissions={'store':True,'display':True,'model_input':True,'export':True}
   if source['kind']=='pdf':body={'title':source['title'],'pdf_base64':base64.b64encode((p/source['file']).read_bytes()).decode(),'published_at':source['published_at'],'text_order':'reading','permissions':permissions};endpoint='/v2/research/sources/pdf'
   else:body={'title':source['title'],'text':source['text'],'url':source['url'],'published_at':source['published_at'],'permissions':permissions};endpoint='/v2/research/sources'
   with patch('app.research_sources.uuid4',return_value=UUID(source['source_id'].removeprefix('source_'))):r=client.post(endpoint,headers=headers,json=body)
   if r.status_code!=201:raise RuntimeError('source_setup_failed')
   assert r.json()['source_id']==source['source_id'];ids.append(source['source_id'])
  r=client.post('/v2/research/topics',headers=headers,json={'question':task['question'],'source_ids':ids})
  if r.status_code!=201:raise RuntimeError('topic_setup_failed')
  topic=r.json();path='/v2/research/topics/'+topic['topic_id'];r=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'eval70-'+current},json={'parent_version_id':topic['versions'][0]['version_id'],'max_search_calls':0,'max_model_calls':1,'support_review':False,'max_total_tokens':10000,'max_elapsed_seconds':120})
  if r.status_code!=202:raise RuntimeError('run_receipt_unconfirmed')
  op=r.json()['operation']['operation_id'];(p/(current+'-receipt.json')).write_text(json.dumps({'manifest_sha256':h(mb),'operation_id':op,'source_ids':ids},indent=2)+'\n');status='unknown'
  while time.monotonic()-begin<150:
   try:
    check=client.get(path+'/runs/'+op,headers=headers)
    if check.status_code==200:
     status=check.json()['operation']['status']
     if status in ('completed','failed','cancelled'):break
    else:
     errors.append(check.status_code)
     if check.status_code not in (429,503):break
   except Exception as e:errors.append(type(e).__name__)
   time.sleep(2)
  result={'source_attribution':m['source_attributions'][current],'id':current,'manifest_sha256':h(mb),'operation_status':status,'operation_id':op,'poll_errors':errors,'elapsed_seconds_observed':round(time.monotonic()-begin,3),'cost_usd':'unknown'}
  if status in ('completed','failed','cancelled'):
   r=client.get(path,headers=headers)
   if r.status_code!=200:raise RuntimeError('result_fetch_failed')
   result['content']=r.json()['versions'][-1]['content']
 except Exception as e:
  result={'source_attribution':m['source_attributions'][current],'id':current,'manifest_sha256':h(mb),'operation_status':'local_failure','phase':str(e) if isinstance(e,RuntimeError) else type(e).__name__,'operation_id':op,'poll_errors':errors,'cost_usd':'unknown'}
 (p/(current+'-result.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'id':current,'status':result['operation_status'],'model_calls':result.get('content',{}).get('run',{}).get('model_calls')},ensure_ascii=False),flush=True)
 usage=result.get('content',{}).get('run',{}).get('total_tokens')
 if result['operation_status'] not in ('completed','failed','cancelled') or type(usage) is not int or usage>m['max_tokens_per_task']:break
