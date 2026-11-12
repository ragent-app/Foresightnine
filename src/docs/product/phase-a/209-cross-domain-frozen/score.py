from common import *
import re

mb=(HERE/'manifest.json').read_bytes(); m=json.loads(mb); mh=digest(mb)
assert all(digest((HERE/f).read_bytes())==v for f,v in m['files'].items())
assert all(digest((SOURCE/f).read_bytes())==v for f,v in m['source_files'].items())
assert json.loads((HERE/'run-started.json').read_text())['manifest_sha256']==mh
results=[]
for condition in m['conditions']:
    name=condition['task']; path=HERE/(name+'-result.json')
    if not path.exists():
        results.append({'task':name,'status':'not_run','mechanical_pass':False});continue
    out=json.loads(path.read_text()); assert out['manifest_sha256']==mh
    content=out.get('content',{}); claims=content.get('claims',[])
    _,sources=get_task(name);source=sources[0]
    def valid(c):
        loc=c.get('location',{}); a,b=loc.get('character_start'),loc.get('character_end')
        return (c.get('source_id')==source.source_id and type(a) is int and type(b) is int and
                0<=a<b<=len(source.text) and source.text[a:b]==c.get('quotation') and not c.get('partial_token'))
    texts=' '.join(c.get('text','') for c in claims)
    quotes=' '.join(q.get('quotation','') for c in claims for q in c.get('citations',[]) if valid(q))
    def both(pattern):return bool(re.search(pattern,texts,re.I|re.S) and re.search(pattern,quotes,re.I|re.S))
    if name=='BANK_2025':
        patterns={'bank':r'bank\s+of\s+america', 'metric':r'common\s+equity\s+tier\s+1|cet1',
                  'actual':r'\b11\.9\b','end':r'\b10\.7\b','minimum':r'\b10\.2\b',
                  'actual_label':r'actual','projected_label':r'project','minimum_label':r'minimum',
                  'scenario':r'severely\s+adverse'}
    elif name=='PCE_2021':
        patterns={'total':r'2\.9','services':r'services','goods':r'goods',
                  'increased':r'increas|rose|rise','decreased':r'decreas|fell|declin'}
    else:
        patterns={'range_low':r'0\.75|3\s*/\s*4','range_high':r'\b1(?:\.0+)?\b',
                  'dissenter':r'kashkari','preference':r'maintain','target':r'target\s+range'}
    checks={k:both(v) for k,v in patterns.items()}
    checks['all_claims_located']=bool(claims) and all(c.get('citations') and c.get('support_status')!='incomplete' and all(valid(q) for q in c['citations']) for c in claims)
    warnings=('uncited_amounts','uncited_claim_amounts','uncited_dollar_amounts','uncited_percentages','uncited_quarters','balance_direction_warnings','citation_context_warnings','uncited_basis_points','uncited_percentage_points','uncited_percentage_bounds','reused_percentages','uncited_fact_values')
    checks['no_known_claim_warnings']=not any(c.get(k) for c in claims for k in warnings)
    run=content.get('run',{}); checks['bounded_call']=run.get('model_calls')==1 and type(run.get('total_tokens')) is int and run['total_tokens']<=10000
    checks['partial_disclosed']=run.get('status')=='completed' or (run.get('status')=='partial' and bool(content.get('limitations')))
    results.append({'task':name,'status':out.get('status'),'research_status':run.get('status'),'mechanical_checks':checks,
                    'mechanical_pass':out.get('status')=='completed' and all(checks.values()),'reported_tokens':run.get('total_tokens'),
                    'semantic_support':'not established by mechanical checks'})
result={'results':results,'mechanical_pass':len(results)==3 and all(x['mechanical_pass'] for x in results),
        'total_reported_tokens':sum(x['reported_tokens'] for x in results if type(x.get('reported_tokens')) is int),
        'unknown_usage':any(type(x.get('reported_tokens')) is not int for x in results),
        'semantic_support':'requires separate assessment; matching numbers and terms is insufficient'}
(HERE/'score.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
