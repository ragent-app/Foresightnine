"""One non-inference GET of configured coding endpoint models; emit allowed metadata only."""
import json, os, time
from pathlib import Path
from app.openai_compatible import _GLM_CODING_BASE_URL, _GLM_CODING_DEFAULT_MODEL, _default_binary_transport
p=Path(__file__).resolve().parent/'result.json'
fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:
    json.dump({'state':'pending','calls':1,'model_calls':0},f);f.flush();os.fsync(f.fileno())
key='FORESIGHTNINE_GLM_CODING_API_KEY'
for line in Path('src/infra/docker/staging.env').read_text().splitlines():
    if line.startswith(key+'='): os.environ[key]=line.split('=',1)[1].strip().strip('"\'')
start=time.monotonic(); result={'state':'failed','calls':1,'model_calls':0,'capacities':{}}
try:
    response=_default_binary_transport('GET',_GLM_CODING_BASE_URL+'/models',{'Authorization':'Bearer '+os.environ[key]},b'',20,connect_timeout_seconds=15)
    result['status_code']=response.status_code
    if response.status_code == 200:
        obj=json.loads(response.body)
        rows=obj.get('data',[]) if isinstance(obj,dict) else []
        row=next((x for x in rows if isinstance(x,dict) and x.get('id')==_GLM_CODING_DEFAULT_MODEL),None)
        result['exact_model_found']=row is not None
        if row:
            result['capacities']={k:row[k] for k in ('context_length','context_window','max_context_tokens','max_output_tokens') if type(row.get(k)) is int and row[k]>0}
        result['state']='returned'
    else: result['state']='http_response'
except Exception as e:
    result['exception']=type(e).__name__; result['errno']=getattr(e,'errno',None)
result['elapsed_ms']=round((time.monotonic()-start)*1000)
tmp=p.with_suffix('.tmp')
with tmp.open('w') as f:
    json.dump(result,f,indent=2);f.flush();os.fsync(f.fileno())
os.replace(tmp,p)
print(json.dumps(result,indent=2))
