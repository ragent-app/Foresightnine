"""Single independent analysis call; file marker forbids replay."""
import hashlib, json, os, time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

ROOT = Path(__file__).resolve().parents[5]
HERE = Path(__file__).resolve().parent
source_path = ROOT / 'src/docs/product/phase-a/337-stage-b-independent-acceptance/snapshots/bea-2022-q3.txt'
source_bytes = source_path.read_bytes()
assert hashlib.sha256(source_bytes).hexdigest() == 'f36fc389b6b0420ccd8416938f1028fd8c644b1c14c2e0624e5ba7ebb15aa4ef'
marker = HERE / 'attempt.json'
fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
initial = {'state':'pending', 'attempts':1, 'reserved_tokens':10000, 'provider_response':'unknown', 'actual_tokens':None, 'cost':'unknown'}
with os.fdopen(fd, 'w') as f:
    json.dump(initial, f); f.flush(); os.fsync(f.fileno())
def write(path, value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, default=str); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
class IsolatedRepository:
    def __init__(self): self.record = {'owner_id':'350-isolated'}
    def get(self, key): return self.record
    def put(self, key, value): self.record = value; write(HERE/'private-journal.json', value)
    def with_advisory_lock(self, key, fn): return fn()
key = 'FORESIGHTNINE_GLM_CODING_API_KEY'
for line in (ROOT/'src/infra/docker/staging.env').read_text().splitlines():
    if line.startswith(key+'='): os.environ[key] = line.split('=',1)[1].strip().strip('"\'')
os.environ.update(FORESIGHTNINE_LLM_PROVIDER='glm_coding', FORESIGHTNINE_LLM_TIMEOUT_SECONDS='120', FORESIGHTNINE_LLM_CONNECT_TIMEOUT_SECONDS='15', FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS='6000', FORESIGHTNINE_GLM_CODING_THINKING='enabled', FORESIGHTNINE_GLM_CODING_REASONING_EFFORT='low', FORESIGHTNINE_GLM_CODING_DO_SAMPLE='false')
os.environ.pop('FORESIGHTNINE_LLM_RESPONSE_TIMEOUT_SECONDS', None)
from app.phase3 import build_llm_provider
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext, execute_generation
from app.research_recovery import RecoveryJournal
from app.research_sources import ResearchSource, SourcePermissions
repo = IsolatedRepository()
journal = RecoveryJournal(repo, '350-isolated', '350-isolated')
start = time.monotonic()
context = ResearchExecutionContext(ResearchExecutionBudget(1,0,10000,start+360), False, lambda:None, Event,
    lambda call_id,tokens: journal.begin_external(call_id=call_id,kind='model',stage='analysis'),
    lambda call_id,tokens: None, lambda:None, persist_private_diagnostics=journal.record_private_diagnostics)
source = ResearchSource(source_id='source_00000000000000000000000000000001', owner_id='350-isolated', title='bea-2022-q3', text=source_bytes.decode(), url='https://www.bea.gov/sites/default/files/2022-10/gdp3q22_adv.pdf', kind='official', obtained_at=datetime(2026,11,14,7,32,33,tzinfo=timezone.utc), content_sha256=hashlib.sha256(source_bytes).hexdigest(), permissions=SourcePermissions(store=True,model_input=True,display=True,export=True))
question = 'Using the supplied BEA release, report the real GDP growth rates for the second and third quarters of 2022 and calculate the difference in percentage points. Explain the estimate and period limitations.'
observed = {}
try:
    provider = build_llm_provider().for_research(6000)
    g = provider.settings.glm_generation if provider.settings else None
    assert provider.settings and provider.settings.api_key and provider.settings.model == 'glm-5.3-flash' and provider.configuration_error is None
    assert provider.policy.max_input_tokens == 6000 and provider.policy.max_output_tokens == 4000 and provider.policy.timeout_seconds == 120
    assert g and g.thinking_enabled and g.reasoning_effort == 'low' and g.do_sample is False
    def observe(prompt, *, cancel_event, system, timeout_seconds, diagnostic_callback=None):
        observed.update(prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(), prompt_bytes=len(prompt.encode()), system_sha256=hashlib.sha256(system.encode()).hexdigest(), system_bytes=len(system.encode()), timeout_seconds=timeout_seconds)
        return provider.generate_structured(prompt,cancel_event=cancel_event,system=system,timeout_seconds=timeout_seconds,diagnostic_callback=diagnostic_callback)
    result = execute_generation(question=question,scope='',sources=[source],as_of=None,provider=provider,generate=observe,context=context,call_id='350-analysis',focus=None,planning_steps=[],require_counterarguments=True)
    saved_result = asdict(result)
    saved_result.pop('prompt_text', None)
    write(HERE/'analysis-result.json', saved_result)
    receipt = context.model_call_receipts.get('350-analysis', {})
    output = {**initial,'state':'finished','error_code':result.unavailable_reason,'provider_response':receipt.get('provider','not_invoked'),'actual_tokens':result.generation_tokens,'accounted_tokens':context.accounted_tokens,'local_receipt':receipt,'observation':observed,'diagnostics':context.private_diagnostics.get('350-analysis',{})}
except BaseException as e:
    code = getattr(e, 'code', None)
    output = {**initial,'state':'failed','error_code':code if isinstance(code,str) and code.startswith(('provider_','invalid_')) else 'local_error','accounted_tokens':context.accounted_tokens,'local_receipt':context.model_call_receipts.get('350-analysis'),'observation':observed,'diagnostics':context.private_diagnostics.get('350-analysis',{})}
output['elapsed_ms']=round((time.monotonic()-start)*1000)
write(marker,output)
print(json.dumps(output,ensure_ascii=False))
