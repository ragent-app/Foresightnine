"""Replay saved BEA output against the original prompt; no model call."""
from pathlib import Path
from datetime import datetime, timezone
from copy import deepcopy
from hashlib import sha256
import json, subprocess, sys, types
from app.research_sources import ResearchSource, SourcePermissions
p = Path('docs/product/phase-a/45-reserved-cross-domain-rerun')
manifest = json.loads((p / 'manifest.json').read_text())
task = json.loads((p / 'BEA_Q3-input.json').read_text())
old = json.loads((p / 'BEA_Q3-result.json').read_text())
provider = json.loads((p / 'BEA_Q3-provider-output.json').read_text())
raw = deepcopy(provider['payload'])
removed = 0
for field, key, limit in (('claims', 'status', 100), ('counterarguments', 'status', 100), ('calculations', 'note', 1000)):
    for row in raw.get(field, []):
        if isinstance(row, dict) and isinstance(row.get(key), str) and len(row[key]) <= limit:
            row.pop(key); removed += 1
assert removed == 3
old_code = subprocess.check_output(['git', 'show', manifest['implementation_commit'] + ':src/apps/api/app/research_analysis.py'], text=True)
module = types.ModuleType('app._metadata_replay'); module.__package__ = 'app'; sys.modules[module.__name__] = module
exec(compile(old_code, '<frozen>', 'exec'), module.__dict__)
text = (p / 'BEA_Q3-source.txt').read_text()
sid = next(iter(old['content']['run']['source_hashes']))
source = ResearchSource(source_id=sid, owner_id='test', title=task['source_title'], text=text, kind='user_supplied',
    obtained_at=datetime.now(timezone.utc), published_at=datetime.fromisoformat(task['published_at'].replace('Z', '+00:00')),
    content_sha256=sha256(text.encode()).hexdigest(), retrieval_method='user_pdf',
    permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))
result = module.analyze(task['question'], [source], None, lambda _: (raw, provider['reported_tokens']),
                        max_prompt_chars=3000 - len(module.SYSTEM_PROMPT.encode()) - 512)
assert result.prompt_sha256 == old['content']['run']['prompt_sha256']
out = {'kind': 'offline replay after dropping three known annotations; original frozen source selection; no model call or rescore',
       'prompt_sha256_unchanged': True, 'discarded_fields': removed, 'claims': result.claims,
       'counterarguments': result.counterarguments, 'facts': result.facts, 'calculations': result.calculations,
       'limitations': result.limitations}
print(json.dumps(out, ensure_ascii=False, indent=2))
