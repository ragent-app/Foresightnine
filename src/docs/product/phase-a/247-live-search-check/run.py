from pathlib import Path
from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic
from urllib.parse import urlsplit, urlunsplit
import json, subprocess, sys
HERE=Path(__file__).resolve().parent; ROOT=HERE.parents[3];sys.path.insert(0,str(ROOT/'apps/api'))
from app.glm_mcp_search import GlmCodingMcpSearchEvidenceProvider
from app.openai_compatible import _default_transport
from app.phase3 import ProviderFailure
query='BEA gross domestic product fourth quarter 2023 advance estimate'
assert len(query)<=70
args={'query':query,'limit':5,'search_calls':1,'max_attempts_per_http':1,'per_http_timeout_seconds':15,'deadline_seconds':30,'model_calls':0,'cost_usd':'unknown'}
with (HERE/'started.json').open('x') as handle:json.dump(args,handle,indent=2)
started=monotonic();calls=[]
try:
 env=json.loads(subprocess.check_output(['docker','inspect','--format','{{json .Config.Env}}','docker-api-1']))
 key=next((line.split('=',1)[1] for line in env if line.startswith('FORESIGHTNINE_GLM_CODING_API_KEY=')),None)
 if not key:raise ValueError('credential unavailable')
 def transport(method,url,headers,body,timeout):
  payload=json.loads(body);calls.append(payload.get('method','unknown'))
  return _default_transport(method,url,headers,body,timeout)
 provider=GlmCodingMcpSearchEvidenceProvider(key,transport=transport,timeout_seconds=15,max_attempts=1)
 candidates=provider.search(query,limit=5,deadline=started+30)
 rows=[]
 for item in candidates:
  p=urlsplit(item.url)
  host=p.hostname or ''
  safe_url=urlunsplit((p.scheme,host,p.path,'',''))
  rows.append({'title':item.title,'url':safe_url,'publisher':item.publisher,
               'published_at':item.published_at.isoformat() if item.published_at else None,
               'excerpt_sha256':sha256(item.excerpt.encode()).hexdigest(),
               'eligible_official_host':p.scheme=='https' and p.hostname in {'www.bea.gov','www.federalreserve.gov'}})
 out={'status':'completed','candidates':rows}
except (ProviderFailure,ValueError,subprocess.SubprocessError) as error:
 out={'status':'failed','error_code':error.code if isinstance(error,ProviderFailure) else type(error).__name__}
out.update({'http_methods':calls,'elapsed_seconds':round(monotonic()-started,3),'checked_at':datetime.now(timezone.utc).isoformat(),'model_calls':0,'cost_usd':'unknown'})
serialized=json.dumps(out,ensure_ascii=False,indent=2)
if 'key' in locals() and key:serialized=serialized.replace(key,'[REDACTED]')
(HERE/'result.json').write_text(serialized+'\n');print(json.dumps({'status':out['status'],'candidate_count':len(out.get('candidates',[])),'http_methods':calls,'elapsed_seconds':out['elapsed_seconds']}))
