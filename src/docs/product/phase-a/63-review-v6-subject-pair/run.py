"""One call per fixed condition, stopping on unknown outcomes; no retries."""
from pathlib import Path
from hashlib import sha256
from threading import Event
import json,os,subprocess,sys,time
p=Path(__file__).resolve().parent;root=p.parents[3];sys.path.insert(0,str(root/'apps/api'))
from app.research_verification import REVIEW_SYSTEM_PROMPT, REVIEW_METHOD, apply_support_review
from common import build,prior,replay
h=lambda b:sha256(b).hexdigest()
mb=(p/'manifest.json').read_bytes();m=json.loads(mb)
for filename,digest in m['input_sha256'].items():
    file=replay if filename=='replay-result.json' else prior/filename
    assert h(file.read_bytes())==digest
for filename,key in (('common.py','common_sha256'),('run.py','runner_sha256'),('prepare.py','prepare_sha256')):
    assert h((p/filename).read_bytes())==m[key]
assert REVIEW_METHOD==m['method'] and h(REVIEW_SYSTEM_PROMPT.encode())==m['system_prompt_sha256']
claims,batch,baseline,variant=build()
assert {k:h(v.encode()) for k,v in [('without_subject',baseline),('with_subject',variant)]}==m['prompts_sha256']
assert batch.indexes==m['claim_indexes'] and m['max_calls_total']==2 and m['retry_external_calls']==0
for line in json.loads(subprocess.check_output(['docker','inspect','--format','{{json .Config.Env}}','docker-api-1'])):
    key,sep,value=line.partition('=')
    if sep and (key.startswith('FORESIGHTNINE_LLM_') or key=='FORESIGHTNINE_GLM_CODING_API_KEY'):
        os.environ[key]=value
os.environ.update(FORESIGHTNINE_LOCAL_AUTH_MODE='mock',FORESIGHTNINE_STORAGE_MODE='memory',FORESIGHTNINE_LLM_TIMEOUT_SECONDS='60')
import app.main as api
provider=api.challenge_provider
assert provider.name=='openai-compatible' and provider.settings.model==m['model']
params=json.loads(provider._request_body(None,'probe',system='probe'));params.pop('messages')
assert params==m['generation_parameters'] and provider.policy.max_input_tokens==3000 and provider.policy.timeout_seconds==60
for name,prompt in (('without_subject',baseline),('with_subject',variant)):
    with (p/(name+'-started.json')).open('x') as file:
        json.dump({'manifest_sha256':h(mb),'prompt_sha256':h(prompt.encode()),'status':'started_once','retry_external_calls':0},file,indent=2)
    start=time.monotonic();tokens=None
    try:
        raw,tokens=provider.generate_structured(prompt,cancel_event=Event(),system=REVIEW_SYSTEM_PROMPT,timeout_seconds=60)
        assessed=apply_support_review(claims,batch,raw)
        verdicts=[item['support_review']['verdict'] for item in assessed]
        outcome={'status':'completed','raw':raw,'verdicts':verdicts,'target_pass':verdicts[1] in ('does_not_support','contradicts','uncertain'),
                 'reported_tokens':tokens,'elapsed_seconds':round(time.monotonic()-start,3),'cost_usd':'unknown'}
    except Exception as error:
        outcome={'status':'failed_or_unknown','error_type':type(error).__name__,'reported_tokens':tokens if tokens is not None else 'unknown',
                 'elapsed_seconds':round(time.monotonic()-start,3),'cost_usd':'unknown'}
    serialized=json.dumps(outcome,ensure_ascii=False,indent=2)
    for key in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY'):
        secret=os.environ.get(key)
        if secret:serialized=serialized.replace(secret,'[REDACTED]')
    (p/(name+'-result.json')).write_text(serialized+'\n')
    print(json.dumps({'condition':name,'status':outcome['status'],'reported_tokens':outcome['reported_tokens']},ensure_ascii=False),flush=True)
    if outcome['status']!='completed' or type(tokens) is not int or tokens>m['max_tokens_per_call']:
        break
