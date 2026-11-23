"""Freeze existing prior-turn behavior before modifying context projection."""
import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from app.research_context_manager import _prior_research
from app.research_sources import ResearchSource, SourcePermissions

root = Path(__file__).resolve().parent
raw = (root.parent / 'evidence/frozen-cases.json').read_bytes()
fixture = json.loads(raw)
row = fixture['financial']['statement']
original = row['text']
digest = sha256(original.encode()).hexdigest()
prior = {'source_hashes': {row['id']: digest},
         'read_ranges': [{'source_id':row['id'], 'character_start':0, 'character_end':len(original)}],
         'facts':[{'fact_id':'fact-2025', 'label':'Revenue', 'value':120, 'unit':'USD million', 'currency':'USD', 'period':'FY2025', 'source_id':row['id'], 'quotation':original, 'character_start':0, 'character_end':len(original)}],
         'claims':[{'claim_id':'claim-1','role':'support','text':'FY2025 revenue was USD 120 million.', 'citations':[{'source_id':row['id'],'quotation':original,'character_start':0,'character_end':len(original)}]}]}
result={'fixture_sha256':sha256(raw).hexdigest(), 'cases':{}}
for name, text, allowed in [('unchanged', original, True), ('revoked',original,False), ('revised',original + ' Correction.',True)]:
    src=ResearchSource(source_id=row['id'], owner_id='fixture', title='Statement', text=text, kind='user_supplied', published_at=datetime(2026,2,1,tzinfo=timezone.utc), obtained_at=datetime(2026,3,1,tzinfo=timezone.utc), content_sha256=sha256(text.encode()).hexdigest(), permissions=SourcePermissions(store=True,model_input=allowed,display=True,export=True))
    summary, state = _prior_research(prior,[src],datetime(2026,4,1,tzinfo=timezone.utc),token_budget=5000)
    result['cases'][name]={'summary':summary, 'prior_state':state}
print(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2))
