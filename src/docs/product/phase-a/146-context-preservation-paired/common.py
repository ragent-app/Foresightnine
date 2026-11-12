from pathlib import Path
from hashlib import sha256
from datetime import datetime,timezone
import json,sys
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[3]
ORIGINAL=HERE.parent/'140-trade-bank-fresh'
sys.path.insert(0,str(ROOT/'apps/api'))
from app.research_analysis import analyze,SYSTEM_PROMPT
from app.research_sources import ResearchSource,SourcePermissions

def digest(data):return sha256(data).hexdigest()
def get_task(name):
 task=json.loads((ORIGINAL/(name+'-input.json')).read_text())
 source=task['source'];text=(ORIGINAL/source['text_file']).read_text()
 obj=ResearchSource(source_id=source['source_id'],owner_id='fresh-diagnostic',title=source['title'],text=text,
   kind='user_supplied',obtained_at=datetime(2026,11,12,tzinfo=timezone.utc),published_at=None,
   content_sha256=digest(text.encode()),retrieval_method='user_pdf' if source['kind']=='pdf' else 'user_supplied',
   pdf_text_order='reading' if source['kind']=='pdf' else None,permissions=SourcePermissions(store=True,display=True,model_input=True,export=True))
 return task,[obj]
def run_analysis(task,sources,generate):
 return analyze(task['question'],sources,None,generate,max_prompt_chars=3000-len(SYSTEM_PROMPT.encode())-512)
