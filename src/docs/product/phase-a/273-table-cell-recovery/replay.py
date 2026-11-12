from pathlib import Path
import json, sys

HERE = Path(__file__).resolve().parent
EXPERIMENT = HERE.parent / '272-cross-domain-review'
sys.path.insert(0, str(EXPERIMENT))
from common import load_task, analyze, SYSTEM_PROMPT, digest
from app.research_calculations import xbrl_source_consistent

manifest = json.loads((EXPERIMENT/'manifest.json').read_text())
case = next(item for item in manifest['conditions'] if item['task'] == 'COMPANY_CISCO_2024')
task, source, raw = load_task(case['task'])
assert digest(raw) == case['source_sha256'] and source.content_sha256 == case['text_sha256']
provider_path = EXPERIMENT/'COMPANY_CISCO_2024-generation-1-provider.json'
provider = json.loads(provider_path.read_text())
assert provider['prompt_sha256'] == case['prompt_sha256']
result = analyze(task['question'], [source], None, lambda _: (provider['payload'], provider['reported_tokens']),
                 max_prompt_chars=manifest['input_limit'] - len(SYSTEM_PROMPT.encode()) - 512)
assert result.prompt_sha256 == case['prompt_sha256'] and result.segments == case['segments']
summary = {'source_sha256':case['source_sha256'], 'provider_output_sha256':digest(provider_path.read_bytes()),
           'model_calls':0, 'facts':[{key:fact.get(key) for key in
               ('fact_id','value','period','quotation_match','verification','table_structure_alignment')} |
               {'source_tag_consistent':xbrl_source_consistent(fact)} for fact in result.facts],
           'calculations':result.calculations,
           'claim_count':len(result.claims), 'counterargument_count':len(result.counterarguments),
           'limitations':result.limitations}
(HERE/'replay.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'facts':len(result.facts),'calculations':[(c['value'],c['unit'],c['input_status']) for c in result.calculations],
                  'counterarguments':len(result.counterarguments),'model_calls':0},ensure_ascii=False))
