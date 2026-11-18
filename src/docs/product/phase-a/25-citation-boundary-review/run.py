"""One-shot two-variant model review probe; stops on uncertain provider state."""
import hashlib,json,os,subprocess,sys,time
from datetime import datetime,timezone
from pathlib import Path
from threading import Event
p=Path(__file__).resolve().parent;root=p.parents[3]
h=lambda b:hashlib.sha256(b).hexdigest()
mb=(p/'manifest.json').read_bytes();m=json.loads(mb)
for n in ('run.py','baseline-system.txt'):
 assert h((p/n).read_bytes())==m['files_sha256'][n]
source_path=p/'../24-cross-domain-final-candidates/C24-input.json'
assert h(source_path.read_bytes())==m['source_input_sha256']
with (p/'started.json').open('x') as f: json.dump({'manifest_sha256':h(mb),'once_only':True},f)
keys={'FORESIGHTNINE_LLM_PROVIDER','FORESIGHTNINE_LLM_BASE_URL','FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_LLM_MODEL','FORESIGHTNINE_LLM_ALLOWED_HOSTS','FORESIGHTNINE_LLM_ALLOW_PRIVATE_NETWORK','FORESIGHTNINE_GLM_CODING_API_KEY'}
for item in json.loads(subprocess.check_output(['docker','inspect','docker-api-1','--format','{{json .Config.Env}}'],text=True)):
 k,_,v=item.partition('=')
 if k in keys:os.environ[k]=v
os.environ.update(FORESIGHTNINE_STORAGE_MODE='memory',FORESIGHTNINE_LOCAL_AUTH_MODE='mock',FORESIGHTNINE_LLM_TIMEOUT_SECONDS='60')
sys.path.insert(0,str(root/'apps/api'))
import app.main as api
from app.research_sources import ResearchSource,SourcePermissions
from app.research_verification import prepare_support_review,REVIEW_SYSTEM_PROMPT,ReviewOutput
provider=api.challenge_provider
assert provider.name=='openai-compatible' and provider.settings.model=='glm-5.3-flash'
raw=json.loads(source_path.read_text());text=raw['source_text'];quote='"total_revenue":{"2023":242290,"2024":254453}'
a=text.index(quote);b=a+len(quote)
source=ResearchSource(source_id='source',owner_id='probe',title=raw['source_title'],text=text,kind='user_supplied',obtained_at=datetime.now(timezone.utc),content_sha256=h(text.encode()),permissions=SourcePermissions(store=True,display=True,model_input=True))
cite={'source_id':'source','quotation':quote,'location':{'character_start':a,'character_end':b}}
claims=[{'text':'The 2023 fiscal year had 53 weeks and 2024 had 52 weeks.','citations':[cite]},
        {'text':'Fiscal 2024 total revenue was 254453 USD millions.','citations':[cite]},
        {'text':'Fiscal 2024 total revenue was 242290 USD millions.','citations':[cite]}]
batch=prepare_support_review(claims,[source],1800)
assert batch.indexes==[0,1,2] and not batch.omitted
v2=json.loads(batch.prompt)
assert all(e['text'][e['quotation_start']:e['quotation_end']]==quote for c in v2['claims'] for e in c['excerpts'])
v1=json.loads(batch.prompt)
for c in v1['claims']:
 for e in c['excerpts']:
  for k in ('quotation','quotation_start','quotation_end'): del e[k]
baseline_system=(p/'baseline-system.txt').read_text()
secrets=[os.environ.get(k) for k in ('FORESIGHTNINE_LLM_API_KEY','FORESIGHTNINE_GLM_CODING_API_KEY')]
for name,payload,system in [('baseline',v1,baseline_system),('bounded_quote',v2,REVIEW_SYSTEM_PROMPT)]:
 prompt=json.dumps(payload,ensure_ascii=False,separators=(',',':'));started=time.monotonic()
 try:
  output,used=provider.generate_structured(prompt,cancel_event=Event(),system=system,timeout_seconds=60)
  parsed=ReviewOutput.model_validate(output)
  verdicts={x.claim_index:x.verdict for x in parsed.reviews}
  if len(verdicts)!=3 or set(verdicts)!={0,1,2}: raise ValueError('incomplete review')
  result={'variant':name,'status':'returned','prompt_sha256':h(prompt.encode()),'system_sha256':h(system.encode()),'model':provider.settings.model,'elapsed_seconds':round(time.monotonic()-started,3),'reported_tokens':used,'verdicts':verdicts,'raw_response':output,'cost_usd':None}
 except Exception as e:
  result={'variant':name,'status':'failed_or_unknown','error_type':type(e).__name__,'cost_usd':None}
 serialized=json.dumps(result,ensure_ascii=False,indent=2)
 for secret in secrets:
  if secret:serialized=serialized.replace(secret,'[REDACTED]')
 (p/(name+'-result.json')).write_text(serialized+'\n')
 print(json.dumps({'variant':name,'status':result['status'],'reported_tokens':result.get('reported_tokens')}),flush=True)
 if result['status']!='returned':break
