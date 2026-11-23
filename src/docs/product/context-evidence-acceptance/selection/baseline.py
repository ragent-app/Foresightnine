"""Frozen baseline: real local library retrieval, no model, network, or answer-driven queries."""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'apps/api'))
from app.research_library import ResearchLibrary
from app.research_library_tool import SearchLibraryRequest, sync_library, run_search_library
from app.research_sources import ResearchSource, SourcePermissions

HERE = Path(__file__).resolve().parent
raw = (HERE / 'corpus.json').read_bytes()
fixture = json.loads(raw)
as_of = datetime.fromisoformat(fixture['as_of'])
sources = {}
for item in fixture['sources']:
    sources[item['id']] = ResearchSource(
        source_id=item['id'], owner_id='offline-owner', title=item['title'], text=item['text'], kind='user_supplied',
        published_at=datetime.fromisoformat(item['published_at']), obtained_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        content_sha256=sha256(item['text'].encode()).hexdigest(),
        permissions=SourcePermissions(store=True, display=True, model_input=item.get('model_input', True)))
results = []
for case in fixture['cases']:
    selected_sources = {sid:sources[sid] for sid in case['included']}
    lib = ResearchLibrary([], vector_scorer=None)
    sync_library(lib, selected_sources, {}, as_of, vector_scorer=None)
    # Same wide candidate pool; top K projection is the existing baseline.
    req = SearchLibraryRequest(query=fixture['query'], max_hits=20, rationale='baseline question')
    result, _ = run_search_library(req, library=lib, source_by_id=selected_sources, versions={}, as_of=as_of)
    pool = result['hits']
    k = case.get('capacity_passages', fixture['capacity_passages'])
    top = pool[:k]
    ids = {row['source_id'] for row in top}
    groups = case['expected_groups']
    results.append({'case_id':case['id'], 'query':req.query, 'capacity_passages':k,
                    'candidate_pool':pool, 'coverage':result['library_coverage'],
                    'selected_source_ids':[row['source_id'] for row in top],
                    'necessary_groups_covered':sum(bool(ids.intersection(g)) for g in groups),
                    'necessary_groups_available':len(groups),
                    'missing_requirements':case.get('expected_missing', [])})
output = {'fixture_sha256':sha256(raw).hexdigest(), 'method':'BM25, vector disabled, top K of same candidate pool', 'cases':results}
(HERE / 'baseline.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
