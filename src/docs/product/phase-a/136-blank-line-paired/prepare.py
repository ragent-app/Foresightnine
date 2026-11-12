from common import *
import subprocess
prior=json.loads((HERE.parent/'128-selection-paired-recheck/manifest.json').read_text())
tasks=['FED_PURCHASES']
conditions=[]
for name in tasks:
 task, sources=get_task(name)
 for budget in (6000,):
  prompts=[]
  result=run_analysis(task,sources,budget,lambda prompt:(prompts.append(prompt) or {},0))
  assert len(prompts)==1
  conditions.append({'task':name,'input_limit':budget,'prompt_sha256':digest(prompts[0].encode()),
    'prompt_bytes':len(prompts[0].encode()),'segments':result.segments,
    'source_hashes':{s.source_id:s.content_sha256 for s in sources}})
files=['common.py','prepare.py','run.py','score.py','FED_PURCHASES-input.json']
manifest={'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
 'model':prior['model'],'generation_parameters':prior['generation_parameters'],'system_sha256':digest(SYSTEM_PROMPT.encode()),
 'files':{name:digest((HERE/name).read_bytes()) for name in files},'conditions':conditions,
 'prior_result_sha256':digest((HERE.parent/'134-context-budget-paired/FED_PURCHASES-6000-result.json').read_bytes()),
 'prior_score_sha256':digest((HERE.parent/'134-context-budget-paired/score.json').read_bytes()),
 'max_calls':1,'per_call_max_total_tokens':10000,'max_total_tokens':10000,'timeout_seconds':60,
 'search_calls':0,'retries':0,'cost_usd':'unknown','previous_known_tokens':143314}
(HERE/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
