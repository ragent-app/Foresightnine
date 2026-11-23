"""Offline query comparison on frozen documents, no model, vectors or network."""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / 'apps/api'))
from app.research_library import ResearchLibrary
from app.research_library_tool import sync_library
from app.research_sources import ResearchSource, SourcePermissions

fixture_raw = (HERE / 'corpus.json').read_bytes()
requirement_raw = (HERE / 'requirements.json').read_bytes()
fixture, reqs = json.loads(fixture_raw), json.loads(requirement_raw)
as_of = datetime.fromisoformat(fixture['as_of'])
sources = {x['id']: ResearchSource(source_id=x['id'], owner_id='offline-owner', title=x['title'], text=x['text'],
    kind='user_supplied', published_at=datetime.fromisoformat(x['published_at']),
    obtained_at=datetime(2026,3,1,tzinfo=timezone.utc), content_sha256=sha256(x['text'].encode()).hexdigest(),
    permissions=SourcePermissions(store=True, display=True, model_input=x.get('model_input', True))) for x in fixture['sources']}

def run_arm(lib, queries, k):
    pool, seen = [], set()
    trace = []
    for requirement_id, query in queries:
        hits = lib.search(query, top_k=20, use_vector=False)
        trace.append({'requirement_id':requirement_id, 'query':query, 'hit_source_ids':[h.source_id for h in hits]})
        for h in hits:
            key = (h.source_id, h.char_start, h.char_end)
            if key not in seen:
                seen.add(key)
                pool.append(h)
    # The exact same deterministic first-unique-passage selector and capacity in both arms.
    selected, texts, used = [], set(), 0
    for h in pool:
        body = sources[h.source_id].text[h.char_start:h.char_end]
        n = len(h.snippet.encode())
        if body in texts or len(selected) >= k or used + n > reqs['byte_limit']:
            continue
        selected.append(h.source_id)
        texts.add(body)
        used += n
    return {'queries':trace, 'candidate_source_ids':[h.source_id for h in pool],
            'selected_source_ids':selected, 'selected_bytes':used}

cases = []
for case in fixture['cases']:
    lib = ResearchLibrary([], vector_scorer=None)
    sync_library(lib, {sid:sources[sid] for sid in case['included']}, {}, as_of, vector_scorer=None)
    k = case.get('capacity_passages', fixture['capacity_passages'])
    # Equal three-call allowance. The topic arm repeats its frozen original query;
    # the gap arm uses only request descriptions, not evaluation labels or answers.
    topic = run_arm(lib, [(None,fixture['query'])] * 3, k)
    gap_queries = [(rid,reqs['requirements'][rid]['description']) for rid in case['requirements']][:3]
    gap = run_arm(lib, gap_queries, k)
    labels = case['expected_groups']
    for arm in (topic, gap):
        candidates = set(arm['candidate_source_ids'])
        selected = set(arm['selected_source_ids'])
        arm['candidate_groups_covered'] = sum(bool(candidates.intersection(g)) for g in labels)
        arm['selected_groups_covered'] = sum(bool(selected.intersection(g)) for g in labels)
    cases.append({'case_id':case['id'], 'necessary_groups_available':len(labels), 'topic':topic, 'gap':gap})
output = {'fixture_sha256':sha256(fixture_raw).hexdigest(), 'requirements_sha256':sha256(requirement_raw).hexdigest(),
          'max_local_queries_per_arm':3, 'selector':'first unique exact passage, same K and 512-byte cap', 'cases':cases}
(HERE / 'comparison-b.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
