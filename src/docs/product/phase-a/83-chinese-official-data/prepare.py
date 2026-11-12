"""Freeze an attributed, text-only statistical extract and one model call."""
from pathlib import Path
from hashlib import sha256
from datetime import datetime,timezone
from uuid import uuid4
import json,subprocess,sys
p=Path(__file__).resolve().parent;root=p.parents[3];sys.path.insert(0,str(root/'apps/api'))
from app.research_sources import ResearchSource,SourcePermissions
from app.research_analysis import SYSTEM_PROMPT,analyze
from app.research_verification import REVIEW_METHOD,REVIEW_SYSTEM_PROMPT
h=lambda x:sha256(x).hexdigest();pdf=Path('/tmp/censtatd-retail-nov2024.pdf');raw=pdf.read_bytes()
text=subprocess.check_output(['pdftotext','-layout',str(pdf),'-']).decode('utf-8');pages=text.split('\f')
# Textual statistical report excerpts only; images and graph pages omitted.
parts=[pages[0],pages[3].split('圖2.1：')[0],pages[4],pages[6],pages[7].split('表3.1：')[0],pages[10],pages[11],pages[12]]
source='資料來源：香港特別行政區政府統計處；資料知識產權屬香港特別行政區政府。下文按原順序摘錄並保留部分統計表，省略圖形與部分頁面，屬修改版本。\n'+'\n'.join(parts)
(p/'HK_RETAIL-source.txt').write_text(source)
task={'id':'HK_RETAIL','question':'根據香港政府統計處2024年11月報告，零售業總銷貨價值及網上銷售價值各是多少、同比變化多少？網上銷售佔總銷貨價值多少？指出數字的估計局限。',
'sources':[{'kind':'text','title':'香港政府統計處：2024年11月零售業銷貨額按月統計調查報告（節錄）','text':source,'url':'https://www.censtatd.gov.hk/tc/wbr.html?ecode=B10800032024MM11','source_id':'source_'+uuid4().hex}]}
(p/'HK_RETAIL-input.json').write_text(json.dumps(task,ensure_ascii=False,indent=2)+'\n')
refs={'total_value_hkd_100m':'317','total_change_percent':'-7.3','online_value_hkd_100m':'29','online_change_percent':'-7.0','online_share_percent':'9.3','estimate':'臨時估計'}
(p/'references.json').write_text(json.dumps(refs,ensure_ascii=False,indent=2)+'\n')
s=task['sources'][0];report=ResearchSource(source_id=s['source_id'],owner_id='experiment',title=s['title'],text=source,kind='user_supplied',obtained_at=datetime.now(timezone.utc),content_sha256=h(source.encode()),permissions=SourcePermissions(store=True,display=True,model_input=True,export=True))
model=[];analysis=analyze(task['question'],[report],None,lambda q:(model.append(q) or {},1),max_prompt_chars=3000-len(SYSTEM_PROMPT.encode())-512)
assert len(model)==1
old=json.loads((p.parent/'76-singular-paired-diagnostic/manifest.json').read_text())
m={'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'assessment':'Nonblind official-source Chinese development check using attributed, modified textual extracts; no independent semantic review and no phase acceptance claim.',
'original_pdf_url':'https://www.censtatd.gov.hk/wbr/B1080003/B10800032024MM11/att/tc/B10800032024MM11.pdf','original_pdf_sha256':h(raw),'original_text_sha256':h(text.encode()),'extract_sha256':h(source.encode()),'model':old['model'],'generation_parameters':old['generation_parameters'],'provider_input_token_limit':3000,'provider_timeout_seconds':60,
 'tasks':[{'id':task['id'],'input_sha256':h((p/'HK_RETAIL-input.json').read_bytes())}], 'script_sha256':{n:h((p/n).read_bytes()) for n in ('prepare.py','run.py','score.py')},'references_sha256':h((p/'references.json').read_bytes()),'research_system_prompt_sha256':h(SYSTEM_PROMPT.encode()),'review_system_prompt_sha256':h(REVIEW_SYSTEM_PROMPT.encode()),'review_method':REVIEW_METHOD,'new_prompt_sha256':h(model[0].encode()),'preselected_segments':analysis.segments,
'max_calls_per_task':1,'max_calls_total':1,'max_tokens_per_task':10000,'max_tokens_total':10000,'max_elapsed_seconds_per_task':120,'search_calls':0,'retry_external_calls':0,'cost_usd':'unknown', 'pass_rule':'All fixed mechanical checks must pass; this does not establish semantic support or stage B acceptance.'}
(p/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
