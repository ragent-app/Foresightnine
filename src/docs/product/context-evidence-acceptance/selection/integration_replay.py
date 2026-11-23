"""Exercise the real tool path against frozen offline inputs and revocation."""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import sys
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[3] / 'apps/api'))
from app.research_library import ResearchLibrary
from app.research_library_tool import SearchLibraryRequest, sync_library, run_search_library
from app.research_sources import ResearchSource, SourcePermissions
fixture = json.loads((HERE/'corpus.json').read_text())
reqs = json.loads((HERE/'requirements.json').read_text())['requirements']
as_of = datetime.fromisoformat(fixture['as_of'])
sources = {x['id']: ResearchSource(source_id=x['id'],owner_id='offline',title=x['title'],text=x['text'],
    kind='user_supplied',published_at=datetime.fromisoformat(x['published_at']),obtained_at=datetime(2026,3,1,tzinfo=timezone.utc),
    content_sha256=sha256(x['text'].encode()).hexdigest(),permissions=SourcePermissions(store=True,display=True,
    model_input=x.get('model_input',True))) for x in fixture['sources']}
results=[]
for case in fixture['cases']:
    subset={sid:sources[sid] for sid in case['included']}
    lib=ResearchLibrary([],vector_scorer=None)
    sync_library(lib,subset,{},as_of,vector_scorer=None)
    requirements=[{'id':rid,'question':reqs[rid]['description'],'status':'missing','core':True} for rid in case['requirements']]
    req=SearchLibraryRequest(query=fixture['query'],max_hits=case.get('capacity_passages',fixture['capacity_passages']))
    result,_=run_search_library(req,library=lib,source_by_id=subset,versions={},as_of=as_of,requirements=requirements)
    ids=[x['source_id'] for x in result['hits']]
    results.append({'case_id':case['id'],'selected':ids,'requirements':requirements,'result':result,
                    'necessary_groups_covered':sum(bool(set(ids).intersection(g)) for g in case['expected_groups']),
                    'necessary_groups_available':len(case['expected_groups'])})
    if case['id']=='S1':
        old=subset['annual-table']
        subset['annual-table']=old.model_copy(update={'permissions':old.permissions.model_copy(update={'model_input':False})})
        sync_library(lib,subset,{},as_of,vector_scorer=None)
        revoked,_=run_search_library(req,library=lib,source_by_id=subset,versions={},as_of=as_of,requirements=requirements)
        assert 'annual-table' not in [x['source_id'] for x in revoked['hits']]
        assert any(x['source_id']=='annual-table' and x['reason']=='model_permission_missing' for x in revoked['library_coverage']['sources_excluded'])
        results[-1]['revoked_result']=revoked
output={'description':'Offline current search_library, no model/network; revocation checked after indexing','cases':results}
(HERE/'integration-replay.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
