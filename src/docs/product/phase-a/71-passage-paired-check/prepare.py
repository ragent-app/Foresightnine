"""Freeze three nonblind task inputs and source snapshots before model calls."""
from pathlib import Path
from hashlib import sha256
from uuid import uuid4
import json,subprocess,sys
p=Path(__file__).resolve().parent;root=p.parents[3];sys.path.insert(0,str(root/'apps/api'))
from app.research_uploads import _extract_text
from app.research_analysis import SYSTEM_PROMPT
from app.research_verification import REVIEW_SYSTEM_PROMPT,REVIEW_METHOD
h=lambda b:sha256(b).hexdigest()
a=json.loads((root/'docs/product/phase-a/15-heldout-walmart/H01-input.json').read_text())
b=json.loads((root/'docs/product/phase-a/16-independent-candidates/H04-input.json').read_text())
tasks=[{'id':'FISCAL_COMPARE','question':'比较 Walmart FY2024 与 Coca-Cola 2024 年收入增长；它们是否属于完全相同的期间？列出各自财年结束日期、来源和比较局限。','sources':[{'kind':'text','title':a['source_title'],'text':a['source_text'],'url':a['source_url']},{'kind':'text','title':b['source_title'],'text':b['source_text'],'url':b['source_url']}]},
{'id':'GDP_ESTIMATE','question':'根据所附 BEA 公告，2022 年第三季度实际 GDP 的第二次和初次估计分别是多少？说明期间、折年口径和估计局限。','sources':[{'kind':'pdf','file':'bea-gdp-2022q3-second.pdf','title':'BEA GDP third quarter 2022 second estimate','published_at':'2022-11-30T13:30:00Z'}]},
{'id':'FED_PURCHASES','question':'根据所附 2021 年 12 月美联储声明，分别列出国债和机构抵押贷款支持证券的月度购买速度减幅及次年一月的购买速度，并说明限制。','sources':[{'kind':'pdf','file':'fed-dec-2021.pdf','title':'Federal Reserve FOMC statement December 2021','published_at':'2021-12-15T19:00:00Z'}]}]
for task in tasks:
 for source in task['sources']:
  source['source_id']='source_'+uuid4().hex
  if source['kind']=='pdf':
   data=(p/source['file']).read_bytes();text=_extract_text(data,text_order='reading');source['sha256']=h(data);source['text_sha256']=h(text.encode());(p/(task['id']+'-source.txt')).write_text(text)
 (p/(task['id']+'-input.json')).write_text(json.dumps(task,ensure_ascii=False,indent=2)+'\n')
ref={'FISCAL_COMPARE':{'dates':{'source_0':'January 31','source_1':'December 31'}},'GDP_ESTIMATE':{'facts':['2.9','2.6'],'period':'2022'},'FED_PURCHASES':{'facts':['10','20','40'],'unit':'billion'}}
(p/'references.json').write_text(json.dumps(ref,ensure_ascii=False,indent=2)+'\n')
old=json.loads((root/'docs/product/phase-a/60-reserved-full-review/manifest.json').read_text())
m={'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'assessment':'Nonblind evaluation on two previously used derived company snapshots and two public PDFs. BEA source is related to previously tested GDP data. No independent human review or general accuracy claim.',
   'model':old['model'],'generation_parameters':old['generation_parameters'],'provider_input_token_limit':3000,'provider_timeout_seconds':60,
   'tasks':[{'id':t['id'],'input_sha256':h((p/(t['id']+'-input.json')).read_bytes())} for t in tasks],
   'script_sha256':{name:h((p/name).read_bytes()) for name in ('prepare.py','run.py','score.py')},'references_sha256':h((p/'references.json').read_bytes()),
   'research_system_prompt_sha256':h(SYSTEM_PROMPT.encode()),'review_system_prompt_sha256':h(REVIEW_SYSTEM_PROMPT.encode()),'review_method':REVIEW_METHOD,
   'max_calls_per_task':2,'max_calls_total':6,'max_tokens_per_task':10000,'max_tokens_total':30000,'max_elapsed_seconds_per_task':120,'search_calls':0,'retry_external_calls':0,'cost_usd':'unknown',
   'pass_rule':'All three tasks must meet their fixed mechanical checks, correct source positions, complete review coverage with supports verdicts and call budgets. This narrow check does not establish semantic quality or full phase B acceptance.'}
(p/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'tasks':[t['id'] for t in tasks],'max_calls':6,'max_tokens':30000}))
