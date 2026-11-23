"""Synthetic stress, same deterministic pool, wall time and traced Python allocations."""
import cProfile,hashlib,json,statistics,time,tracemalloc
from pathlib import Path
from app.research_evidence_selection import select_evidence_candidates
here=Path(__file__).resolve().parent
candidates=[{'source_id':f's{i}','start':0,'end':len(t),'text':t,'rank':i} for i in range(1000) for t in [f'Issuer {i} revenue growth in 2024 and 2025; price contribution risk decline {i}.']]
requirements=[{'id':'growth','kind':'contribution:revenue','question':'revenue change contribution and risk'}]
def run(): return select_evidence_candidates(candidates,requirements,max_hits=2,max_bytes=512)
times=[]
for _ in range(3):
 start=time.perf_counter(); indices,diag=run(); times.append(time.perf_counter()-start)
tracemalloc.start(); start=time.perf_counter(); run(); traced=time.perf_counter()-start; _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
p=cProfile.Profile();start=time.perf_counter();p.enable();run();p.disable();profiled=time.perf_counter()-start
result={'n':len(candidates),'unprofiled_seconds':times,'median_seconds':statistics.median(times),'tracemalloc_seconds':traced,'tracemalloc_peak_bytes':peak,'cprofile_seconds':profiled,'chosen_indices':indices,'diagnostics':diag,'caveat':'tracemalloc is Python allocation peak, not RSS; synthetic homogeneous candidate pool, excludes retrieval.'}
(here/'performance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
