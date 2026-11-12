from pathlib import Path
from hashlib import sha256
from datetime import datetime, timezone
import json
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
SOURCE = HERE
sys.path.insert(0, str(ROOT / "apps/api"))
from app.research_analysis import analyze, SYSTEM_PROMPT  # noqa: E402
from app.research_sources import ResearchSource, SourcePermissions  # noqa: E402
from app.research_uploads import _extract_text  # noqa: E402


def digest(data):
    return sha256(data).hexdigest()


def get_task(name):
    task = next(t for t in json.loads((SOURCE / "tasks.json").read_text()) if t["id"] == name)
    raw = (SOURCE / task["source"]).read_bytes()
    text = _extract_text(raw, text_order="reading")
    source = ResearchSource(source_id=task["source_id"], owner_id="fresh-evaluation", title=task["source"], text=text,
        kind="user_supplied", obtained_at=datetime(2026, 11, 12, tzinfo=timezone.utc), published_at=datetime.fromisoformat(task["published_at"]),
        content_sha256=digest(text.encode()), retrieval_method="user_pdf", pdf_text_order="reading",
        permissions=SourcePermissions(store=True, display=True, model_input=True, export=True))
    return task, [source]


def run_analysis(task, sources, generate):
    return analyze(task["question"], sources, None, generate, max_prompt_chars=6000-len(SYSTEM_PROMPT.encode())-512)
