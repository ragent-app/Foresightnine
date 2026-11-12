"""Freeze inputs, excerpts, model settings, references, scripts and hashes before calls."""
from pathlib import Path
from hashlib import sha256
from datetime import datetime,timezone
from uuid import uuid4
import json,subprocess,sys
p=Path(__file__).resolve().parent;root=p.parents[3];sys.path.insert(0,str(root/'apps/api'))
from app.research_sources import ResearchSource,SourcePermissions
from app.research_analysis import analyze,SYSTEM_PROMPT
from app.research_uploads import _extract_text
from app.research_verification import REVIEW_METHOD,REVIEW_SYSTEM_PROMPT
h=lambda b:sha256(b).hexdigest()
items={
 'bea':('bea-third.pdf','BEA GDP fourth quarter 2023 third estimate','2024-03-28T12:30:00Z','https://www.bea.gov/sites/default/files/2024-03/gdp4q23-3rd.pdf'),
 'fed':('fed-statement.txt','Federal Reserve 2021-07-28 FOMC statement (HTML-extracted text)','2021-07-28T18:00:00Z','https://www.federalreserve.gov/newsevents/pressreleases/monetary20210728a.htm')}
sources={}
for key,(name,title,published,url) in items.items():
 raw=(p/name).read_bytes();text=_extract_text(raw,text_order='reading') if key=='bea' else raw.decode()
 textfile=key+'-source.txt';(p/textfile).write_text(text)
 item={'kind':'pdf' if key=='bea' else 'text','title':title,'url':url,'published_at':published,'source_id':'source_'+uuid4().hex}
 if key=='bea':item.update(file=name,text_file=textfile,sha256=h(raw),text_sha256=h(text.encode()))
 else:item['text']=text
 sources[key]=item
tasks=[{'id':'GDP_THIRD','question':'根据所附 BEA 公告，2023 年第四季度实际 GDP 第三次和第二次估计的年化增长率分别是多少？说明上修的主要来源，以及抵消上修的反向因素。','sources':[sources['bea']]},
 {'id':'FED_PURCHASES','question':'根据所附 2021 年 7 月美联储声明，联邦基金利率目标区间是多少？每月国债和机构抵押贷款支持证券购买至少各是多少？指出声明中仍存在的经济风险或尚未恢复的领域。','sources':[sources['fed']]}]
prompts={};segments={}
for task in tasks:
 (p/(task['id']+'-input.json')).write_text(json.dumps(task,ensure_ascii=False,indent=2)+'\n');objects=[]
 for s in task['sources']:
  text=(p/s['text_file']).read_text() if s['kind']=='pdf' else s['text']
  objects.append(ResearchSource(source_id=s['source_id'],owner_id='experiment',title=s['title'],text=text,kind='user_supplied',obtained_at=datetime.now(timezone.utc),published_at=datetime.fromisoformat(s['published_at'].replace('Z','+00:00')),content_sha256=h(text.encode()),retrieval_method='user_pdf' if s['kind']=='pdf' else 'user_supplied',pdf_text_order='reading' if s['kind']=='pdf' else None,permissions=SourcePermissions(store=True,display=True,model_input=True,export=True)))
 captured=[];result=analyze(task['question'],objects,None,lambda q:(captured.append(q) or {},1),max_prompt_chars=3000-len(SYSTEM_PROMPT.encode())-512)
 assert len(captured)==1;prompts[task['id']]=result.prompt_sha256;segments[task['id']]=result.segments
refs={'GDP_THIRD':{'annualized_third':'3.4','second':'3.2','upward':['consumer spending','nonresidential fixed investment'],'offset':'private inventory investment'},
      'FED_PURCHASES':{'lower':'0','upper':'0.25','treasury_billions_per_month':'80','mbs_billions_per_month':'40','risk':'public health or incomplete recovery'}}
(p/'references.json').write_text(json.dumps(refs,ensure_ascii=False,indent=2)+'\n');provider=json.loads((p.parent/'104-revision-paired-recheck/manifest.json').read_text())
m={'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'model':provider['model'],'generation_parameters':provider['generation_parameters'],'provider_input_token_limit':3000,'provider_timeout_seconds':60,
 'tasks':[{'id':t['id'],'input_sha256':h((p/(t['id']+'-input.json')).read_bytes())} for t in tasks], 'source_attributions':{'GDP_THIRD':'U.S. Bureau of Economic Analysis, 2023 Q4 third estimate, original PDF.','FED_PURCHASES':'Board of Governors of the Federal Reserve System, 2021-07-28 statement, extracted HTML text.'},
 'script_sha256':{name:h((p/name).read_bytes()) for name in ('prepare.py','run.py','score.py','validate_scorer.py')},'references_sha256':h((p/'references.json').read_bytes()),
 'research_system_prompt_sha256':h(SYSTEM_PROMPT.encode()),'review_system_prompt_sha256':h(REVIEW_SYSTEM_PROMPT.encode()),'review_method':REVIEW_METHOD,
 'prompt_sha256_by_task':prompts,'preselected_segments':segments,'max_calls_per_task':1,'max_calls_total':2,'max_tokens_per_task':10000,'max_tokens_total':20000,'max_elapsed_seconds_per_task':120,'search_calls':0,'retry_external_calls':0,'cost_usd':'unknown'}
(p/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
