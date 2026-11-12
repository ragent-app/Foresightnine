"""Freeze one previously used SEC extraction and a new cash-flow task."""
from pathlib import Path
from hashlib import sha256
from datetime import datetime,timezone
from uuid import uuid4
import json,subprocess,sys
p=Path(__file__).resolve().parent;root=p.parents[3];sys.path.insert(0,str(root/'apps/api'))
from app.research_sources import ResearchSource,SourcePermissions
from app.research_analysis import analyze,SYSTEM_PROMPT
from app.research_verification import REVIEW_METHOD,REVIEW_SYSTEM_PROMPT
h=lambda b:sha256(b).hexdigest();old=json.loads((p.parent/'45-reserved-cross-domain-rerun/NVDA_REV-input.json').read_text())
task={'id':'CASH_FLOW','question':'根据所附申报材料，列出2023年和2022年经营活动现金流、各年度截止日及单位，并计算同比变化。指出这份摘录中尚不能确定的内容。',
 'sources':[{'kind':'text','text':old['source_text'],'url':old['source_url'],'title':old['source_title'],'source_id':'source_'+uuid4().hex}]}
(p/'CASH_FLOW-input.json').write_text(json.dumps(task,ensure_ascii=False,indent=2)+'\n');s=task['sources'][0];text=s['text']
source=ResearchSource(source_id=s['source_id'],owner_id='e',title=s['title'],text=text,kind='user_supplied',obtained_at=datetime.now(timezone.utc),content_sha256=h(text.encode()),retrieval_method='user_supplied',permissions=SourcePermissions(store=True,display=True,model_input=True,export=True))
q=[];result=analyze(task['question'],[source],None,lambda prompt:(q.append(json.loads(prompt)) or {},1),max_prompt_chars=3000-len(SYSTEM_PROMPT.encode())-512)
assert q and all(value in ' '.join(x['text'] for x in q[0]['sources']) for value in ('5,641','9,108','2023','2022','millions'))
prompt=json.dumps(q[0],ensure_ascii=False,separators=(',',':'));assert h(prompt.encode())==result.prompt_sha256
refs={'company':'NVIDIA','metric':'net cash provided by operating activities','year_2023':'5641','year_2022':'9108','unit':'USD millions','growth_percent':'-38.07','end_2023':'2023-01-29','end_2022':'2022-01-30'}
(p/'references.json').write_text(json.dumps(refs,ensure_ascii=False,indent=2)+'\n');provider=json.loads((p.parent/'104-revision-paired-recheck/manifest.json').read_text())
m={'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'model':provider['model'],'generation_parameters':provider['generation_parameters'],'provider_input_token_limit':3000,'provider_timeout_seconds':60,
 'tasks':[{'id':'CASH_FLOW','input_sha256':h((p/'CASH_FLOW-input.json').read_bytes())}], 'source_attributions':{'CASH_FLOW':'NVIDIA 2023 Form 10-K, SEC EDGAR HTML extraction; table separators are generated.'},
 'script_sha256':{name:h((p/name).read_bytes()) for name in ('prepare.py','run.py','score.py','validate_scorer.py')},'references_sha256':h((p/'references.json').read_bytes()),
 'research_system_prompt_sha256':h(SYSTEM_PROMPT.encode()),'review_system_prompt_sha256':h(REVIEW_SYSTEM_PROMPT.encode()),'review_method':REVIEW_METHOD,
 'prompt_sha256_by_task':{'CASH_FLOW':result.prompt_sha256},'preselected_segments':{'CASH_FLOW':result.segments},'max_calls_per_task':1,'max_calls_total':1,
 'max_tokens_per_task':10000,'max_tokens_total':10000,'max_elapsed_seconds_per_task':120,'search_calls':0,'retry_external_calls':0,'cost_usd':'unknown'}
(p/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
