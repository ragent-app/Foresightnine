from common import *
import subprocess
from app.research_verification import REVIEW_METHOD, REVIEW_SYSTEM_PROMPT

baseline = json.loads((HERE.parent / '266-cross-domain-recheck' / 'manifest.json').read_text())
task, source, raw = load_task('MACRO_GDP_2022Q4')
prompt, segments = capture_prompt(task, source)
conditions = [dict(case=case, task=task['id'], set='nonblind_development', max_tokens=budget,
                   source_sha256=digest(raw), text_sha256=source.content_sha256,
                   prompt_sha256=digest(prompt.encode()), prompt_bytes=len(prompt.encode()), segments=segments)
              for case, budget in [('budget_10000', 10000), ('budget_20000', 20000)]]
m = {'implementation_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
     'model': baseline['model'], 'generation_parameters': baseline['generation_parameters'],
     'system_sha256': digest(SYSTEM_PROMPT.encode()), 'review_method': REVIEW_METHOD,
     'review_system_sha256': digest(REVIEW_SYSTEM_PROMPT.encode()),
     'files': {name: digest((HERE / name).read_bytes()) for name in ['common.py', 'prepare.py', 'run.py', 'score.py']},
     'source_files': {name: digest((DATA / name).read_bytes()) for name in ['tasks.json', 'reference.json', 'bea-gdp4q22.pdf', 'fomc-20170614.pdf']},
     'conditions': conditions, 'input_limit': 6000, 'output_limit': 4000,
     'max_calls_per_task': 2, 'max_calls_total': 4, 'max_tokens_total': 30000,
     'timeout_seconds': 60, 'elapsed_seconds': 120, 'search': False, 'retries': 0,
     'previous_known_tokens': 291515, 'cost_usd': 'unknown'}
assert conditions[0]['prompt_sha256'] == conditions[1]['prompt_sha256']
(HERE / 'manifest.json').write_text(json.dumps(m, ensure_ascii=False, indent=2) + '\n')
