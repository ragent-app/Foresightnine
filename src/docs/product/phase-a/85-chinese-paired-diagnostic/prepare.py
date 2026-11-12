"""Freeze paired prompt evidence before one external call."""
from pathlib import Path
from hashlib import sha256
from datetime import datetime,timezone
import json,subprocess,sys,types
p=Path(__file__).resolve().parent;root=p.parents[3];sys.path.insert(0,str(root/'apps/api'))
from app.research_sources import ResearchSource,SourcePermissions
import app.research_analysis as current
from app.research_verification import REVIEW_METHOD,REVIEW_SYSTEM_PROMPT
h=lambda b:sha256(b).hexdigest();old_commit='6f6aba2'
code=subprocess.check_output(['git','show',old_commit+':src/apps/api/app/research_analysis.py'],text=True)
old=types.ModuleType('app.chinese_paired_previous');old.__package__='app';sys.modules[old.__name__]=old;exec(compile(code,old.__name__,'exec'),old.__dict__)
ib=(p/'HK_RETAIL-input.json').read_bytes();t=json.loads(ib);s=t['sources'][0];text=(p/'HK_RETAIL-source.txt').read_text();assert s['text']==text
src=ResearchSource(source_id=s['source_id'],owner_id='experiment',title=s['title'],text=text,kind='user_supplied',obtained_at=datetime.now(timezone.utc),content_sha256=h(text.encode()),permissions=SourcePermissions(store=True,display=True,model_input=True,export=True))
assert old.SYSTEM_PROMPT==current.SYSTEM_PROMPT
captured=[]
for mod in (old,current):
 prompts=[];result=mod.analyze(t['question'],[src],None,lambda q:(prompts.append(q) or {},1),max_prompt_chars=3000-len(current.SYSTEM_PROMPT.encode())-512)
 assert len(prompts)==1;captured.append((prompts[0],result.segments))
prior=json.loads((p.parent/'83-chinese-official-data/HK_RETAIL-generation-provider-output.json').read_text());assert h(captured[0][0].encode())==prior['prompt_sha256']
a,b=(json.loads(q) for q,_ in captured);old_sources=a.pop('sources');new_sources=b.pop('sources');assert a==b
# The change in passage count is part of selection. Every repeated source
# envelope must otherwise have the same ID, title, dates and coverage.
def metadata(item):return {k:v for k,v in item.items() if k!='text'}
assert old_sources and new_sources and all(metadata(x)==metadata(old_sources[0]) for x in old_sources+new_sources)
assert [x['text'] for x in old_sources]!=[x['text'] for x in new_sources]
previous=json.loads((p.parent/'83-chinese-official-data/manifest.json').read_text())
m={'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'baseline_implementation_commit':old_commit,'assessment':'Nonblind single-task paired development diagnostic using attributed and modified official statistics; source selection changes, including segment count. Not independent, causal proof, or stage B acceptance.',
'original_pdf_url':previous['original_pdf_url'],'original_pdf_sha256':previous['original_pdf_sha256'],'extract_sha256':h(text.encode()),'model':previous['model'],'generation_parameters':previous['generation_parameters'],'provider_input_token_limit':3000,'provider_timeout_seconds':60,
 'tasks':[{'id':'HK_RETAIL','input_sha256':h(ib)}],'script_sha256':{n:h((p/n).read_bytes()) for n in ('prepare.py','run.py','score.py')},'references_sha256':h((p/'references.json').read_bytes()),'research_system_prompt_sha256':h(current.SYSTEM_PROMPT.encode()),'review_system_prompt_sha256':h(REVIEW_SYSTEM_PROMPT.encode()),'review_method':REVIEW_METHOD,
'old_prompt_sha256':h(captured[0][0].encode()),'new_prompt_sha256':h(captured[1][0].encode()),'old_segments':captured[0][1],'new_segments':captured[1][1],
'max_calls_per_task':1,'max_calls_total':1,'max_tokens_per_task':10000,'max_tokens_total':10000,'max_elapsed_seconds_per_task':120,'search_calls':0,'retry_external_calls':0,'cost_usd':'unknown','pass_rule':'All previously fixed mechanical checks in score.py must pass; this does not establish semantic support or stage B acceptance.'}
(p/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
