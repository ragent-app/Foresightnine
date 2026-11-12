from pathlib import Path
import json,sys
HERE=Path(__file__).resolve().parent
experiment=HERE.parent/'278-revision-two-source-run'
sys.path.insert(0,str(experiment))
from common import inputs,digest
from app.research_evidence import suggest_cross_source_comparison_evidence

p=experiment/'result.json'; content=json.loads(p.read_text())['content'];_,sources,_=inputs()
claims=suggest_cross_source_comparison_evidence(content['claims'],content['facts'],sources)
summary={'model_calls':0,'original_result_sha256':digest(p.read_bytes()),
         'claims':[{'claim_index':i,'citations':c.get('citations'),
                    'uncited_fact_values':c.get('uncited_fact_values',[]),
                    'suggested_fact_evidence':c.get('suggested_fact_evidence',[])} for i,c in enumerate(claims)],
         'limitations':'Candidates are not attached citations or independent verification.'}
(HERE/'replay.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'model_calls':0,'candidate_counts':[len(c['suggested_fact_evidence']) for c in summary['claims']]}))
