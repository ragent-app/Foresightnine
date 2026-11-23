"""Run against the API code selected by PYTHONPATH, then grade its output separately."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app.research_library import ResearchLibrary
from app.research_library_tool import SearchLibraryRequest, run_search_library, sync_library
from app.research_sources import ResearchSource, SourcePermissions

HERE = Path(__file__).resolve().parent
CLOCK = datetime(2026, 11, 22, tzinfo=timezone.utc)
PASSAGES = 2
EVIDENCE_BYTES = 512


def fixtures(suite):
    for name, digest in json.loads((suite / 'freeze.json').read_text()).items():
        if digest != hashlib.sha256((suite / name).read_bytes()).hexdigest():
            raise ValueError(f'Frozen file changed: {name}')
    return json.loads((suite / 'materials.json').read_text())


def sources_for(case):
    return [
        ResearchSource(
            source_id=sid, owner_id='context-qa', title=title, kind='user_supplied', text=text,
            obtained_at=CLOCK, published_at=CLOCK, content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
        )
        for sid, title, text in case['sources']
    ]


def retrieve(case, mode, requests):
    sources = {src.source_id: src for src in sources_for(case)}
    library = ResearchLibrary([], vector_scorer=None)
    sync_library(library, sources, {}, CLOCK, vector_scorer=None)
    request = SearchLibraryRequest(query=case['question'], max_hits=PASSAGES)
    kwargs = {}
    if mode == 'runtime':
        from app.research_delivery import assess
        kwargs['requirements'] = assess(case['question'], {}, sources, as_of=CLOCK)
    elif mode == 'decomposed':
        kwargs['requirements'] = [
            {'id': f'item-{i}', 'question': text, 'status': 'missing', 'core': True, 'references': []}
            for i, text in enumerate(requests[case['id']])
        ]
    result, _ = run_search_library(request, library=library, source_by_id=sources, versions={}, as_of=CLOCK, **kwargs)
    selected = []
    evidence_bytes = 0
    for hit in result['hits']:
        sid = hit['source_id']
        location = hit['read_sources']
        passage = sources[sid].text[location['start']:location['end']]
        size = len(passage.encode()) + len((hit.get('table_header') or '').encode())
        if evidence_bytes + size <= EVIDENCE_BYTES:
            evidence_bytes += size
            selected.append(sid)
    return {'id': case['id'], 'selected': selected, 'evidence_bytes': evidence_bytes}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    parser.add_argument('--mode', choices=['plain', 'runtime', 'decomposed'], default='plain')
    parser.add_argument('--suite', type=Path, default=HERE)
    args = parser.parse_args()
    requests = json.loads((args.suite / 'requests.json').read_text())
    rows = [retrieve(case, args.mode, requests) for case in fixtures(args.suite)]
    keys = json.loads((args.suite / 'expectations.json').read_text())
    for row in rows:
        groups = keys[row['id']]['required_groups']
        row['complete_groups'] = sum(set(group) <= set(row['selected']) for group in groups)
        row['required_groups'] = len(groups)
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
