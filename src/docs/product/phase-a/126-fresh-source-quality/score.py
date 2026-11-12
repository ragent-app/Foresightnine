"""Pre-registered mechanical checks; no semantic or investment-quality verdict."""
from pathlib import Path
from hashlib import sha256
import json,re
p=Path(__file__).resolve().parent
h=lambda b:sha256(b).hexdigest()
def includes(pattern,text):return bool(re.search(pattern,text,re.IGNORECASE))
def assess(task,result,prompt_hash):
 s=task['sources'][0];source_text=(p/s['text_file']).read_text() if s['kind']=='pdf' else s['text']
 c=result.get('content') or {};run=c.get('run') or {};claims=c.get('claims') or []
 def valid(citation):
  if not isinstance(citation,dict) or citation.get('source_id')!=s['source_id'] or citation.get('partial_token'):return False
  loc,quote=citation.get('location'),citation.get('quotation')
  if not isinstance(loc,dict) or loc.get('status')=='ambiguous' or not isinstance(quote,str):return False
  a,b=loc.get('character_start'),loc.get('character_end')
  return type(a) is int and type(b) is int and 0<=a<b<=len(source_text) and source_text[a:b]==quote
 valid_claims=[x for x in claims if isinstance(x,dict) and x.get('citations') and all(valid(cit) for cit in x['citations'])]
 def combined(role=None):
  chosen=[x for x in valid_claims if role is None or x.get('role')==role]
  return ' '.join(str(x.get('text','')) for x in chosen),' '.join(cit['quotation'] for x in chosen for cit in x['citations'])
 text,quotes=combined();counter,counter_quotes=combined('counterargument')
 if task['id']=='GDP_THIRD':
  detail={'rates_in_claim_and_quotes':all(includes(r'(?<!\d)'+n+r'(?!\d)',t) for n in (r'3\.4',r'3\.2') for t in (text,quotes)),
    'annualized_in_claim_and_quotes':includes('年化|annual',text) and includes('annual',quotes),
    'upward_components':includes('消费|消費|consumer',text) and includes('固定投资|固定投資|fixed investment',text) and includes('consumer spending',quotes) and includes('nonresidential fixed investment',quotes),
    'offset_in_counterargument':includes('存货|存貨|库存|庫存|inventor',counter) and includes('下|减少|減少|declin|down',counter) and includes('private inventory',counter_quotes) and includes('downward|decrease',counter_quotes)}
 else:
  detail={'rate_in_claim_and_quotes':includes(r'(?<!\d)0(?:\.0+)?\s*(?:%|％)?\s*(?:至|到|[-–—~]|to)\s*(?:0\.25|1/4)',text) and includes(r'0\s+to\s+1/4',quotes),
    'treasury_in_claim_and_quotes':includes(r'(?:80\s*(?:billion|十亿)|800\s*亿)',text) and includes(r'(?:treasur|国债|國債)',text) and includes(r'treasury securities',quotes) and includes(r'\$80\s+billion',quotes),
    'mbs_in_claim_and_quotes':includes(r'(?:40\s*(?:billion|十亿)|400\s*亿)',text) and includes(r'(?:抵押|mortgage)',text) and includes(r'mortgage',quotes) and includes(r'\$40\s+billion',quotes),
    'risk_in_counterargument':includes(r'疫情|病毒|公共卫生|公共衛生|尚未恢复|尚未恢復|virus|pandemic|not fully recovered',counter) and includes(r'virus|pandemic|public health|not fully recovered',counter_quotes)}
 checks={'required_elements':all(detail.values()),'all_claim_citations_located':bool(claims) and len(valid_claims)==len(claims) and all(x.get('support_status')!='incomplete' for x in claims),
 'budget_and_provenance':result.get('operation_status')=='completed' and run.get('model_calls')==1 and run.get('search_calls')==0 and type(run.get('total_tokens')) is int and run['total_tokens']<=10000 and run.get('source_hashes')=={s['source_id']:h(source_text.encode())} and run.get('prompt_sha256')==prompt_hash}
 return {'operation_status':result.get('operation_status'),'research_status':run.get('status'),'reported_tokens':run.get('total_tokens'),'required_detail':detail,'checks':checks,'narrow_mechanical_pass':all(checks.values()),'semantic_support':'not independently established'}
def main():
 mb=(p/'manifest.json').read_bytes();m=json.loads(mb);assert json.loads((p/'run-started.json').read_text())['manifest_sha256']==h(mb)
 assert all(h((p/n).read_bytes())==v for n,v in m['script_sha256'].items()) and h((p/'references.json').read_bytes())==m['references_sha256']
 out={'manifest_sha256':h(mb),'results':{}}
 for task in m['tasks']:
  name=task['id'];raw=(p/(name+'-input.json')).read_bytes();assert h(raw)==task['input_sha256'];f=p/(name+'-result.json')
  if f.exists():
   result=json.loads(f.read_text());assert result['manifest_sha256']==h(mb);out['results'][name]=assess(json.loads(raw),result,m['prompt_sha256_by_task'][name])
  else:out['results'][name]={'status':'not_run','narrow_mechanical_pass':False}
 (p/'score.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n');print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
