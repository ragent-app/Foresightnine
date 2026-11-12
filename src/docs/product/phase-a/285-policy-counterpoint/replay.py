from common import *
from app.research_evidence import flag_uncited_causal_attribution

m=json.loads((HERE/'manifest.json').read_text());task,source,raw=load_task('POLICY_FOMC_201706')
assert digest(raw)==m['conditions'][0]['source_sha256']
p=HERE/'POLICY_FOMC_201706-counterpoint-2-provider.json';saved=json.loads(p.read_text())
allowed={'counterarguments','limitations'}
restricted={key:saved['payload'][key] for key in allowed if key in saved['payload']}
r=analyze(task['question'],[source],None,lambda _:(restricted,saved['reported_tokens']),
          max_prompt_chars=m['input_limit']-len(SYSTEM_PROMPT.encode())-512)
assert r.segments==m['conditions'][0]['segments']
claims=flag_uncited_causal_attribution(r.counterarguments,[source])
out={'model_calls':0,'provider_output_sha256':digest(p.read_bytes()),
     'discarded_top_level_fields':len(set(saved['payload'])-allowed),'counterarguments':claims,
     'limitations':r.limitations,'note':'Located quotation and lexical cues do not establish semantic support.'}
(HERE/'replay.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'discarded_fields':out['discarded_top_level_fields'],'counterarguments':len(claims),
                  'located':[len(c.get('citations',[])) for c in claims],
                  'causal_warnings':[bool(c.get('causal_attribution_unverified')) for c in claims], 'model_calls':0}))
