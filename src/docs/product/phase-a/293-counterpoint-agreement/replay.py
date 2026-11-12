from pathlib import Path
import json, sys
here = Path(__file__).resolve().parent
root = here.parents[3]
sys.path.insert(0, str(root / 'apps/api'))
from app.research_evidence import flag_counterpoint_agreement
from app.research_runs import has_usable_counterpoint
content = json.loads((here.parent / '292-new-source-quality/FOMC_202507-result.json').read_text())['content']
before = content['claims']
after = flag_counterpoint_agreement(before)
checks = {'original_supplement_was_reported_completed':content['run']['counterpoint_status']=='completed',
          'supplement_explicit_agreement_is_flagged': bool(after[-1].get('counterpoint_relevance_unverified')),
          'supplement_no_longer_considered_usable_by_cue':not has_usable_counterpoint([after[-1]]),
          'old_result_unchanged':not before[-1].get('counterpoint_relevance_unverified')}
out = {'checks':checks, 'all_checks_pass':all(checks.values()), 'model_calls':0,
       'limitation':'This lexical cue does not establish relevance or irrelevance and does not catch all paraphrases.'}
(here/'replay.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(out,indent=2))
if not out['all_checks_pass']: raise SystemExit(1)
