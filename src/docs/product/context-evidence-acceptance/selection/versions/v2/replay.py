"""Run both frozen v1 and current tool with actual pre-analysis requirement assessment."""
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
from pathlib import Path
import json, sys
HERE=Path(__file__).resolve().parent
SELECTION=HERE.parent.parent
ROOT=HERE.parents[5]
sys.path.insert(0,str(ROOT/'apps/api'))
from app.research_library import ResearchLibrary
from app.research_delivery import assess
from app.research_sources import ResearchSource, SourcePermissions
from app import research_library_tool as v2
spec=importlib.util.spec_from_file_location('app.selection_v1_library_tool',SELECTION/'versions/v1/research_library_tool.py')
v1=importlib.util.module_from_spec(spec); spec.loader.exec_module(v1)
raw=(HERE/'development.json').read_bytes(); fixture=json.loads(raw); as_of=datetime.fromisoformat(fixture['as_of'])
rows=[]
for case in fixture['cases']:
    sources={x['id']:ResearchSource(source_id=x['id'],owner_id='offline',title='Source',text=x['text'],kind=x['kind'],published_at=datetime(2026,1,1,tzinfo=timezone.utc),obtained_at=datetime(2026,2,1,tzinfo=timezone.utc),content_sha256=sha256(x['text'].encode()).hexdigest(),permissions=SourcePermissions(store=True,display=True,model_input=True)) for x in case['sources']}
    requirements=assess(case['question'],{},sources,as_of=as_of)
    arms={}
    for label,module in [('v1',v1),('v2',v2)]:
        lib=ResearchLibrary([],vector_scorer=None); module.sync_library(lib,sources,{},as_of,vector_scorer=None)
        req=module.SearchLibraryRequest(query=case['query'],max_hits=case['k'])
        result,_=module.run_search_library(req,library=lib,source_by_id=sources,versions={},as_of=as_of,requirements=requirements)
        selected={r['source_id'] for r in result['hits']}
        arms[label]={'selected':[r['source_id'] for r in result['hits']],
                     'groups_covered':sum(bool(selected.intersection(g)) for g in case['groups']),
                     'result':result}
    rows.append({'case_id':case['id'],'requirements':requirements,'available_groups':len(case['groups']),'arms':arms})
out={'fixture_sha256':sha256(raw).hexdigest(),'description':'Pre-analysis requirements from real assess; fixed development materials and same K; no model, vectors, or network','cases':rows}
(HERE/'development-results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
