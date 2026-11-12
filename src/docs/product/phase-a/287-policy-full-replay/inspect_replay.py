"""Check integration behavior, not semantic truth or general model quality."""
import json
from pathlib import Path

here = Path(__file__).resolve().parent
result = json.loads((here / 'POLICY_FOMC_201706-result.json').read_text())
content = result['content']
run = content['run']
counterpoints = [item for item in content['claims'] if item.get('role') == 'counterargument']
checks = {
    'operation_finished': result['status'] == 'completed',
    'research_partial': run['status'] == 'partial',
    'checkpoint_preserved': bool(result.get('generation_checkpoint')),
    'one_extra_field_discarded': run['counterpoint_discarded_fields'] == 1,
    'supplement_requires_review': run['counterpoint_status'] == 'needs_review',
    'old_and_new_counterpoints_retained': len(counterpoints) == 2,
    'new_counterpoint_cited_and_warned': bool(counterpoints and counterpoints[-1].get('citations') and counterpoints[-1].get('causal_attribution_unverified')),
    'replayed_usage_accounted': run['generation_tokens'] == 1862 and run['counterpoint_tokens'] == 1573 and run['total_tokens'] == 3435,
}
output = {'checks': checks, 'all_checks_pass': all(checks.values()), 'live_model_calls': 0,
          'note': 'Replayed usage is historical; source quotation matching and flags do not establish semantic support.'}
(here / 'integration-check.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(output, ensure_ascii=False, indent=2))
if not output['all_checks_pass']:
    raise SystemExit(1)
