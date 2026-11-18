"""Two bounded independent tasks, exclusively marked; no retries or search."""
import hashlib,json,os,time
from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path
from threading import Event
ROOT=Path(__file__).resolve().parents[5]; HERE=Path(__file__).resolve().parent
TASKS=[('macro','bea-2022-q3.txt','f36fc389b6b0420ccd8416938f1028fd8c644b1c14c2e0624e5ba7ebb15aa4ef','Using the supplied BEA release, report the real GDP growth rates for the second and third quarters of 2022 and calculate the difference in percentage points. Explain the estimate and period limitations.','https://www.bea.gov/sites/default/files/2022-10/gdp3q22_adv.pdf'),('qualitative','fed-2020-june.txt','fec9898ca7650e7ed9a8271704c57ea4c7157d34174546273a1169c150f415ed','Using the supplied Federal Reserve statement, identify the policy decision and the strongest explicitly stated uncertainty or qualification; cite the relevant passages and explain whether the qualification challenges the decision or instead limits what can be concluded.','https://www.federalreserve.gov/monetarypolicy/files/monetary20200610a1.pdf')]
for _,name,digest,_,_ in TASKS:
 assert hashlib.sha256((ROOT/'src/docs/product/phase-a/337-stage-b-independent-acceptance/snapshots'/name).read_bytes()).hexdigest()==digest
