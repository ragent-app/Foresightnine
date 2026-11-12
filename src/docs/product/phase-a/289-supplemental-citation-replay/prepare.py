from common import *
import subprocess
from app.research_verification import REVIEW_METHOD, REVIEW_SYSTEM_PROMPT
from app.research_counterpoint import prepare_counterpoint_prompt

baseline = json.loads((HERE.parent / '258-requested-facts-prompt-check' / 'manifest.json').read_text())
tasks = [item for item in json.loads((DATA / 'tasks.json').read_text()) if item['id'] == 'POLICY_FOMC_201706']
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
     'max_calls_per_task': 2, 'max_tokens_per_task': 20000, 'max_calls_total': 2, 'max_tokens_total': 20000,
     'timeout_seconds': 60, 'elapsed_seconds': 120, 'search': False, 'retries': 0,
     'previous_known_tokens': 321313, 'cost_usd': 'unknown'}
prior = HERE.parent / '272-cross-domain-review' / 'POLICY_FOMC_201706-generation-1-provider.json'
saved = json.loads(prior.read_text())
assert saved['payload'].get('counterarguments')
task, source, _ = load_task('POLICY_FOMC_201706')
prompt, _ = capture_prompt(task, source)
assert digest(prompt.encode()) == saved['prompt_sha256']
analysis = analyze(task['question'], [source], None, lambda _: (saved['payload'], saved['reported_tokens']),
                   max_prompt_chars=6000-len(SYSTEM_PROMPT.encode())-512)
assert analysis.prompt_sha256 == saved['prompt_sha256']
followup = prepare_counterpoint_prompt(prompt, analysis.claims, 6000-len(SYSTEM_PROMPT.encode())-512)
assert followup is not None
m['replayed_generation_sha256'] = digest(prior.read_bytes())
m['followup_prompt_sha256'] = followup.prompt_sha256
m['replayed_generation_tokens'] = saved['reported_tokens']
m['replayed_followup_sha256'] = digest((HERE.parent / '285-policy-counterpoint' / 'POLICY_FOMC_201706-counterpoint-2-provider.json').read_bytes())
m['live_model_calls'] = 0
(HERE / 'manifest.json').write_text(json.dumps(m, ensure_ascii=False, indent=2) + '\n')
