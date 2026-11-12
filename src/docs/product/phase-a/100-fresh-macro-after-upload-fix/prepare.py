"""Freeze official PDF inputs, extracted text, prompt hashes and evaluator."""
from pathlib import Path
from hashlib import sha256
from datetime import datetime, timezone
from uuid import uuid4
import json, subprocess, sys
p=Path(__file__).resolve().parent; root=p.parents[3];sys.path.insert(0,str(root/'apps/api'))
from app.research_sources import ResearchSource,SourcePermissions
from app.research_analysis import SYSTEM_PROMPT,analyze
from app.research_uploads import _extract_text
from app.research_verification import REVIEW_METHOD,REVIEW_SYSTEM_PROMPT
h=lambda b:sha256(b).hexdigest()
specs={
 'bea-second':('bea-second.pdf','BEA GDP Q3 2024 second estimate','2024-11-27T13:30:00Z','https://www.bea.gov/sites/default/files/2024-11/gdp3q24-2nd.pdf'),
 'bea-third':('bea-third.pdf','BEA GDP Q3 2024 third estimate','2024-12-19T13:30:00Z','https://www.bea.gov/sites/default/files/2024-12/gdp3q24-3rd.pdf'),
 'fed':('fed-source.txt','Federal Reserve September 20 2023 FOMC statement (extracted text)','2023-09-20T18:00:00Z','https://www.federalreserve.gov/monetarypolicy/files/monetary20230920a1.pdf')}
sources={}
for key,(file,title,published,url) in specs.items():
 raw=(p/file).read_bytes();text=raw.decode() if key=='fed' else _extract_text(raw,text_order='reading');textfile=key+'-source.txt';(p/textfile).write_text(text)
 if key=='fed':
  sources[key]={'kind':'text','text':text,'url':url,'title':title,'published_at':published,'source_id':'source_'+uuid4().hex}
 else:
  sources[key]={'kind':'pdf','file':file,'text_file':textfile,'url':url,'title':title,'published_at':published,'source_id':'source_'+uuid4().hex,'sha256':h(raw),'text_sha256':h(text.encode())}
tasks=[{'id':'GDP_REVISION','question':'根据提供的两份 BEA 公告，比较 2024 年第三季度美国实际 GDP 的第二次与第三次估计，说明年化口径、修订方向和原因，并指出被抵消的因素与资料局限。','sources':[sources['bea-second'],sources['bea-third']]},
{'id':'FED_HOLD','question':'根据所附 2023 年 9 月美联储声明，联邦基金目标利率区间是什么，决定是维持还是调整？列出后续政策可能考虑的因素与声明不能证明的内容。','sources':[sources['fed']]}]
prompts={};segments={}
for task in tasks:
 (p/(task['id']+'-input.json')).write_text(json.dumps(task,ensure_ascii=False,indent=2)+'\n'); objects=[]
 for s in task['sources']:
  text=s['text'] if s['kind']=='text' else (p/s['text_file']).read_text();objects.append(ResearchSource(source_id=s['source_id'],owner_id='experiment',title=s['title'],text=text,kind='user_supplied',obtained_at=datetime.now(timezone.utc),published_at=datetime.fromisoformat(s['published_at'].replace('Z','+00:00')),content_sha256=h(text.encode()),retrieval_method='user_pdf' if s['kind']=='pdf' else 'user_supplied',pdf_text_order='reading' if s['kind']=='pdf' else None,permissions=SourcePermissions(store=True,display=True,model_input=True,export=True)))
 capture=[];result=analyze(task['question'],objects,None,lambda q:(capture.append(q) or {},1),max_prompt_chars=3000-len(SYSTEM_PROMPT.encode())-512);assert len(capture)==1;prompts[task['id']]=h(capture[0].encode());segments[task['id']]=result.segments
refs={'GDP_REVISION':{'second':'2.8','third':'3.1','delta_pp':'0.3','up':['exports','consumer spending'],'offset':'private inventory investment'},'FED_HOLD':{'lower':'5.25','upper':'5.5','decision':'maintain','factors':['cumulative tightening','lags','economic and financial developments']}}
(p/'references.json').write_text(json.dumps(refs,ensure_ascii=False,indent=2)+'\n');old=json.loads((p.parent/'92-selection-paired-recheck/manifest.json').read_text())
m={'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'assessment':'Previously unused public official sources; prepared and assessed by same developer; nonblind and not a formal independent human quality assessment.',
'model':old['model'],'generation_parameters':old['generation_parameters'],'provider_input_token_limit':3000,'provider_timeout_seconds':60,'tasks':[{'id':t['id'],'input_sha256':h((p/(t['id']+'-input.json')).read_bytes())} for t in tasks],
'source_attributions':{'GDP_REVISION':'U.S. Bureau of Economic Analysis; third-quarter 2024 second and third official GDP estimates.','FED_HOLD':'Board of Governors of the Federal Reserve System; September 20 2023 FOMC statement.'},
'script_sha256':{n:h((p/n).read_bytes()) for n in ('prepare.py','run.py','score.py','validate_scorer.py')},'references_sha256':h((p/'references.json').read_bytes()),'research_system_prompt_sha256':h(SYSTEM_PROMPT.encode()),'review_system_prompt_sha256':h(REVIEW_SYSTEM_PROMPT.encode()),'review_method':REVIEW_METHOD,'prompt_sha256_by_task':prompts,'preselected_segments':segments,
'max_calls_per_task':1,'max_calls_total':2,'max_tokens_per_task':10000,'max_tokens_total':20000,'max_elapsed_seconds_per_task':120,'search_calls':0,'retry_external_calls':0,'cost_usd':'unknown','pass_rule':'All pre-registered expected evidence, provenance, no numeric warnings and completed research; narrow mechanical result, not semantic proof.'}
(p/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
