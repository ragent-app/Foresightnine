"""Fixed narrow numeric/provenance checks, not full semantic validation."""
from pathlib import Path
from hashlib import sha256
from decimal import Decimal,InvalidOperation
import json,re
p=Path(__file__).resolve().parent;h=lambda b:sha256(b).hexdigest()
def decimal(v):
 try:
  x=Decimal(str(v));return x if x.is_finite() else None
 except (InvalidOperation,TypeError,ValueError):return None
def assess(task,result,prompt_hash):
 s=task['sources'][0];text=s['text'];content=result.get('content') or {};run=content.get('run') or {};facts=content.get('facts') or [];claims=content.get('claims') or []
 def located(item):
  quote,loc=item.get('quotation'),item.get('location')
  if item.get('source_id')!=s['source_id'] or not isinstance(quote,str) or not isinstance(loc,dict) or loc.get('status')=='ambiguous' or item.get('partial_token'):return False
  a,b=loc.get('character_start'),loc.get('character_end');return type(a) is int and type(b) is int and 0<=a<b<=len(text) and text[a:b]==quote
 def fact_check(value,year):
  return any(decimal(f.get('value'))==Decimal(value) and year in str(f.get('period','')) and
   'million' in str(f.get('unit','')).lower() and str(f.get('currency','')).upper()=='USD' and located(f) and
   value in re.sub(r'(?<=\d),(?=\d)', '', f['quotation']) for f in facts)
 quotes=[c for claim in claims for c in claim.get('citations',[]) if located(c)]
 qtext=' '.join(c['quotation'] for c in quotes); qflat=' '.join(qtext.split());qdigits=re.sub(r'(?<=\d),(?=\d)','',qflat)
 calc=any(x.get('formula_id')=='growth_percent' and decimal(x.get('value'))==Decimal('-38.07') and
  [decimal(a.get('value')) for a in x.get('inputs',[])]==[Decimal('5641'),Decimal('9108')] for x in content.get('calculations',[]))
 expected={'fact_2023':fact_check('5641','2023'),'fact_2022':fact_check('9108','2022'),
  'header_dates_and_units':bool(re.search(r'January\s+29,?\s+2023',qflat,re.I) and re.search(r'January\s+30,?\s+2022',qflat,re.I) and re.search(r'\bmillions\b',qflat,re.I)),
  'cash_flow_row':bool(re.search(r'net\s+cash\s+provided\s+by\s+operating\s+activities',qflat,re.I) and '5641' in qdigits and '9108' in qdigits),
  'growth_calculation':calc}
 warnings=bool(content.get('calculation_differences') or content.get('amount_differences') or content.get('evidence_differences') or any(x.get('uncited_amounts') or x.get('uncited_percentages') or x.get('uncited_basis_points') or x.get('uncited_fact_values') or x.get('citation_context_warnings') or x.get('rate_unit_warnings') or x.get('publication_date_warnings') for x in claims))
 omitted=any('was omitted' in x or 'were omitted' in x for x in content.get('limitations',[]) if isinstance(x,str))
 checks={'expected':all(expected.values()),'all_claim_citations_located':bool(claims) and all(c.get('citations') and c.get('support_status')!='incomplete' and all(located(x) for x in c['citations']) for c in claims),
 'no_system_warnings':not warnings and not omitted,'budget_and_provenance':result.get('operation_status')=='completed' and run.get('model_calls')==1 and run.get('search_calls')==0 and type(run.get('total_tokens')) is int and run['total_tokens']<=10000 and run.get('source_hashes')=={s['source_id']:h(text.encode())} and run.get('prompt_sha256')==prompt_hash}
 return {'operation_status':result.get('operation_status'),'research_status':run.get('status'),'reported_tokens':run.get('total_tokens'),'expected_detail':expected,'checks':checks,'narrow_mechanical_pass':all(checks.values())}
def main():
 mb=(p/'manifest.json').read_bytes();m=json.loads(mb);assert json.loads((p/'run-started.json').read_text())['manifest_sha256']==h(mb)
 assert all(h((p/n).read_bytes())==v for n,v in m['script_sha256'].items()) and h((p/'references.json').read_bytes())==m['references_sha256']
 task_bytes=(p/'CASH_FLOW-input.json').read_bytes();assert h(task_bytes)==m['tasks'][0]['input_sha256'];path=p/'CASH_FLOW-result.json'
 out={'manifest_sha256':h(mb),'semantic_support':'not independently established'}
 if path.exists():
  result=json.loads(path.read_text());assert result['manifest_sha256']==h(mb);out['result']=assess(json.loads(task_bytes),result,m['prompt_sha256_by_task']['CASH_FLOW'])
 else:out['result']={'status':'not_run','narrow_mechanical_pass':False}
 (p/'score.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
