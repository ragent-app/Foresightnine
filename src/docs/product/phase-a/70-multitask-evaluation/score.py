"""Pre-registered narrow checks; quoted numbers are not semantic proof."""
from pathlib import Path
from hashlib import sha256
from decimal import Decimal,InvalidOperation
import json,re
p=Path(__file__).resolve().parent;h=lambda b:sha256(b).hexdigest();mb=(p/'manifest.json').read_bytes();m=json.loads(mb)
assert json.loads((p/'run-started.json').read_text())['manifest_sha256']==h(mb)
assert all(h((p/name).read_bytes())==digest for name,digest in m['script_sha256'].items()) and h((p/'references.json').read_bytes())==m['references_sha256']
refs=json.loads((p/'references.json').read_text());report={'manifest_sha256':h(mb),'semantic_support':'not established','independence':m['assessment'],'tasks':{}}
def num(value):
 try:
  n=Decimal(str(value));return n if n.is_finite() else None
 except (InvalidOperation,TypeError,ValueError):return None
def has(text,value):return bool(re.search(r'(?<![\d.,])'+re.escape(value)+r'(?![\d.])',str(text)))
for entry in m['tasks']:
 id=entry['id'];ib=(p/(id+'-input.json')).read_bytes();assert h(ib)==entry['input_sha256'];task=json.loads(ib);path=p/(id+'-result.json')
 if not path.exists():report['tasks'][id]={'mechanical_pass':False,'status':'not_run'};continue
 out=json.loads(path.read_text());assert out['manifest_sha256']==h(mb);c=out.get('content') or {};run=c.get('run') or {};claims=c.get('claims',[]);facts=c.get('facts',[])
 texts={s['source_id']:((p/(id+'-source.txt')).read_text() if s['kind']=='pdf' else s['text']) for s in task['sources']}
 def located(entry):
  source=texts.get(entry.get('source_id'));loc=entry.get('location');q=entry.get('quotation')
  if not isinstance(source,str) or not isinstance(loc,dict) or loc.get('status')=='ambiguous' or not isinstance(q,str) or entry.get('partial_token'):return False
  a,b=loc.get('character_start'),loc.get('character_end');return type(a) is int and type(b) is int and 0<=a<b<=len(source) and source[a:b]==q
 def fact_ok(value,unit):
  return any(num(x.get('value'))==num(value) and unit in str(x.get('unit','')).lower() and located(x) and has(x['quotation'],value) and not x.get('currency_context_unverified') and not x.get('direction_context_unverified') for x in facts)
 def claim_value_ok(value):
  return any(x.get('role')=='support' and has(x.get('text',''),value) and any(located(z) and has(z['quotation'],value) for z in x.get('citations',[])) for x in claims)
 expected={}
 if id in ('GDP_ESTIMATE','FED_PURCHASES'):
  unit='percent' if id=='GDP_ESTIMATE' else 'billion'
  for value in refs[id]['facts']:expected[value]=fact_ok(value,unit) and claim_value_ok(value)
 else:
  src0,src1=[s['source_id'] for s in task['sources']]
  def date_claim(x):
   text=str(x.get('text',''));cit=x.get('citations',[])
   first=bool(re.search(r'1\s*月\s*31|january\s*31',text,re.I));second=bool(re.search(r'12\s*月\s*31|december\s*31',text,re.I))
   different=bool(re.search(r'不同|不一致|不相同|并非|无法.{0,12}同期|different|not.{0,8}same',text,re.I))
   def quote(sid,term):return any(z.get('source_id')==sid and located(z) and term.lower() in z['quotation'].lower() for z in cit)
   return x.get('role')=='support' and first and second and different and quote(src0,'January 31') and quote(src1,'December 31')
  expected['different_fiscal_end_dates_with_both_quotes']=any(date_claim(x) for x in claims)
  calcs=c.get('calculations',[])
  for label,values,result in [('walmart',('648125','611289'),'6.03'),('coca_cola',('47061','45754'),'2.86')]:
   expected[label+'_growth']=any(x.get('formula_id')=='growth_percent' and num(x.get('value'))==num(result) and [num(z.get('value')) for z in x.get('inputs',[])]==[num(z) for z in values] for x in calcs)
 supports=[x for x in claims if x.get('role')=='support'];counters=[x for x in claims if x.get('role')=='counterargument']
 citations_ok=bool(supports) and bool(counters) and all(x.get('support_status')!='incomplete' and x.get('citations') and all(located(z) for z in x['citations']) for x in claims)
 review_ok=run.get('model_calls')==2 and run.get('support_review_status')=='complete' and all(x.get('support_review',{}).get('verdict')=='supports' for x in claims)
 warnings=bool(c.get('amount_differences') or c.get('calculation_differences') or any(x.get('uncited_fact_values') or x.get('uncited_amounts') or x.get('uncited_percentages') or x.get('citation_context_warnings') or x.get('rate_unit_warnings') or x.get('publication_date_warnings') for x in claims))
 budget=out.get('operation_status')=='completed' and run.get('search_calls')==0 and type(run.get('total_tokens')) is int and run['total_tokens']<=m['max_tokens_per_task']
 provenance=run.get('source_hashes')=={sid:h(text.encode()) for sid,text in texts.items()}
 passed=bool(expected and all(expected.values()) and citations_ok and review_ok and not warnings and budget and provenance)
 report['tasks'][id]={'status':out.get('operation_status'),'research_status':run.get('status'),'expected':expected,'citations_located':citations_ok,'review_coverage_and_verdicts_ok':review_ok,'warnings':warnings,'budget_ok':budget,'provenance_ok':provenance,'reported_tokens':run.get('total_tokens'),'mechanical_pass':passed,'semantic_support':'not established'}
report['passed']=sum(x['mechanical_pass'] for x in report['tasks'].values());report['total']=len(m['tasks']);(p/'score.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False,indent=2))
