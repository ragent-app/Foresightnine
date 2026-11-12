from common import *
import subprocess

baseline_dir = HERE.parent / '306-counterpoint-relevance-preserved'
baseline = (baseline_dir / 'baseline-prompt.json').read_text()
data = json.loads(baseline)
generation_path = DATA / 'FOMC_202507-generation-1-provider.json'
generation = json.loads(generation_path.read_text())
claims = generation['payload']['claims']
assert len(claims) == 4 and len(data['previous_findings_untrusted']) == 2
assert all(item['text'] == claims[i]['text'] for i, item in enumerate([{'text':x} for x in data['previous_findings_untrusted']]))
data['previous_findings_untrusted'] = [item['text'][:60] for item in claims]
candidate = json.dumps(data, ensure_ascii=False, separators=(',',':'))
assert len(candidate.encode()) <= 6000 - len(SYSTEM_PROMPT.encode()) - 512
assert 'Prior findings may be truncated.' in data['counterpoint_request']
(HERE/'baseline-prompt.json').write_text(baseline)
(HERE/'candidate-prompt.json').write_text(candidate)
old = json.loads((baseline_dir/'manifest.json').read_text())
m = {'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
     'baseline_sha256':digest(baseline.encode()),'candidate_sha256':digest(candidate.encode()),
     'generation_sha256':digest(generation_path.read_bytes()),'system_sha256':digest(SYSTEM_PROMPT.encode()),
     'baseline_provider_sha256':old['baseline_sha256'],'model':old['model'],
     'generation_parameters':old['generation_parameters'],'output_limit':4000,'timeout_seconds':60,
     'max_tokens':10000,'max_calls':1,'previous_known_tokens':337502,'cost_usd':'unknown',
     'change':'previous_findings_untrusted: first two complete findings to first 60 characters of each of four findings',
     'files':{name:digest((HERE/name).read_bytes()) for name in ['common.py','prepare.py','run.py']}}
(HERE/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
