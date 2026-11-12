from common import *
import os, subprocess

raw_manifest = (HERE/'manifest.json').read_bytes(); m = json.loads(raw_manifest); mh = digest(raw_manifest)
assert all(digest((HERE/name).read_bytes()) == expected for name,expected in m['files'].items())
assert digest((HERE/m['baseline']).read_bytes()) == m['baseline_sha256']
assert digest((HERE/'candidate-prompt.json').read_bytes()) == m['candidate_prompt_sha256']
assert digest((HERE/'baseline-prompt.json').read_bytes()) == m['baseline_prompt_sha256']
assert digest(SYSTEM_PROMPT.encode()) == m['system_sha256']
a = json.loads((HERE/'baseline-prompt.json').read_text()); b = json.loads((HERE/'candidate-prompt.json').read_text())
assert b.pop('counterpoint_request') != a.pop('counterpoint_request') and a == b
subprocess.run(['git','diff','--quiet',m['implementation_commit'],'--','apps'],cwd=ROOT,check=True)
for line in json.loads(subprocess.check_output(['docker','inspect','--format','{{json .Config.Env}}','docker-api-1'])):
    key,sep,value = line.partition('=')
    if sep and (key.startswith('FORESIGHTNINE_LLM_') or key == 'FORESIGHTNINE_GLM_CODING_API_KEY'):
        os.environ[key] = value
os.environ.update(FORESIGHTNINE_STORAGE_MODE='memory', FORESIGHTNINE_LOCAL_AUTH_MODE='mock',
                  FORESIGHTNINE_LLM_TIMEOUT_SECONDS='60', FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS='6000')
import app.main as api
from threading import Event
api.challenge_provider = api.challenge_provider.for_research(); provider = api.challenge_provider
assert provider.name == 'openai-compatible' and provider.settings.model == m['model']
params = json.loads(provider._request_body(None,'probe',system='probe')); params.pop('messages')
assert params == m['generation_parameters'] and provider.policy.max_output_tokens == m['output_limit']
with (HERE/'run-started.json').open('x') as handle:
    json.dump({'manifest_sha256':mh,'status':'started_once'},handle,indent=2)
try:
    output, used = provider.generate_structured((HERE/'candidate-prompt.json').read_text(),
        cancel_event=Event(),system=SYSTEM_PROMPT,timeout_seconds=m['timeout_seconds'])
    result = {'status':'completed','payload':output,'reported_tokens':used,'manifest_sha256':mh,'cost_usd':'unknown'}
    assert type(used) is int and 0 <= used <= m['max_tokens']
except Exception as error:
    result = {'status':'failed_or_unknown','error':type(error).__name__,'manifest_sha256':mh,'cost_usd':'unknown'}
value=json.dumps(result,ensure_ascii=False,indent=2)
for key in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY'):
    if os.environ.get(key): value=value.replace(os.environ[key],'[REDACTED]')
(HERE/'candidate-provider.json').write_text(value+'\n')
print(json.dumps({k:v for k,v in result.items() if k != 'payload'}))
