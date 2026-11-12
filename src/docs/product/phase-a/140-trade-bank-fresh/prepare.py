from common import *
import subprocess
from uuid import uuid4
from app.research_verification import REVIEW_METHOD,REVIEW_SYSTEM_PROMPT
prior=json.loads((HERE.parent/'136-blank-line-paired/manifest.json').read_text())
tasks=[{'id':'TRADE_FEB','question':'According to this release, what were the February 2024 goods and services deficit, exports and imports and the revised January deficit? Explain how changes in the goods deficit and services surplus contributed, including an offsetting fact.',
 'source':{'kind':'text','title':'BEA U.S. international trade in goods and services, February 2024','text_file':'bea-trade-source.txt','source_id':'source_'+uuid4().hex}},
 {'id':'STRESS_2023','question':'According to the 2023 Federal Reserve stress test, what happened to the aggregate CET1 ratio under the severely adverse scenario, and did individual banks remain above minimums? Give a material counterpoint or risk, and distinguish actual and projected values.',
 'source':{'kind':'pdf','title':'Federal Reserve 2023 stress test results','text_file':'fed-stress-source.txt','source_id':'source_'+uuid4().hex}}]
conditions=[]
for task in tasks:
 name=task['id'];(HERE/(name+'-input.json')).write_text(json.dumps(task,ensure_ascii=False,indent=2)+'\n')
 loaded,objects=get_task(name);prompts=[];result=run_analysis(loaded,objects,lambda p:(prompts.append(p) or {},0))
 assert len(prompts)==1
 conditions.append({'task':name,'prompt_sha256':digest(prompts[0].encode()),'prompt_bytes':len(prompts[0].encode()),
  'segments':result.segments,'source_hashes':{s.source_id:s.content_sha256 for s in objects}})
files=['extract.py','common.py','prepare.py','run.py','score.py','validate_score.py','bea-trade.html','bea-trade-source.txt','fed-stress.pdf','fed-stress-source.txt','TRADE_FEB-input.json','STRESS_2023-input.json']
m={'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
 'model':prior['model'],'generation_parameters':prior['generation_parameters'],'system_sha256':digest(SYSTEM_PROMPT.encode()),
 'review_method':REVIEW_METHOD,'review_system_sha256':digest(REVIEW_SYSTEM_PROMPT.encode()),
 'files':{f:digest((HERE/f).read_bytes()) for f in files},'conditions':conditions,
 'input_limit':3000,'max_calls':2,'per_call_max_total_tokens':10000,'max_total_tokens':20000,
 'timeout_seconds':60,'search_calls':0,'retries':0,'cost_usd':'unknown','previous_known_tokens':148116}
(HERE/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
