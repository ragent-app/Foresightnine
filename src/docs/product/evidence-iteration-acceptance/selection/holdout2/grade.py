"""Offline evaluator. A candidate covers a labelled source only when its full text is present."""
import itertools,json,sys,hashlib
from pathlib import Path
here=Path(__file__).resolve().parent
for name,digest in json.loads((here/'freeze.json').read_text()).items():
 if hashlib.sha256((here/name).read_bytes()).hexdigest()!=digest: raise ValueError('Frozen file changed: '+name)
cases=json.loads((here/'inputs.json').read_text()); answers=json.loads((here/'answers.json').read_text()); trace={x['id']:x for x in json.loads(Path(sys.argv[1]).read_text())}; out=[]
for c in cases:
 cid=c['id']; src={s[0]:s[2] for s in c['sources']}; meta=c.get('source_meta',{}); clock='2026-03-31' if cid.startswith('S') else '2026-11-22'; cap=c.get('passages',2); groups=answers[cid]['groups']; result=trace[cid]['result']
 def eligible(s): return meta.get(s,{}).get('model_input',True) and meta.get(s,{}).get('published_at','0000')[:10]<=clock
 def score(ids): return sum(any(set(alt)<=set(ids) for alt in group) for group in groups)
 def best(ids):
  choices=[]
  for n in range(cap+1):
   for combo in itertools.combinations(sorted(set(ids)),n):
    texts=[src[x] for x in combo]; size=sum(len(t.encode()) for t in texts)
    if size<=512 and len(set(texts))==len(texts): choices.append((score(combo),size,combo))
  x=min(choices,key=lambda x:(-x[0],x[1],x[2])); return {'ids':x[2],'complete_items':x[0],'bytes':x[1]}
 corpus=[s for s in src if eligible(s)]; pool=[x['source_id'] for x in result.get('selection_trace',[]) if x['source_id'] in src and eligible(x['source_id']) and x['start']==0 and x['end']>=len(src[x['source_id']])]
 chosen=[]; size=0
 for hit in result.get('hits',[]):
  sid=hit['source_id']; loc=hit['read_sources']; b=len(src[sid][loc['start']:loc['end']].encode())+len((hit.get('table_header') or '').encode())
  if size+b<=512:
   size+=b
   if loc['start']==0 and loc['end']>=len(src[sid]): chosen.append(sid)
 a,b=best(corpus),best(pool); n=score(chosen); total=len(groups)
 loss='recall' if b['complete_items']<a['complete_items'] else 'selection_or_capacity' if n<b['complete_items'] else 'none_observed'
 out.append({'id':cid,'corpus_oracle':a,'candidate_pool_oracle':b,'candidate_ids':pool,'selected_full_sources':chosen,'selected_bytes':size,'selected_items':n,'total_items':total,'complete':n==total,'first_loss':loss,'actual_model_input':'not_observed','delivery_and_reopened_version':'not_observed','limitations':answers[cid].get('limitations'),'capacity_passages':cap,'capacity_bytes':512,'selected_ineligible':[s for s in chosen if not eligible(s)]})
Path(sys.argv[2]).write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
