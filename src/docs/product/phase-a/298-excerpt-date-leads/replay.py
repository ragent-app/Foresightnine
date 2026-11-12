from pathlib import Path
import json, sys
here=Path(__file__).resolve().parent
sys.path.insert(0,str(here.parent/'292-new-source-quality'))
from common import load_task
from app.research_evidence import suggest_excerpt_dates
_,source,_=load_task('FOMC_202507')
content=json.loads((here.parent/'292-new-source-quality/FOMC_202507-result.json').read_text())['content']
before=content['claims'][0]
after=suggest_excerpt_dates([before],[source],content['run']['selected_segments'])[0]
leads=after.get('suggested_related_quotations',[])
checks={'original_date_warning_retained':after.get('uncited_exact_dates')==['July 30, 2025'],
        'release_date_line_found':any('For release' in x['quotation'] and 'July 30, 2025' in x['quotation'] for x in leads),
        'candidate_not_attached':after['citations']==before['citations'],
        'leads_marked_as_unverified':bool(leads) and all(x['from_claim_index']==-2 and x['status']=='requires_semantic_review' for x in leads)}
out={'checks':checks,'all_checks_pass':all(checks.values()),'candidates':leads,'model_calls':0,
     'note':'Publication date can be different from meeting or event date; the candidate does not establish their equivalence.'}
(here/'replay.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'checks':checks,'model_calls':0},indent=2))
assert out['all_checks_pass']
