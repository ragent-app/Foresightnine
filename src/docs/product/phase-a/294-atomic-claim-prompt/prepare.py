from common import *
import subprocess

baseline = json.loads((HERE.parent / '292-new-source-quality' / 'manifest.json').read_text())
task, source, raw = load_task('BEA_2026Q1')
prompt, segments = capture_prompt(task, source)
assert digest(prompt.encode()) == baseline['conditions'][0]['prompt_sha256']
parsed = json.loads(prompt); old_guidance = parsed['evidence_guidance']
old = 'Put all requested facts in claims; put gaps in limitations.'
new = 'Put each fact in its own claim; quote its value, period and relationships. Put gaps in limitations.'
assert old in old_guidance
parsed['evidence_guidance'] = old_guidance.replace(old, new)
candidate = json.dumps(parsed, ensure_ascii=False, separators=(',', ':'))
assert len(candidate.encode()) <= baseline['input_limit'] - len(SYSTEM_PROMPT.encode()) - 512
(HERE / 'baseline-prompt.json').write_text(prompt)
(HERE / 'candidate-prompt.json').write_text(candidate)
baseline_path = HERE.parent / '292-new-source-quality' / 'BEA_2026Q1-generation-1-provider.json'
m = {'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
     'task':task['id'],'source_sha256':digest(raw),'text_sha256':source.content_sha256,'segments':segments,
     'baseline':'../292-new-source-quality/BEA_2026Q1-generation-1-provider.json',
     'baseline_sha256':digest(baseline_path.read_bytes()), 'baseline_prompt_sha256':digest(prompt.encode()),
     'candidate_prompt_sha256':digest(candidate.encode()), 'change':'one evidence-guidance sentence only',
     'old_guidance':old_guidance,'candidate_guidance':parsed['evidence_guidance'],
     'model':baseline['model'],'generation_parameters':baseline['generation_parameters'],
     'system_sha256':digest(SYSTEM_PROMPT.encode()),'input_limit':6000,'output_limit':4000,
     'max_calls':1,'max_tokens':10000,'timeout_seconds':60,'search':False,'retries':0,
     'previous_known_tokens':331509,'cost_usd':'unknown',
     'files':{name:digest((HERE/name).read_bytes()) for name in ['common.py','prepare.py','run.py','inspect_results.py']}}
(HERE/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
