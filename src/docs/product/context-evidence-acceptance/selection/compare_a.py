"""Development-only selector comparison on the frozen retrieval result, without answer labels as selector input."""
import hashlib
import itertools
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
raw_fixture = (HERE / 'corpus.json').read_bytes()
raw_candidates = (HERE / 'baseline.json').read_bytes()
raw_requirements = (HERE / 'requirements.json').read_bytes()
fixture = json.loads(raw_fixture)
baseline = json.loads(raw_candidates)
inputs = json.loads(raw_requirements)
assert hashlib.sha256(raw_fixture).hexdigest() == baseline['fixture_sha256']

# Coarse, transparent development heuristics. They do not establish semantic support.
def relevance(row, kind):
    text = (row['title'] + ' ' + row['snippet']).lower()
    if kind == 'comparison':
        return int(bool(re.search(r'consolidated|statement', text)) and 'revenue' in text and
                   '2025' in text and '2024' in text)
    if kind == 'drivers':
        return int(bool(re.search(r'contribut|driver|attribut|caused by', text)) and 'revenue' in text)
    if kind == 'counterevidence':
        return int(bool(re.search(r'decline|decrease|risk|loss|weakness|contract', text)))
    return 0

def choose(pool, requirements, k, byte_limit):
    """Enumerate small fixed pool; prioritize core coverage, then non-duplication and stable ranks."""
    candidates = []
    for idx, row in enumerate(pool):
        matches = [r for r in requirements if relevance(row, inputs['requirements'][r]['kind'])]
        candidates.append((idx, row, matches))
    best = None
    for size in range(min(k, len(pool)) + 1):
        for combo in itertools.combinations(candidates, size):
            size_bytes = sum(len(x[1]['snippet'].encode('utf-8')) for x in combo)
            if size_bytes > byte_limit:
                continue
            texts = [x[1]['snippet'].strip().casefold() for x in combo]
            if len(set(texts)) != len(texts):
                continue
            covered = set(r for _, _, matches in combo for r in matches)
            # Avoid filling space with material that does not address a remaining requirement.
            if any(not matches for _, _, matches in combo):
                continue
            key = (len(covered), -size_bytes, tuple(-x[0] for x in combo))
            if best is None or key > best[0]:
                best = (key, combo, covered, size_bytes)
    assert best is not None
    _, combo, covered, size_bytes = best
    selected_indices = {x[0] for x in combo}
    selected_texts = {x[1]['snippet'].strip().casefold() for x in combo}
    trace = []
    for idx, row, matches in candidates:
        if idx in selected_indices:
            reason = 'addresses_requirement'
        elif row['snippet'].strip().casefold() in selected_texts:
            reason = 'duplicate_text'
        elif not matches:
            reason = 'no_requirement_match'
        else:
            reason = 'capacity_or_already_covered'
        trace.append({'source_id':row['source_id'], 'location':row['read_sources'],
                      'requirement_ids':matches, 'decision':'selected' if idx in selected_indices else 'omitted',
                      'reason':reason, 'candidate_rank':idx + 1})
    return {'selected_source_ids':[x[1]['source_id'] for x in combo], 'selected_bytes':size_bytes,
            'unmatched_requirements':[r for r in requirements if r not in covered], 'trace':trace}

cases = []
for case, original in zip(fixture['cases'], baseline['cases'], strict=True):
    assert case['id'] == original['case_id']
    pool = original['candidate_pool']
    k = original['capacity_passages']
    cap = inputs['byte_limit']
    first = pool[:k]
    assert sum(len(r['snippet'].encode('utf-8')) for r in first) <= cap
    chosen = choose(pool, case['requirements'], k, cap)
    # Labels are accessed only after selection, for evaluation.
    labels = case['expected_groups']
    old_ids = {row['source_id'] for row in first}
    new_ids = set(chosen['selected_source_ids'])
    old_score = sum(bool(old_ids.intersection(g)) for g in labels)
    new_score = sum(bool(new_ids.intersection(g)) for g in labels)
    cases.append({'case_id':case['id'], 'capacity_passages':k, 'byte_limit':cap,
                  'baseline':{'selected_source_ids':[r['source_id'] for r in first],
                              'selected_bytes':sum(len(r['snippet'].encode('utf-8')) for r in first),
                              'necessary_groups_covered':old_score},
                  'selector':{**chosen, 'necessary_groups_covered':new_score},
                  'necessary_groups_available':len(labels), 'expected_missing':case.get('expected_missing', [])})
output = {'fixture_sha256':hashlib.sha256(raw_fixture).hexdigest(),
          'baseline_sha256':hashlib.sha256(raw_candidates).hexdigest(),
          'requirements_sha256':hashlib.sha256(raw_requirements).hexdigest(),
          'byte_measure':'sum of UTF-8 snippet bytes, not full serialized request', 'cases':cases}
(HERE / 'comparison-a.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
