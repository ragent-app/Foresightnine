"""Compare current selector and frozen top K using exactly the frozen candidate pool."""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import sys
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parents[5]/'apps/api'))
from app.research_library import ResearchLibrary, HitList
from app.research_library_tool import SearchLibraryRequest, sync_library, run_search_library
from app.research_sources import ResearchSource, SourcePermissions
fraw=(HERE/'corpus.json').read_bytes(); braw=(HERE/'baseline.json').read_bytes(); rraw=(HERE/'requirements.json').read_bytes()
f=json.loads(fraw); baseline=json.loads(braw); definitions=json.loads(rraw)['requirements']; as_of=datetime.fromisoformat(f['as_of'])
assert sha256(fraw).hexdigest()==baseline['fixture_sha256']
sources={x['id']:ResearchSource(source_id=x['id'],owner_id='offline',title=x['title'],text=x['text'],kind='user_supplied',published_at=datetime.fromisoformat(x['published_at']),obtained_at=datetime(2026,3,1,tzinfo=timezone.utc),content_sha256=sha256(x['text'].encode()).hexdigest(),permissions=SourcePermissions(store=True,display=True,model_input=x.get('model_input',True))) for x in f['sources']}
class FrozenLibrary:
    def __init__(self,lib, allowed): self.lib,self.allowed=lib,allowed
    def search(self,*args,**kwargs):
        hits=self.lib.search(*args,**kwargs)
        return HitList([h for h in hits if (h.source_id,h.char_start,h.char_end) in self.allowed],hits.coverage)
results=[]
for case,original in zip(f['cases'],baseline['cases'],strict=True):
    src={sid:sources[sid] for sid in case['included']}; lib=ResearchLibrary([],vector_scorer=None)
    sync_library(lib,src,{},as_of,vector_scorer=None)
    pool=original['candidate_pool']; allowed={(r['source_id'],r['read_sources']['start'],r['read_sources']['end']) for r in pool}
    requirements=[{'id':rid,'question':definitions[rid]['description'],'status':'missing','core':True} for rid in case['requirements']]
    k=original['capacity_passages']; req=SearchLibraryRequest(query=f['query'],max_hits=k)
    result,_=run_search_library(req,library=FrozenLibrary(lib,allowed),source_by_id=src,versions={},as_of=as_of,requirements=requirements)
    b=pool[:k]; selected=result['hits']; assert sum(len(r['snippet'].encode()) for r in b)<=512
    assert sum(len(r['snippet'].encode()) for r in selected)<=512
    def score(rows):
        ids={r['source_id'] for r in rows}; return sum(bool(ids.intersection(g)) for g in case['expected_groups'])
    results.append({'case_id':case['id'],'capacity_passages':k,'byte_limit':512,'available_groups':len(case['expected_groups']),
                    'baseline_selected':[r['source_id'] for r in b],'baseline_covered':score(b),
                    'online_selected':[r['source_id'] for r in selected],'online_covered':score(selected),
                    'selection_trace':result['selection_trace']})
out={'fixture_sha256':sha256(fraw).hexdigest(),'baseline_sha256':sha256(braw).hexdigest(),'requirements_sha256':sha256(rraw).hexdigest(),'cases':results}
(HERE/'comparison-a-online.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
