from common import *
from app.research_counterpoint import prepare_counterpoint_prompt
import subprocess

baseline=json.loads((DATA/'manifest.json').read_text())
task,source,raw=load_task('FOMC_202507')
prompt,segments=capture_prompt(task,source)
condition=next(x for x in baseline['conditions'] if x['task']==task['id'])
assert digest(raw)==condition['source_sha256'] and source.content_sha256==condition['text_sha256']
assert digest(prompt.encode())==condition['prompt_sha256'] and segments==condition['segments']
generation_path=DATA/'FOMC_202507-generation-1-provider.json'
generation=json.loads(generation_path.read_text())
analysis=analyze(task['question'],[source],None,lambda _:(generation['payload'],generation['reported_tokens']),
                 max_prompt_chars=6000-len(SYSTEM_PROMPT.encode())-512)
follow=prepare_counterpoint_prompt(prompt,analysis.claims,6000-len(SYSTEM_PROMPT.encode())-512)
assert follow is not None
baseline_path=DATA/'FOMC_202507-counterpoint-2-provider.json'
assert follow.prompt_sha256==json.loads(baseline_path.read_text())['prompt_sha256']
parsed=json.loads(follow.prompt);old=parsed['counterpoint_request']
parsed['counterpoint_request']=old+' Agreement or a missing fact alone is not a counterargument.'
candidate=json.dumps(parsed,ensure_ascii=False,separators=(',',':'))
assert len(candidate.encode())<=6000-len(SYSTEM_PROMPT.encode())-512
(HERE/'baseline-prompt.json').write_text(follow.prompt)
(HERE/'candidate-prompt.json').write_text(candidate)
m={'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
   'task':task['id'],'source_sha256':digest(raw),'text_sha256':source.content_sha256,'segments':segments,
   'baseline':'../292-new-source-quality/FOMC_202507-counterpoint-2-provider.json',
   'baseline_sha256':digest(baseline_path.read_bytes()),'generation_sha256':digest(generation_path.read_bytes()),
   'baseline_prompt_sha256':follow.prompt_sha256,'candidate_prompt_sha256':digest(candidate.encode()),
   'change':'append one sentence to counterpoint_request, preserving truncation disclosure','old_request':old,'new_request':parsed['counterpoint_request'],
   'model':baseline['model'],'generation_parameters':baseline['generation_parameters'],
   'system_sha256':digest(SYSTEM_PROMPT.encode()),'input_limit':6000,'output_limit':4000,
   'max_calls':1,'max_tokens':10000,'timeout_seconds':60,'search':False,'retries':0,
   'previous_known_tokens':335796,'cost_usd':'unknown',
   'files':{name:digest((HERE/name).read_bytes()) for name in ['common.py','prepare.py','run.py','inspect_results.py']}}
(HERE/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
