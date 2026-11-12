from pathlib import Path
import json,sys
here=Path(__file__).resolve().parent
sys.path.insert(0,str(here.parent/'292-new-source-quality'))
from common import load_task
from app.research_evidence import flag_component_direction
_,source,_=load_task('BEA_2026Q1')
content=json.loads((here.parent/'292-new-source-quality/BEA_2026Q1-result.json').read_text())['content']
old=content['claims'];new=flag_component_direction(old,[source])
checks={'original_counterpoint_retained':old[3]['text']==new[3]['text'],
        'wrong_metric_number_flagged':any(w.get('basis')=='metric_value_attribution' for w in new[3].get('balance_direction_warnings',[])),
        'original_result_not_changed':not old[3].get('balance_direction_warnings')}
out={'checks':checks,'all_checks_pass':all(checks.values()),'model_calls':0,
     'warnings':new[3].get('balance_direction_warnings',[]),
     'note':'A lexical cue is not a semantic verdict and cannot establish metric, period, or source truth.'}
(here/'replay.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'checks':checks,'model_calls':0},indent=2))
if not out['all_checks_pass']:raise SystemExit(1)
