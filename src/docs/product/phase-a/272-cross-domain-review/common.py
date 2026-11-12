from pathlib import Path
from hashlib import sha256
from datetime import datetime, timezone
import base64, gzip, json, sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
DATA = HERE.parent / '263-holdout-candidates'
sys.path.insert(0, str(ROOT / 'apps/api'))
from app.research_analysis import analyze, SYSTEM_PROMPT
from app.research_sources import ResearchSource, SourcePermissions
from app.research_uploads import HtmlUploadRequest, extract_uploaded_html, _extract_text

def digest(value):
    return sha256(value).hexdigest()

def load_task(name):
    task = next(item for item in json.loads((DATA / 'tasks.json').read_text()) if item['id'] == name)
    path = (HERE.parent / '60-reserved-full-review' / task['source']) if task['format'] == 'html' else DATA / task['source']
    raw = gzip.decompress(path.read_bytes()) if task['format'] == 'html' else path.read_bytes()
    permissions = SourcePermissions(store=True, display=True, model_input=True, export=True)
    if task['format'] == 'html':
        encoded = base64.b64encode(raw).decode()
        text, raw_hash = extract_uploaded_html(HtmlUploadRequest(title=task['source'], html_base64=encoded, permissions=permissions))
    else:
        encoded = None; raw_hash = digest(raw); text = _extract_text(raw, text_order='layout')
    source = ResearchSource(source_id=task['source_id'], owner_id='evaluation', title=task['source'], text=text,
        kind='user_supplied', obtained_at=datetime(2026, 11, 12, 19, 57, tzinfo=timezone.utc),
        published_at=datetime.fromisoformat(task['published_at']) if task['published_at'] else None,
        content_sha256=digest(text.encode()), retrieval_method='user_html' if encoded else 'user_pdf',
        pdf_text_order=None if encoded else 'layout', response_sha256=raw_hash,
        original_html_base64=encoded, permissions=permissions)
    return task, source, raw

def capture_prompt(task, source):
    prompts = []
    result = analyze(task['question'], [source], None, lambda prompt: (prompts.append(prompt) or {}, 0),
                     max_prompt_chars=6000 - len(SYSTEM_PROMPT.encode()) - 512)
    assert len(prompts) == 1
    return prompts[0], result.segments
