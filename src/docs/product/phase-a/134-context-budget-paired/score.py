from common import *
import re

def contains(pattern, value): return bool(re.search(pattern,value,re.I))
def assess(out, source):
 entries=[*out.get('claims',[]),*out.get('counterarguments',[])]
 def located(c):
  q, loc=c.get('quotation'),c.get('location',{})
  a,b=loc.get('character_start'),loc.get('character_end')
  return (c.get('source_id')==source.source_id and not c.get('partial_token') and loc.get('status')!='ambiguous'
    and type(a) is int and type(b) is int and isinstance(q,str) and 0<=a<b<=len(source.text) and source.text[a:b]==q)
 valid=[c for c in entries if c.get('citations') and all(located(x) for x in c['citations'])]
 text=' '.join(c.get('text','') for c in valid)
 quotes=' '.join(' '.join(x['quotation'].split()) for c in valid for x in c['citations'])
 counter=' '.join(c.get('text','') for c in out.get('counterarguments',[]) if c in valid)
 counterquotes=' '.join(' '.join(x['quotation'].split()) for c in out.get('counterarguments',[]) if c in valid for x in c['citations'])
 if out['task']=='GDP_THIRD':
  items={'rates':all(contains(r'(?<!\d)'+v+r'(?!\d)',x) for v in ('3\\.4','3\\.2') for x in (text,quotes)),
   'upward_drivers':contains('消费|消費|consumer',text) and contains('固定投资|固定投資|fixed investment',text) and contains('consumer spending',quotes) and contains('nonresidential fixed investment',quotes),
   'inventory_counter':contains('存货|存貨|库存|庫存|inventor',counter) and contains('private inventory',counterquotes)}
 else:
  items={'target_range':contains(r'0\s+to\s+1/4',quotes) and contains(r'0(?:\.0+)?\s*(?:%|％)?\s*(?:至|到|[-–—~]|to)\s*(?:0\.25|1/4)',text),
   'treasury_amount':contains(r'\$80\s+billion',quotes) and contains(r'(?:80\s*(?:billion|十亿)|800\s*亿)',text),
   'mortgage_amount':contains(r'\$40\s+billion',quotes) and contains(r'(?:40\s*(?:billion|十亿)|400\s*亿)',text),
   'risk_any_claim':contains('virus|pandemic|public health|疫情|病毒|公共卫生|公共衛生',text) and contains('virus|pandemic|public health',quotes)}
 return {'signals':items,'all_retained_claims_located':bool(entries) and len(valid)==len(entries),
         'valid_claims':len(valid),'claims_total':len(entries),'counter_count':len(out.get('counterarguments',[])),
         'semantic_support':'not independently established'}

m=json.loads((HERE/'manifest.json').read_text());started=json.loads((HERE/'run-started.json').read_text())
assert started['manifest_sha256']==digest((HERE/'manifest.json').read_bytes())
assert all(digest((HERE/n).read_bytes())==h for n,h in m['files'].items())
result={}
for c in m['conditions']:
 name=c['task'];path=HERE/f'{name}-{c["input_limit"]}-result.json'
 if not path.exists():continue
 out=json.loads(path.read_text());assert out['manifest_sha256']==started['manifest_sha256']
 _,sources=get_task(name)
 result[f'{name}-{c["input_limit"]}']=assess(out,sources[0]) if out['status']=='completed' else {'status':out['status']}
(HERE/'score.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
