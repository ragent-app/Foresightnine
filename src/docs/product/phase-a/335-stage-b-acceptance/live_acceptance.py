"""One preregistered non-blind development-set acceptance, no search/retries."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[5]
OUT = Path(__file__).resolve().parent
PHASE = OUT.parent

def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()

def pdf(path: Path) -> str:
    return subprocess.run(['pdftotext', '-layout', str(path), '-'], capture_output=True, check=True).stdout.decode('utf-8')

def sources_and_tasks():
    msft_path = PHASE / 'baseline/msft-2023-details.json'
    cisco_path = PHASE / '64-cisco-retrieval-diagnostic/CISCO_REV-input.json'
    bea_a = PHASE / 'original-source-candidate-v01/D21/bea-gdp-2023-q4-advance.pdf'
    bea_b = PHASE / 'original-source-candidate-v01/D21/bea-gdp-2024-q4-advance.pdf'
    fed = PHASE / 'original-source-candidate-v01/D23/fed-fomc-2024-09-18.pdf'
    msft = msft_path.read_text()
    cisco = json.loads(cisco_path.read_text())
    sources = {
      'msft': {'title':'Microsoft SEC FY2023 derived facts; not the original filing', 'text':msft,
               'url':json.loads(msft)['source'], 'artifact':str(msft_path.relative_to(ROOT)), 'sha256':digest(msft_path.read_bytes())},
      'cisco': {'title':cisco['source_title'], 'text':cisco['source_text'], 'url':cisco['source_url'],
                'artifact':str(cisco_path.relative_to(ROOT)), 'sha256':digest(cisco_path.read_bytes())},
      'bea_a': {'title':'BEA GDP 2023 Q4 advance, January 2024', 'text':pdf(bea_a),
                'url':'https://www.bea.gov/sites/default/files/2024-01/gdp4q23-adv.pdf',
                'artifact':str(bea_a.relative_to(ROOT)), 'sha256':digest(bea_a.read_bytes())},
      'bea_b': {'title':'BEA GDP 2024 Q4 advance, January 2025', 'text':pdf(bea_b),
                'url':'https://www.bea.gov/sites/default/files/2025-01/gdp4q24-adv.pdf',
                'artifact':str(bea_b.relative_to(ROOT)), 'sha256':digest(bea_b.read_bytes())},
      'fed': {'title':'Federal Reserve September 18 2024 FOMC statement', 'text':pdf(fed),
              'url':'https://www.federalreserve.gov/monetarypolicy/files/monetary20240918a1.pdf',
              'artifact':str(fed.relative_to(ROOT)), 'sha256':digest(fed.read_bytes())},
    }
    tasks = [
      {'id':'SEC_FACT_CALC', 'question':'Using the supplied SEC-derived record, what are Microsoft total revenues for fiscal 2023 and 2022, and their change? State units and limits of the evidence.', 'sources':['msft'], 'require_counterarguments':False},
      {'id':'SEC_COMP_COUNTER', 'question':'Compare the reported annual revenues and year-over-year changes for Microsoft and Cisco in the supplied materials. Explain any fiscal period and source limitations and provide a cited counterargument or limitation.', 'sources':['msft','cisco'], 'require_counterarguments':True},
      {'id':'BEA_REVISION', 'question':'Compare the two releases\' estimates for annual real GDP growth in 2023. Identify release dates and explain the difference between the estimates and any unresolved uncertainty.', 'sources':['bea_a','bea_b'], 'require_counterarguments':True},
      {'id':'FED_EVENT_MISSING', 'question':'What target range and change did the September 2024 Federal Reserve decision state? Identify any reasons and missing evidence that prevent inferring subsequent policy decisions.', 'sources':['fed'], 'require_counterarguments':True},
    ]
    return sources,tasks

def load_glm_profile():
    allowed = {'FORESIGHTNINE_GLM_CODING_API_KEY','FORESIGHTNINE_GLM_CODING_MODEL',
      'FORESIGHTNINE_GLM_CODING_THINKING','FORESIGHTNINE_GLM_CODING_REASONING_EFFORT',
      'FORESIGHTNINE_GLM_CODING_DO_SAMPLE','FORESIGHTNINE_LLM_TIMEOUT_SECONDS',
      'FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS'}
    for line in (ROOT/'src/infra/docker/staging.env').read_text().splitlines():
        if '=' not in line or line.lstrip().startswith('#'): continue
        key,value = line.split('=',1)
        if key.strip() in allowed:
            os.environ[key.strip()] = value.strip().strip('"\'')
    os.environ.update({'FORESIGHTNINE_LLM_PROVIDER':'glm_coding', 'FORESIGHTNINE_STORAGE_MODE':'memory',
                       'FORESIGHTNINE_LOCAL_AUTH_MODE':'mock', 'FORESIGHTNINE_SEARCH_PROVIDER':'client'})
    for k in ('DATABASE_URL','REDIS_URL','FORESIGHTNINE_DATABASE_URL','FORESIGHTNINE_REDIS_URL'):
        os.environ.pop(k, None)

def prepare():
    sources,tasks=sources_and_tasks()
    document={'version':'1','kind':'non_blind_development_acceptance', 'independent_holdout':False,
      'fixed_tasks':tasks, 'sources':{k:{'artifact':v['artifact'],'file_sha256':v['sha256'],
          'text_sha256':digest(v['text'].encode()),'title':v['title'],'url':v['url']} for k,v in sources.items()},
      'permissions':'existing saved SEC/BEA/Federal Reserve material, model input and display for local acceptance only',
      'provider':'existing configured GLM Coding, glm-5.3-flash', 'generation_parameters':'existing configured profile; no added temperature or sampling override',
      'budget':{'tasks':4,'max_model_calls_per_task':2,'max_model_calls_total':8,'max_tokens_per_task':20000,
                'max_tokens_total':80000,'max_elapsed_seconds_per_task':180,'search_calls':0,'automatic_source_fetch':False,
                'provider_post_retries':0,'actual_cost_usd':'unknown'},
      'stop':'Stop on provider authorization, quota, transport or service failure; no retries or provider substitution.',
      'assessment':'Report task completion, citations, evidence gaps, usage and failure; these development materials and model review are not independent quality proof.'}
    path=OUT/'preregistration.json'
    if path.exists():
        raise RuntimeError('Preregistration already exists; do not silently replace the frozen task set.')
    path.write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n')
    print('Frozen preregistration written:',path)

def run():
    manifest=json.loads((OUT/'preregistration.json').read_text())
    if (OUT/'run-started.json').exists(): raise RuntimeError('Run already started; automatic rerun is prohibited.')
    sources,tasks=sources_and_tasks()
    if tasks != manifest['fixed_tasks'] or any(digest(v['text'].encode()) != manifest['sources'][k]['text_sha256'] or v['sha256'] != manifest['sources'][k]['file_sha256'] for k,v in sources.items()):
        raise RuntimeError('Frozen tasks or source snapshots changed.')
    load_glm_profile()
    sys.path.insert(0,str(ROOT/'src/apps/api'))
    from fastapi.testclient import TestClient
    import app.main as api
    from app.research_analysis import SYSTEM_PROMPT, SELECTION_METHOD
    p=api.challenge_provider.for_research()
    if p.configuration_error or not p.settings or not p.settings.api_key or p.settings.model != 'glm-5.3-flash':
        raise RuntimeError('The authorized fixed provider profile is unavailable.')
    if p.policy.max_input_tokens+p.policy.max_output_tokens > 10000:
        raise RuntimeError('Per-call configured limit cannot fit the approved two-call budget.')
    (OUT/'run-started.json').write_text(json.dumps({'model':p.settings.model,'provider':p.name,
      'prompt_sha256':digest(SYSTEM_PROMPT.encode()),'selection_method':SELECTION_METHOD,
      'max_input_tokens':p.policy.max_input_tokens,'max_output_tokens':p.policy.max_output_tokens,
      'provider_timeout_seconds':p.policy.timeout_seconds,'cost_usd':'unknown'},indent=2)+'\n')
    client=TestClient(api.app)
    auth=client.post('/v1/auth/local/register',json={'email':f'accept-{uuid4().hex}@example.test',
      'password':'correct horse battery staple!'})
    if auth.status_code != 201: raise RuntimeError(f'Isolated local auth failed: {auth.status_code}')
    headers={'Authorization':'Bearer '+auth.json()['access_token']}
    total_calls=0
    for task in tasks:
        source_ids=[]
        for key in task['sources']:
            item=sources[key]
            response=client.post('/v2/research/sources',headers=headers,json={'title':item['title'],'text':item['text'],
              'url':item['url'],'kind':'official','permissions':{'store':True,'display':True,'model_input':True,'export':False}})
            if response.status_code != 201: raise RuntimeError(f"{task['id']} source admission failed: {response.status_code}")
            source_ids.append(response.json()['source_id'])
        topic=client.post('/v2/research/topics',headers=headers,json={'question':task['question'],'source_ids':source_ids})
        if topic.status_code != 201: raise RuntimeError(f"{task['id']} topic admission failed: {topic.status_code}")
        record=topic.json(); path='/v2/research/topics/'+record['topic_id']
        payload={'parent_version_id':record['versions'][0]['version_id'], 'selected_source_ids':source_ids,
          'max_model_calls':2,'max_search_calls':0,'max_total_tokens':20000,'max_elapsed_seconds':180,
          'auto_fetch_official':False,'plan_research':False,'support_review':True,
          'require_counterarguments':task['require_counterarguments']}
        receipt=client.post(path+'/runs',headers={**headers,'Idempotency-Key':f"accept-{task['id']}-{uuid4().hex}"},json=payload)
        if receipt.status_code != 202: raise RuntimeError(f"{task['id']} run admission failed: {receipt.status_code}")
        operation=receipt.json()['operation']; deadline=time.monotonic()+190
        while time.monotonic()<deadline:
            state=client.get(path+'/runs/'+operation['operation_id'],headers=headers).json()['operation']
            if state['status'] in ('completed','failed','cancelled'): break
            time.sleep(0.5)
        else:
            client.post('/v1/operations/'+operation['operation_id']+'/cancel',headers=headers)
            raise RuntimeError(f"{task['id']} exceeded the acceptance deadline")
        current=client.get(path,headers=headers).json()['versions'][-1]
        content=current['content']; run=content.get('run',{}); total_calls += run.get('model_calls',0)
        result={'task_id':task['id'],'operation':state,'version':current,'cost_usd':'unknown',
                'model_calls_cumulative':total_calls}
        (OUT/(task['id']+'-result.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(task['id'], 'operation=',state['status'],'research=',run.get('status'),
              'model_calls=',run.get('model_calls'),'tokens=',run.get('total_tokens'),'error=',run.get('error'),flush=True)
        if total_calls>8: raise RuntimeError('Total call limit exceeded.')
        if state['status'] != 'completed' or run.get('error') in {'provider_request_rejected','provider_unavailable','provider_secret_missing','provider_transport_error','provider_timeout'}:
            print('Stopping: service or operation failure.',flush=True); break
    print('Actual calls recorded:',total_calls, 'USD cost: unknown',flush=True)

if __name__=='__main__':
    {'prepare':prepare,'run':run}[sys.argv[1]]()
