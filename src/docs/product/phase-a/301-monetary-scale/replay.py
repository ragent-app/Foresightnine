from pathlib import Path
import json,sys
here=Path(__file__).resolve().parent
experiment=here.parent/'276-financial-context-run'
sys.path.insert(0,str(experiment))
from common import load_task, analyze, SYSTEM_PROMPT, digest
from app.research_calculations import xbrl_source_consistent
m=json.loads((experiment/'manifest.json').read_text());case=m['conditions'][0]
task,source,raw=load_task(case['task']);provider_path=experiment/'COMPANY_CISCO_2024-generation-1-provider.json'
provider=json.loads(provider_path.read_text())
assert digest(raw)==case['source_sha256'] and source.content_sha256==case['text_sha256']
assert provider['prompt_sha256']==case['prompt_sha256']
result=analyze(task['question'],[source],None,lambda _:(provider['payload'],provider['reported_tokens']),
    max_prompt_chars=m['input_limit']-len(SYSTEM_PROMPT.encode())-512)
assert result.prompt_sha256==case['prompt_sha256'] and result.segments==case['segments']
out={'model_calls':0,'source_sha256':case['source_sha256'],'provider_sha256':digest(provider_path.read_bytes()),
     'facts':[{'fact_id':f['fact_id'],'currency':f.get('currency'),'scale_quote_missing':bool(f.get('scale_quote_missing')),
               'currency_quote_missing':bool(f.get('currency_quote_missing')),'source_tag_consistent':xbrl_source_consistent(f)} for f in result.facts],
     'calculations':result.calculations,
     'note':'Source-tag consistency is not independent verification of filing accuracy or semantic support.'}
(here/'replay.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'model_calls':0,'facts':out['facts'],'calculations':[(c['value'],c['input_status']) for c in out['calculations']]},ensure_ascii=False,indent=2))
