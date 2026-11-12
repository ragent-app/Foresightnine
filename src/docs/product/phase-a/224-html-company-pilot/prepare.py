from pathlib import Path
from hashlib import sha256
from datetime import datetime, timezone
import base64, gzip, json, subprocess, sys
p=Path(__file__).resolve().parent
root=p.parents[3]
sys.path.insert(0,str(root/'apps/api'))
from app.research_analysis import analyze, SYSTEM_PROMPT
from app.research_uploads import HtmlUploadRequest, extract_uploaded_html
from app.research_sources import ResearchSource, SourcePermissions
h=lambda b:sha256(b).hexdigest()
raw=gzip.decompress((root/'docs/product/phase-a/60-reserved-full-review/cisco-2024-sec.html.gz').read_bytes())
permissions=SourcePermissions(store=True,display=True,model_input=True,export=True)
text,raw_hash=extract_uploaded_html(HtmlUploadRequest(title='Cisco FY2024 SEC 10-K',html_base64=base64.b64encode(raw).decode(),permissions=permissions))
source=ResearchSource(source_id='source_22400000000000000000000000000001',owner_id='pilot',title='Cisco FY2024 SEC 10-K',
 text=text,kind='user_supplied',obtained_at=datetime(2026,11,13,tzinfo=timezone.utc),content_sha256=h(text.encode()),
 retrieval_method='user_html',response_sha256=raw_hash,response_content_type='text/html',permissions=permissions)
task={'question':'根据所附 Cisco 2024 年报，列出 2024 和 2023 财年的总收入、期间和单位，计算同比变化，并引用原文说明一个有关收入变化的限制。',
 'source_id':source.source_id,'title':source.title,'source_sha256':raw_hash,'text_sha256':source.content_sha256,
 'reference':{'revenue_2024_million_usd':'53803','revenue_2023_million_usd':'56998','growth_percent':'-5.61'}}
(p/'task.json').write_text(json.dumps(task,ensure_ascii=False,indent=2)+'\n')
prompt={}
def capture(value):
 prompt['sha256']=h(value.encode());prompt['bytes']=len(value.encode());return {},0
result=analyze(task['question'],[source],None,capture,max_prompt_chars=6000-len(SYSTEM_PROMPT.encode())-512)
manifest={'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
 'task_sha256':h((p/'task.json').read_bytes()),'prepare_sha256':h((p/'prepare.py').read_bytes()),
 'run_sha256':h((p/'run.py').read_bytes()),'system_sha256':h(SYSTEM_PROMPT.encode()),'prompt':prompt,
 'segments':result.segments,'source_hash':raw_hash,'text_hash':source.content_sha256,
 'max_calls':1,'max_tokens':10000,'input_limit':6000,'output_limit':4000,'max_elapsed_seconds':120,
 'model':'glm-5.3-flash','search':False,'support_review':False,'retries':0,'cost_usd':'unknown'}
(p/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'prompt':prompt,'segments':len(result.segments),'source_bytes':len(raw),'text_chars':len(text)}))
