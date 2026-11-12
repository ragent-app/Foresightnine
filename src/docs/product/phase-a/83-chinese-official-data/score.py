"""Narrow fixed checks; not a semantic or independent assessment."""
from pathlib import Path
from hashlib import sha256
import json,re
p=Path(__file__).resolve().parent;h=lambda b:sha256(b).hexdigest();mb=(p/'manifest.json').read_bytes();m=json.loads(mb)
assert json.loads((p/'run-started.json').read_text())['manifest_sha256']==h(mb)
assert all(h((p/n).read_bytes())==v for n,v in m['script_sha256'].items()) and h((p/'references.json').read_bytes())==m['references_sha256']
entry=m['tasks'][0];ib=(p/(entry['id']+'-input.json')).read_bytes();assert h(ib)==entry['input_sha256'];task=json.loads(ib);source=(p/(entry['id']+'-source.txt')).read_text();assert h(source.encode())==m['extract_sha256'];path=p/(entry['id']+'-result.json')
if not path.exists():out={'status':'not_run','mechanical_pass':False}
else:
 result=json.loads(path.read_text());assert result['manifest_sha256']==h(mb);content=result.get('content') or {};run=content.get('run') or {};claims=content.get('claims',[])
 def located(c):
  loc=c.get('location');q=c.get('quotation')
  if c.get('source_id')!=task['sources'][0]['source_id'] or not isinstance(loc,dict) or loc.get('status')=='ambiguous' or not isinstance(q,str) or c.get('partial_token'):return False
  a,b=loc.get('character_start'),loc.get('character_end');return type(a) is int and type(b) is int and 0<=a<b<=len(source) and source[a:b]==q
 def matches(text,value):return bool(re.search(r'(?<![\d.])'+value+r'(?![\d.])',str(text)))
 def claim_value(value):return any(matches(c.get('text',''),value) and any(located(q) and matches(q.get('quotation',''),value) for q in c.get('citations',[])) for c in claims)
 checks={name:claim_value(value) for name,value in [('total_317','317'),('total_change_7_3',r'7\.3'),('online_29','29'),('online_change_7',r'7(?:\.0)?'),('online_share_9_3',r'9\.3')]}
 checks['provisional_estimate']=any(re.search('臨時|临时|provisional',str(c.get('text','')),re.I) and any(located(q) and re.search('臨時|临时|provisional',str(q.get('quotation','')),re.I) for q in c.get('citations',[])) for c in claims)
 checks['all_claim_citations']=bool(claims) and all(c.get('citations') and c.get('support_status')!='incomplete' and all(located(q) for q in c['citations']) for c in claims)
 checks['no_numeric_or_context_warnings']=not any(c.get('uncited_amounts') or c.get('uncited_percentages') or c.get('uncited_basis_points') or c.get('uncited_fact_values') or c.get('citation_context_warnings') or c.get('rate_unit_warnings') or c.get('publication_date_warnings') for c in claims)
 checks['budget_and_provenance']=result.get('operation_status')=='completed' and run.get('model_calls')==1 and run.get('search_calls')==0 and type(run.get('total_tokens')) is int and run['total_tokens']<=10000 and run.get('source_hashes')=={task['sources'][0]['source_id']:h(source.encode())} and run.get('prompt_sha256')==m['new_prompt_sha256']
 out={'status':result.get('operation_status'),'research_status':run.get('status'),'reported_tokens':run.get('total_tokens'),'checks':checks,'mechanical_pass':all(checks.values()),'semantic_support':'not established'}
(p/'score.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');print(json.dumps(out,ensure_ascii=False,indent=2))
