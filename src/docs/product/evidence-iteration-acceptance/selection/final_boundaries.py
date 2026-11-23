"""Deterministic UTF-8 header and original-source interval bounds."""
import json
from pathlib import Path
from app.research_evidence_selection import select_evidence_candidates
p=Path(__file__).resolve().parent
# A three-byte character repeated 170 times plus a two-byte body is exactly 512 bytes.
base={'source_id':'s','start':0,'end':2,'text':'ab','rank':0}
within={**base,'header':'中'*170}; over={**base,'header':'中'*171}
a,da=select_evidence_candidates([within],[],max_hits=2,max_bytes=512,baseline_indices=[0]);b,db=select_evidence_candidates([over],[],max_hits=2,max_bytes=512,baseline_indices=[0]);assert a==[0] and da['bytes']==512 and b==[] and db['bytes']==0
results={'utf8_header_at_512_accepted':True,'utf8_header_at_515_rejected':True,'header_fixture':'synthetic direct selector; no real table header in short replay fixtures','suite_checks':{}}
for folder in (p,p/'holdout2',p/'holdout3'):
 cases={c['id']:c for c in json.loads((folder/'inputs.json').read_text())}; rows=json.loads((folder/'final-natural-trace.json').read_text()); violations=[];max_bytes=0
 for r in rows:
  c=cases[r['id']];s={x[0]:x[2] for x in c['sources']}; meta=c.get('source_meta',{});clock='2026-03-31' if r['id'].startswith('S') else '2026-11-22';total=0
  if len(r['result']['hits'])>c.get('passages',2):violations.append([r['id'],'passage_count'])
  for hit in r['result']['hits']:
   loc=hit['read_sources']; sid=loc['source_id']; source=s[sid];start,end=loc['start'],loc['end']
   if not (0<=start<end<=len(source)): violations.append([r['id'],'invalid_range',sid]);continue
   if not meta.get(sid,{}).get('model_input',True) or meta.get(sid,{}).get('published_at','0000')[:10]>clock: violations.append([r['id'],'ineligible',sid])
   total+=len(source[start:end].encode())+len((hit.get('table_header') or '').encode())
  if total>512:violations.append([r['id'],'bytes',total])
  max_bytes=max(max_bytes,total)
 results['suite_checks'][folder.name]={'count':len(rows),'violations':violations,'max_sum_utf8_bytes_including_each_header':max_bytes}
(p/'final-boundaries.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
