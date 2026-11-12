"""Frozen narrow mechanical check; does not determine semantic support."""
import hashlib,json
from pathlib import Path
from matching import mentions_usd_millions
p=Path(__file__).resolve().parent
h=lambda b:hashlib.sha256(b).hexdigest()
m=json.loads((p/'manifest.json').read_bytes())
assert h(Path(__file__).read_bytes())==m['scorer_sha256']
assert h((p/'matching.py').read_bytes())==m['matching_sha256']
started=json.loads((p/'run-started.json').read_text())
assert started['manifest_sha256']==h((p/'manifest.json').read_bytes())
r=json.loads((p/'CISCO_REV-result.json').read_text())
source=json.loads((p/'CISCO_REV-input.json').read_text())['source_text']
checks=[]
for c in r.get('content',{}).get('claims',[]):
    if c.get('role')!='support': continue
    citations=c.get('citations',[]); texts=[];located=True
    for cite in citations:
        loc=cite.get('location',{});a,b=loc.get('character_start'),loc.get('character_end');q=cite.get('quotation')
        if type(a)!=int or type(b)!=int or not 0<=a<b<=len(source) or source[a:b]!=q or cite.get('partial_token'):
            located=False;continue
        texts.append(q)
    joined=' '.join(texts).lower(); t=c.get('text','')
    checks.append({'both_amounts_in_claim':mentions_usd_millions(t,'53803') and mentions_usd_millions(t,'56998'),
        'all_citations_located':bool(citations) and located,'cited_both_amounts':'53,803' in joined and '56,998' in joined,
        'cited_periods':'2024' in joined and '2023' in joined,'cited_unit':'million' in joined,
        'table_warning':bool(c.get('citation_context_warnings'))})
passed=any(all(c[k] for k in ('both_amounts_in_claim','all_citations_located','cited_both_amounts','cited_periods','cited_unit')) and not c['table_warning'] for c in checks)
out={'operation_status':r.get('operation_status'),'claims':checks,'narrow_mechanical_pass':passed,
     'semantic_support':'not established','independent_test':False}
(p/'score.json').write_text(json.dumps(out,indent=2)+'\n');print(json.dumps(out,indent=2))
