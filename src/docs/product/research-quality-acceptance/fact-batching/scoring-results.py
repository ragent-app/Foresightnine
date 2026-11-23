#!/usr/bin/env python3
"""Reproduce manually reviewed fact-batching scorecard; no external calls.
Judgments are fixed annotations checked against evidence, not NLP classification.
"""
import hashlib,json
from pathlib import Path
from decimal import Decimal
P=Path(__file__).resolve().parent
cases={c['id']:c for c in json.loads((P.parent/'cases.json').read_text())['cases']}
# Checked from each checkpoint; first is revenue/target return, second EPS or benchmark.
J={
'D1-A-1':('supported','supported'),'D1-A-2':('unsupported','unsupported'),
'D1-B-1':('unsupported','unsupported'),'D1-B-2':('supported','supported'),
'D2-A-1':('supported','supported'),'D2-A-2':('supported','unsupported'),
'D2-B-1':('supported','unsupported'),'D2-B-2':('unsupported','unsupported'),
'D3-A-1':('supported','unsupported'),'D3-A-2':('unsupported','unsupported'),
'D3-B-1':('supported','unsupported'),'D3-B-2':('supported','unsupported'),
'D4-A-1':('unsupported','unsupported'),'D4-A-2':('unsupported','unsupported'),
'D4-B-1':('supported','unsupported'),'D4-B-2':('supported','unsupported')}
rows=[]
for n,judgments in J.items():
 p=P/'results'/f'{n}.json'; x=json.loads(p.read_text()); cid=x['case']; snap=cases[cid]['snapshot']
 assert (P/'results'/f'{n}.started').exists() and x['arm']==n.split('-')[1] and cid==n[:2]
 assert x['status']=='partial' and x['submission_acceptance']=='not_observed' and not x['submit_research_calls']
 assert not any(t.get('tool')=='submit_research' for t in x['tool_io']) and not x['report']
 events=[e for e in x['events'] if e['kind'] in ('model','model_failure')]
 assert len(events)==x['calls'] and all(e.get('external_state')=='returned' and e.get('settlement')=='settled' for e in events)
 assert sum(e['usage'] for e in events)==x['usage']['reported_tokens']==x['budget_accounted_tokens'] and x['search_calls']==0
 facts=x['facts']; calcs=x['calculations']; factmap={f['fact_id']:f for f in facts}
 for f in facts:
  a,b=f['character_start'],f['character_end']; assert f['source_id']=='exhibit' and snap[a:b]==f['quotation']
 for c in calcs:
  assert all(i.get('fact_id') in factmap for i in c['inputs'])
  for i in c['inputs']: assert Decimal(str(i['value']))==Decimal(str(factmap[i['fact_id']]['value']))
  v=[Decimal(str(i['value'])) for i in c['inputs']]
  if c['formula_id']=='growth_percent': assert abs((v[0]/v[1]-1)*100-Decimal(str(c['value'])))<Decimal('.00001')
  if c['formula_id'] in ('subtract','difference'): assert v[0]-v[1]==Decimal(str(c['value']))
 if judgments[0]=='supported':
  expected = ('120','100') if cid in ('D1','D2') else ('12','10')
  chosen = [c for c in calcs if c['formula_id']=='growth_percent' and Decimal(c['value'])==20 and tuple(str(i['value']).rstrip('0').rstrip('.') if '.' in str(i['value']) else str(i['value']) for i in c['inputs'])==expected]
  assert chosen, n
  chosen_facts=[factmap[i['fact_id']] for i in chosen[0]['inputs']]
  if cid in ('D1','D2'):
   assert [f['period'] for f in chosen_facts]==['FY2025','FY2024'] and all('revenue' in f['label'].lower() for f in chosen_facts)
  else:
   assert all(('gamma' if cid=='D3' else 'delta') in f['label'].lower() for f in chosen_facts)
   assert 'day 2' in chosen_facts[0]['period'] and 'day 1' in chosen_facts[1]['period']
 if cid=='D1' and judgments[1]=='supported': assert any(c['formula_id'] in ('subtract','difference') and Decimal(c['value'])==1 for c in calcs)
 if n=='D2-A-1':
  assert len(x['claims'])==2 and 'FY2024 diluted EPS baseline' in x['claims'][1]['text'] and 'cannot be quantified' in x['claims'][1]['text']
  for c in x['claims'][1]['citations']:
   loc=c['location']; assert snap[loc['character_start']:loc['character_end']]==c['quotation']
 elif cid in ('D2','D4'): assert not x['claims']
 if cid in ('D3','D4') and judgments[0]=='supported':
  assert {Decimal(f['value']) for f in facts}.issuperset({Decimal(10),Decimal(12)})
 # D3's 10% benchmark calculation exists in 3 runs, but no 10 percentage-point excess saved.
 if cid=='D3': assert not any(c['formula_id'] in ('subtract','difference') for c in calcs)
 model_events=[{'kind':e['kind'],'code':e.get('code'),'external_state':e.get('external_state'),'settlement':e.get('settlement'),'usage':e.get('usage')} for e in events]
 notes=[]
 if cid=='D1': notes=['EPS absolute change saved in calculation; question_parts display 50%, not +1' if judgments[1]=='supported' else 'no saved eligible facts or calculations']
 if cid=='D2': notes=['Specific absence supported by saved claim with cited prior-EPS sentence; report empty' if n=='D2-A-1' else 'No specific prior EPS absence disclosed in checkpoint; no prior EPS invented']
 if cid=='D3': notes=['Relative-day synthetic source, not a structured market receipt; actual shared trading dates unverified; no saved excess difference']
 if cid=='D4': notes=['Relative-day synthetic source, not a structured market receipt; no specific duplicate-as-benchmark explanation or invented benchmark']
 rows.append({'id':n,'raw_path':str(p.relative_to(P)),'raw_sha256':hashlib.sha256(p.read_bytes()).hexdigest(), 'case':cid,'arm':x['arm'],'repeat':int(n[-1]),
 'formal_submission':'passive_partial','submission_evidence':{'observed':x['submit_research_observed'],'calls':x['submit_research_calls'],'acceptance':x['submission_acceptance'],'report_empty':not x['report']},
 'core':[{'number':i+1,'judgment':j,'criterion':cases[cid]['core'][i]} for i,j in enumerate(judgments)],
 'evidence':{'source_snapshot_sha256':x['source']['content_sha256'],'facts':facts,'calculations':calcs,'claims':x['claims'],'question_parts':x['question_parts'],'gaps':x['gaps']},
 'negative_disclosure':('specific_saved_claim_not_formal_delivery' if n=='D2-A-1' else 'not_specific') if cid in ('D2','D4') else None,
 'invented_missing_value':'not_observed_in_checkpoint' if cid in ('D2','D4') else None,
 'stop_reason':x['reason'],'model_events':model_events,'calls':x['calls'],'reported_tokens':x['usage']['reported_tokens'],'budget_accounted_tokens':x['budget_accounted_tokens'],'seconds':x['elapsed_seconds'], 'actual_cost':x['actual_cost'], 'notes':notes})
