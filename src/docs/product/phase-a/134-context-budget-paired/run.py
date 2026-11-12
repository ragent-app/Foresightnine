from common import *
from dataclasses import replace
from threading import Event
from time import monotonic
import os, subprocess
from app.phase3 import build_llm_provider, ProviderFailure
from app.openai_compatible import OpenAICompatibleLlmProvider

manifest_bytes=(HERE/'manifest.json').read_bytes();m=json.loads(manifest_bytes); mh=digest(manifest_bytes)
for name, expected in m['files'].items(): assert digest((HERE/name).read_bytes())==expected
assert digest(SYSTEM_PROMPT.encode())==m['system_sha256']
subprocess.run(['git','diff','--quiet',m['implementation_commit'],'--','apps'],cwd=ROOT,check=True)
for line in json.loads(subprocess.check_output(['docker','inspect','--format','{{json .Config.Env}}','docker-api-1'])):
 key,sep,value=line.partition('=')
 if sep and (key.startswith('FORESIGHTNINE_LLM_') or key=='FORESIGHTNINE_GLM_CODING_API_KEY'): os.environ[key]=value
os.environ['FORESIGHTNINE_LLM_TIMEOUT_SECONDS']='60'
provider=build_llm_provider()
assert isinstance(provider,OpenAICompatibleLlmProvider) and provider.settings.model==m['model']
params=json.loads(provider._request_body(None,'probe',system='probe'));params.pop('messages')
assert params==m['generation_parameters'] and provider.policy.max_output_tokens==4000
with (HERE/'run-started.json').open('x') as f:json.dump({'manifest_sha256':mh,'status':'started_once'},f,indent=2)
for condition in m['conditions']:
 name=condition['task'];budget=condition['input_limit'];task,sources=get_task(name)
 assert {s.source_id:s.content_sha256 for s in sources}==condition['source_hashes']
 instance=OpenAICompatibleLlmProvider(provider.settings, policy=replace(provider.policy,max_input_tokens=budget,max_attempts=1), transport=provider.transport)
 capture={};started=monotonic()
 def generate(prompt):
  assert digest(prompt.encode())==condition['prompt_sha256']
  raw,usage=instance.generate_structured(prompt,cancel_event=Event(),system=SYSTEM_PROMPT,timeout_seconds=60)
  capture['raw']=raw;capture['usage']=usage
  return raw,usage
 out={'task':name,'input_limit':budget,'manifest_sha256':mh,'cost_usd':'unknown'}
 try:
  result=run_analysis(task,sources,budget,generate)
  out.update(status='completed',reported_tokens=result.total_tokens,prompt_sha256=result.prompt_sha256,
             segments=result.segments,claims=result.claims,counterarguments=result.counterarguments,
             facts=result.facts,calculations=result.calculations,limitations=result.limitations,raw=capture.get('raw'))
 except Exception as error:
  out.update(status='failure',error=error.code if isinstance(error,ProviderFailure) else type(error).__name__,
             reported_tokens=error.reported_tokens if isinstance(error,ProviderFailure) else capture.get('usage'))
 out['elapsed_seconds']=round(monotonic()-started,3)
 serialized=json.dumps(out,ensure_ascii=False,indent=2)
 for key in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY'):
  secret=os.environ.get(key)
  if secret:serialized=serialized.replace(secret,'[REDACTED]')
 (HERE/f'{name}-{budget}-result.json').write_text(serialized+'\n')
 print(json.dumps({'task':name,'input_limit':budget,'status':out['status'],'reported_tokens':out.get('reported_tokens')}),flush=True)
 if out['status']!='completed' or type(out['reported_tokens']) is not int or out['reported_tokens']>m['per_call_max_total_tokens']:break
