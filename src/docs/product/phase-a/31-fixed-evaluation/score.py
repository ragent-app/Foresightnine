"""Frozen narrow machine checks; natural-language entailment remains unverified."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
from pathlib import Path
import json,re
p=Path(__file__).resolve().parent;h=lambda b:sha256(b).hexdigest()
mb=(p/'manifest.json').read_bytes();m=json.loads(mb)
assert h(mb)==json.loads((p/'run-started.json').read_text())['manifest_sha256']
assert h((p/'score.py').read_bytes())==m['scorer_sha256'] and h((p/'references.json').read_bytes())==m['references_sha256']
refs=json.loads((p/'references.json').read_bytes())
report={'manifest_sha256':h(mb),'independence':'new task inputs not previously sent to model; nonblind authors; not independent human evaluation','semantic_support':'unverifiable','historical_availability':'unverified','tasks':{}}
def num(v):
 try:
  x=Decimal(str(v));return x if x.is_finite() else None
 except (InvalidOperation,ValueError):return None
def locate(x,text):
 loc=x.get('location');q=x.get('quotation')
 if not isinstance(loc,dict) or not isinstance(q,str):return False
 a,b=loc.get('character_start'),loc.get('character_end')
 return type(a) is int and type(b) is int and 0<=a<b<=len(text) and text[a:b]==q
def quote_has(quote,expected):
 normalized=' '.join(str(quote or '').lower().split())
 return expected['literal'] in normalized and (not expected.get('basis') or any(s in normalized for s in expected['basis']))
def unit_ok(f,expected,ref):
 unit=str(f.get('unit','')).lower(); currency=str(f.get('currency') or '').lower()
 if expected.get('unit_override'):return 'point' in unit or '百分点' in unit
 if ref['unit']=='percent':return ('%'==unit or 'percent' in unit or '百分' in unit) and not currency
 return ('million' in unit or '百万' in unit) and ('usd' in unit or currency=='usd' or '美元' in unit)
pct=re.compile(r'(?<![A-Za-z0-9_.])([-−]?\d+(?:\.\d+)?)\s*(?:%|％|percent\b)',re.I)
scale=re.compile(r'(?:/|÷)\s*[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*\)*\s*[×*]\s*$')
for entry in m['inputs']:
 id=entry['id'];raw=(p/(id+'-input.json')).read_bytes();assert h(raw)==entry['sha256'];task=json.loads(raw)
 if task['source_kind']=='original_pdf':
  assert h((p/task['source_file']).read_bytes())==task['source_sha256']
  text=(p/(id+'-source.txt')).read_text();assert h(text.encode())==task['source_text_sha256']
 else:text=task['source_text']
 f=p/(id+'-result.json')
 if not f.exists():report['tasks'][id]={'status':'missing','mechanical_pass':False};continue
 output=json.loads(f.read_text());assert output['manifest_sha256']==h(mb)
 c=output.get('content') or {};run=c.get('run') or {};facts=c.get('facts') or [];support=[x for x in c.get('claims',[]) if x.get('role')=='support'];ref=refs[id]
 checks=[]
 for exp in ref['facts']:
  def fact_match(f):
   period=(str(f.get('period') or '')+' '+str(f.get('label') or '')).lower()
   return num(f.get('value'))==num(exp['value']) and exp['period'] in period and unit_ok(f,exp,ref) and locate(f,text) and quote_has(f.get('quotation'),exp)
  has_fact=any(fact_match(f) for f in facts)
  has_cited_claim=any(any(locate(q,text) and quote_has(q.get('quotation'),exp) for q in (claim.get('citations') or [])) for claim in support)
  checks.append({'value':exp['value'],'located_fact_with_period_and_unit':has_fact,'cited_supporting_claim_mentions_reference':has_cited_claim})
 all_support_located=bool(support) and all(x.get('citations') and all(locate(y,text) for y in x['citations']) for x in support)
 calc=ref['calculation'];calc_ok=True
 if calc:calc_ok=any(x.get('formula_id')==calc['formula_id'] and num(x.get('value'))==num(calc['value']) and x.get('unit')=='percent' and [num(y.get('value')) for y in x.get('inputs',[])]==[num(y) for y in calc['inputs']] for x in c.get('calculations',[]))
 unknown=[]
 for claim in c.get('claims',[]):
  claim_text=str(claim.get('text',''))
  for match in pct.finditer(claim_text):
   raw_value=match.group(1).replace('−','-');value=num(raw_value)
   if value==100 and scale.search(claim_text[:match.start()]):continue
   places=len(raw_value.split('.',1)[1]) if '.' in raw_value else 0
   if value is None or places>8:unknown.append(raw_value);continue
   quantum=Decimal(1).scaleb(-places)
   if not any(num(a).quantize(quantum,rounding=ROUND_HALF_UP)==value for a in ref['allowed_percentages']):unknown.append(raw_value)
 omitted=any('quotation' in str(x).lower() and 'omitted' in str(x).lower() for x in c.get('limitations',[]))
 budget=(output.get('operation_status')=='completed' and run.get('model_calls')==1 and run.get('search_calls')==0 and
         type(run.get('total_tokens')) is int and run['total_tokens']<=m['max_tokens_per_task'] and
         isinstance(run.get('elapsed_seconds'),(int,float)) and run['elapsed_seconds']<=m['max_elapsed_seconds_per_task'])
 pass_checks=budget and all(x['located_fact_with_period_and_unit'] and x['cited_supporting_claim_mentions_reference'] for x in checks) and bool(all_support_located) and calc_ok and not unknown and not c.get('calculation_differences') and not c.get('amount_differences') and not any(x.get('publication_date_warnings') for x in c.get('claims',[])) and not omitted
 report['tasks'][id]={'input_kind':task['source_kind'],'operation_status':output.get('operation_status'),'research_status':run.get('status'),
  'facts':checks,'all_supporting_claims_have_locations':bool(all_support_located),'required_calculation_ok':calc_ok,
  'unknown_percentages_requiring_review':unknown,'calculation_differences':c.get('calculation_differences',[]),'amount_differences':c.get('amount_differences',[]), 'publication_date_warnings':[x.get('publication_date_warnings') for x in c.get('claims',[]) if x.get('publication_date_warnings')],
  'citation_omission_requires_review':omitted,'budget_ok':budget,'reported_tokens':run.get('total_tokens'),
  'mechanical_pass':bool(pass_checks),'semantic_support':'unverifiable','cost_usd':'unknown'}
report['passed']=sum(t['mechanical_pass'] for t in report['tasks'].values());report['total']=len(m['inputs'])
(p/'score.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False,indent=2))
