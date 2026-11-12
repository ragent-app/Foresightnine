from pathlib import Path
import json,re
from hashlib import sha256
HERE=Path(__file__).resolve().parent
m=json.loads((HERE/'manifest.json').read_text());mh=sha256((HERE/'manifest.json').read_bytes()).hexdigest()
assert json.loads((HERE/'run-started.json').read_text())['manifest_sha256']==mh
out=json.loads((HERE/'result.json').read_text());assert out['manifest_sha256']==mh
source=(HERE/'source.txt').read_text();c=out.get('content',{});claims=c.get('claims',[])
def valid(q):
 loc=q.get('location',{});a,b=loc.get('character_start'),loc.get('character_end')
 return type(a) is int and type(b) is int and 0<=a<b<=len(source) and source[a:b]==q.get('quotation') and not q.get('partial_token')
texts=' '.join(x.get('text','') for x in claims);quotes=' '.join(q.get('quotation','') for x in claims for q in x.get('citations',[]) if valid(q))
def both(pattern):return bool(re.search(pattern,texts,re.I|re.S) and re.search(pattern,quotes,re.I|re.S))
checks={'annual_value':both(r'\b2\.5\b'),'quarterly_value':both(r'\b3\.3\b'),
        'year':both(r'\b2023\b'),'quarter':both(r'fourth\s+quarter|q4'),
        'all_claims_located':bool(claims) and all(x.get('citations') and x.get('support_status')!='incomplete' and all(valid(q) for q in x['citations']) for x in claims),
        'no_known_warnings':not any(x.get(k) for x in claims for k in ('uncited_percentages','uncited_quarters','uncited_fact_values','causal_attribution_unverified','citation_context_warnings','publication_date_warnings','rate_unit_warnings')),
        'no_omissions':not any('omitted' in x for x in c.get('limitations',[]) if isinstance(x,str)),
        'one_call':c.get('run',{}).get('model_calls')==1 and type(c.get('run',{}).get('total_tokens')) is int and c['run']['total_tokens']<=10000,
        'official_fetch':c.get('run',{}).get('official_fetch_calls')==1 and c.get('run',{}).get('official_fetch_status')=='completed',
        'partial_disclosed':c.get('run',{}).get('status')=='completed' or (c.get('run',{}).get('status')=='partial' and bool(c.get('limitations')))}
result={'status':out.get('status'),'checks':checks,'mechanical_pass':out.get('status')=='completed' and all(checks.values()),
        'reported_tokens':c.get('run',{}).get('total_tokens'),'semantic_support':'not established by mechanical checks'}
(HERE/'score.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
