from pathlib import Path
from hashlib import sha256
import json, sys

HERE=Path(__file__).resolve().parent
experiment=HERE.parent/'272-cross-domain-review'
sys.path.insert(0,str(experiment))
from common import load_task, digest
from app.research_evidence import flag_uncited_causal_attribution

path=experiment/'POLICY_FOMC_201706-result.json'
manifest=json.loads((experiment/'manifest.json').read_text())
condition=next(x for x in manifest['conditions'] if x['task']=='POLICY_FOMC_201706')
task,source,raw=load_task(condition['task'])
assert digest(raw)==condition['source_sha256'] and source.content_sha256==condition['text_sha256']
content=json.loads(path.read_text())['content']
claims=flag_uncited_causal_attribution(content['claims'],[source])
result={'model_calls':0,'result_sha256':sha256(path.read_bytes()).hexdigest(),
        'source_sha256':condition['source_sha256'],
        'flagged_claim_indexes':[i for i,c in enumerate(claims) if c.get('causal_attribution_unverified')],
        'meaning':'The warning identifies an uncited cue; it does not establish the correct motive.'}
(HERE/'replay.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
