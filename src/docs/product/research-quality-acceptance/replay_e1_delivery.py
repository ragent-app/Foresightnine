"""Replay complete and incomplete E1 financial deliveries against their recorded evidence."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

from app.research_delivery import assemble_parts, assess
from app.research_partial_answer import merge_checkpoint_parts
from app.research_sources import ResearchSource, SourcePermissions


def replay(case_id):
    here = Path(__file__).resolve().parent
    case = next(case for case in json.loads((here / 'cases.json').read_text())['cases'] if case['id'] == case_id)
    receipt = json.loads((here / 'e1-results' / f'{case_id}-A-1.json').read_text())
    original = receipt['question_parts']
    assert any(p['status'] == 'unresolved' and 'EPS' in p['question'] for p in original)
    if case_id == 'D1':
        assert any(p['status'] == 'answered' and 'EPS' in p['question'] for p in original)
    text = case['snapshot']
    source = ResearchSource(source_id='exhibit', owner_id='isolated', title='Synthetic exhibit', text=text,
                            kind='user_supplied', obtained_at=datetime(2026, 11, 23, tzinfo=timezone.utc),
                            content_sha256=sha256(text.encode()).hexdigest(),
                            permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))
    sources = {source.source_id: source}
    state = {key: receipt[key] for key in ('facts', 'calculations')}
    claims = receipt['claims']
    needed = assess(case['question'], state, sources, claims=claims)
    rows = assemble_parts(case['question'], state, sources, set(sources), needed, claims)
    rows = merge_checkpoint_parts(case['question'], state, set(sources), rows, sources)
    assert len(rows) == 2 and len({p['id'] for p in rows}) == 2
    answered = [p for p in rows if p['status'] == 'answered']
    assert all('尚未回答' not in p['reason'] and any(r.get('calculation_id') for r in p['references']) for p in answered)
    if case_id == 'D1':
        assert len(answered) == 2
    else:
        assert len(answered) == 1 and '收入' in answered[0]['question']
        assert any(p['status'] == 'partial' and '2024' in p['reason'] and 'EPS' in p['reason'] for p in rows)
    print(f"{case_id}-A1: {len(original)} original rows -> {len(rows)} rows, {len(answered)} answered")


if __name__ == '__main__':
    for case_id in ('D1', 'D2'):
        replay(case_id)
