"""Two primary-query arms with the same current online selector and local query allowance."""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import sys
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parents[5]/'apps/api'))
from app.research_library import ResearchLibrary
from app.research_library_tool import SearchLibraryRequest, sync_library, run_search_library
from app.research_sources import ResearchSource, SourcePermissions
fixture_raw=(HERE/'corpus.json').read_bytes(); requirements_raw=(HERE/'requirements.json').read_bytes()
f=json.loads(fixture_raw); definitions=json.loads(requirements_raw)['requirements']; as_of=datetime.fromisoformat(f['as_of'])
sources={x['id']:ResearchSource(source_id=x['id'],owner_id='offline',title=x['title'],text=x['text'],kind='user_supplied',published_at=datetime.fromisoformat(x['published_at']),obtained_at=datetime(2026,3,1,tzinfo=timezone.utc),content_sha256=sha256(x['text'].encode()).hexdigest(),permissions=SourcePermissions(store=True,display=True,model_input=x.get('model_input',True))) for x in f['sources']}
results=[]
for case in f['cases']:
    src={sid:sources[sid] for sid in case['included']}
    requirements=[{'id':rid,'question':definitions[rid]['description'],'status':'missing','core':True} for rid in case['requirements']]
    k=case.get('capacity_passages',f['capacity_passages']); arms={}
    queries={'topic':f['query'],'gap':' '.join(r['question'] for r in requirements)[:500]}
    for name,query in queries.items():
        lib=ResearchLibrary([],vector_scorer=None); sync_library(lib,src,{},as_of,vector_scorer=None)
        result,_=run_search_library(SearchLibraryRequest(query=query,max_hits=k),library=lib,source_by_id=src,versions={},as_of=as_of,requirements=requirements)
        # Both arms use the same tool, requirement queries, K and byte cap. A cap is
        # enforced below for scoring and must not be exceeded by actual tool output.
        byte_count=sum(len(row['snippet'].encode()) for row in result['hits'])
        assert byte_count<=512
        candidates={row['source_id'] for row in result['selection_trace']}; selected={row['source_id'] for row in result['hits']}
        arms[name]={'primary_query':query,'requirement_queries':result['requirement_queries'],'selection_trace':result['selection_trace'],
                    'selected_source_ids':[r['source_id'] for r in result['hits']],'selected_bytes':byte_count,
                    'local_query_count':1+len(result['requirement_queries']),
                    'candidate_groups_covered':sum(bool(candidates.intersection(g)) for g in case['expected_groups']),
                    'selected_groups_covered':sum(bool(selected.intersection(g)) for g in case['expected_groups'])}
    assert arms['topic']['local_query_count']==arms['gap']['local_query_count']
    results.append({'case_id':case['id'],'capacity_passages':k,'byte_limit':512,'necessary_groups_available':len(case['expected_groups']),'arms':arms})
out={'fixture_sha256':sha256(fixture_raw).hexdigest(),'requirements_sha256':sha256(requirements_raw).hexdigest(),
     'selector':'current run_search_library, including identical requirement expansion in both arms','cases':results}
(HERE/'comparison-b-online.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
