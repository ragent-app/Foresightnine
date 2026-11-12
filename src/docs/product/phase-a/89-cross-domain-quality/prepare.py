"""Freeze one derived SEC sample and one official policy PDF before calling."""
from pathlib import Path
from hashlib import sha256
from datetime import datetime,timezone
from uuid import uuid4
import json,subprocess,sys
p=Path(__file__).resolve().parent;root=p.parents[3];sys.path.insert(0,str(root/'apps/api'))
from app.research_sources import ResearchSource,SourcePermissions
from app.research_analysis import SYSTEM_PROMPT,analyze
from app.research_uploads import _extract_text
from app.research_verification import REVIEW_METHOD,REVIEW_SYSTEM_PROMPT
h=lambda b:sha256(b).hexdigest(); raw=(p/'msft-source.json').read_bytes(); ref=json.loads(raw); assert ref['type']=='derived_sec_facts_not_full_filing'
assert [(x['end'],x['val']) for x in ref['records']]==[('2022-06-30',198270000000),('2023-06-30',211915000000)]
fed=(p/'fed-nov-2022.pdf').read_bytes();fed_text=_extract_text(fed,text_order='reading');(p/'FED_POLICY-source.txt').write_text(fed_text)
tasks=[{'id':'MSFT_REVENUE','question':'根據提供的 SEC 記錄，列出截至2023年與2022年6月30日的12個月收入各為多少美元，計算同比增長率，並說明資料限制。注意不要把填報的 fy 欄位誤當成每條觀察的實際期間。',
'sources':[{'kind':'text','title':'Microsoft: SEC 2023 filing revenue facts (derived extract)','text':raw.decode(),'url':ref['source_url'],'source_id':'source_'+uuid4().hex}]},
{'id':'FED_POLICY','question':'根據所附2022年11月美聯儲聲明，聯邦基金目標利率區間是多少？列出決議對未來利率路徑的表述，並指出能從這份聲明確認與不能確認的限制。',
'sources':[{'kind':'pdf','file':'fed-nov-2022.pdf','title':'Federal Reserve FOMC statement 2022-11-02','published_at':'2022-11-02T18:00:00Z','source_id':'source_'+uuid4().hex,'sha256':h(fed),'text_sha256':h(fed_text.encode())}]}]
prompts={};segments={}
for t in tasks:
 (p/(t['id']+'-input.json')).write_text(json.dumps(t,ensure_ascii=False,indent=2)+'\n');s=t['sources'][0];text=s['text'] if s['kind']=='text' else fed_text
 src=ResearchSource(source_id=s['source_id'],owner_id='experiment',title=s['title'],text=text,kind='user_supplied',obtained_at=datetime.now(timezone.utc),published_at=datetime.fromisoformat(s['published_at'].replace('Z','+00:00')) if s.get('published_at') else None,content_sha256=h(text.encode()),retrieval_method='user_pdf' if s['kind']=='pdf' else 'user_supplied',pdf_text_order='reading' if s['kind']=='pdf' else None,permissions=SourcePermissions(store=True,display=True,model_input=True,export=True))
 capture=[];result=analyze(t['question'],[src],None,lambda q:(capture.append(q) or {},1),max_prompt_chars=3000-len(SYSTEM_PROMPT.encode())-512);assert len(capture)==1;prompts[t['id']]=h(capture[0].encode());segments[t['id']]=result.segments
refs={'MSFT_REVENUE':{'usd_2022':'198270000000','usd_2023':'211915000000','growth_percent':'6.88'},'FED_POLICY':{'lower':'3.75','upper':'4','source_range':'3-3/4 to 4 percent','future':'ongoing increases'}}
(p/'references.json').write_text(json.dumps(refs,ensure_ascii=False,indent=2)+'\n')
old=json.loads((p.parent/'85-chinese-paired-diagnostic/manifest.json').read_text())
m={'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'assessment':'Nonblind development check; Microsoft data are previously used derived SEC observations, not a full filing, and current copies do not prove historical availability. Fed PDF is a retained public primary source. Not an independent test or formal phase acceptance.',
'model':old['model'],'generation_parameters':old['generation_parameters'],'provider_input_token_limit':3000,'provider_timeout_seconds':60,'tasks':[{'id':t['id'],'input_sha256':h((p/(t['id']+'-input.json')).read_bytes())} for t in tasks],
'msft_snapshot_sha256':h(raw),'source_attributions':{'MSFT_REVENUE':'SEC EDGAR companyfacts, derived Microsoft observations; not an original filing.','FED_POLICY':'Board of Governors of the Federal Reserve System; public FOMC statement.'},
'script_sha256':{n:h((p/n).read_bytes()) for n in ('prepare.py','run.py','score.py','validate_scorer.py')},'references_sha256':h((p/'references.json').read_bytes()),'research_system_prompt_sha256':h(SYSTEM_PROMPT.encode()),'review_system_prompt_sha256':h(REVIEW_SYSTEM_PROMPT.encode()),'review_method':REVIEW_METHOD,'prompt_sha256_by_task':prompts,'preselected_segments':segments,
'max_calls_per_task':1,'max_calls_total':2,'max_tokens_per_task':10000,'max_tokens_total':20000,'max_elapsed_seconds_per_task':120,'search_calls':0,'retry_external_calls':0,'cost_usd':'unknown','pass_rule':'All fixed checks per task and a completed result are needed for a narrow mechanical pass; this does not establish semantic support or stage B acceptance.'}
(p/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
