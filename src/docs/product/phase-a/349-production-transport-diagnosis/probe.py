"""One independent, production-instrumented call; no retries or raw output."""
import json, os, time
from pathlib import Path
from threading import Event
from app.phase3 import build_llm_provider
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext
from app.research_recovery import RecoveryJournal

HERE=Path(__file__).resolve().parent
marker=HERE/'attempt.json'
fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
initial={'state':'pending','reserved_tokens':10000,'attempts':1,'provider_response':'unknown','settlement':'unknown','actual_tokens':None,'cost':'unknown'}
with os.fdopen(fd,'w') as f:
    json.dump(initial,f); f.flush(); os.fsync(f.fileno())
def write(path, obj):
    tmp=path.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(obj,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)
class IsolatedRepository:
    """Private test-only journal, separate from all real research operations."""
    def __init__(self): self.record={'owner_id':'349-isolated'}
    def get(self, key): return self.record
    def put(self, key, value): self.record=value; write(HERE/'private-journal.json',value)
    def with_advisory_lock(self, key, fn): return fn()
repo=IsolatedRepository()
journal=RecoveryJournal(repo,'349-isolated','349-isolated')
key='FORESIGHTNINE_GLM_CODING_API_KEY'
for line in Path('src/infra/docker/staging.env').read_text().splitlines():
    if line.startswith(key+'='): os.environ[key]=line.split('=',1)[1].strip().strip('"\'')
os.environ.update(FORESIGHTNINE_LLM_PROVIDER='glm_coding', FORESIGHTNINE_LLM_TIMEOUT_SECONDS='120', FORESIGHTNINE_LLM_CONNECT_TIMEOUT_SECONDS='15', FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS='6000', FORESIGHTNINE_GLM_CODING_THINKING='enabled', FORESIGHTNINE_GLM_CODING_REASONING_EFFORT='low', FORESIGHTNINE_GLM_CODING_DO_SAMPLE='false')
os.environ.pop('FORESIGHTNINE_LLM_RESPONSE_TIMEOUT_SECONDS',None)
context=ResearchExecutionContext(ResearchExecutionBudget(1,0,10000,float('inf')),False,lambda:None,Event,
    lambda call_id,tokens: journal.begin_external(call_id=call_id,kind='model',stage='diagnostic'),
    lambda call_id,tokens: None,lambda:None,persist_private_diagnostics=journal.record_private_diagnostics)
started=time.monotonic(); before=None; usage=None
try:
    provider=build_llm_provider().for_research(6000)
    g=provider.settings.glm_generation if provider.settings else None
    assert provider.settings and provider.settings.api_key and provider.settings.model=='glm-5.3-flash' and provider.configuration_error is None
    assert provider.policy.max_input_tokens==6000 and provider.policy.max_output_tokens==4000 and provider.policy.timeout_seconds==120
    assert g and g.thinking_enabled and g.reasoning_effort=='low' and g.do_sample is False
    before=context.start_model('349-single',10000)
    payload,usage=context.invoke_model('349-single',provider.generate_structured,
        'Return exactly a JSON object containing the key ok with value true.',
        system='Return only the requested JSON object.',timeout_seconds=120)
    result={**initial,'state':'returned','provider_response':'returned','settlement':'reported_usage' if usage is not None else 'unknown','actual_tokens':usage,'schema_valid':payload=={'ok':True}}
except Exception as e:
    code=getattr(e,'code',None)
    result={**initial,'state':'failed','error_code':code if isinstance(code,str) and code.startswith(('provider_','invalid_provider_')) else 'unknown'}
finally:
    if before is not None: context.finish_model('349-single',usage,before)
    result['elapsed_ms']=round((time.monotonic()-started)*1000)
    result['diagnostics']=context.private_diagnostics.get('349-single',{})
    result['local_receipt']=context.model_call_receipts.get('349-single')
    write(marker,result)
print(json.dumps(result,ensure_ascii=False,indent=2))
