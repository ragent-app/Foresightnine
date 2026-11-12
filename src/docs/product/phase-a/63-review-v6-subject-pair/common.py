"""Rebuild the two fixed prompts for the same saved research claims."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
from app.research_sources import ResearchSource, SourcePermissions
from app.research_verification import prepare_support_review
root=Path(__file__).resolve().parents[4]
prior=root/'docs/product/phase-a/45-reserved-cross-domain-rerun'
replay=root/'docs/product/phase-a/50-extra-metadata-replay/result.json'
h=lambda b:sha256(b).hexdigest()

def build():
    original=json.loads((prior/'BEA_Q3-result.json').read_text()); task=json.loads((prior/'BEA_Q3-input.json').read_text())
    text=(prior/'BEA_Q3-source.txt').read_text(); sid=next(iter(original['content']['run']['source_hashes']))
    source=ResearchSource(source_id=sid,owner_id='test',title=task['source_title'],text=text,kind='user_supplied',
        obtained_at=datetime.now(timezone.utc),content_sha256=h(text.encode()),retrieval_method='user_pdf',
        permissions=SourcePermissions(store=True,display=True,model_input=True))
    claims=json.loads(replay.read_text())['claims']
    batch=prepare_support_review(claims,[source],1770)
    assert batch.indexes==[0,1] and not batch.omitted
    variant=json.loads(batch.prompt); baseline=deepcopy(variant)
    targeted=next(item for item in baseline['claims'] if item['claim_index']==1)['excerpts'][0]
    original_excerpt=next(item for item in variant['claims'] if item['claim_index']==1)['excerpts'][0]
    assert 'personal saving rate' in original_excerpt['preceding_context'].lower()
    quotation=targeted['quotation'];targeted.update(preceding_context='',following_context='')
    assert 'personal saving rate' not in (targeted['preceding_context'] + targeted['quotation'] + targeted['following_context']).lower()
    for key in ('source_id','quotation'):
        assert targeted[key]==original_excerpt[key]
    assert baseline['claims'][0]==variant['claims'][0] and baseline['output_schema']==variant['output_schema']
    encode=lambda value:json.dumps(value,ensure_ascii=False,separators=(',',':'))
    baseline,variant=encode(baseline),encode(variant)
    assert len(baseline.encode())<=1770 and len(variant.encode())<=1770
    return claims,batch,baseline,variant
