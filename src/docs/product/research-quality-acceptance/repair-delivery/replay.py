"""Offline reassembly of frozen terminal state; no provider or network calls."""
import hashlib, json, sys
from pathlib import Path
from datetime import datetime, timezone
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / 'apps/api'))
from app.research_delivery import assess, assemble_parts
from app.research_partial_answer import merge_checkpoint_parts
from app.research_sources import ResearchSource, SourcePermissions

baseline = json.loads((HERE/'frozen-baseline.json').read_text())
case_path = HERE.parent/'cases.json'
assert hashlib.sha256(case_path.read_bytes()).hexdigest() == '7e62e0cd385e9923ea1030b1fd9825b2fba41ade816d6601e8e57e288cdcd316'
cases = {x['id']: x for x in json.loads(case_path.read_text())['cases']}
outputs = []
for entry in baseline['rows']:
    path = ROOT.parent / entry['path']
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == entry['sha256']
    run = json.loads(data); case = cases[run['case']]; text = case['snapshot']
    assert hashlib.sha256(text.encode()).hexdigest() == run['source']['content_sha256']
    source = ResearchSource(source_id='exhibit', owner_id='isolated', title='Synthetic exhibit', text=text, kind='user_supplied', obtained_at=datetime(2026,11,23,tzinfo=timezone.utc), content_sha256=run['source']['content_sha256'], permissions=SourcePermissions(**run['source']['permissions']))
    state = run['final_state_projection']; sources = {'exhibit': source}; eligible = {'exhibit'}
    state = {k: ([] if v is None and k != 'submission' else v) for k,v in state.items()}
    state['_sources'] = [run['source']]
    claims = state.get('claims') or []
    rows = assess(case['question'], state, sources, claims=claims)
    parts = assemble_parts(case['question'], state, sources, eligible, rows, claims)
    parts = merge_checkpoint_parts(case['question'], state, eligible, parts, sources)
    outputs.append({'run':entry['run'], 'status':run['status'], 'reason':run['reason'], 'submission_acceptance':run['submission_acceptance'], 'parts':parts})
print(json.dumps(outputs, ensure_ascii=False, indent=2, default=str))
