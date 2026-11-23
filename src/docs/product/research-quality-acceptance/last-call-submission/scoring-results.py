#!/usr/bin/env python3
"""Rebuild manually adjudicated scorecard and check preserved evidence; no model calls."""
import hashlib,json
from decimal import Decimal
from pathlib import Path
P=Path(__file__).resolve().parent
cases={c['id']:c for c in json.loads((P.parent/'cases.json').read_text())['cases']}
hashes=json.loads((P/'results-sha256.json').read_text())
for name,h in hashes.items(): assert hashlib.sha256((P/name).read_bytes()).hexdigest()==h,name
# Human semantic judgments, after batch stop, not a general classifier.
J={'D1-A-1':(1,0),'D1-A-2':(1,1),'D1-B-1':(0,0),'D1-B-2':(0,0),
'D2-A-1':(1,0),'D2-A-2':(0,0),'D2-B-1':(0,0),'D2-B-2':(0,0),
'D3-A-1':(0,0),'D3-A-2':(0,0),'D3-B-1':(0,0),'D3-B-2':(0,0),
'D4-A-1':(0,0),'D4-B-1':(0,0)}
assert len(hashes)==28
rows=[]
for n,j in J.items():
 p=P/'results'/f'{n}.json'; x=json.loads(p.read_text()); case=x['case']; snap=cases[case]['snapshot']; assert (P/'results'/f'{n}.started').exists()
 assert x['arm']==n[3] and case==n[:2]
 assert x['status']=='partial' and x['submission_acceptance']=='not_observed' and x['state_submission'] is None and not x['submit_research_calls'] and not x['submit_handler_receipts'] and not x['submit_research_observed'] and not x['report']
 assert not any(t.get('tool')=='submit_research' for t in x['tool_io'])
 facts=x['facts']; calcs=x['calculations']; fm={f['fact_id']:f for f in facts}
 for f in facts:
  assert f['source_id']=='exhibit' and snap[f['character_start']:f['character_end']]==f['quotation']
 for c in calcs:
  v=[Decimal(str(i['value'])) for i in c['inputs']]
  assert all(i['fact_id'] in fm and Decimal(str(fm[i['fact_id']]['value']))==Decimal(str(i['value'])) for i in c['inputs'])
  if c['formula_id']=='growth_percent': assert abs((v[0]/v[1]-1)*100-Decimal(str(c['value'])))<Decimal('.00001')
  if c['formula_id'] in ('subtract','difference'): assert v[0]-v[1]==Decimal(str(c['value']))
 if j[0]:
  target=[c for c in calcs if c['formula_id']=='growth_percent' and Decimal(c['value'])==20 and [Decimal(str(i['value'])) for i in c['inputs']]==[120,100]];assert target
  ff=[fm[i['fact_id']] for i in target[0]['inputs']]; assert [f['period'] for f in ff]==['FY2025','FY2024'] and all('revenue' in f['label'].lower() for f in ff)
 if j[1]:
  target=[c for c in calcs if c['formula_id'] in ('subtract','difference') and Decimal(c['value'])==1 and [Decimal(str(i['value'])) for i in c['inputs']]==[3,2]];assert target
  ff=[fm[i['fact_id']] for i in target[0]['inputs']];assert [f['period'] for f in ff]==['FY2025','FY2024'] and all('eps' in f['label'].lower() for f in ff)
 assert not x['claims']
 events=[e for e in x['events'] if e['kind'] in ('model','model_failure')]
 assert len(events)==x['calls'] and x['search_calls']==0
 reason=['有绑定两期收入的20%计算' if j[0] else '无符合条件的目标计算及证据', '有绑定两期EPS的1美元/股差额计算' if j[1] else ('仅50%增幅，无已保存差额计算或明确差额' if n=='D1-A-1' else '未保存符合该核心项的计算或具体缺口披露')]
 rows.append({'id':n,'case':case,'arm':x['arm'],'repeat':int(n[-1]),'raw_path':str(p.relative_to(P)),'raw_sha256':hashes[str(p.relative_to(P))],
 'submission':'passive_partial','submission_evidence':{'status':x['status'],'reason':x['reason'],'observed':x['submit_research_observed'],'attempts':x['submit_research_calls'],'receipts':x['submit_handler_receipts'],'state_submission':x['state_submission'],'acceptance':x['submission_acceptance'],'report':x['report']},
 'core':[{'number':i+1,'criterion':cases[case]['core'][i],'checkpoint':'supported' if val else 'unsupported','formal_presentation':'unsupported','reason':reason[i]} for i,val in enumerate(j)],
 'evidence':{'source':x['source'],'facts':facts,'calculations':calcs,'claims':x['claims'],'question_parts':x['question_parts'],'gaps':x['gaps']},
 'negative_disclosure':'not_specific' if case in ('D2','D4') else None,'invented_missing_value':'not_observed_in_checkpoint' if case in ('D2','D4') else None,
 'last_call_candidate_reached':any(b['eligible_last_call'] for b in x['build_observations']),'prompt_exposure':x['prompt_exposure'],
 'events':[{'kind':e['kind'],'code':e.get('code'),'external_state':e.get('external_state'),'settlement':e.get('settlement'),'usage':e.get('usage'),'cache_telemetry':e.get('cache_telemetry')} for e in events],
 'calls':x['calls'],'reported_tokens':x['usage']['reported_tokens'],'unknown_usage_calls':x['usage']['unknown_usage_calls'],'budget_accounted_tokens':x['budget_accounted_tokens'],'seconds':x['elapsed_seconds'],'cost':x['actual_cost']})