fd=os.open(HERE/'batch.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f: json.dump({'state':'pending','max_calls':2,'attempted':0,'cost':'unknown'},f);f.flush();os.fsync(f.fileno())
def write(path,value):
 tmp=path.with_suffix('.tmp')
 with tmp.open('w') as f: json.dump(value,f,ensure_ascii=False,indent=2,default=str);f.flush();os.fsync(f.fileno())
 os.replace(tmp,path)
class Repo:
 def __init__(self,name): self.name=name;self.record={'owner_id':'354-'+name}
 def get(self,key):return self.record
 def put(self,key,value):self.record=value;write(HERE/(self.name+'-private-journal.json'),value)
 def with_advisory_lock(self,key,fn):return fn()
key='FORESIGHTNINE_GLM_CODING_API_KEY'
for line in (ROOT/'src/infra/docker/staging.env').read_text().splitlines():
 if line.startswith(key+'='):os.environ[key]=line.split('=',1)[1].strip().strip('"\'')
os.environ.update(FORESIGHTNINE_LLM_PROVIDER='glm_coding',FORESIGHTNINE_LLM_TIMEOUT_SECONDS='120',FORESIGHTNINE_LLM_CONNECT_TIMEOUT_SECONDS='15',FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS='6000',FORESIGHTNINE_GLM_CODING_THINKING='enabled',FORESIGHTNINE_GLM_CODING_REASONING_EFFORT='low',FORESIGHTNINE_GLM_CODING_DO_SAMPLE='false')
os.environ.pop('FORESIGHTNINE_LLM_RESPONSE_TIMEOUT_SECONDS',None);os.environ.pop('FORESIGHTNINE_LLM_CAPABILITY_PROFILE',None)
from app.phase3 import build_llm_provider
from app.research_output_contract import ANALYSIS_CONTRACT
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext,execute_generation,research_prompt_budget
from app.research_recovery import RecoveryJournal
from app.research_sources import ResearchSource,SourcePermissions
provider=build_llm_provider().for_research(6000);g=provider.settings.glm_generation if provider.settings else None
assert provider.settings and provider.settings.api_key and provider.settings.model=='glm-5.3-flash' and provider.configuration_error is None
assert provider.policy.max_input_tokens==6000 and provider.policy.max_output_tokens==4000 and provider.policy.timeout_seconds==120 and g and g.thinking_enabled and g.reasoning_effort=='low' and g.do_sample is False
attempted=0;results=[]
for name,filename,digest,question,url in TASKS:
 fd=os.open(HERE/(name+'-attempt.json'),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as f:json.dump({'state':'pending','reserved_tokens':10000,'provider_response':'unknown','cost':'unknown'},f);f.flush();os.fsync(f.fileno())
 repo=Repo(name);journal=RecoveryJournal(repo,'354-'+name,'354-'+name);start=time.monotonic(); observed={}
 ctx=ResearchExecutionContext(ResearchExecutionBudget(1,0,10000,start+360),False,lambda:None,Event,lambda call_id,tokens:journal.begin_external(call_id=call_id,kind='model',stage='analysis'),lambda call_id,tokens:None,lambda:None,persist_private_diagnostics=journal.record_private_diagnostics)
 source=ResearchSource(source_id='source_00000000000000000000000000000001',owner_id='354-'+name,title=filename,text=(ROOT/'src/docs/product/phase-a/337-stage-b-independent-acceptance/snapshots'/filename).read_text(),url=url,kind='official',obtained_at=datetime(2026,11,14,tzinfo=timezone.utc),content_sha256=digest,permissions=SourcePermissions(store=True,model_input=True,display=True,export=True))
 try:
  def observe(prompt,*,cancel_event,system,timeout_seconds,diagnostic_callback=None,contract=None):
   global attempted
   parsed=json.loads(prompt);assert contract is ANALYSIS_CONTRACT and contract.version=='research-draft-v1' and parsed['output_schema']['version']==contract.version
   assert parsed['sources'] and any(x.get('passages') for x in parsed['sources'])
   payload=json.loads(provider._request_body(None,prompt,system=system,contract=contract));assert payload['response_format']=={'type':'json_object'}
   overhead=provider.contract_overhead_bytes(contract);assert len(prompt.encode())<=research_prompt_budget(6000,system)-overhead
   observed.update(contract=contract.version,mode='json_object',prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),prompt_bytes=len(prompt.encode()),system_sha256=hashlib.sha256(system.encode()).hexdigest(),budget_bytes=research_prompt_budget(6000,system)-overhead,source_passages=sum(len(x.get('passages',[])) for x in parsed['sources']))
   attempted+=1;write(HERE/'batch.json',{'state':'running','max_calls':2,'attempted':attempted,'cost':'unknown'})
   return provider.generate_structured(prompt,cancel_event=cancel_event,system=system,timeout_seconds=timeout_seconds,diagnostic_callback=diagnostic_callback,contract=contract)
  generation=execute_generation(question=question,scope='',sources=[source],as_of=None,provider=provider,generate=observe,context=ctx,call_id='354-'+name,focus=None,planning_steps=[],require_counterarguments=True)
  saved=asdict(generation);saved.pop('prompt_text',None);write(HERE/(name+'-analysis-result.json'),saved)
  receipt=ctx.model_call_receipts.get('354-'+name,{})
  out={'state':'finished','error_code':generation.unavailable_reason,'provider_response':receipt.get('provider','not_invoked'),'actual_tokens':generation.generation_tokens,'reserved_tokens':10000,'accounted_tokens':ctx.accounted_tokens,'cost':'unknown','local_receipt':receipt,'observation':observed,'diagnostics':ctx.private_diagnostics.get('354-'+name,{}),'claims':len(generation.analysis.claims),'facts':len(generation.analysis.facts),'calculations':len(generation.analysis.calculations),'counterarguments':len(generation.analysis.counterarguments),'structured_output_status':generation.analysis.structured_output_status}
 except BaseException as e:
  code=getattr(e,'code',None);out={'state':'failed','error_code':code if isinstance(code,str) and code.startswith(('provider_','invalid_')) else 'local_error','provider_response':ctx.model_call_receipts.get('354-'+name,{}).get('provider','unknown'),'actual_tokens':None,'reserved_tokens':10000,'accounted_tokens':ctx.accounted_tokens,'cost':'unknown','local_receipt':ctx.model_call_receipts.get('354-'+name),'observation':observed,'diagnostics':ctx.private_diagnostics.get('354-'+name,{})}
 out['elapsed_ms']=round((time.monotonic()-start)*1000);write(HERE/(name+'-attempt.json'),out);results.append(out)
 if name=='macro' and (out['state']!='finished' or out['error_code'] or out['provider_response']!='returned' or out['local_receipt'].get('settlement')!='settled' or not(out['claims'] or out['facts'])): break
write(HERE/'batch.json',{'state':'finished','max_calls':2,'attempted':attempted,'cost':'unknown','results':results})
print(json.dumps({'attempted':attempted,'results':results},ensure_ascii=False))
