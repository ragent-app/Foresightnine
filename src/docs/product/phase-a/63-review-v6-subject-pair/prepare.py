"""Freeze prompts, inputs, configuration and narrow scoring before model calls."""
from pathlib import Path
from hashlib import sha256
import json,subprocess,sys
p=Path(__file__).resolve().parent;root=p.parents[3];sys.path.insert(0,str(root/'apps/api'))
from app.research_verification import REVIEW_SYSTEM_PROMPT, REVIEW_METHOD
from common import build,prior,replay
h=lambda b:sha256(b).hexdigest()
claims,batch,baseline,variant=build()
old=json.loads((root/'docs/product/phase-a/40-review-budget/manifest.json').read_text())
m={'purpose':'Nonblind paired development diagnostic of missing versus present original subject context for the same two saved claims. Both prompts use identical claim text, quotations, indexes and system instructions. This is not a full implementation comparison or independent acceptance.',
   'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
   'method':REVIEW_METHOD,'input_sha256':{'BEA_Q3-result.json':h((prior/'BEA_Q3-result.json').read_bytes()),'BEA_Q3-input.json':h((prior/'BEA_Q3-input.json').read_bytes()),'BEA_Q3-source.txt':h((prior/'BEA_Q3-source.txt').read_bytes()),'replay-result.json':h(replay.read_bytes())},
   'common_sha256':h((p/'common.py').read_bytes()),'runner_sha256':h((p/'run.py').read_bytes()),'prepare_sha256':h((p/'prepare.py').read_bytes()),
   'system_prompt_sha256':h(REVIEW_SYSTEM_PROMPT.encode()),'prompts_sha256':{'without_subject':h(baseline.encode()),'with_subject':h(variant.encode())},
   'max_prompt_bytes':1770,'claim_indexes':[0,1],'segments_with_subject':batch.segments,
   'model':old['model'],'generation_parameters':old['generation_parameters'],'max_calls_total':2,'max_tokens_per_call':10000,'max_tokens_total':20000,
   'timeout_seconds':60,'retry_external_calls':0,'cost_usd':'unknown',
   'scoring_rule':'Predefined narrow target: verdict for claim index 1 must be does_not_support, contradicts or uncertain; supports fails. Record index 0 and all outputs. One sample per condition, no general semantic or phase acceptance claim.'}
(p/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'prompt_hashes':m['prompts_sha256'],'lengths':[len(baseline.encode()),len(variant.encode())]},ensure_ascii=False))
