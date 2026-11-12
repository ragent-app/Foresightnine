"""Fixed mechanical checks; does not infer semantic support."""
from pathlib import Path
from hashlib import sha256
import json,re
p=Path(__file__).resolve().parent; h=lambda x:sha256(x).hexdigest(); mb=(p/'manifest.json').read_bytes();m=json.loads(mb)
assert json.loads((p/'run-started.json').read_text())['manifest_sha256']==h(mb)
assert all(h((p/n).read_bytes())==d for n,d in m['script_sha256'].items()) and h((p/'references.json').read_bytes())==m['references_sha256']
out={'manifest_sha256':h(mb),'semantic_support':'not established','tasks':{}}
def has(s, pattern): return bool(re.search(pattern,str(s),re.I))
for e in m['tasks']:
 id=e['id']; data=(p/(id+'-input.json')).read_bytes();assert h(data)==e['input_sha256']; task=json.loads(data); f=p/(id+'-result.json')
 if not f.exists():out['tasks'][id]={'status':'not_run','mechanical_pass':False};continue
 r=json.loads(f.read_text());assert r['manifest_sha256']==h(mb); content=r.get('content') or {};run=content.get('run') or {}; claims=content.get('claims',[])
 source=(p/(id+'-source.txt')).read_text();sid=task['sources'][0]['source_id']
 def located(c):
  q=c.get('quotation');loc=c.get('location');
  if c.get('source_id')!=sid or not isinstance(q,str) or not isinstance(loc,dict) or loc.get('status')=='ambiguous' or c.get('partial_token'):return False
  a,b=loc.get('character_start'),loc.get('character_end');return type(a) is int and type(b) is int and 0<=a<b<=len(source) and source[a:b]==q
 def claim_and_quote(claim_pattern,quote_pattern):
  return any(has(x.get('text',''),claim_pattern) and any(located(z) and has(z.get('quotation',''),quote_pattern) for z in x.get('citations',[])) for x in claims)
 if id=='PCE_PRICES':
  expected={'monthly_0_3':claim_and_quote(r'0\.3',r'0\.3'), 'annual_5':claim_and_quote(r'(?<!\d)5(?:\.0)?(?!\d)',r'5\.0'),
            'comparison_periods':any(has(x.get('text',''),r'环比|上月|preceding month|month.on.month') for x in claims) and any(has(x.get('text',''),r'同比|去年|上年|year.on.year|one year') for x in claims)}
 else:
  expected={'target_low':claim_and_quote(r'0\.25|1/4|四分之一',r'1/4'), 'target_high':claim_and_quote(r'0\.5|1/2|二分之一',r'1/2'),
            'dissenter':claim_and_quote(r'Bullard|布拉德',r'Bullard'), 'dissent_high':claim_and_quote(r'0\.75|3/4|四分之三',r'3/4')}
 citations=bool(claims) and all(x.get('citations') and x.get('support_status')!='incomplete' and all(located(z) for z in x['citations']) for x in claims)
 review=run.get('support_review_status')=='complete' and all(x.get('support_review',{}).get('verdict')=='supports' for x in claims)
 warnings=bool(content.get('amount_differences') or content.get('calculation_differences') or any(x.get('uncited_fact_values') or x.get('uncited_amounts') or x.get('uncited_percentages') or x.get('citation_context_warnings') or x.get('rate_unit_warnings') or x.get('publication_date_warnings') for x in claims))
 budget=r.get('operation_status')=='completed' and run.get('search_calls')==0 and type(run.get('total_tokens')) is int and run['total_tokens']<=10000 and run.get('model_calls',0)<=2
 provenance=run.get('source_hashes')=={sid:h(source.encode())}
 passed=all(expected.values()) and citations and review and not warnings and budget and provenance
 out['tasks'][id]={'status':r.get('operation_status'),'research_status':run.get('status'),'expected':expected,'citations_located':citations,'model_review_coverage':review,'warnings':warnings,'budget_ok':budget,'provenance_ok':provenance,'reported_tokens':run.get('total_tokens'),'mechanical_pass':passed}
out['passed']=sum(v.get('mechanical_pass',False) for v in out['tasks'].values());out['total']=len(m['tasks']);(p/'score.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');print(json.dumps(out,ensure_ascii=False,indent=2))
