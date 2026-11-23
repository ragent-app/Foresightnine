"""Offline candidate selector against exact baseline candidate intervals and order."""
import json,sys
from pathlib import Path
from app.research_evidence_selection import select_evidence_candidates
here=Path(__file__).resolve().parent
cases={x['id']:x for x in json.loads((here/'inputs.json').read_text())}; rows=json.loads((here/'baseline-trace.json').read_text())
for row in rows:
 c=cases[row['id']]; sources={x[0]:x[2] for x in c['sources']}; trace=row['result']['selection_trace']; candidates=[]
 for rank,t in enumerate(trace):
  candidates.append({'source_id':t['source_id'],'start':t['start'],'end':t['end'],'text':sources[t['source_id']][t['start']:t['end']],'rank':rank})
 keys=[(item['source_id'],item['start'],item['end']) for item in candidates]
 base=[keys.index((h['source_id'],h['read_sources']['start'],h['read_sources']['end'])) for h in row['result']['hits']]
 idx,diag=select_evidence_candidates(candidates,row['requirements'],max_hits=c.get('passages',2),max_bytes=512,baseline_indices=base)
 row['result']['hits']=[{'source_id':candidates[i]['source_id'],'read_sources':{k:candidates[i][k] for k in ('source_id','start','end')}} for i in idx]
 row['fixed_pool_diagnostics']=diag
Path(sys.argv[1]).write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
