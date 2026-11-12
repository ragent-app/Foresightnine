from pathlib import Path
from hashlib import sha256
from datetime import datetime, timezone
import json,sys
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[3]
DATA=HERE.parent/'29-new-revision-evaluation'
sys.path.insert(0,str(ROOT/'apps/api'))
from app.research_analysis import analyze,SYSTEM_PROMPT
from app.research_sources import ResearchSource,SourcePermissions
from app.research_uploads import _extract_text

def digest(value): return sha256(value).hexdigest()

def inputs():
    original=json.loads((DATA/'manifest.json').read_text())
    question=json.loads((DATA/'input.json').read_text())['question']
    sources=[]
    for i,item in enumerate(original['sources']):
        raw=(DATA/item['pdf']).read_bytes()
        assert digest(raw)==item['pdf_sha256']
        text=_extract_text(raw,text_order='layout')
        assert digest(text.encode())==item['text_sha256']
        sources.append(ResearchSource(source_id='source_'+f'{i+1:032x}',owner_id='evaluation',title=item['title'],text=text,
            kind='user_supplied',obtained_at=datetime(2026,11,13,tzinfo=timezone.utc),
            published_at=datetime.fromisoformat(item['published_at'].replace('Z','+00:00')),
            content_sha256=digest(text.encode()),retrieval_method='user_pdf',pdf_text_order='layout',
            response_sha256=digest(raw),permissions=SourcePermissions(store=True,display=True,model_input=True,export=True)))
    return question,sources,original['sources']

def prompt_for(question,sources):
    prompts=[]
    result=analyze(question,sources,None,lambda prompt:(prompts.append(prompt) or {},0),
                   max_prompt_chars=6000-len(SYSTEM_PROMPT.encode())-512)
    assert len(prompts)==1
    return prompts[0],result.segments
