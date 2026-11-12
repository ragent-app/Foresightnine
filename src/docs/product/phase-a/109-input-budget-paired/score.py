"""Fixed mechanical checks: matches are necessary but not semantic proof."""
from pathlib import Path
from hashlib import sha256
import json,re
p=Path(__file__).resolve().parent;h=lambda b:sha256(b).hexdigest()
def assess(task, result, expected_hash):
 c=result.get('content') or {};run=c.get('run') or {};claims=c.get('claims') or []
 texts={s['source_id']:(s['text'] if s['kind']=='text' else (p/s['text_file']).read_text()) for s in task['sources']}
 def located(citation):
  source=texts.get(citation.get('source_id'));loc=citation.get('location');q=citation.get('quotation')
  if not isinstance(source,str) or not isinstance(loc,dict) or not isinstance(q,str) or loc.get('status')=='ambiguous' or citation.get('partial_token'):return False
  a,b=loc.get('character_start'),loc.get('character_end');return type(a) is int and type(b) is int and 0<=a<b<=len(source) and source[a:b]==q
 def matching(claim_pattern, quote_pattern, sid=None, role='support'):
  return any(re.search(claim_pattern,str(claim.get('text','')),re.I|re.S) and any((sid is None or cite.get('source_id')==sid) and located(cite) and re.search(quote_pattern,cite.get('quotation',''),re.I|re.S) for cite in claim.get('citations',[])) for claim in claims if claim.get('role')==role)
 if task['id']=='GDP_REVISION':
  s1,s2=[x['source_id'] for x in task['sources']]
  expected={'second_2_8':matching(r'2\.8',r'2\.8\s+percent',s1), 'third_3_1':matching(r'3\.1',r'3\.1\s+percent',s2),
  'annualized':matching(r'年化|折年|annual',r'annual rate|annualized',s2),
  'upward_components':matching(r'出口|export',r'exports',s2) and matching(r'消费|消費|consumer',r'consumer spending',s2),
  'offset_inventory':matching(r'库存|存货|存貨|inventor',r'(?:downward|downturn).{0,100}(?:private\s+)?inventor',s2,role='counterargument')}
 else:
  expected={'hold_and_range':matching(r'维持|維持|maintain|unchanged',r'maintain.{0,100}5-1/4\s+to\s+5-1/2\s+percent') and matching(r'5(?:\.25|-1/4|又四分之一)',r'5-1/4\s+to\s+5-1/2\s+percent'),
  'tightening':matching(r'紧缩|緊縮|tightening',r'cumulative tightening'),
  'lags':matching(r'滞后|滯後|lag',r'lags'),
  'conditions':matching(r'经济|經濟|economic',r'economic and financial developments')}
 warnings=bool(c.get('calculation_differences') or c.get('amount_differences') or c.get('evidence_differences') or any(x.get('uncited_amounts') or x.get('uncited_percentages') or x.get('uncited_basis_points') or x.get('uncited_fact_values') or x.get('citation_context_warnings') or x.get('rate_unit_warnings') or x.get('publication_date_warnings') for x in claims))
 omitted=any('was omitted' in x or 'were omitted' in x for x in c.get('limitations',[]) if isinstance(x,str))
 checks={'expected':all(expected.values()),'all_citations_located':bool(claims) and all(x.get('citations') and x.get('support_status')!='incomplete' and all(located(z) for z in x['citations']) for x in claims),
 'no_system_warnings':not warnings and not omitted,'research_complete':run.get('status')=='completed',
 'budget_and_provenance':result.get('operation_status')=='completed' and run.get('model_calls')==1 and run.get('search_calls')==0 and type(run.get('total_tokens')) is int and run['total_tokens']<=10000 and run.get('source_hashes')=={sid:h(text.encode()) for sid,text in texts.items()} and run.get('prompt_sha256')==expected_hash}
 return {'operation_status':result.get('operation_status'),'research_status':run.get('status'),'reported_tokens':run.get('total_tokens'),'expected_detail':expected,'checks':checks,'mechanical_pass':all(checks.values())}
def main():
 mb=(p/'manifest.json').read_bytes();m=json.loads(mb);assert json.loads((p/'run-started.json').read_text())['manifest_sha256']==h(mb)
 assert all(h((p/n).read_bytes())==v for n,v in m['script_sha256'].items()) and h((p/'references.json').read_bytes())==m['references_sha256']
 output={'manifest_sha256':h(mb),'semantic_support':'not independently established','tasks':{}}
 for entry in m['tasks']:
  id=entry['id'];ib=(p/(id+'-input.json')).read_bytes();assert h(ib)==entry['input_sha256'];path=p/(id+'-result.json')
  if not path.exists():output['tasks'][id]={'status':'not_run','mechanical_pass':False};continue
  result=json.loads(path.read_text());assert result['manifest_sha256']==h(mb)
  output['tasks'][id]=assess(json.loads(ib),result,m['prompt_sha256_by_task'][id])
 output['passed']=sum(x.get('mechanical_pass',False) for x in output['tasks'].values());output['total']=len(m['tasks']);(p/'score.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n');print(json.dumps(output,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
