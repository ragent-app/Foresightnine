from pathlib import Path
import json,sys
here=Path(__file__).resolve().parent
sys.path.insert(0,str(here.parent/'292-new-source-quality'))
from common import load_task
from app.research_evidence import suggest_related_claim_quotations
_,source,_=load_task('BEA_2026Q1')
content=json.loads((here.parent/'292-new-source-quality/BEA_2026Q1-result.json').read_text())['content']
before=content['claims'];after=suggest_related_claim_quotations(before,[source])
checks={'revision_period_still_flagged':'Q1 2026' in after[1].get('uncited_quarters',[]),
        'same_source_quarter_candidate':any(c['from_claim_index']==0 and 'Q1 2026' in c['matched_amounts'] for c in after[1].get('suggested_related_quotations',[])),
        'comparison_periods_still_flagged':set(after[2].get('uncited_quarters',[]))=={'Q1 2026','Q4 2025'},
        'comparison_candidate':any(c['from_claim_index']==0 and set(c['matched_amounts']) >= {'Q1 2026','Q4 2025'} for c in after[2].get('suggested_related_quotations',[])),
        'no_automatic_attachment':all(before[i]['citations']==after[i]['citations'] for i in (1,2))}
out={'checks':checks,'all_checks_pass':all(checks.values()),'model_calls':0,
     'candidates':{str(i):after[i].get('suggested_related_quotations',[]) for i in (1,2)},
     'note':'Matching quarters do not establish metric, value, comparison or semantic support.'}
(here/'replay.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'checks':checks,'model_calls':0},indent=2))
if not out['all_checks_pass']:raise SystemExit(1)
