from pathlib import Path
import json,sys
here=Path(__file__).resolve().parent
sys.path.insert(0,str(here.parent/'294-atomic-claim-prompt'))
from common import load_task
from app.research_evidence import flag_component_direction
_,source,_=load_task('BEA_2026Q1')
inspection=json.loads((here.parent/'294-atomic-claim-prompt/inspection.json').read_text())
candidate=next(r for r in inspection['results'] if r['case']=='candidate')
claims=candidate['claim_text_and_citations']
result=flag_component_direction(claims,[source])
findings=[{'claim_index':i,'warnings':c['balance_direction_warnings']} for i,c in enumerate(result) if c.get('balance_direction_warnings')]
out={'model_calls':0,'findings':findings,'note':'Narrow cue; the original output and score remain unchanged. Metric and context still require review.'}
(here/'replay.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(out,ensure_ascii=False,indent=2))
assert len(findings)==1 and findings[0]['claim_index']==4 and findings[0]['warnings'][0]['basis']=='period_rate_comparison'
