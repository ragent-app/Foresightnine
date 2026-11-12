from common import *
from app.research_evidence import flag_partial_source_absence, flag_uncited_quarters, suggest_percentage_evidence

m=json.loads((HERE/'manifest.json').read_text()); task, source, raw=load_task(m['task'])
assert digest(raw)==m['source_sha256'] and source.content_sha256==m['text_sha256']
rows=[]
for case,path in [('baseline',HERE/m['baseline']),('candidate',HERE/'candidate-provider.json')]:
    saved=json.loads(path.read_text())
    if not isinstance(saved.get('payload'),dict):
        rows.append({'case':case,'status':'unavailable','reported_tokens':saved.get('reported_tokens')}); continue
    try:
        r=analyze(task['question'],[source],None,lambda _:(saved['payload'],saved['reported_tokens']),
                  max_prompt_chars=m['input_limit']-len(SYSTEM_PROMPT.encode())-512)
        assert r.segments==m['segments']
        claims=[{**c,'role':'support'} for c in r.claims]+[{**c,'role':'counterargument'} for c in r.counterarguments]
        claims=flag_partial_source_absence(flag_uncited_quarters(suggest_percentage_evidence(claims,r.facts),[source]),[source],r.segments)
        rows.append({'case':case,'status':'parsed','reported_tokens':saved['reported_tokens'],'claim_count':len(claims),
                     'claims_with_citations':sum(bool(c.get('citations')) for c in claims),
                     'cited_counterarguments':sum(c['role']=='counterargument' and bool(c.get('citations')) for c in claims),
                     'uncited_quarter_claims':[i for i,c in enumerate(claims) if c.get('uncited_quarters')],
                     'uncited_percentage_claims':[i for i,c in enumerate(claims) if c.get('uncited_percentages')],
                     'unscoped_absence_claims':[i for i,c in enumerate(claims) if c.get('source_scope_absence_unverified')],
                     'claim_text_and_citations':[{'role':c['role'],'text':c['text'],'citations':c.get('citations',[])} for c in claims],
                     'limitations':r.limitations})
    except (ValueError, AssertionError) as error:
        rows.append({'case':case,'status':'invalid','error':type(error).__name__,'reported_tokens':saved.get('reported_tokens')})
out={'results':rows,'model_calls':0,'semantic_support':'requires separate source-level assessment'}
(HERE/'inspection.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps([{k:v for k,v in row.items() if k not in ('claim_text_and_citations','limitations')} for row in rows],ensure_ascii=False,indent=2))
