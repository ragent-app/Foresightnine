from pathlib import Path
from hashlib import sha256
from datetime import datetime, timezone
import json, subprocess, sys
HERE=Path(__file__).resolve().parent; ROOT=HERE.parents[3]; sys.path.insert(0,str(ROOT/'apps/api'))
from app.research_analysis import analyze, SYSTEM_PROMPT, SELECTION_METHOD
from app.research_sources import ResearchSource, SourcePermissions
from app.research_verification import REVIEW_METHOD
sha=lambda data: sha256(data).hexdigest()
pre=json.loads((HERE/'preflight-result.json').read_text());assert pre['status']=='matched'
raw=(HERE/'source.html').read_bytes();text=(HERE/'source.txt').read_text();assert sha(raw)==pre['response_sha256'] and sha(text.encode())==pre['text_sha256']
question="According to BEA's January 2024 advance estimate, what was real GDP growth for full-year 2023 and the annualized rate for the fourth quarter of 2023? Cite the source and distinguish the periods."
source=ResearchSource(source_id='source_'+'1'*32,owner_id='fixture',title='BEA advance estimate',text=text,kind='official',obtained_at=datetime.now(timezone.utc),content_sha256=sha(text.encode()),retrieval_method='official_html',permissions=SourcePermissions(store=True,display=True,model_input=True,export=True))
prompts=[];result=analyze(question,[source],None,lambda p:(prompts.append(p) or {},0),max_prompt_chars=6000-len(SYSTEM_PROMPT.encode())-512)
prior=json.loads((HERE.parent/'235-cross-domain-recheck/manifest.json').read_text())
files=['preflight.py','prepare.py','run.py','score.py']; manifest={
 'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
 'question':question,'source_url':'https://www.bea.gov/news/2024/gross-domestic-product-fourth-quarter-and-year-2023-advance-estimate',
 'source_sha256':sha(raw),'text_sha256':sha(text.encode()),'prompt_sha256':sha(prompts[0].encode()),'prompt_bytes':len(prompts[0].encode()),
 'model':prior['model'],'generation_parameters':prior['generation_parameters'],'system_sha256':sha(SYSTEM_PROMPT.encode()),
 'selection_method':SELECTION_METHOD,'review_method':REVIEW_METHOD,'files':{f:sha((HERE/f).read_bytes()) for f in files},
 'input_limit':6000,'output_limit':4000,'max_model_calls':1,'max_total_tokens':10000,'max_search_calls':1,
 'search':'fixed synthetic candidate, no external search','real_official_fetches_during_run':1,'timeout_seconds':60,'run_elapsed_seconds':120,
 'retry':0,'model_review':False,'as_of':None,'dollar_cost':'unknown','previous_known_tokens':268091}
(HERE/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
