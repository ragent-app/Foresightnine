from common import *
import re

def has(pattern,text):return bool(re.search(pattern,text,re.I))
def evaluate(name,out,source):
 entries=[*out.get('claims',[]),*out.get('counterarguments',[])]
 def located(c):
  q,l=c.get('quotation'),c.get('location',{});a,b=l.get('character_start'),l.get('character_end')
  return c.get('source_id')==source.source_id and not c.get('partial_token') and l.get('status')!='ambiguous' and isinstance(q,str) and type(a) is int and type(b) is int and 0<=a<b<=len(source.text) and source.text[a:b]==q
 valid=[c for c in entries if c.get('citations') and all(located(x) for x in c['citations'])]
 text=' '.join(c.get('text','') for c in valid); quotes=' '.join(' '.join(x['quotation'].split()) for c in valid for x in c['citations'])
 counter=' '.join(c.get('text','') for c in out.get('counterarguments',[]) if c in valid)
 cq=' '.join(' '.join(x['quotation'].split()) for c in out.get('counterarguments',[]) if c in valid for x in c['citations'])
 if name=='TRADE_FEB':
  signals={'four_values':all(has(r'(?<!\d)'+v+r'(?!\d)',t) for v in (r'68\.9',r'67\.6',r'263(?:\.0)?',r'331\.9') for t in (text,quotes)),
    'goods_services':has(r'goods deficit|商品.*逆差|货物.*逆差',text) and has(r'services surplus|服务.*顺差',text) and has(r'goods deficit',quotes) and has(r'services surplus',quotes),
    'counter_evidence':has(r'goods|商品|货物',counter) and has(r'goods deficit',cq)}
 else:
  signals={'actual_and_minimum':all(has(r'(?<!\d)'+v+r'(?!\d)',t) for v in (r'12\.4',r'9\.9') for t in (text,quotes)),
    'scenario_qualifier':has(r'severely adverse|严重不利|嚴重不利',text) and has(r'severely adverse',quotes),
    'individual_minimum':has(r'individual|individual banks|各.*银行|各.*銀行',text) and has(r'individual bank',quotes),
    'counter_evidence':has(r'commercial real estate|credit cards|mortgages|商业地产|商業地產|房地产|房地產',counter) and has(r'commercial real estate|credit cards|mortgages',cq)}
 return {'signals':signals,'all_claims_located':bool(entries) and len(valid)==len(entries),'claim_count':len(entries),
         'counter_count':len(out.get('counterarguments',[])),'semantic_support':'not independently established'}

def main():
    m=json.loads((HERE/'manifest.json').read_text());mh=digest((HERE/'manifest.json').read_bytes())
    assert json.loads((HERE/'run-started.json').read_text())['manifest_sha256']==mh
    assert all(digest((HERE/f).read_bytes())==h for f,h in m['files'].items())
    results={}
    for c in m['conditions']:
     name=c['task'];p=HERE/(name+'-result.json')
     if p.exists():
      out=json.loads(p.read_text());assert out['manifest_sha256']==mh
      content=out.get('content',{});claims=content.get('claims',[])
      shaped={'claims':[c for c in claims if c.get('role')!='counterargument'], 'counterarguments':[c for c in claims if c.get('role')=='counterargument']}
      _,sources=get_task(name);results[name]=evaluate(name,shaped,sources[0]) if out['status']=='completed' else {'status':out['status']}
      if out['status']=='completed':results[name]['run']={k:content.get('run',{}).get(k) for k in ('status','model_calls','total_tokens','support_review_status','support_review_omissions')}
     else:results[name]={'status':'not_run'}
    (HERE/'score.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(results,ensure_ascii=False,indent=2))

if __name__ == "__main__":
    main()
