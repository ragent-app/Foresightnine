"""Replay a saved development response; no model or network calls."""
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(root / 'src/apps/api'))
from app.research_evidence import flag_truncated_previous_findings
from app.research_runs import has_usable_counterpoint

inspection = json.loads((root / 'src/docs/product/phase-a/306-counterpoint-relevance-preserved/inspection.json').read_text())
candidate = next(item for item in inspection['results'] if item['case'] == 'candidate')['counterarguments'][0]
flagged = flag_truncated_previous_findings([{**candidate, 'prior_findings_truncated': True}])[0]
assert flagged['prior_findings_scope_unverified']
assert not has_usable_counterpoint([flagged], set())
print(json.dumps({'model_calls': 0, 'warning': flagged['prior_findings_scope_unverified'],
                  'usable_counterpoint': False, 'input': '306 saved candidate'}, indent=2))
