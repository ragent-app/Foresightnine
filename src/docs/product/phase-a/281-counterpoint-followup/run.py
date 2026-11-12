from common import *
import os,subprocess
from threading import Event

mb=(HERE/'manifest.json').read_bytes();m=json.loads(mb);mh=digest(mb)
assert all(digest((HERE/name).read_bytes())==value for name,value in m['files'].items())
assert digest((HERE/'candidate-prompt.json').read_bytes())==m['prompt_sha256']
assert digest((HERE.parent/'276-financial-context-run'/'COMPANY_CISCO_2024-result.json').read_bytes())==m['prior_result_sha256']
task,source,raw=load_task('COMPANY_CISCO_2024');base,segments=capture_prompt(task,source)
assert digest(raw)==m['source_sha256'] and source.content_sha256==m['text_sha256']
assert digest(base.encode())==m['original_prompt_sha256'] and segments==m['source_segments']
assert json.loads(base)['sources']==json.loads((HERE/'candidate-prompt.json').read_text())['sources']
subprocess.run(['git','diff','--quiet',m['implementation_commit'],'--','apps'],cwd=ROOT,check=True)
for line in json.loads(subprocess.check_output(['docker','inspect','--format','{{json .Config.Env}}','docker-api-1'])):
    key,sep,value=line.partition('=')
    if sep and (key.startswith('FORESIGHTNINE_LLM_') or key=='FORESIGHTNINE_GLM_CODING_API_KEY'):os.environ[key]=value
os.environ.update(FORESIGHTNINE_LOCAL_AUTH_MODE='mock',FORESIGHTNINE_STORAGE_MODE='memory',
                  FORESIGHTNINE_LLM_TIMEOUT_SECONDS='60',FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS='6000')
import app.main as api
api.challenge_provider=api.challenge_provider.for_research();provider=api.challenge_provider
assert provider.name=='openai-compatible' and provider.settings.model==m['model']
params=json.loads(provider._request_body(None,'probe',system='probe'));params.pop('messages')
assert params==m['generation_parameters'] and provider.policy.max_output_tokens==m['output_limit']
with (HERE/'run-started.json').open('x') as f:json.dump({'manifest_sha256':mh,'status':'started_once'},f,indent=2)
try:
    raw,used=provider.generate_structured((HERE/'candidate-prompt.json').read_text(),cancel_event=Event(),
                                          system=SYSTEM_PROMPT,timeout_seconds=m['timeout_seconds'])
    assert type(used) is int and 0<=used<=m['max_tokens']
    out={'status':'completed','manifest_sha256':mh,'payload':raw,'reported_tokens':used,'cost_usd':'unknown'}
except Exception as error:
    out={'status':'failed_or_unknown','manifest_sha256':mh,'error':type(error).__name__,'cost_usd':'unknown'}
serialized=json.dumps(out,ensure_ascii=False,indent=2)
for key in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY'):
    if os.environ.get(key):serialized=serialized.replace(os.environ[key],'[REDACTED]')
(HERE/'provider.json').write_text(serialized+'\n')
print(json.dumps({k:v for k,v in out.items() if k!='payload'}))
