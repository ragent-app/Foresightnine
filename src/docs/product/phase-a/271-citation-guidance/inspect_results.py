from common import *
from app.research_evidence import flag_partial_source_absence, flag_uncited_quarters

m = json.loads((HERE/'manifest.json').read_text())
task, source, raw = load_task(m['task'])
assert digest(raw) == m['source_sha256'] and source.content_sha256 == m['text_sha256']
results = []
for case, path in [('baseline', HERE/m['baseline']), ('candidate', HERE/'candidate-provider.json')]:
    saved = json.loads(path.read_text())
    assert isinstance(saved.get('payload'), dict)
    result = analyze(task['question'], [source], None, lambda _: (saved['payload'], saved['reported_tokens']),
                     max_prompt_chars=m['input_limit'] - len(SYSTEM_PROMPT.encode()) - 512)
    assert result.segments == m['segments']
    claims = [{**claim, 'role':'support'} for claim in result.claims]
    claims += [{**claim, 'role':'counterargument'} for claim in result.counterarguments]
    claims = flag_uncited_quarters(claims,[source])
    claims = flag_partial_source_absence(claims,[source],result.segments)
    results.append({'case':case, 'reported_tokens':saved['reported_tokens'],
                    'claims':len(claims),
                    'cited_counterarguments':sum(1 for c in claims if c['role']=='counterargument' and c.get('citations')),
                    'quarter_warning_claims':[i for i,c in enumerate(claims) if c.get('uncited_quarters')],
                    'full_source_absence_warning_claims':[i for i,c in enumerate(claims) if c.get('source_scope_absence_unverified')],
                    'limitations':result.limitations,
                    'semantic_support':'requires source-level assessment'})
(HERE/'inspection.json').write_text(json.dumps({'results':results,'model_calls':0},ensure_ascii=False,indent=2)+'\n')
print(json.dumps(results,ensure_ascii=False,indent=2))
