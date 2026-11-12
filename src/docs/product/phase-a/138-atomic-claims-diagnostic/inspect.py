from common import *
import re

def inspect(result):
 entries=[*result.get('claims',[]),*result.get('counterarguments',[])]
 norms=lambda s:' '.join(s.casefold().split())
 patterns={'labor':r'labor market conditions|labour market conditions|就业市场|勞動市場|劳动市场',
           'health':r'public health|公共卫生|公共衛生',
           'inflation':r'inflation pressures|通胀压力|通脹壓力',
           'international':r'international developments|国际.*发展|國際.*發展'}
 per=[]
 for i,item in enumerate(entries):
  text=norms(item.get('text',''));quotes=norms(' '.join(c.get('quotation','') for c in item.get('citations',[])))
  named=[name for name,pattern in patterns.items() if re.search(pattern,text)]
  if named:
   per.append({'claim_index':i,'mentioned':named,
               'missing_exact_terms_from_quotations':[name for name in named if not re.search(patterns[name],quotes)]})
 return {'monitoring_claims':per,'mechanical_only':True,'semantic_support':'not independently established'}

m=json.loads((HERE/'manifest.json').read_text());started=json.loads((HERE/'run-started.json').read_text())
assert started['manifest_sha256']==digest((HERE/'manifest.json').read_bytes())
assert all(digest((HERE/n).read_bytes())==v for n,v in m['files'].items())
prior=(HERE.parent/'136-blank-line-paired/FED_PURCHASES-6000-result.json').read_bytes()
assert digest(prior)==m['prior_result_sha256']
current=json.loads((HERE/'FED_PURCHASES-6000-result.json').read_text()); assert current['status']=='completed'
result={'baseline':inspect(json.loads(prior)),'variant':inspect(current)}
(HERE/'comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
