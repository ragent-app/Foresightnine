"""Deterministic checks fixed before the holdout output was inspected.

Numerical checks are not a judgment of natural-language entailment or of
historical source availability. Reference answers are never sent to the model.
"""
from __future__ import annotations
from decimal import Decimal,InvalidOperation
from hashlib import sha256
import json
from pathlib import Path

folder=Path(__file__).resolve().parent
manifest_bytes=(folder/'manifest.json').read_bytes(); manifest=json.loads(manifest_bytes)
started=json.loads((folder/'run-started.json').read_text())
assert sha256(manifest_bytes).hexdigest()==started['manifest_sha256']
reference_bytes=(folder/'references.json').read_bytes()
assert sha256(reference_bytes).hexdigest()==manifest['reference_sha256']
refs=json.loads(reference_bytes)

def number(value):
    try:
        result=Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError): return None

def unit_ok(fact):
    unit=str(fact.get('unit','')).casefold();currency=str(fact.get('currency') or '').casefold()
    return ('million' in unit and ('usd' in unit or currency=='usd'))

def location_ok(fact,source):
    loc=fact.get('location'); quote=fact.get('quotation')
    if not isinstance(loc,dict) or not isinstance(quote,str): return False
    start,end=loc.get('character_start'),loc.get('character_end')
    return type(start) is int and type(end) is int and 0<=start<end<=len(source) and source[start:end]==quote

def expected_period(id,value):
    return '2023' if id=='H01' and value=='611289' else '2024'

report={'manifest_sha256':started['manifest_sha256'],'semantic_support':'unverifiable','historical_availability':'unverified',
        'sample':'known, unblinded and correlated; no population quality inference','tasks':{}}
for entry in manifest['inputs']:
    id=entry['id'];b=(folder/(id+'-input.json')).read_bytes();assert sha256(b).hexdigest()==entry['sha256']
    source=json.loads(b)['source_text'];path=folder/(id+'-result.json')
    if not path.exists():
        report['tasks'][id]={'status':'missing','mechanical_pass':False};continue
    result=json.loads(path.read_text());assert result['manifest_sha256']==report['manifest_sha256']
    content=result.get('content') or {};run=content.get('run') or {};facts=content.get('facts') or [];calculations=content.get('calculations') or []
    details={}
    for value in refs[id]['facts']:
        candidates=[f for f in facts if number(f.get('value'))==number(value)]
        details[value]={'present':bool(candidates),'unit_period_and_source':any(unit_ok(f) and expected_period(id,value) in str(f.get('period','')) and location_ok(f,source) for f in candidates)}
    calc_checks=[]
    for expected in refs[id].get('calculations',[]):
        def matches(actual):
            inputs=actual.get('inputs') or []
            return (actual.get('formula_id')==expected['formula_id'] and number(actual.get('value'))==number(expected['value']) and
                actual.get('unit')==expected['unit'] and len(inputs)==2 and
                [number(x.get('value')) for x in inputs]==[number(x) for x in expected['input_values']])
        calc_checks.append({'formula':expected['formula_id'],'expected_value':expected['value'],'matched':any(matches(a) for a in calculations)})
    budget_ok=(result.get('operation_status')=='completed' and run.get('model_calls')==1 and run.get('search_calls')==0 and
               run.get('total_tokens') is not None and run['total_tokens']<=manifest['max_tokens_per_task'])
    passed=budget_ok and all(v['present'] and v['unit_period_and_source'] for v in details.values()) and all(x['matched'] for x in calc_checks)
    report['tasks'][id]={'operation_status':result.get('operation_status'),'research_status':run.get('status'),'reported_tokens':run.get('total_tokens'),
                         'budget_ok':budget_ok,'facts':details,'calculations':calc_checks,'mechanical_pass':passed,
                         'semantic_support':'unverifiable','cost_usd':'unknown'}
report['mechanical_tasks_passed']=sum(t.get('mechanical_pass',False) for t in report['tasks'].values())
report['mechanical_task_count']=len(manifest['inputs'])
(folder/'score.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))
