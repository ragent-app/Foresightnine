from common import *
import subprocess
from app.research_verification import REVIEW_METHOD, REVIEW_SYSTEM_PROMPT

baseline = json.loads((HERE.parent / '258-requested-facts-prompt-check' / 'manifest.json').read_text())
tasks = json.loads((DATA / 'tasks.json').read_text())
conditions = []
for task in tasks:
    task, source, raw = load_task(task['id'])
    prompt, segments = capture_prompt(task, source)
    conditions.append({'task': task['id'], 'set': 'development_recheck', 'source_sha256': digest(raw),
                       'text_sha256': source.content_sha256, 'prompt_sha256': digest(prompt.encode()),
                       'prompt_bytes': len(prompt.encode()), 'segments': segments})
m = {'implementation_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
     'model': baseline['model'], 'generation_parameters': baseline['generation_parameters'],
     'system_sha256': digest(SYSTEM_PROMPT.encode()), 'review_method': REVIEW_METHOD,
     'review_system_sha256': digest(REVIEW_SYSTEM_PROMPT.encode()),
     'files': {name: digest((HERE / name).read_bytes()) for name in ['common.py', 'prepare.py', 'run.py', 'score.py']},
     'source_files': {name: digest((DATA / name).read_bytes()) for name in ['tasks.json', 'reference.json', 'bea-gdp4q22.pdf', 'fomc-20170614.pdf']},
     'conditions': conditions, 'input_limit': 6000, 'output_limit': 4000,
     'max_calls_per_task': 2, 'max_tokens_per_task': 20000, 'max_calls_total': 6, 'max_tokens_total': 60000,
     'timeout_seconds': 60, 'elapsed_seconds': 120, 'search': False, 'retries': 0,
     'previous_known_tokens': 300186, 'cost_usd': 'unknown'}
(HERE / 'manifest.json').write_text(json.dumps(m, ensure_ascii=False, indent=2) + '\n')