assert len(rows)==16
pairs=[]
for c in ('D1','D2','D3','D4'):
 for rep in (1,2):
  a,b=[next(r for r in rows if r['id']==f'{c}-{arm}-{rep}') for arm in ('A','B')]
  score=lambda r:sum(i['judgment']=='supported' for i in r['core'])
  pairs.append({'case':c,'repeat':rep,'A_core':score(a),'B_core':score(b),'B_minus_A_core':score(b)-score(a),'A_formal':0,'B_formal':0,'B_minus_A_formal':0,'B_minus_A_calls':b['calls']-a['calls'],'B_minus_A_reported_tokens':b['reported_tokens']-a['reported_tokens'],'B_minus_A_seconds':round(b['seconds']-a['seconds'],3)})
def aggregate(arm):
 rr=[r for r in rows if r['arm']==arm]
 return {'runs':len(rr),'formal_completed':0,'formal_partial':0,'passive_partial':len(rr),'formal_rate':'0/8','core_supported':sum(i['judgment']=='supported' for r in rr for i in r['core']),'core_unknown':sum(i['judgment']=='unknown' for r in rr for i in r['core']),'core_total':len(rr)*2,'calls':sum(r['calls'] for r in rr),'reported_tokens':sum(r['reported_tokens'] for r in rr),'budget_accounted_tokens':sum(r['budget_accounted_tokens'] for r in rr),'seconds':round(sum(r['seconds'] for r in rr),3)}
out={'version':1,'assessment':'Non-blinded manual judgments plus offline evidence and arithmetic assertions; no independent human semantic review','rows':rows,'pairs':pairs,'aggregate':{a:aggregate(a) for a in ('A','B')}}
(P/'scoring-results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'aggregate':out['aggregate'],'pairs':pairs},ensure_ascii=False,indent=2))