pairs=[]
for c in ('D1','D2','D3','D4'):
 for rep in (1,2):
  rs=[next((r for r in rows if r['id']==f'{c}-{a}-{rep}'),None) for a in 'AB']
  if None in rs:continue
  a,b=rs; score=lambda r:sum(i['checkpoint']=='supported' for i in r['core'])
  pairs.append({'case':c,'repeat':rep,'A_checkpoint':score(a),'B_checkpoint':score(b),'B_minus_A_checkpoint':score(b)-score(a),'A_formal':0,'B_formal':0,'B_minus_A_formal':0,'B_minus_A_formal_presented_items':0,'B_minus_A_calls':b['calls']-a['calls'],'B_minus_A_reported_tokens':None if a['unknown_usage_calls'] or b['unknown_usage_calls'] else b['reported_tokens']-a['reported_tokens'],'B_minus_A_budget_accounted_tokens':b['budget_accounted_tokens']-a['budget_accounted_tokens'],'B_minus_A_seconds':round(b['seconds']-a['seconds'],3)})
def agg(arm):
 rr=[r for r in rows if r['arm']==arm]; return {'started':len(rr),'planned':8,'accepted':0,'passive_partial':len(rr),'formal_unknown':0,'checkpoint_supported':sum(i['checkpoint']=='supported' for r in rr for i in r['core']),'checkpoint_unknown':0,'core_total':2*len(rr),'formal_presented':0,'calls':sum(r['calls'] for r in rr),'reported_tokens':sum(r['reported_tokens'] for r in rr),'unknown_usage_calls':sum(r['unknown_usage_calls'] for r in rr),'budget_accounted_tokens':sum(r['budget_accounted_tokens'] for r in rr),'seconds':round(sum(r['seconds'] for r in rr),3)}
out={'method':'Non-blinded manual semantic review; offline evidence and arithmetic checks; no independent human semantic review','planned_runs':16,'started_runs':14,'not_started':['D4-A-2','D4-B-2'],'rows':rows,'pairs':pairs,'aggregate':{a:agg(a) for a in 'AB'}}
(P/'scoring-results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'aggregate':out['aggregate'],'pairs':pairs},ensure_ascii=False,indent=2))
