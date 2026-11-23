"""Reassess saved offline state under revocation and source revision."""
import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from app.research_delivery import assess, assemble_parts
from tests.test_research_runtime_attention import _source
ROOT=Path(__file__).resolve().parent
f=json.loads((ROOT/'frozen-cases.json').read_text())
r=json.loads((ROOT/'replay-results.json').read_text())['F1']
now=datetime(2026,4,1,tzinfo=timezone.utc)
sources={}
for key in ('statement','increment_note'):
    item=f['financial'][key]; src=_source(item['id'],item['text']); src.obtained_at=now; src.published_at=datetime(2026,2,1,tzinfo=timezone.utc); src.content_sha256=sha256(src.text.encode()).hexdigest(); sources[src.source_id]=src
state={'facts':r['runtime_facts'],'calculations':r['runtime_calculations'],'read_ranges':r['read_ranges']}
q=f['financial']['question']
def evaluate():
    rows=assess(q,state,sources,as_of=now,claims=r['runtime_claims'])
    ids={k for k,s in sources.items() if s.permissions.model_input}
    return assemble_parts(q,state,sources,ids,rows,r['runtime_claims'])
normal=evaluate()
sources['note-increment-v1'].permissions.model_input=False
revoked=evaluate()
sources['note-increment-v1'].permissions.model_input=True
sources['note-increment-v1'].text='Revised source; old quote no longer present.'
revised=evaluate()
output={'normal':normal,'revoked':revoked,'revised':revised}
(ROOT/'recheck-results.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
assert any('90.00' in x['answer'] for x in normal)
for group in (revoked,revised):
    assert not any('90.00' in x['answer'] for x in group)
    assert any('20.00' in x['answer'] for x in group)
print('normal: growth and contribution; revoked/revised: growth only')
