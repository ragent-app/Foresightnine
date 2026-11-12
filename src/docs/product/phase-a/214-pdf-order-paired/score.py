from common import *
import re
mb=(HERE/'manifest.json').read_bytes();m=json.loads(mb);mh=digest(mb)
assert all(digest((HERE/f).read_bytes())==v for f,v in m['files'].items())
assert all(digest((SOURCE/f).read_bytes())==v for f,v in m['source_files'].items())
assert json.loads((HERE/'run-started.json').read_text())['manifest_sha256']==mh
results=[]
for condition in m['conditions']:
 name=condition['task'];path=HERE/(name+'-result.json')
 if not path.exists():results.append({'task':name,'status':'not_run','mechanical_pass':False});continue
 out=json.loads(path.read_text());assert out['manifest_sha256']==mh
 task,sources=get_task(name);source=sources[0];content=out.get('content',{});claims=content.get('claims',[])
 def valid(c):
  loc=c.get('location',{});a,b=loc.get('character_start'),loc.get('character_end')
  return (c.get('source_id')==source.source_id and type(a) is int and type(b) is int and
          0<=a<b<=len(source.text) and source.text[a:b]==c.get('quotation') and not c.get('partial_token'))
 texts=' '.join(c.get('text','') for c in claims)
 quotes=' '.join(q.get('quotation','') for c in claims for q in c.get('citations',[]) if valid(q))
 def both(pattern):return bool(re.search(pattern,texts,re.I|re.S) and re.search(pattern,quotes,re.I|re.S))
 checks={key:both(pattern) for key,pattern in {'total':r'2\.9','goods':r'goods','services':r'services',
                                               'increased':r'increas|rose|rise','decreased':r'decreas|fell|declin'}.items()}
 checks['all_claims_unique_and_complete']=bool(claims) and all(c.get('citations') and c.get('support_status')!='incomplete' and all(valid(q) for q in c['citations']) for c in claims)
 checks['selected_source_only']=out.get('input_sources')==[task['source_id']]
 run=content.get('run',{});checks['bounded_call']=run.get('model_calls')==1 and type(run.get('total_tokens')) is int and run['total_tokens']<=10000
 results.append({'task':name,'status':out.get('status'),'research_status':run.get('status'),
                 'checks':checks,'mechanical_pass':out.get('status')=='completed' and all(checks.values()),
                 'reported_tokens':run.get('total_tokens'),'semantic_support':'not established by mechanical checks'})
result={'results':results,'total_reported_tokens':sum(x['reported_tokens'] for x in results if type(x.get('reported_tokens')) is int),
        'unknown_usage':any(type(x.get('reported_tokens')) is not int for x in results),
        'semantic_support':'requires separate assessment; matching numbers and terms is insufficient'}
(HERE/'score.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
