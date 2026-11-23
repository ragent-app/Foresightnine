from datetime import datetime, timezone
from time import perf_counter
import json, tracemalloc
from app.research_library import ResearchLibrary
from app.research_library_tool import sync_library, run_search_library, SearchLibraryRequest
from app.research_sources import ResearchSource, SourcePermissions
sources={}
for i in range(20):
    text='\n'.join(f'Entity {i} report paragraph {j} revenue growth 2025 compared with 2024 value {j} million and risk factors.' for j in range(1200))
    sid=f's{i}'; sources[sid]=ResearchSource(source_id=sid,owner_id='test',title=sid,text=text,kind='official',published_at=datetime(2026,1,1,tzinfo=timezone.utc),obtained_at=datetime(2026,2,1,tzinfo=timezone.utc),content_sha256='0'*64,permissions=SourcePermissions(store=True,display=True,model_input=True))
lib=ResearchLibrary([],vector_scorer=None)
t=perf_counter();sync_library(lib,sources,{},None,vector_scorer=None); indexing=perf_counter()-t
requirements=[{'id':f'r{i}','question':f'revenue growth 2025 period {i}','kind':'financial:revenue:0','metric':'revenue','status':'missing','core':True} for i in range(10)]
tracemalloc.start();t=perf_counter(); result,_=run_search_library(SearchLibraryRequest(query='revenue growth 2025',max_hits=20),library=lib,source_by_id=sources,versions={},as_of=None,requirements=requirements); elapsed=perf_counter()-t
_,peak=tracemalloc.get_traced_memory()
print(json.dumps({'sources':len(sources),'source_characters':sum(len(s.text) for s in sources.values()),'indexed_passages':lib.stats()['passages'],'index_seconds':round(indexing,3),'search_seconds_under_tracemalloc':round(elapsed,3),'peak_traced_bytes':peak,'candidate_count':len(result['selection_trace']),'selection_keys':[(x['source_id'],x['start'],x['end']) for x in result['selection_trace'] if x['decision']=='selected']}))
