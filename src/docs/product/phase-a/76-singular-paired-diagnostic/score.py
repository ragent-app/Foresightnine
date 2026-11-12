"""Fixed, conservative mechanical checks, not semantic validation."""
from pathlib import Path
from hashlib import sha256
import json,re
p=Path(__file__).resolve().parent;h=lambda b:sha256(b).hexdigest();mb=(p/'manifest.json').read_bytes();m=json.loads(mb)
assert json.loads((p/'run-started.json').read_text())['manifest_sha256']==h(mb)
assert all(h((p/n).read_bytes())==v for n,v in m['script_sha256'].items()) and h((p/'references.json').read_bytes())==m['references_sha256']
task=json.loads((p/'FOMC_DISSENT-input.json').read_text());source=(p/'FOMC_DISSENT-source.txt').read_text();file=p/'FOMC_DISSENT-result.json'
if not file.exists():out={'status':'not_run','mechanical_pass':False}
else:
 result=json.loads(file.read_text());assert result['manifest_sha256']==h(mb);content=result.get('content') or {};run=content.get('run') or {};claims=content.get('claims',[])
 def located(c):
  loc=c.get('location');q=c.get('quotation')
  if c.get('source_id')!=task['sources'][0]['source_id'] or not isinstance(loc,dict) or loc.get('status')=='ambiguous' or not isinstance(q,str) or c.get('partial_token'):return False
  a,b=loc.get('character_start'),loc.get('character_end');return type(a) is int and type(b) is int and 0<=a<b<=len(source) and source[a:b]==q
 def match(s,p):return bool(re.search(p,str(s),re.I))
 def target(c):return match(c.get('text',''),r'1/4|0\.25|四分之一') and match(c.get('text',''),r'1/2|0\.5|二分之一') and any(located(q) and match(q.get('quotation',''),r'1/4\s+to\s+1/2\s+percent') for q in c.get('citations',[]))
 def dissent(c):return match(c.get('text',''),r'Bullard|布拉德') and match(c.get('text',''),r'0\.5|半个|1/2') and match(c.get('text',''),r'0\.75|3/4|四分之三') and any(located(q) and match(q.get('quotation',''),r'Bullard') and match(q.get('quotation',''),r'0\.5\s+percentage\s+point') and match(q.get('quotation',''),r'1/2\s+to\s+3/4\s+percent') for q in c.get('citations',[]))
 checks={'target_range_and_source':any(target(c) for c in claims),'dissenter_preference_and_source':any(dissent(c) for c in claims), 'all_citations_located':bool(claims) and all(c.get('citations') and c.get('support_status')!='incomplete' and all(located(q) for q in c['citations']) for c in claims), 'no_system_warnings':not any(c.get('uncited_amounts') or c.get('uncited_percentages') or c.get('uncited_fact_values') or c.get('citation_context_warnings') or c.get('rate_unit_warnings') or c.get('publication_date_warnings') for c in claims),
 'budget_and_provenance':result.get('operation_status')=='completed' and run.get('model_calls')==1 and run.get('search_calls')==0 and type(run.get('total_tokens')) is int and run['total_tokens']<=10000 and run.get('source_hashes')=={task['sources'][0]['source_id']:h(source.encode())} and run.get('prompt_sha256')==m['new_prompt_sha256']}
 out={'status':result.get('operation_status'),'research_status':run.get('status'),'reported_tokens':run.get('total_tokens'),'checks':checks,'mechanical_pass':all(checks.values()),'semantic_support':'not established'}
(p/'score.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');print(json.dumps(out,ensure_ascii=False,indent=2))
