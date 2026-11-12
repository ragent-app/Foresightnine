from pathlib import Path
import json,sys
HERE=Path(__file__).resolve().parent
experiment=HERE.parent/'278-revision-two-source-run'
sys.path.insert(0,str(experiment))
from common import inputs,analyze,SYSTEM_PROMPT,digest

m=json.loads((experiment/'manifest.json').read_text());q,sources,_=inputs()
p=experiment/'generation-1-provider.json';saved=json.loads(p.read_text())
assert saved['prompt_sha256']==m['prompt_sha256']
r=analyze(q,sources,None,lambda _:(saved['payload'],saved['reported_tokens']),
          max_prompt_chars=m['input_limit']-len(SYSTEM_PROMPT.encode())-512)
assert r.prompt_sha256==m['prompt_sha256'] and r.segments==m['segments']
out={'model_calls':0,'provider_output_sha256':digest(p.read_bytes()),'prompt_sha256':r.prompt_sha256,
     'claims':r.claims,'counterarguments':r.counterarguments,'limitations':r.limitations}
(HERE/'replay.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'claims':len(r.claims),'fragment_counts':[len(c['citations']) for c in r.claims],
                  'claim_statuses':[c['support_status'] for c in r.claims], 'model_calls':0}))
