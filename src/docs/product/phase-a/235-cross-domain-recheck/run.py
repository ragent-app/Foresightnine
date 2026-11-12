from common import *
from uuid import UUID,uuid4
from unittest.mock import patch
import base64,os,subprocess,time
mbytes=(HERE/'manifest.json').read_bytes();m=json.loads(mbytes);mh=digest(mbytes)
assert all(digest((HERE/f).read_bytes())==v for f,v in m['files'].items())
assert all(digest((SOURCE/f).read_bytes())==v for f,v in m['source_files'].items())
subprocess.run(['git','diff','--quiet',m['implementation_commit'],'--','apps'],cwd=ROOT,check=True)
for line in json.loads(subprocess.check_output(['docker','inspect','--format','{{json .Config.Env}}','docker-api-1'])):
 key,sep,value=line.partition('=')
 if sep and (key.startswith('FORESIGHTNINE_LLM_') or key=='FORESIGHTNINE_GLM_CODING_API_KEY'):os.environ[key]=value
os.environ.update(FORESIGHTNINE_STORAGE_MODE='memory',FORESIGHTNINE_LOCAL_AUTH_MODE='mock',
                  FORESIGHTNINE_LLM_TIMEOUT_SECONDS='60',FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS='6000')
import app.main as api
from fastapi.testclient import TestClient
assert digest(api.RESEARCH_SYSTEM_PROMPT.encode())==m['system_sha256']
assert digest(api.REVIEW_SYSTEM_PROMPT.encode())==m['review_system_sha256'] and api.REVIEW_METHOD==m['review_method']
api.challenge_provider=api.challenge_provider.for_research()
provider=api.challenge_provider;assert provider.name=='openai-compatible' and provider.settings.model==m['model']
assert provider.policy.max_input_tokens==6000 and provider.policy.max_output_tokens==4000
params=json.loads(provider._request_body(None,'probe',system='probe'));params.pop('messages');assert params==m['generation_parameters']
original=provider.generate_structured;current='';call_counts={}
with (HERE/'run-started.json').open('x') as f:json.dump({'manifest_sha256':mh,'status':'started_once'},f,indent=2)
def capture(prompt,*,cancel_event,system,timeout_seconds):
 kind='generation' if system==api.RESEARCH_SYSTEM_PROMPT else 'review'
 assert system in (api.RESEARCH_SYSTEM_PROMPT,api.REVIEW_SYSTEM_PROMPT)
 call_counts[current]=call_counts.get(current,0)+1;assert call_counts[current]<=1 and sum(call_counts.values())<=3
 if kind=='generation':assert digest(prompt.encode())==next(c['prompt_sha256'] for c in m['conditions'] if c['task']==current)
 raw,used=original(prompt,cancel_event=cancel_event,system=system,timeout_seconds=timeout_seconds)
 value=json.dumps({'manifest_sha256':mh,'kind':kind,'payload':raw,'reported_tokens':used,
                   'prompt_sha256':digest(prompt.encode()),'system_sha256':digest(system.encode())},ensure_ascii=False,indent=2)
 for key in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY'):
  if os.environ.get(key):value=value.replace(os.environ[key],'[REDACTED]')
 (HERE/(current+'-'+kind+'-'+str(call_counts[current])+'-provider.json')).write_text(value+'\n')
 return raw,used
provider.generate_structured=capture
client=TestClient(api.app)
reg=client.post('/v1/auth/local/register',json={'email':'pipeline-'+uuid4().hex+'@example.test','password':'correct horse battery staple!'})
assert reg.status_code==201;headers={'Authorization':'Bearer '+reg.json()['access_token']}
total_usage=0
for condition in m['conditions']:
 if total_usage+10000>m['max_total_tokens']:break
 current=condition['task'];task,sources=get_task(current);started=time.monotonic();op=None
 try:
  permissions={'store':True,'display':True,'model_input':True,'export':True}
  endpoint='/v2/research/sources/pdf';body={'title':task['source'],'pdf_base64':base64.b64encode((SOURCE/task['source']).read_bytes()).decode(),
      'text_order':'layout','permissions':permissions}
  with patch('app.research_sources.uuid4',return_value=UUID(task['source_id'].removeprefix('source_'))):
   created=client.post(endpoint,headers=headers,json=body)
  assert created.status_code==201 and created.json()['source_id']==task['source_id']
  assert created.json()['content_sha256']==sources[0].content_sha256
  topic=client.post('/v2/research/topics',headers=headers,json={'question':task['question'],'source_ids':[task['source_id']]})
  assert topic.status_code==201
  path='/v2/research/topics/'+topic.json()['topic_id']
  receipt=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'pipeline-'+current},json={
    'parent_version_id':topic.json()['versions'][0]['version_id'],'max_search_calls':0,'max_model_calls':1,
    'support_review':False,'require_counterarguments':task['require_counterarguments'],'max_input_tokens_per_call':6000,'max_total_tokens':10000,'max_elapsed_seconds':120})
  assert receipt.status_code==202;op=receipt.json()['operation']['operation_id']
  (HERE/(current+'-receipt.json')).write_text(json.dumps({'manifest_sha256':mh,'operation_id':op},indent=2)+'\n')
  status='unknown';poll_errors=[]
  while time.monotonic()-started<150:
   check=client.get(path+'/runs/'+op,headers=headers)
   if check.status_code==200:
    status=check.json()['operation']['status']
    if status in ('completed','failed','cancelled'):break
   else:
    poll_errors.append(check.status_code)
    if check.status_code not in (429,503):break
   time.sleep(2)
  out={'task':current,'status':status,'operation_id':op,'manifest_sha256':mh,'poll_errors':poll_errors,'cost_usd':'unknown'}
  if status in ('completed','failed','cancelled'):
   updated=client.get(path,headers=headers);assert updated.status_code==200
   versions=updated.json()['versions'];out['content']=versions[-1]['content']
   out['versions']=[{'version_id':v['version_id'],'parent_version_id':v.get('parent_version_id'),
                     'run_status':v['content'].get('run',{}).get('status'),
                     'checkpoint':v['content'].get('run',{}).get('checkpoint')} for v in versions]
 except Exception as error:
  out={'task':current,'status':'local_failure','operation_id':op,'manifest_sha256':mh,'error':type(error).__name__,'cost_usd':'unknown'}
 out['elapsed_seconds']=round(time.monotonic()-started,3)
 serialized=json.dumps(out,ensure_ascii=False,indent=2)
 for key in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY'):
  if os.environ.get(key):serialized=serialized.replace(os.environ[key],'[REDACTED]')
 (HERE/(current+'-result.json')).write_text(serialized+'\n')
 usage=out.get('content',{}).get('run',{}).get('total_tokens')
 print(json.dumps({'task':current,'status':out['status'],'reported_tokens':usage,'calls':call_counts.get(current,0)}),flush=True)
 if out['status'] not in ('completed','failed','cancelled') or type(usage) is not int or usage>10000:break
 total_usage+=usage
