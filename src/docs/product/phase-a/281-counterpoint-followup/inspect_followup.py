from common import *
from app.research_evidence import flag_uncited_usd_amounts,flag_uncited_causal_attribution,flag_partial_source_absence
m=json.loads((HERE/'manifest.json').read_text());task,source,raw=load_task('COMPANY_CISCO_2024')
saved=json.loads((HERE/'provider.json').read_text());assert saved['status']=='completed'
result=analyze(task['question'],[source],None,lambda _:(saved['payload'],saved['reported_tokens']),
               max_prompt_chars=m['input_limit']-len(SYSTEM_PROMPT.encode())-512)
assert result.segments==m['source_segments']
claims=[{**x,'role':'counterargument'} for x in result.counterarguments]
claims=flag_uncited_usd_amounts(claims,[source])
claims=flag_uncited_causal_attribution(claims,[source])
claims=flag_partial_source_absence(claims,[source],result.segments)
out={'model_calls':0,'provider_sha256':digest((HERE/'provider.json').read_bytes()),'claims':claims,
     'limitations':result.limitations,'semantic_support':'requires independent source review'}
(HERE/'inspection.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'counterarguments':len(claims),'quotation_counts':[len(x.get('citations',[])) for x in claims],
                  'warnings':[{k:v for k,v in x.items() if k in ('uncited_claim_amounts','causal_attribution_unverified','source_scope_absence_unverified')} for x in claims]},ensure_ascii=False))
