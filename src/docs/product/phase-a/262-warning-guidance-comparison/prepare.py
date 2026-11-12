from common import *
import subprocess
from app.research_verification import REVIEW_METHOD, REVIEW_SYSTEM_PROMPT, prepare_isolated_support_reviews

prior = HERE.parent / '258-requested-facts-prompt-check'
claims_file = prior / 'GDP_COUNTER_2024-result.json'
claims = json.loads(claims_file.read_text())['content']['claims']
_, sources = get_task('GDP_COUNTER_2024')
baseline = json.loads((prior / 'manifest.json').read_text())
assert sources[0].content_sha256 == baseline['conditions'][0]['source_hashes'][sources[0].source_id]
budget = 6000 - len(REVIEW_SYSTEM_PROMPT.encode()) - 512
plan = prepare_isolated_support_reviews(claims, sources, budget)
assert not plan.omissions and len(plan.batches) == 3
manifest = {
    'implementation_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
    'claims_sha256': digest(claims_file.read_bytes()), 'source_sha256': digest((SOURCE / 'bea-gdp1q24.pdf').read_bytes()),
    'source_text_sha256': sources[0].content_sha256, 'model': baseline['model'],
    'generation_parameters': baseline['generation_parameters'], 'review_method': REVIEW_METHOD,
    'system_sha256': digest(REVIEW_SYSTEM_PROMPT.encode()), 'review_input_limit': 6000,
    'max_calls': 3, 'max_total_tokens': 12000, 'timeout_seconds': 60, 'elapsed_seconds': 120,
    'search': False, 'retries': 0, 'cost_usd': 'unknown', 'previous_known_tokens': 275516,
    'files': {name: digest((HERE / name).read_bytes()) for name in ['common.py', 'prepare.py', 'run.py', 'score.py']},
    'batches': [{'index': b.indexes[0], 'prompt_sha256': b.prompt_sha256,
                 'prompt_bytes': len(b.prompt.encode()), 'guards': b.guarded[b.indexes[0]],
                 'segments': b.segments} for b in plan.batches],
}
baseline = json.loads((HERE.parent / '260-guarded-review-live' / 'manifest.json').read_text())
for key in ('claims_sha256', 'source_sha256', 'source_text_sha256', 'model', 'generation_parameters', 'system_sha256', 'review_input_limit', 'max_calls', 'max_total_tokens'):
    assert manifest[key] == baseline[key], key
for current, previous in zip(manifest['batches'], baseline['batches']):
    assert current['index'] == previous['index'] and current['guards'] == previous['guards']
    assert current['segments'] == previous['segments'] and current['prompt_sha256'] != previous['prompt_sha256']
(HERE / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
