"""Offline reassembly of frozen facts results; does not invoke a model or mutate them."""
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
from pathlib import Path

from app.research_delivery import assess, assemble_parts
from app.research_partial_answer import merge_checkpoint_parts
from app.research_sources import ResearchSource, SourcePermissions

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('paired_acceptance', HERE / 'paired.py')
paired = importlib.util.module_from_spec(spec)
spec.loader.exec_module(paired)
text, question = paired.CASES['facts']
source = ResearchSource(source_id='exhibit', owner_id='isolated', title='Independent exercise', text=text,
                        kind='user_supplied', obtained_at=datetime(2026, 11, 22, tzinfo=timezone.utc),
                        content_sha256=sha256(text.encode()).hexdigest(),
                        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))
sources = {'exhibit': source}
results = []
for run_id in ('facts-1-A', 'facts-1-B', 'facts-2-A', 'facts-2-B'):
    path = HERE / 'paired-results' / (run_id + '.json')
    raw = path.read_bytes()
    saved = json.loads(raw)
    state = {'facts': saved['facts'], 'calculations': saved['calculations'],
             'read_ranges': [{'source_id': 'exhibit', 'start': 0, 'end': len(text)}]}
    rows = assess(question, state, sources, claims=saved['claims'])
    parts = merge_checkpoint_parts(question, state, {'exhibit'},
                                   assemble_parts(question, state, sources, {'exhibit'}, rows, saved['claims']), sources)
    results.append({'run_id': run_id, 'input_sha256': sha256(raw).hexdigest(),
                    'original_status': saved['status'], 'original_reason': saved['reason'],
                    'parts': [{'question': p['question'], 'status': p['status'], 'answer': p.get('answer', ''),
                               'references': p.get('references', []), 'reason': p.get('reason', '')} for p in parts]})
assert all(len(r['parts']) == 3 for r in results)
output = {'method': 'offline reassessment from frozen saved facts/calculations and authorized fixed exhibit; no model requests',
          'question': question, 'evidence_limit': 'Numeric and source locations matched; semantic support is not automatically verified. Original run status is unchanged.',
          'results': results}
(HERE / 'paired-facts-reassembly.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
print('Reassembled', len(results), 'frozen runs')
