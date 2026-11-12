from common import *
import base64,os,subprocess,time
from uuid import UUID,uuid4
from unittest.mock import patch
mbytes=(HERE/'manifest.json').read_bytes();m=json.loads(mbytes);mh=digest(mbytes)
assert all(digest((HERE/name).read_bytes())==value for name,value in m['files'].items())
assert digest((DATA/'input.json').read_bytes())==m['task_sha256']
q,sources,source_data=inputs();prompt,segments=prompt_for(q,sources)
assert digest(prompt.encode())==m['prompt_sha256'] and segments==m['segments']
subprocess.run(['git','diff','--quiet',m['implementation_commit'],'--','apps'],cwd=ROOT,check=True)
for line in json.loads(subprocess.check_output(['docker','inspect','--format','{{json .Config.Env}}','docker-api-1'])):
    key,sep,value=line.partition('=')
    if sep and (key.startswith('FORESIGHTNINE_LLM_') or key=='FORESIGHTNINE_GLM_CODING_API_KEY'): os.environ[key]=value
os.environ.update(FORESIGHTNINE_LOCAL_AUTH_MODE='mock',FORESIGHTNINE_STORAGE_MODE='memory',
                  FORESIGHTNINE_LLM_TIMEOUT_SECONDS='60',FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS='6000')
import app.main as api
from fastapi.testclient import TestClient
assert digest(api.RESEARCH_SYSTEM_PROMPT.encode())==m['system_sha256']
assert api.REVIEW_METHOD==m['review_method'] and digest(api.REVIEW_SYSTEM_PROMPT.encode())==m['review_system_sha256']
api.challenge_provider=api.challenge_provider.for_research();provider=api.challenge_provider
assert provider.name=='openai-compatible' and provider.settings.model==m['model']
params=json.loads(provider._request_body(None,'probe',system='probe'));params.pop('messages')
assert params==m['generation_parameters'] and provider.policy.max_output_tokens==m['output_limit']
original=provider.generate_structured;calls=0
with (HERE/'run-started.json').open('x') as f:json.dump({'manifest_sha256':mh,'status':'started_once'},f,indent=2)
def generate(prompt,*,cancel_event,system,timeout_seconds):
    global calls
    calls+=1;assert calls<=m['max_calls'] and system in (api.RESEARCH_SYSTEM_PROMPT,api.REVIEW_SYSTEM_PROMPT)
    kind='generation' if system==api.RESEARCH_SYSTEM_PROMPT else 'review'
    if kind=='generation': assert digest(prompt.encode())==m['prompt_sha256']
    raw,used=original(prompt,cancel_event=cancel_event,system=system,timeout_seconds=timeout_seconds)
    serialized=json.dumps({'manifest_sha256':mh,'payload':raw,'reported_tokens':used,
                           'prompt_sha256':digest(prompt.encode()),'system_sha256':digest(system.encode())},ensure_ascii=False,indent=2)
    for key in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY'):
        if os.environ.get(key): serialized=serialized.replace(os.environ[key],'[REDACTED]')
    (HERE/f'{kind}-{calls}-provider.json').write_text(serialized+'\n')
    return raw,used
provider.generate_structured=generate
client=TestClient(api.app);reg=client.post('/v1/auth/local/register',json={'email':'revision-'+uuid4().hex+'@example.test','password':'correct horse battery staple!'})
assert reg.status_code==201;headers={'Authorization':'Bearer '+reg.json()['access_token']};ids=[]
for source,item in zip(sources,source_data):
    body={'title':source.title,'pdf_base64':base64.b64encode((DATA/item['pdf']).read_bytes()).decode(),
          'published_at':item['published_at'],'text_order':'layout',
          'permissions':{'store':True,'display':True,'model_input':True,'export':True}}
    with patch('app.research_sources.uuid4',return_value=UUID(source.source_id.removeprefix('source_'))):
        created=client.post('/v2/research/sources/pdf',headers=headers,json=body)
    assert created.status_code==201 and created.json()['source_id']==source.source_id and created.json()['content_sha256']==source.content_sha256
    ids.append(source.source_id)
topic=client.post('/v2/research/topics',headers=headers,json={'question':q,'source_ids':ids});assert topic.status_code==201
path='/v2/research/topics/'+topic.json()['topic_id'];start=time.monotonic()
receipt=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'revision-278'},json={
    'parent_version_id':topic.json()['versions'][0]['version_id'],'max_search_calls':0,'max_model_calls':2,
    'support_review':True,'support_review_mode':'batch','max_input_tokens_per_call':6000,
    'max_total_tokens':20000,'max_elapsed_seconds':120})
assert receipt.status_code==202;op=receipt.json()['operation']['operation_id']
(HERE/'receipt.json').write_text(json.dumps({'manifest_sha256':mh,'operation_id':op,'source_ids':ids},indent=2)+'\n')
status='unknown';errors=[]
while time.monotonic()-start<150:
    check=client.get(path+'/runs/'+op,headers=headers)
    if check.status_code==200:
        status=check.json()['operation']['status']
        if status in ('completed','failed','cancelled'):break
    else:
        errors.append(check.status_code)
        if check.status_code not in (429,503):break
    time.sleep(2)
out={'manifest_sha256':mh,'operation_status':status,'source_ids':ids,'poll_errors':errors,
     'elapsed_seconds_observed':round(time.monotonic()-start,3),'cost_usd':'unknown'}
if status in ('completed','failed','cancelled'):
    updated=client.get(path,headers=headers);assert updated.status_code==200
    out['content']=updated.json()['versions'][-1]['content']
serialized=json.dumps(out,ensure_ascii=False,indent=2)
for key in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY'):
    if os.environ.get(key): serialized=serialized.replace(os.environ[key],'[REDACTED]')
(HERE/'result.json').write_text(serialized+'\n')
print(json.dumps({'status':status,'calls':calls,'reported_tokens':out.get('content',{}).get('run',{}).get('total_tokens')}))
