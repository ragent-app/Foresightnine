"""Predefined narrow checks; never declare semantic support verified."""
import hashlib, json
from pathlib import Path
p = Path(__file__).resolve().parent
h = lambda b: hashlib.sha256(b).hexdigest()
manifest_bytes = (p / 'manifest.json').read_bytes(); m = json.loads(manifest_bytes)
assert json.loads((p/'started.json').read_text())['manifest_sha256'] == h(manifest_bytes)
for name in ('input.json', 'run.py', 'score.py'):
    assert h((p/name).read_bytes()) == m['sha256'][name]
text = json.loads((p/'input.json').read_text())['source_text']
report = {'manifest_sha256': h(manifest_bytes), 'blind': False,
          'semantic_support': 'not independently verified', 'variants': {}}
for variant in ('baseline', 'counterevidence_guidance'):
    file = p / (variant+'-result.json')
    if not file.exists():
        report['variants'][variant] = {'status': 'not_run', 'mechanical_pass': False}; continue
    output = json.loads(file.read_text()); result = output.get('result', {})
    counters = result.get('counterarguments', [])
    located = []
    for c in counters:
        qs = []
        for quote in c.get('citations', []):
            loc = quote.get('location', {}); a = loc.get('character_start'); b = loc.get('character_end')
            if isinstance(a, int) and isinstance(b, int) and text[a:b] == quote.get('quotation'):
                qs.append(quote['quotation'])
        if qs: located.append({'text': c.get('text'), 'quotations': qs})
    # The source must contain both reported gross-profit figures. This is a
    # retrieval check, not a determination that the prose is logically sound.
    has_counter_values = any(all(v in ' '.join(c['quotations']) for v in ('17660', '17450')) for c in located)
    report['variants'][variant] = {'status': output['status'], 'located_counterarguments': located,
        'one_counterargument_cites_both_gross_profit_values': has_counter_values,
        'research_has_limitations': bool(result.get('limitations')),
        'mechanical_pass': output['status'] == 'returned' and has_counter_values,
        'reported_tokens': output.get('reported_tokens'), 'cost_usd': None}
(p/'score.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
print(json.dumps(report, ensure_ascii=False, indent=2))
