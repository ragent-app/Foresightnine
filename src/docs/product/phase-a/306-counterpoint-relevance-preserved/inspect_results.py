from common import *
from app.research_counterpoint import filter_counterpoint_rows
from app.research_evidence import (flag_counterpoint_agreement, flag_partial_source_absence, flag_uncited_causal_attribution,
    flag_uncited_exact_dates, flag_uncited_quarters, suggest_percentage_evidence, flag_component_direction)
from app.research_runs import CLAIM_EVIDENCE_WARNINGS
m=json.loads((HERE/'manifest.json').read_text());task,source,raw=load_task(m['task'])
assert digest(raw)==m['source_sha256'] and source.content_sha256==m['text_sha256']
rows=[]
for case,path in [('baseline',HERE/m['baseline']),('candidate',HERE/'candidate-provider.json')]:
    saved=json.loads(path.read_text()); payload=saved.get('payload')
    if not isinstance(payload,dict):
        rows.append({'case':case,'status':'unavailable','reported_tokens':saved.get('reported_tokens')});continue
    filtered=filter_counterpoint_rows(payload.get('counterarguments'))
    if filtered is None:
        rows.append({'case':case,'status':'invalid','reported_tokens':saved.get('reported_tokens')});continue
    valid,omitted=filtered
    try:
        r=analyze(task['question'],[source],None,lambda _:({'counterarguments':valid,'limitations':payload.get('limitations',[])},saved.get('reported_tokens')),
                  max_prompt_chars=m['input_limit']-len(SYSTEM_PROMPT.encode())-512)
        assert r.segments==m['segments']
        claims=[{**c,'role':'counterargument'} for c in r.counterarguments]
        claims=flag_uncited_quarters(suggest_percentage_evidence(claims,[]),[source])
        claims=flag_uncited_exact_dates(flag_uncited_causal_attribution(claims,[source]),[source])
        claims=flag_counterpoint_agreement(flag_partial_source_absence(flag_component_direction(claims,[source]),[source],r.segments))
        rows.append({'case':case,'status':'parsed','reported_tokens':saved.get('reported_tokens'),'omitted_rows':omitted,
                     'counterargument_count':len(claims),'located_count':sum(bool(c.get('citations')) for c in claims),
                     'warning_fields':[sorted(k for k in CLAIM_EVIDENCE_WARNINGS if c.get(k)) for c in claims],
                     'counterarguments':claims,'limitations':r.limitations})
    except (ValueError,AssertionError) as error:
        rows.append({'case':case,'status':'invalid','error':type(error).__name__,'reported_tokens':saved.get('reported_tokens')})
out={'results':rows,'model_calls':0,'semantic_support':'requires separate source-level assessment'}
(HERE/'inspection.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps([{k:v for k,v in r.items() if k not in ('counterarguments','limitations')} for r in rows],ensure_ascii=False,indent=2))
