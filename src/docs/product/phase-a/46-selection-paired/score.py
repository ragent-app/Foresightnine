"""Pre-fixed conservative checks; do not infer full semantic correctness."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
from pathlib import Path
from datetime import datetime
import json,re
p=Path(__file__).resolve().parent
h=lambda x:sha256(x).hexdigest()
mb=(p/'manifest.json').read_bytes();m=json.loads(mb)
assert h(mb)==json.loads((p/'run-started.json').read_text())['manifest_sha256']
assert h((p/'score.py').read_bytes())==m['scorer_sha256'] and h((p/'references.json').read_bytes())==m['references_sha256']
refs=json.loads((p/'references.json').read_bytes())
def num(value):
 try:
  result=Decimal(str(value)); return result if result.is_finite() else None
 except (InvalidOperation,ValueError,TypeError): return None
def located(citation,text):
 loc=citation.get('location'); quote=citation.get('quotation')
 if citation.get('source_id') not in current_allowed or not isinstance(loc,dict) or not isinstance(quote,str) or loc.get('status')=='ambiguous' or citation.get('partial_token'): return False
 a,b=loc.get('character_start'),loc.get('character_end')
 return type(a) is int and type(b) is int and 0<=a<b<=len(text) and text[a:b]==quote
def quote_matches(citation,exp,text):
 if not located(citation,text):return False
 quote=' '.join(citation['quotation'].lower().split())
 return exp['literal'].lower() in quote and (not exp.get('basis') or any(word.lower() in quote for word in exp['basis']))
def unit_matches(fact,unit):
 value=str(fact.get('unit') or '').lower();currency=str(fact.get('currency') or '').upper()
 if unit=='point':return ('point' in value or '百分点' in value) and not currency
 if unit=='percent':return (value=='%' or 'percent' in value or '百分' in value) and 'point' not in value and '百分点' not in value and not currency
 return ('million' in value or '百万' in value) and ('usd' in value or currency=='USD' or '美元' in value) and currency in ('','USD')
def claim_mentions(text,exp):
 compact=text.lower().replace(',','')
 if any(alias.lower() in compact for alias in exp.get('claim_aliases',[])):return True
 for part in re.findall(r'(?<![A-Za-z0-9_.])[-−]?\d+(?:\.\d+)?',compact):
  if num(part.replace('−','-'))==num(exp['value']):return True
 return False
report={'manifest_sha256':h(mb),'independence':'reserved sources and tasks, nonblind to evaluator; no independent human review','semantic_support':'not established by mechanical checks','historical_availability':'not established','tasks':{}}
for entry in m['inputs']:
 id=entry['id']; b=(p/(id+'-input.json')).read_bytes();assert h(b)==entry['sha256']; task=json.loads(b);ref=refs[id]
 if task['source_kind']=='original_pdf':
  assert h((p/task['source_file']).read_bytes())==task['source_sha256']; text=(p/(id+'-source.txt')).read_text();assert h(text.encode())==task['source_text_sha256']
 else:text=task['source_text']
 path=p/(id+'-result.json')
 if not path.exists():report['tasks'][id]={'mechanical_pass':False,'status':'not_run'};continue
 out=json.loads(path.read_text());assert out['manifest_sha256']==h(mb); c=out.get('content') or {}; run=c.get('run') or {}; claims=[x for x in c.get('claims',[]) if x.get('role')=='support'];facts=c.get('facts',[]);checks=[]
 expected_hash=h(text.encode()); current_allowed={x.get('source_id') for x in c.get('sources',[]) if x.get('content_sha256')==expected_hash and isinstance(x.get('source_id'),str)}
 provenance_ok=len(current_allowed)==1 and run.get('source_hashes')=={next(iter(current_allowed)):expected_hash}
 if not provenance_ok:current_allowed=set()
 declared_ok=True
 if task.get('published_at'):
  try: declared_ok=any(datetime.fromisoformat(x['published_at'].replace('Z','+00:00'))==datetime.fromisoformat(task['published_at'].replace('Z','+00:00')) for x in c.get('sources',[]) if x.get('source_id') in current_allowed and x.get('published_at'))
  except (ValueError,TypeError): declared_ok=False
 for exp in ref['facts']:
  fact=any(num(x.get('value'))==num(exp['value']) and exp['period'] in (str(x.get('period',''))+' '+str(x.get('label',''))) and unit_matches(x,exp['unit']) and quote_matches(x,exp,text) and not x.get('currency_context_unverified') for x in facts)
  claim=any(claim_mentions(x.get('text',''),exp) and any(quote_matches(q,exp,text) for q in x.get('citations',[])) for x in claims)
  checks.append({'value':exp['value'],'unit':exp['unit'],'fact_matches':fact,'same_claim_mentions_value_and_cites_source':claim})
 calc=ref.get('calculation');calc_ok=not calc or any(x.get('formula_id')==calc['formula_id'] and num(x.get('value'))==num(calc['value']) and x.get('unit')=='percent' and [num(y.get('value')) for y in x.get('inputs',[])]==[num(y) for y in calc['inputs']] for x in c.get('calculations',[]))
 names_ok=all(any(any(n.lower() in str(x.get('text','')).lower() for n in check['claim_aliases']) and any(located(q,text) and check['quote_term'].lower() in q['quotation'].lower() for q in x.get('citations',[])) for x in claims) for check in ref.get('name_checks',[]))
 citations_ok=bool(claims) and all(x.get('citations') and x.get('support_status')!='incomplete' and all(located(q,text) for q in x['citations']) for x in claims)
 omitted=any(('quotation' in str(x).lower() and ('omitted' in str(x).lower() or 'cuts through' in str(x).lower())) for x in c.get('limitations',[]))
 unknown_percentages=[]
 for claim_item in c.get('claims',[]):
  text_value=str(claim_item.get('text',''))
  for match in re.finditer(r'(?<![A-Za-z0-9_.])([-−]?\d+(?:\.\d+)?)\s*(?:%|％|percent\b)', text_value, re.I):
   token=match.group(1).replace('−','-'); value=num(token); places=len(token.split('.',1)[1]) if '.' in token else 0
   if value==100 and re.search(r'(?:/|÷)\s*[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*\)*\s*[×*]\s*$',text_value[:match.start()]):continue
   if value is None or places>8 or not any(num(x).quantize(Decimal(1).scaleb(-places),rounding=ROUND_HALF_UP)==value for x in ref['allowed_percentages']): unknown_percentages.append(token)
 unresolved_percentages=any(any(value not in {item.get('claimed_percentage') for item in x.get('suggested_calculations',[])} for value in x.get('uncited_percentages',[])) for x in c.get('claims',[]))
 warnings=bool(unknown_percentages or unresolved_percentages or c.get('calculation_differences') or c.get('amount_differences') or any(x.get('publication_date_warnings') or x.get('rate_unit_warnings') for x in c.get('claims',[])))
 counter=[x for x in c.get('claims',[]) if x.get('role')=='counterargument'];counter_ok=(all(x.get('citations') and x.get('support_status')!='incomplete' and all(located(q,text) for q in x['citations']) for x in counter) if counter else any('No counterarguments were provided' in str(x) for x in c.get('limitations',[])))
 budget=out.get('operation_status')=='completed' and run.get('model_calls')==1 and run.get('search_calls')==0 and type(run.get('total_tokens')) is int and run['total_tokens']<=m['max_tokens_per_task'] and isinstance(run.get('elapsed_seconds'),(int,float)) and run['elapsed_seconds']<=m['max_elapsed_seconds_per_task']
 passed=budget and provenance_ok and declared_ok and all(x['fact_matches'] and x['same_claim_mentions_value_and_cites_source'] for x in checks) and calc_ok and names_ok and citations_ok and counter_ok and not omitted and not warnings
 report['tasks'][id]={'operation_status':out.get('operation_status'),'research_status':run.get('status'),'facts':checks,'calculation_ok':bool(calc_ok),'name_checks_ok':names_ok,'counterargument_citations_located':counter_ok,'support_citations_located_and_complete':bool(citations_ok),'omitted_or_truncated_quotation':omitted,'review_warnings':warnings,'unknown_percentages':unknown_percentages,'source_provenance_ok':provenance_ok,'declared_publication_matches':declared_ok,'budget_ok':bool(budget),'reported_tokens':run.get('total_tokens'),'mechanical_pass':bool(passed),'semantic_support':'not established'}
report['passed']=sum(x['mechanical_pass'] for x in report['tasks'].values());report['total']=len(m['inputs']);(p/'score.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False,indent=2))
