"""Single independent model call with passive stage tracing; marker forbids replay."""
import functools, json, os, socket, ssl, time
from pathlib import Path
from http.client import HTTPConnection, HTTPResponse
from app.phase3 import build_llm_provider
HERE=Path(__file__).resolve().parent
marker=HERE/'attempt.json'
fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
record={'state':'pending','reserved_tokens':10000,'attempts':1,'provider_response':'unknown','settlement':'unknown','actual_tokens':None,'cost':'unknown','stages':[]}
def persist():
    tmp=HERE/'attempt.tmp'
    with tmp.open('w') as f:
        json.dump(record,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,marker)
with os.fdopen(fd,'w') as f:
    json.dump(record,f); f.flush(); os.fsync(f.fileno())
def wrap(obj,name,stage,detail=None):
    original=getattr(obj,name)
    @functools.wraps(original)
    def wrapped(*args,**kwargs):
        start=time.monotonic(); item={'stage':stage}
        if detail: item.update(detail(*args,**kwargs))
        try:
            result=original(*args,**kwargs)
            item['result']='success'
            if stage=='http_response_headers': item['http_status']=result.status
            return result
        except BaseException as e:
            item.update(result='failure',exception=type(e).__name__,errno=getattr(e,'errno',None))
            raise
        finally:
            item['elapsed_ms']=round((time.monotonic()-start)*1000)
            record['stages'].append(item); persist()
    setattr(obj,name,wrapped)
wrap(socket,'getaddrinfo','dns')
wrap(socket,'create_connection','tcp',lambda address,*a,**k:{'address':address[0]})
wrap(ssl.SSLContext,'wrap_socket','tls')
wrap(HTTPConnection,'request','http_request')
wrap(HTTPConnection,'getresponse','http_response_headers')
wrap(HTTPResponse,'read','http_read')
key='FORESIGHTNINE_GLM_CODING_API_KEY'
for line in Path('src/infra/docker/staging.env').read_text().splitlines():
    if line.startswith(key+'='): os.environ[key]=line.split('=',1)[1].strip().strip('"\'')
os.environ.update(FORESIGHTNINE_LLM_PROVIDER='glm_coding', FORESIGHTNINE_LLM_TIMEOUT_SECONDS='120', FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS='6000', FORESIGHTNINE_GLM_CODING_THINKING='enabled', FORESIGHTNINE_GLM_CODING_REASONING_EFFORT='low', FORESIGHTNINE_GLM_CODING_DO_SAMPLE='false')
started=time.monotonic()
try:
    provider=build_llm_provider().for_research(6000)
    g=provider.settings.glm_generation if provider.settings else None
    assert provider.settings and provider.settings.api_key and provider.settings.model=='glm-5.3-flash' and provider.configuration_error is None
    assert provider.policy.max_input_tokens==6000 and provider.policy.max_output_tokens==4000 and provider.policy.timeout_seconds==120
    assert g and g.thinking_enabled and g.reasoning_effort=='low' and g.do_sample is False
    data,usage=provider.generate_structured('Return exactly a JSON object containing the key ok with value true.',system='Return only the requested JSON object.',timeout_seconds=120)
    record.update(state='returned',provider_response='returned',settlement='reported_usage' if usage is not None else 'unknown',actual_tokens=usage,schema_valid=data=={'ok':True})
except Exception as e:
    code=getattr(e,'code',None)
    record.update(state='failed',exception=type(e).__name__,error_code=code if isinstance(code,str) and code.startswith(('provider_','invalid_provider_')) else 'unknown')
finally:
    record['elapsed_ms']=round((time.monotonic()-started)*1000); persist()
print(json.dumps(record,ensure_ascii=False,indent=2))
