"""Freeze data and checks before any model calls."""
from pathlib import Path
from hashlib import sha256
from uuid import uuid4
import json, subprocess, sys
p=Path(__file__).resolve().parent; root=p.parents[3]; sys.path.insert(0,str(root/'apps/api'))
from app.research_uploads import _extract_text
from app.research_analysis import SYSTEM_PROMPT
from app.research_verification import REVIEW_SYSTEM_PROMPT, REVIEW_METHOD
h=lambda b:sha256(b).hexdigest()
tasks=[
 {'id':'PCE_PRICES','question':'根据所附 BEA 2023 年 2 月公告，PCE 总物价指数的环比和同比变化分别是多少？解释比较期间，并给出一项不能由该数据单独推断的限制。', 'sources':[{'kind':'pdf','file':'bea-feb-2023.pdf','title':'BEA Personal Income and Outlays February 2023','published_at':'2023-03-31T12:30:00Z'}]},
 {'id':'FOMC_DISSENT','question':'根据所附 2022 年 3 月美联储声明，目标利率区间是多少？谁投了反对票，其建议的加息幅度和目标区间是什么？引用原文并说明局限。', 'sources':[{'kind':'pdf','file':'fed-mar-2022.pdf','title':'FOMC statement March 16 2022','published_at':'2022-03-16T18:00:00Z'}]}]
for t in tasks:
 for s in t['sources']:
  s['source_id']='source_'+uuid4().hex; data=(p/s['file']).read_bytes(); text=_extract_text(data,text_order='reading')
  s['sha256']=h(data);s['text_sha256']=h(text.encode());(p/(t['id']+'-source.txt')).write_text(text)
 (p/(t['id']+'-input.json')).write_text(json.dumps(t,ensure_ascii=False,indent=2)+'\n')
refs={'PCE_PRICES':{'monthly':'0.3','annual':'5.0','period':'February 2023'},'FOMC_DISSENT':{'target':['1/4','1/2'],'dissenter':'Bullard','proposal':['0.5','1/2','3/4']}}
(p/'references.json').write_text(json.dumps(refs,ensure_ascii=False,indent=2)+'\n')
old=json.loads((p.parent/'71-passage-paired-check'/'manifest.json').read_text())
m={'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(), 'assessment':'Fresh-to-this-project official PDFs; task and references known to preparer, not blind or independently reviewed. Current copies do not prove historical byte identity. Not phase acceptance.', 'model':old['model'],'generation_parameters':old['generation_parameters'],'provider_input_token_limit':3000,'provider_timeout_seconds':60,
 'tasks':[{'id':t['id'],'input_sha256':h((p/(t['id']+'-input.json')).read_bytes())} for t in tasks], 'script_sha256':{n:h((p/n).read_bytes()) for n in ('prepare.py','run.py','score.py')},'references_sha256':h((p/'references.json').read_bytes()),'research_system_prompt_sha256':h(SYSTEM_PROMPT.encode()),'review_system_prompt_sha256':h(REVIEW_SYSTEM_PROMPT.encode()),'review_method':REVIEW_METHOD,
 'max_calls_per_task':2,'max_calls_total':4,'max_tokens_per_task':10000,'max_tokens_total':20000,'max_elapsed_seconds_per_task':120,'search_calls':0,'retry_external_calls':0,'cost_usd':'unknown', 'pass_rule':'Both tasks must satisfy all fixed checks in score.py. Mechanical checks do not prove semantic support or full phase B acceptance.'}
(p/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
