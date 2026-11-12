from common import *
from app.research_runs import CLAIM_EVIDENCE_WARNINGS
import re

m = json.loads((HERE / 'manifest.json').read_text())
rows = []
for condition in m['conditions']:
    task_id = condition['task']; path = HERE / (task_id + '-result.json')
    if not path.exists():
        rows.append({'task':task_id,'status':'not_run','mechanical_pass':False}); continue
    result = json.loads(path.read_text()); _, source, _ = load_task(task_id)
    content = result.get('content', {}); claims = content.get('claims', [])
    def located(citation):
        if not isinstance(citation, dict) or citation.get('source_id') != source.source_id or citation.get('partial_token'):
            return False
        loc = citation.get('location', {}); a,b = loc.get('character_start'),loc.get('character_end')
        q = citation.get('quotation')
        return (loc.get('status') != 'ambiguous' and type(a) is int and type(b) is int and
                isinstance(q,str) and 0 <= a < b <= len(source.text) and source.text[a:b] == q and
                source.text.count(q) == 1)
    def cited(pattern):
        return any(re.search(pattern,c.get('text',''),re.I|re.S) and any(located(q) and re.search(pattern,q.get('quotation',''),re.I|re.S) for q in c.get('citations',[])) for c in claims)
    checks = {'claims_located':bool(claims) and all(c.get('citations') and c.get('support_status') != 'incomplete' and all(located(q) for q in c['citations']) for c in claims),
              'no_known_claim_warnings':not any(c.get(key) for c in claims for key in CLAIM_EVIDENCE_WARNINGS),
              'cited_counterpoint':any(c.get('role')=='counterargument' and c.get('citations') and all(located(q) for q in c['citations']) for c in claims),
              'research_completed':content.get('run',{}).get('status')=='completed',
              'within_budget':type(content.get('run',{}).get('total_tokens')) is int and content['run']['total_tokens'] <= m['max_tokens_per_task'] and content['run'].get('model_calls',99) <= 2}
    if task_id == 'BEA_2026Q1':
        checks.update(q1_value=cited(r'(?<!\d)1\.6(?!\d)'), q4_value=cited(r'(?<!\d)0\.5(?!\d)'), revision_value=cited(r'(?<!\d)0\.4(?!\d)'))
    else:
        checks.update(lower_target=cited(r'4\s*[-–]\s*1\s*/\s*4'), upper_target=cited(r'4\s*[-–]\s*1\s*/\s*2'),
                      bowman=cited(r'Bowman'), waller=cited(r'Waller'), cut_preference=cited(r'lower.{0,100}(?:1\s*/\s*4|one.quarter)'))
    rows.append({'task':task_id,'status':result.get('status'),'checks':checks,'mechanical_pass':result.get('status')=='completed' and all(checks.values()),
                 'reported_tokens':content.get('run',{}).get('total_tokens'), 'semantic_support':'requires separate source-level assessment'})
out={'results':rows,'mechanical_pass':len(rows)==len(m['conditions']) and all(r['mechanical_pass'] for r in rows),
     'known_tokens':sum(r['reported_tokens'] for r in rows if type(r.get('reported_tokens')) is int),
     'unknown_usage':any(type(r.get('reported_tokens')) is not int for r in rows),
     'note':'Lexical matching and model self-review do not establish semantic support.'}
(HERE/'score.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(out,ensure_ascii=False,indent=2))
