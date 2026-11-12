from pathlib import Path
from hashlib import sha256
from datetime import datetime, timezone
import json, sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / 'apps/api'))
from app.research_analysis import analyze, SYSTEM_PROMPT
from app.research_sources import ResearchSource, SourcePermissions

def digest(value):
    return sha256(value).hexdigest()

def get_task(name):
    task = json.loads((HERE / (name + '-input.json')).read_text())
    objects = []
    for item in task['sources']:
        content = (HERE / item['text_file']).read_text() if item['kind'] == 'pdf' else item['text']
        objects.append(ResearchSource(source_id=item['source_id'], owner_id='paired-test', title=item['title'],
            text=content, kind='user_supplied', obtained_at=datetime(2026, 11, 12, tzinfo=timezone.utc),
            published_at=datetime.fromisoformat(item['published_at'].replace('Z', '+00:00')),
            content_sha256=digest(content.encode()), retrieval_method='user_pdf' if item['kind']=='pdf' else 'user_supplied',
            pdf_text_order='reading' if item['kind']=='pdf' else None,
            permissions=SourcePermissions(store=True, display=True, model_input=True, export=True)))
    return task, objects

def run_analysis(task, objects, budget, generate):
    return analyze(task['question'], objects, None, generate,
                   max_prompt_chars=budget-len(SYSTEM_PROMPT.encode())-512)
