"""Pre-registered mechanical checks only; not a semantic or blind evaluation."""
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
from hashlib import sha256
import json,re
from pathlib import Path
p=Path(__file__).resolve().parent;h=lambda b:sha256(b).hexdigest();mb=(p/'manifest.json').read_bytes();m=json.loads(mb)
assert h(mb)==json.loads((p/'run-started.json').read_text())['manifest_sha256']
assert h((p/'score.py').read_bytes())==m['scorer_sha256'] and h((p/'references.json').read_bytes())==m['references_sha256']
refs=json.loads((p/'references.json').read_bytes());report={'manifest_sha256':h(mb),'sample':'public, non-blind candidate; derived facts only','semantic_support':'unverifiable','historical_availability':'unverified','tasks':{}}
def num(v):
 try:
  x=Decimal(str(v));return x if x.is_finite() else None
 except (InvalidOperation,ValueError):return None
def locate(x,text):
 loc=x.get('location');q=x.get('quotation');
 if not isinstance(loc,dict) or not isinstance(q,str):return False
 a,b=loc.get('character_start'),loc.get('character_end');return type(a) is int and type(b) is int and 0<=a<b<=len(text) and text[a:b]==q
def unit_ok(f,unit):
 u=str(f.get('unit','')).lower();currency=str(f.get('currency') or '').lower()
 return (('%'==u or 'percent' in u) and not currency) if unit=='percent' else ('million' in u and ('usd' in u or currency=='usd'))
for item in m['inputs']:
 id=item['id'];b=(p/(id+'-input.json')).read_bytes();assert h(b)==item['sha256'];text=json.loads(b)['source_text'];f=p/(id+'-result.json')
 if not f.exists():report['tasks'][id]={'status':'missing','mechanical_pass':False};continue
 r=json.loads(f.read_text());assert r['manifest_sha256']==h(mb);c=r.get('content') or {};run=c.get('run') or {};facts=c.get('facts') or []
 checks=[]
 for expected in refs[id]['facts']:
  def matches(f):
   q=str(f.get('quotation') or '').lower();period=(str(f.get('period') or '')+' '+str(f.get('label') or '')).lower()
   return (num(f.get('value'))==num(expected['value']) and expected['period'] in period and unit_ok(f,refs[id]['unit']) and locate(f,text)
      and (not expected.get('qualifier') or expected['qualifier'] in (period+' '+q))
      and (not expected.get('basis') or expected['basis'] in q))
  checks.append({'value':expected['value'],'valid':any(matches(f) for f in facts)})
 support=[c for c in c.get('claims',[]) if c.get('role')=='support'];located=bool(support) and all(x.get('citations') and all(locate(y,text) for y in x['citations']) for x in support)
 expected=refs[id]['calculation'];calc_ok=True
 if expected:calc_ok=any(x.get('formula_id')==expected['formula_id'] and num(x.get('value'))==num(expected['value']) and x.get('unit')==expected['unit'] and [num(y.get('value')) for y in x.get('inputs',[])]==[num(y) for y in expected['inputs']] for x in c.get('calculations',[]))
 # Conservative scan: unfamiliar percentages trigger review, not a claim of falsity.
 pct=re.compile(r'(?<![\w.])([-−]?\d+(?:\.\d+)?)\s*(?:%|％|percent\b)',re.I);unknown=[]
 for claim in c.get('claims',[]):
  for match in pct.finditer(str(claim.get('text',''))):
   raw=match.group(1).replace('−','-');number=num(raw);places=len(raw.split('.',1)[1]) if '.' in raw else 0
   if places>8 or number is None:unknown.append(raw);continue
   scale=Decimal(1).scaleb(-places)
   if not any(num(allowed).quantize(scale,rounding=ROUND_HALF_UP)==number for allowed in refs[id]['allowed_percentages']):unknown.append(raw)
 budget=r.get('operation_status')=='completed' and run.get('model_calls')==1 and run.get('search_calls')==0 and run.get('total_tokens') is not None and run['total_tokens']<=m['max_tokens_per_task']
 no_conflict=not c.get('calculation_differences') and not unknown
 passed=budget and all(x['valid'] for x in checks) and bool(located) and calc_ok and no_conflict
 report['tasks'][id]={'operation_status':r.get('operation_status'),'research_status':run.get('status'),'facts':checks,'support_claims_have_locations':bool(located),'required_calculation_ok':calc_ok,'unrecognized_claim_percentages_requiring_review':unknown,'program_detected_calculation_differences':c.get('calculation_differences',[]),'budget_ok':budget,'reported_tokens':run.get('total_tokens'),'mechanical_pass':passed,'semantic_support':'unverifiable','cost_usd':'unknown'}
report['mechanical_tasks_passed']=sum(x['mechanical_pass'] for x in report['tasks'].values());report['mechanical_task_count']=len(m['inputs'])
(p/'score.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False,indent=2))
