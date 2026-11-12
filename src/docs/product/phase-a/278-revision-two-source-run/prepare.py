from common import *
import subprocess
from app.research_verification import REVIEW_METHOD,REVIEW_SYSTEM_PROMPT
q,sources,original=inputs();prompt,segments=prompt_for(q,sources)
baseline=json.loads((HERE.parent/'272-cross-domain-review'/'manifest.json').read_text())
m={'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
   'task_sha256':digest((DATA/'input.json').read_bytes()), 'sources':original,
   'files':{name:digest((HERE/name).read_bytes()) for name in ('common.py','prepare.py','run.py','score.py')},
   'model':baseline['model'],'generation_parameters':baseline['generation_parameters'],
   'system_sha256':digest(SYSTEM_PROMPT.encode()),'review_method':REVIEW_METHOD,
   'review_system_sha256':digest(REVIEW_SYSTEM_PROMPT.encode()),'prompt_sha256':digest(prompt.encode()),
   'segments':segments,'input_limit':6000,'output_limit':4000,'max_calls':2,'max_tokens':20000,
   'timeout_seconds':60,'elapsed_seconds':120,'search':False,'retries':0,
   'previous_known_tokens':313227,'cost_usd':'unknown'}
(HERE/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
