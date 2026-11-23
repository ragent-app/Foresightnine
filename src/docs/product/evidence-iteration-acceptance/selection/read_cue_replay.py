"""Controlled second search following an authorized read, after the first 50 hits fill up."""
import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from app.research_library import ResearchLibrary
from app.research_library_tool import SearchLibraryRequest, run_search_library, sync_library
from app.research_sources import ResearchSource, SourcePermissions

now = datetime(2026, 1, 1, tzinfo=timezone.utc)
result = []
for question, cue, missing in [('explain the change in operating costs', 'Note 73', 'Capacity reductions explain the charge.'),
                               ('核实本期交货限制', '附注 73', '客户尚未完成现场验收。')]:
    sources = {}
    def add(sid, text):
        sources[sid] = ResearchSource(source_id=sid, owner_id='offline', title=sid, kind='user_supplied',
            text=text, content_sha256=sha256(text.encode()).hexdigest(), obtained_at=now, published_at=now,
            permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))
    add('report', question + '. ' + cue)
    add('disclosure', cue + '. ' + missing)
    for i in range(64):
        add(f'news-{i}', f'{question}. {i}')
    span = {'source_id':'report','start':0,'end':len(sources['report'].text),'content_sha256':sources['report'].content_sha256}
    for mode in ('repeat', 'directed', 'revised', 'scope_excludes_pointer'):
        library = ResearchLibrary([], vector_scorer=None)
        sync_library(library,sources,{},now,vector_scorer=None)
        search = library.search
        calls = []
        def measured(query, **kwargs):
            actual = question if mode == 'repeat' and query == cue else query
            calls.append(actual)
            return search(actual, **kwargs)
        library.search = measured
        row = dict(span)
        if mode == 'revised':
            row['content_sha256'] = '0' * 64
        ids = ['disclosure'] if mode == 'scope_excludes_pointer' else None
        out, _ = run_search_library(SearchLibraryRequest(query=question, max_hits=2, source_ids=ids), library=library,
                                    source_by_id=sources, versions={}, as_of=now, read_ranges=[row])
        pool = {r['source_id'] for r in out['selection_trace']}
        result.append({'question':question, 'arm':mode, 'queries':calls, 'pointer_target_in_pool':'disclosure' in pool,
                       'expandable_pointer': any(r['source_id']=='disclosure' for r in out.get('unread_followup_locations', [])),
                       'pointer_target_selected':any(r['source_id']=='disclosure' for r in out['hits'])})
    repeat, directed, revised, scoped = result[-4:]
    assert len(repeat['queries']) == len(directed['queries']) == 2
    assert not repeat['pointer_target_in_pool'] and directed['pointer_target_in_pool']
    assert directed['expandable_pointer'] or directed['pointer_target_selected']
    assert len(revised['queries']) == len(scoped['queries']) == 1
Path(__file__).with_name('read-cues.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
