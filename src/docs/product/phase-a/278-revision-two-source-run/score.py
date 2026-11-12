from common import *
from decimal import Decimal,InvalidOperation

mbytes=(HERE/'manifest.json').read_bytes();m=json.loads(mbytes);mh=digest(mbytes)
assert json.loads((HERE/'run-started.json').read_text())['manifest_sha256']==mh
assert all(digest((HERE/name).read_bytes())==value for name,value in m['files'].items())
_,sources,_=inputs();out=json.loads((HERE/'result.json').read_text());assert out['manifest_sha256']==mh
source_map={source.source_id:source.text for source in sources}
c=out.get('content',{});facts=c.get('facts',[]);claims=c.get('claims',[]);run=c.get('run',{})
def located(entry):
    text=source_map.get(entry.get('source_id'));loc=entry.get('location');q=entry.get('quotation')
    if text is None or not isinstance(loc,dict) or not isinstance(q,str) or loc.get('status')=='ambiguous':return False
    a,b=loc.get('character_start'),loc.get('character_end')
    return type(a) is int and type(b) is int and 0<=a<b<=len(text) and text[a:b]==q
def fact_match(f,sid,value):
    try: num=Decimal(str(f.get('value')))
    except (InvalidOperation,ValueError):return False
    period=(str(f.get('period') or '')+' '+str(f.get('label') or '')).lower()
    unit=str(f.get('unit') or '').lower();currency=str(f.get('currency') or '').lower()
    return (f.get('source_id')==sid and num==Decimal(value) and located(f) and value in f['quotation'] and
            '2023' in period and any(x in period for x in ('aug','08','8月')) and
            ('billion' in unit or '十亿' in unit) and (currency in ('usd','美元') or 'dollar' in unit or 'usd' in unit or '美元' in unit))
old,new=out['source_ids']
facts_found=[{'value':value,'source':name,'found':any(fact_match(f,sid,value) for f in facts)}
             for value,name,sid in [('94.0','earlier',old),('105.1','later',new)]]
def revision_claim(claim):
    text=str(claim.get('text') or '');citations=claim.get('citations') or []
    source_ids={q.get('source_id') for q in citations if isinstance(q,dict) and located(q)}
    return ('94.0' in text and '105.1' in text and any(word in text.lower() for word in ('修订','修正','revis','updated')) and {old,new}<=source_ids)
budget=(out.get('operation_status')=='completed' and type(run.get('model_calls')) is int and run['model_calls']<=2 and
        run.get('search_calls')==0 and type(run.get('total_tokens')) is int and run['total_tokens']<=20000 and
        isinstance(run.get('elapsed_seconds'),(int,float)) and run['elapsed_seconds']<=120)
both=any(revision_claim(claim) for claim in claims)
result={'manifest_sha256':mh,'operation_status':out.get('operation_status'),'research_status':run.get('status'),
        'facts':facts_found,'one_revision_claim_cites_both_sources':both,'budget_ok':budget,
        'counterargument_cited':any(x.get('role')=='counterargument' and any(located(q) for q in x.get('citations',[])) for x in claims),
        'review_status':run.get('support_review_status'),'reported_tokens':run.get('total_tokens'),
        'mechanical_pass':bool(all(item['found'] for item in facts_found) and both and budget),
        'semantic_support':'unverified','historical_byte_identity':'unverified','cost_usd':'unknown'}
(HERE/'score.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
