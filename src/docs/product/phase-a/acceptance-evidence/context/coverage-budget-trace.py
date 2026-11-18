import json
from app.research_analysis import analyze,_structured_observation_windows
from app.research_context import evidence_units
from test_research_analysis import source

def run(name,question,text,limit,units):
 captured=[]
 result=analyze(question,[source('s',text=text)],None,lambda p:(captured.append(p) or {},0),max_prompt_chars=limit)
 high=[]
 analyze(question,[source('s',text=text)],None,lambda p:(high.append(p) or {},0),max_prompt_chars=5000)
 data=json.loads(high[0]); data['sources']=[]
 unit_rows=[]
 for group_id,parts in enumerate(units):
  for pos,(start,part) in enumerate(parts):
   item={'source_id':'s','source_coverage':'partial','text':part,'context_unit_ids':[f'u{parts[-1][0]}']}
   if not unit_rows:item['title']='Report'
   unit_rows.append(item)
 data['sources']=unit_rows
 # No page labels added for these text sources.
 cost=len(json.dumps(data,ensure_ascii=False,separators=(',',':')).encode())
 print(name, 'prompt_limit',limit,'minimal_group_json_bytes',cost,'required_source_bytes',sum(len(v['text'].encode()) for v in unit_rows),'fits',cost<=limit)
 print('candidate_ranges',[[[start,len(part)] for start,part in group] for group in units])
 if captured:
  d=json.loads(captured[0]);print('sent_prompt_bytes',len(captured[0].encode()),'selected',[(x['source_id'],x['text'][:100]) for x in d['sources']])
 else:print('no_model_call',result.limitations[:3])

x={'issuer':'Example Corp','concept':'Revenue','unit':'USD','notes':'unrelated '*180,'records':[{'start':'2021-07-01','end':'2022-06-30','val':100,'fy':2023},{'start':'2022-07-01','end':'2023-06-30','val':120,'fy':2023}]}
t=json.dumps(x,indent=2);q='Compare revenue for 2022 and 2023.'
w=_structured_observation_windows(t,q,8000)
run('structured',q,t,2055,[w])
t=('資料來源：星島研究院，星島研究院發布的財務報告。\n'+'其他事項說明沒有相關收入與支出的數字。'*60+'2024年倉儲物流收入為123億元，按年上升5%。'+'其他事項說明沒有相關收入與支出的數字。'*220+'2024年雲端支出為47億元，按年下降3%。'+'其他事項說明。'*300)
q='根據星島研究院2024年報告，倉儲物流收入及雲端支出分別是多少？'
candidates=evidence_units(t)
parts=[]
for needle in ('倉儲物流收入為123億元','雲端支出為47億元'):
 found=[unit for unit in candidates if unit.kind=='sentence' and any(needle in x for _,x in unit.parts)]
 parts.append(found[0].parts)
run('multi_requirement',q,t,2055,parts)
