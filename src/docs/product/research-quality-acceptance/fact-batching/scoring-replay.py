#!/usr/bin/env python3
"""Offline, E1-only reconstruction; review semantic/negative items separately."""
import hashlib, json
from pathlib import Path
from decimal import Decimal
from collections import Counter
P = Path(__file__).resolve().parent.parent
cases = {c['id']: c for c in json.loads((P/'cases.json').read_text())['cases']}
index = {r['id']: r for r in json.loads((P/'e1-index.json').read_text())['runs']}
rows = []
for path in sorted((P/'e1-results').glob('*.json')):
    x = json.loads(path.read_text()); name = path.stem; case = cases[x['case']]; snap = case['snapshot']
    facts = {f['fact_id']: f for f in x['facts']}
    def fact(label, period, value):
        for f in facts.values():
            a, b = f.get('character_start'), f.get('character_end')
            if (label.lower() in f.get('label','').lower() and f.get('period') == period and Decimal(str(f['value'])) == Decimal(value)
                and f.get('source_id') == 'exhibit' and isinstance(a,int) and isinstance(b,int) and snap[a:b] == f.get('quotation')):
                return f['fact_id']
        return None
    def calc(formula, value, a, b):
        return any(c.get('formula_id') == formula and Decimal(str(c['value'])) == Decimal(value)
                   and [i.get('fact_id') for i in c.get('inputs',[])] == [a,b]
                   for c in x['calculations']) if a and b else False
    cid = x['case']; core = [False,False]
    if cid in ('D1','D2'):
        a,b = fact('revenue','FY2025','120'),fact('revenue','FY2024','100')
        core[0] = calc('growth_percent','20',a,b)
    if cid == 'D1':
        a,b = fact('diluted EPS','FY2025','3'),fact('diluted EPS','FY2024','2')
        core[1] = calc('difference','1',a,b)
    # D2 missing-EPS disclosure and D4 duplicate disclosure independently inspected
    # in report/claims/question_parts/gaps: none states the required specific absence.
    # D3/D4 have no facts or calculations at all; do not infer from source text.
    if cid in ('D3','D4'):
        assert not x['facts'] and not x['calculations'], name
    if cid in ('D2','D4'):
        visible = json.dumps({k:x[k] for k in ('report','claims','question_parts','gaps')}, ensure_ascii=False).lower()
        # Conservative assertions trigger re-review if this fixture changes.
        if cid == 'D2': assert not any(s in visible for s in ('prior eps','prior diluted eps','上年稀释','2024年稀释'))
        else: assert not any(s in visible for s in ('duplicate','重复','same instrument','同一标的'))
    models = [e for e in x['events'] if e['kind'] in ('model','model_failure')]
    trunc = [e for e in models if e.get('code') == 'provider_output_truncated']
    assert len(models) == x['calls']
    assert all(e.get('external_state') == 'returned' and e.get('settlement') == 'settled' for e in models)
    assert sum(e.get('usage',0) for e in models) == x['budget_accounted_tokens']
    assert not any(t.get('tool') == 'submit_research' for t in x['tool_io'])
    ref = index[name]
    row = {'id':name,'case':cid,'arm':x['arm'],'repeat':int(name.rsplit('-',1)[1]),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
           'formal_submission':'none', 'status':x['status'], 'core':core, 'core_total':2,
           'specific_negative_disclosure': False if cid in ('D2','D4') else None,
           'stop_reason':x['reason'], 'truncated_settled_returned':bool(trunc), 'calls':x['calls'],
           'reported_tokens':sum(e['usage'] for e in models), 'budget_accounted_tokens':x['budget_accounted_tokens'],
           'seconds':x['elapsed_seconds'], 'e1_index_core_match':sum(core)==ref['core_answered'],
           'e1_index_tokens_match':x['budget_accounted_tokens']==ref['budget_accounted_tokens']}
    rows.append(row)
assert len(rows)==16 and set(r['id'] for r in rows)==set(index)
assert all(r['e1_index_core_match'] and r['e1_index_tokens_match'] for r in rows)
pairs = []
for case in ('D1','D2','D3','D4'):
    for rep in (1,2):
        a,b = [next(r for r in rows if r['case']==case and r['arm']==arm and r['repeat']==rep) for arm in ('A','B')]
        pairs.append({'case':case,'repeat':rep,'A_core':sum(a['core']),'B_core':sum(b['core']), 'B_minus_A':sum(b['core'])-sum(a['core'])})
out={'scope':'Historical E1 offline reconstruction; not new experiment results', 'case_sha256':hashlib.sha256((P/'cases.json').read_bytes()).hexdigest(),
     'rows':rows, 'pairs':pairs, 'aggregate':{arm:{'runs':sum(r['arm']==arm for r in rows),'formal_submissions':0,'core_answered':sum(sum(r['core']) for r in rows if r['arm']==arm),'core_total':sum(r['core_total'] for r in rows if r['arm']==arm)} for arm in ('A','B')},
     'limitations':['E1 synthetic numeric and location checks only; semantic support not independently reviewed','D3/D4 relative-day text is not structured market receipt','negative disclosures independently inspected in visible checkpoints; this replay is not a generic natural-language evaluator']}
(P/'fact-batching'/'scoring-e1-replay.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'aggregate':out['aggregate'],'pairs':pairs},ensure_ascii=False,indent=2))
