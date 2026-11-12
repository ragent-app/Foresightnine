import hashlib,json
from pathlib import Path
from datetime import datetime,timezone
from app.research_sources import ResearchSource,SourcePermissions
from app.research_analysis import analyze,SYSTEM_PROMPT
p=Path('docs/product/phase-a/46-selection-paired');task=json.loads((p/'NVDA_REV-input.json').read_text());old=json.loads((p/'NVDA_REV-result.json').read_text());provider=json.loads((p/'NVDA_REV-provider-output.json').read_text());meta=old['content']['sources'][0];text=task['source_text'];s=ResearchSource(source_id=meta['source_id'],owner_id='test',title=task['source_title'],text=text,kind='user_supplied',obtained_at=datetime.now(timezone.utc),content_sha256=hashlib.sha256(text.encode()).hexdigest(),permissions=SourcePermissions(store=True,model_input=True,display=True,export=True)); result=analyze(task['question'],[s],None,lambda _: (provider['payload'],provider['reported_tokens']),max_prompt_chars=3000-len(SYSTEM_PROMPT.encode())-512)
assert result.prompt_sha256==old['content']['run']['prompt_sha256']
out={'kind':'offline replay of saved model output; no model call, original score unchanged; no semantic certification','prompt_sha256_unchanged':True,'facts':[x for x in result.facts if x['fact_id'].startswith('revenue_')],'calculations':result.calculations,'limitations':result.limitations}
print(json.dumps(out,ensure_ascii=False,indent=2))
