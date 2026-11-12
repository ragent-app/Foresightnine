"""Pre-registered checks; quote matching does not establish semantic support."""
from pathlib import Path
from hashlib import sha256
from decimal import Decimal,InvalidOperation
import json,re
p=Path(__file__).resolve().parent;h=lambda b:sha256(b).hexdigest();mb=(p/'manifest.json').read_bytes();m=json.loads(mb)
assert json.loads((p/'run-started.json').read_text())['manifest_sha256']==h(mb)
assert all(h((p/n).read_bytes())==v for n,v in m['script_sha256'].items()) and h((p/'references.json').read_bytes())==m['references_sha256']
out={'manifest_sha256':h(mb),'semantic_support':'not independently established','tasks':{}}
def dec(value):
 try:
  n=Decimal(str(value));return n if n.is_finite() else None
 except (InvalidOperation,ValueError,TypeError):return None
for entry in m['tasks']:
 id=entry['id'];ib=(p/(id+'-input.json')).read_bytes();assert h(ib)==entry['input_sha256'];task=json.loads(ib);path=p/(id+'-result.json')
 if not path.exists():out['tasks'][id]={'status':'not_run','mechanical_pass':False};continue
 r=json.loads(path.read_text());assert r['manifest_sha256']==h(mb);c=r.get('content') or {};run=c.get('run') or {};claims=c.get('claims',[]);facts=c.get('facts',[]);source=task['sources'][0];text=source['text'] if source['kind']=='text' else (p/(id+'-source.txt')).read_text()
 def located(item):
  q=item.get('quotation');loc=item.get('location')
  if item.get('source_id')!=source['source_id'] or not isinstance(q,str) or not isinstance(loc,dict) or loc.get('status')=='ambiguous' or item.get('partial_token'):return False
  a,b=loc.get('character_start'),loc.get('character_end');return type(a) is int and type(b) is int and 0<=a<b<=len(text) and text[a:b]==q
 if id=='MSFT_REVENUE':
  expected={}
  for year,value in [('2022','198270000000'),('2023','211915000000')]:
   expected['fact_'+year]=any(dec(f.get('value'))==dec(value) and f.get('unit')=='USD' and year in str(f.get('period','')) and located(f) and value in f['quotation'] and year in f['quotation'] for f in facts)
  expected['growth_calculation']=any(x.get('formula_id')=='growth_percent' and dec(x.get('value'))==Decimal('6.88') and [dec(a.get('value')) for a in x.get('inputs',[])]==[Decimal('211915000000'),Decimal('198270000000')] for x in c.get('calculations',[]))
 else:
  def claim_quote(claim_rx,quote_rx):return any(re.search(claim_rx,str(x.get('text','')),re.I) and any(located(z) and re.search(quote_rx,str(z.get('quotation','')),re.I|re.S) for z in x.get('citations',[])) for x in claims if x.get('role')=='support')
  expected={'target_range':claim_quote(r'3(?:\.75|-3/4|又3/4|又四分之三)',r'3-3/4\s+to\s+4\s+percent'),'future_policy':claim_quote(r'持續|持续|繼續|继续|ongoing|further',r'ongoing\s+increases')}
 warnings=bool(c.get('calculation_differences') or c.get('amount_differences') or c.get('evidence_differences') or any(x.get('uncited_amounts') or x.get('uncited_percentages') or x.get('uncited_basis_points') or x.get('uncited_fact_values') or x.get('citation_context_warnings') or x.get('rate_unit_warnings') or x.get('publication_date_warnings') for x in claims))
 omitted=any('was omitted' in x or 'were omitted' in x for x in c.get('limitations',[]) if isinstance(x,str))
 checks={'expected':bool(expected) and all(expected.values()),'all_citations_located':bool(claims) and all(x.get('citations') and x.get('support_status')!='incomplete' and all(located(z) for z in x['citations']) for x in claims),'no_system_warnings':not warnings and not omitted,'research_complete':run.get('status')=='completed','budget_and_provenance':r.get('operation_status')=='completed' and run.get('model_calls')==1 and run.get('search_calls')==0 and type(run.get('total_tokens')) is int and run['total_tokens']<=10000 and run.get('source_hashes')=={source['source_id']:h(text.encode())} and run.get('prompt_sha256')==m['prompt_sha256_by_task'][id]}
 out['tasks'][id]={'operation_status':r.get('operation_status'),'research_status':run.get('status'),'reported_tokens':run.get('total_tokens'),'expected_detail':expected,'checks':checks,'mechanical_pass':all(checks.values())}
out['passed']=sum(x.get('mechanical_pass',False) for x in out['tasks'].values());out['total']=len(m['tasks']);(p/'score.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');print(json.dumps(out,ensure_ascii=False,indent=2))
