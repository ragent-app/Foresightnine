"""Frozen mechanical checks; cannot prove the meaning or completeness of claims."""
from pathlib import Path
from hashlib import sha256
from decimal import Decimal,InvalidOperation
import json,re
p=Path(__file__).resolve().parent;h=lambda b:sha256(b).hexdigest();mb=(p/'manifest.json').read_bytes();m=json.loads(mb)
assert h(mb)==json.loads((p/'run-started.json').read_text())['manifest_sha256']
for name,key in [('run.py','runner_sha256'),('score.py','scorer_sha256'),('input.json','input_sha256')]:assert h((p/name).read_bytes())==m[key]
for s in m['sources']:
 assert h((p/s['pdf']).read_bytes())==s['pdf_sha256'] and h((p/s['text']).read_bytes())==s['text_sha256']
f=p/'result.json'
if not f.exists():
 out={'mechanical_pass':False,'status':'missing','manifest_sha256':h(mb)}
else:
 result=json.loads(f.read_text());assert result['manifest_sha256']==h(mb)
 source_text={sid:(p/src['text']).read_text() for sid,src in zip(result['source_ids'],m['sources'])}
 content=result.get('content') or {};facts=content.get('facts',[]);claims=content.get('claims',[]);run=content.get('run') or {}
 def locate(entry):
  sid=entry.get('source_id');loc=entry.get('location');q=entry.get('quotation');text=source_text.get(sid)
  if text is None or not isinstance(loc,dict) or not isinstance(q,str):return False
  a,b=loc.get('character_start'),loc.get('character_end')
  return type(a) is int and type(b) is int and 0<=a<b<=len(text) and text[a:b]==q
 def fact_match(f,sid,value):
  try:num=Decimal(str(f.get('value')))
  except (InvalidOperation,ValueError):return False
  period=(str(f.get('period') or '')+' '+str(f.get('label') or '')).lower();unit=str(f.get('unit') or '').lower();currency=str(f.get('currency') or '').lower()
  return (f.get('source_id')==sid and num==Decimal(value) and locate(f) and value in f['quotation'] and '2024' in period and
          any(x in period for x in ('sept','09','9月')) and ('billion' in unit or '十亿' in unit) and
          (currency in ('usd','美元') or 'dollar' in unit or 'usd' in unit or '美元' in unit))
 old,new=result['source_ids'];required=[{'value':'74.4','source':'earlier','found':any(fact_match(x,old,'74.4') for x in facts)},
                                     {'value':'88.2','source':'later','found':any(fact_match(x,new,'88.2') for x in facts)}]
 def claim_matches(c):
  text=str(c.get('text') or '');cit=c.get('citations') or []
  if not isinstance(cit,list):return False
  sources={q.get('source_id') for q in cit if isinstance(q,dict) and locate(q)}
  return ('74.4' in text and '88.2' in text and any(word in text.lower() for word in ('修订','修正','revis','updated')) and {old,new}<=sources)
 both=any(claim_matches(x) for x in claims)
 budget=(result.get('operation_status')=='completed' and run.get('model_calls')==1 and run.get('search_calls')==0 and
         type(run.get('total_tokens')) is int and run['total_tokens']<=m['max_tokens_per_task'] and
         isinstance(run.get('elapsed_seconds'),(int,float)) and run['elapsed_seconds']<=m['max_elapsed_seconds_per_task'])
 out={'manifest_sha256':h(mb),'status':result.get('operation_status'),'research_status':run.get('status'),'facts':required,
      'one_revision_claim_cites_both_sources':both,'budget_ok':budget,'reported_tokens':run.get('total_tokens'),
      'mechanical_pass':bool(all(x['found'] for x in required) and both and budget),
      'semantic_support':'unverified','historical_byte_identity':'unverified','cost_usd':'unknown'}
(p/'score.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');print(json.dumps(out,ensure_ascii=False,indent=2))
