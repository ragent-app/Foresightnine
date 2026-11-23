"""Offline, read-only replay of source identity and local filing search/read.

Run with PYTHONPATH=src/apps/api and pass a directory containing the private
history.jsonl and sources.jsonl exports. Only aggregate results are emitted.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from app.research_run_insights import primary_filing_gap, run_coverage_map
from app.research_sources import ResearchSource
from app.research_tool_executor import _read_sources, _search_source
from app.research_tools_registry import ReadRequest, SearchSourceRequest


def replay(directory: Path, checkpoint_row: int = 2) -> dict:
    with (directory / 'history.jsonl').open(encoding='utf-8') as file:
        rows = [json.loads(line) for line in file]
    with (directory / 'sources.jsonl').open(encoding='utf-8') as file:
        sources = [ResearchSource.model_validate_json(line) for line in file]
    state = rows[checkpoint_row]['checkpoint']
    by_id = {source.source_id: source for source in sources}
    coverage = run_coverage_map(state, by_id)
    documents = coverage['documents']
    claims = state.get('draft_claims') or (state.get('submission') or {}).get('claims', [])
    gap = primary_filing_gap(state, by_id, claims)
    if not gap:
        raise ValueError('The snapshot has no eligible, relevant unverified filing gap.')
    query = gap['metric'].replace('_', ' ')
    search = _search_source(SearchSourceRequest(source_id=gap['source_id'], query=query, max_hits=5, rationale='Check financial evidence.'), source_by_id=by_id, as_of=None)
    if not search.get('hits'):
        raise ValueError('No targeted filing search hit was returned.')
    hit = search['hits'][0]
    read_ranges: list[dict] = []
    read, _ = _read_sources(ReadRequest.model_validate({'reads': [{'source_id':gap['source_id'], 'start':hit['snippet_start'], 'end':hit['snippet_end']}], 'rationale':'Inspect the matched filing passage.'}), source_by_id=by_id, as_of=None, read_ranges=read_ranges)
    assert read['results'][0]['status'] == 'read' and read_ranges
    return {'source_count':len(sources), 'origin_counts':dict(Counter(d['origin'] for d in documents)),
            'category_counts':dict(Counter(d['category'] for d in documents)),
            'targeted_metric':gap['metric'], 'targeted_period':gap['period'],
            'search_hits':search['total_matches'], 'read_spans':len(read_ranges),
            'read_characters':sum(row['end'] - row['start'] for row in read_ranges),
            'evidence_gap_remains_until_fact_and_citation':True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--checkpoint-row', type=int, default=2)
    args = parser.parse_args()
    print(json.dumps(replay(args.directory, args.checkpoint_row), ensure_ascii=False, sort_keys=True))
