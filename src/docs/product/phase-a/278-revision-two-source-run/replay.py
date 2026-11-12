from common import *
from app.research_evidence import evidence_differences,flag_partial_source_absence

p=HERE/'result.json';old=json.loads(p.read_text());content=old['content']
_,sources,_=inputs()
claims=flag_partial_source_absence(content['claims'],sources,content['run']['selected_segments'])
out={'model_calls':0,'result_sha256':digest(p.read_bytes()),
     'evidence_differences':evidence_differences(content['facts']),
     'source_scope_absence_warning_claim_indexes':[i for i,x in enumerate(claims) if x.get('source_scope_absence_unverified')],
     'limitations':'Candidates require source, entity, timing, unit and semantic review; no independent verification.'}
(HERE/'replay.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'candidates':len(out['evidence_differences']),'warning_indexes':out['source_scope_absence_warning_claim_indexes'],'model_calls':0}))
