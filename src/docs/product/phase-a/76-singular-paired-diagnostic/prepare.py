"""Freeze old and new generation inputs before one controlled model call."""
from pathlib import Path
from hashlib import sha256
from datetime import datetime, timezone
import json,subprocess,sys,types
p=Path(__file__).resolve().parent;root=p.parents[3];sys.path.insert(0,str(root/'apps/api'))
from app.research_sources import ResearchSource,SourcePermissions
import app.research_analysis as current
from app.research_verification import REVIEW_METHOD,REVIEW_SYSTEM_PROMPT
h=lambda x:sha256(x).hexdigest(); old_commit='f55c717658538d2a9c5314c04dd3ccf573dd6789'
code=subprocess.check_output(['git','show',old_commit+':src/apps/api/app/research_analysis.py'],text=True)
old=types.ModuleType('app.research_analysis_paired_old');old.__package__='app';sys.modules[old.__name__]=old;exec(compile(code,old.__name__,'exec'),old.__dict__)
raw=(p/'FOMC_DISSENT-input.json').read_bytes();t=json.loads(raw);s=t['sources'][0];text=(p/'FOMC_DISSENT-source.txt').read_text()
assert h((p/s['file']).read_bytes())==s['sha256'] and h(text.encode())==s['text_sha256']
source=ResearchSource(source_id=s['source_id'],owner_id='experiment',title=s['title'],text=text,kind='user_supplied',obtained_at=datetime.now(timezone.utc),published_at=datetime.fromisoformat(s['published_at'].replace('Z','+00:00')),content_sha256=s['text_sha256'],retrieval_method='user_pdf',permissions=SourcePermissions(store=True,display=True,model_input=True,export=True))
assert old.SYSTEM_PROMPT == current.SYSTEM_PROMPT
captured=[]
for mod in (old,current):
 calls=[];r=mod.analyze(t['question'],[source],None,lambda prompt:(calls.append(prompt) or {},1),max_prompt_chars=3000-len(current.SYSTEM_PROMPT.encode())-512)
 assert len(calls)==1; captured.append((calls[0],r.segments))
prior=json.loads((p.parent/'73-new-public-sources/FOMC_DISSENT-generation-provider-output.json').read_text()); assert h(captured[0][0].encode())==prior['prompt_sha256']
a,b=(json.loads(q) for q,_ in captured);assert len(a['sources'])==len(b['sources'])==2
old_text=[x.pop('text') for x in a['sources']];new_text=[x.pop('text') for x in b['sources']]
assert a==b and old_text!=new_text and '1/4 to 1/2 percent' not in ' '.join(old_text) and '1/4 to 1/2 percent' in ' '.join(new_text) and 'James Bullard' in ' '.join(new_text)
refs={'target':['1/4','1/2'],'dissenter':'James Bullard','preferred_increase':'0.5 percentage point','preferred_range':['1/2','3/4']}
(p/'references.json').write_text(json.dumps(refs,ensure_ascii=False,indent=2)+'\n')
original=json.loads((p.parent/'73-new-public-sources/manifest.json').read_text())
m={'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'baseline_implementation_commit':old_commit,'assessment':'Single nonblind paired development diagnostic; fixed input and model settings, only source passages differ in generation prompts. Not independent and does not establish causality, semantic support, or phase acceptance.',
'model':original['model'],'generation_parameters':original['generation_parameters'],'provider_input_token_limit':3000,'provider_timeout_seconds':60,'tasks':[{'id':t['id'],'input_sha256':h(raw)}],
'script_sha256':{n:h((p/n).read_bytes()) for n in ('prepare.py','run.py','score.py')},'references_sha256':h((p/'references.json').read_bytes()),'research_system_prompt_sha256':h(current.SYSTEM_PROMPT.encode()),'review_system_prompt_sha256':h(REVIEW_SYSTEM_PROMPT.encode()),'review_method':REVIEW_METHOD,
'old_prompt_sha256':h(captured[0][0].encode()),'new_prompt_sha256':h(captured[1][0].encode()),'old_segments':captured[0][1],'new_segments':captured[1][1],
'max_calls_per_task':1,'max_calls_total':1,'max_tokens_per_task':10000,'max_tokens_total':10000,'max_elapsed_seconds_per_task':120,'search_calls':0,'retry_external_calls':0,'cost_usd':'unknown',
'pass_rule':'All pre-specified checks in score.py must pass. Only generation is compared; review remains off. Mechanical checks do not prove semantic support or general quality.'}
(p/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
