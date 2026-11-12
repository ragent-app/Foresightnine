from common import *
import subprocess
prior=json.loads((ORIGINAL/'manifest.json').read_text())
conditions=[]
for name in ('TRADE_FEB','STRESS_2023'):
 task,sources=get_task(name);prompts=[]
 result=run_analysis(task,sources,lambda p:(prompts.append(p) or {},0))
 assert len(prompts)==1
 previous=next(item for item in prior['conditions'] if item['task']==name)
 assert {s.source_id:s.content_sha256 for s in sources}==previous['source_hashes']
 assert digest(prompts[0].encode())!=previous['prompt_sha256']
 conditions.append({'task':name,'prompt_sha256':digest(prompts[0].encode()),'prompt_bytes':len(prompts[0].encode()),
                    'segments':result.segments,'source_hashes':previous['source_hashes']})
files=['common.py','prepare.py','run.py','score.py']
original_files=['TRADE_FEB-input.json','STRESS_2023-input.json','bea-trade-source.txt','fed-stress-source.txt',
                'TRADE_FEB-result.json','STRESS_2023-result.json','score.py','score.json']
m={'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
 'model':prior['model'],'generation_parameters':prior['generation_parameters'],'system_sha256':digest(SYSTEM_PROMPT.encode()),
 'files':{f:digest((HERE/f).read_bytes()) for f in files},'original_files':{f:digest((ORIGINAL/f).read_bytes()) for f in original_files},
 'baseline_files':{f:digest((HERE.parent/'146-context-preservation-paired'/f).read_bytes()) for f in ('TRADE_FEB-result.json','STRESS_2023-result.json','score.json')},
 'conditions':conditions,'input_limit':3000,'max_calls':2,'per_call_max_total_tokens':10000,'max_total_tokens':20000,
 'timeout_seconds':60,'search_calls':0,'retries':0,'cost_usd':'unknown','previous_known_tokens':163901}
(HERE/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
