from common import *
import subprocess

baseline = json.loads((HERE.parent / '270-review-budget-comparison' / 'manifest.json').read_text())
task, source, raw = load_task('MACRO_GDP_2022Q4')
prompt, segments = capture_prompt(task, source)
assert digest(prompt.encode()) == baseline['conditions'][1]['prompt_sha256']
parsed = json.loads(prompt)
old_guidance = parsed['evidence_guidance']
parsed['evidence_guidance'] = ("Quote continuous passages, not metadata; cite table headers, units and rows. "
    "For each claim cite its period; omit unsupported specific years or quarters. "
    "Cite relevant counterevidence, else state the gap. "
    "Excerpts are partial; do not generalize absence to all sources. Check PDF columns. "
    "Dates are unverified. PDF p=[first,last] labels pages; cite source_page only for labeled PDF.")
candidate = json.dumps(parsed, ensure_ascii=False, separators=(',', ':'))
assert len(candidate.encode()) <= baseline['input_limit'] - len(SYSTEM_PROMPT.encode()) - 512
(HERE / 'baseline-prompt.json').write_text(prompt)
(HERE / 'candidate-prompt.json').write_text(candidate)
m = {'implementation_commit': subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
     'task': task['id'], 'source_sha256': digest(raw), 'text_sha256': source.content_sha256,
     'segments': segments, 'baseline': '../270-review-budget-comparison/budget_20000-generation-1-provider.json',
     'baseline_sha256': digest((HERE.parent / '270-review-budget-comparison/budget_20000-generation-1-provider.json').read_bytes()),
     'baseline_prompt_sha256': digest(prompt.encode()), 'candidate_prompt_sha256': digest(candidate.encode()),
     'change': 'replace evidence guidance only', 'old_guidance': old_guidance,
     'model': baseline['model'], 'generation_parameters': baseline['generation_parameters'],
     'system_sha256': digest(SYSTEM_PROMPT.encode()), 'input_limit':6000, 'output_limit':4000,
     'max_calls':1, 'max_tokens':10000, 'timeout_seconds':60, 'search':False, 'retries':0,
     'previous_known_tokens':297677, 'cost_usd':'unknown',
     'files':{name:digest((HERE/name).read_bytes()) for name in ['common.py','prepare.py','run.py']}}
(HERE/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
