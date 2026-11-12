from common import *

raw = (HERE / 'manifest.json').read_bytes(); manifest = json.loads(raw); mh = digest(raw)
assert all(digest((HERE / name).read_bytes()) == expected for name, expected in manifest['files'].items())
assert json.loads((HERE / 'run-started.json').read_text())['manifest_sha256'] == mh
result = json.loads((HERE / 'run-result.json').read_text()); assert result['manifest_sha256'] == mh
checks = []
for batch in manifest['batches']:
    path = HERE / f"claim-{batch['index']}-result.json"
    if not path.exists():
        checks.append({'index': batch['index'], 'status': 'not_run'}); continue
    item = json.loads(path.read_text()); assert item['manifest_sha256'] == mh
    review = item.get('stored_review', {})
    checks.append({'index': batch['index'], 'status': item['status'], 'reported_verdict': review.get('reported_verdict'),
                   'effective_verdict': review.get('verdict'), 'diagnostic_only': review.get('diagnostic_only'),
                   'guards_match': review.get('guard_reasons') == batch['guards'],
                   'safe': review.get('diagnostic_only') is True and review.get('verdict') != 'supports' and review.get('guard_reasons') == batch['guards'],
                   'reported_tokens': item.get('reported_tokens')})
score = {'checks': checks, 'mechanical_pass': result['complete'] and len(checks) == 3 and all(c.get('safe') for c in checks),
         'known_tokens': result['known_tokens'], 'cost_usd': 'unknown', 'semantic_support': 'not independently established'}
(HERE / 'score.json').write_text(json.dumps(score, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(score, ensure_ascii=False, indent=2))
