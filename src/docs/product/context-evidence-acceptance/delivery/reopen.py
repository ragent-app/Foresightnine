"""Open a version finalized by the phase-2 replay via actual Web API routes."""
import json
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import main as api
from app.research_topics import ResearchContent, ResearchInput, ResearchTopics, VersionInput
from app.research_sources import ResearchSources, SourceInput
from app.storage import InMemoryDocumentRepository

HERE = Path(__file__).resolve().parent
frozen = json.loads((HERE.parent / 'evidence/frozen-cases.json').read_text())
content = json.loads((HERE.parent / 'evidence/replay-results.json').read_text())['F1']['persisted_content']
owner = 'offline'
sources = ResearchSources(InMemoryDocumentRepository())
topics = ResearchTopics(InMemoryDocumentRepository())
ids = []
for row in [frozen['financial'][k] for k in ('statement', 'increment_note')]:
    src = sources.create(owner, SourceInput(title=row['id'], text=row['text'], kind='user_supplied',
                         published_at=datetime.fromisoformat(row['published']).replace(tzinfo=timezone.utc),
                         permissions={'store': True, 'model_input': True, 'display': True, 'export': True}), source_id=row['id'])
    ids.append(src.source_id)
topic = topics.create(owner, ResearchInput(question=frozen['financial']['question'], as_of=frozen['as_of'], source_ids=ids))
topic = topics.add_version(owner, topic.topic_id, VersionInput(parent_version_id=topic.versions[0].version_id,
                           content=ResearchContent.model_validate(content), review='draft'), source_ids=ids)
version = topic.versions[-1].version_id
path = f'/v2/research/topics/{topic.topic_id}/versions/{version}'
note = sources.get(owner, ids[1])
rows = []
with ExitStack() as stack:
    stack.enter_context(patch.object(api, 'research_sources', sources))
    stack.enter_context(patch.object(api, 'research_topics', topics))
    stack.enter_context(patch.object(api, '_authenticated_account', lambda _: SimpleNamespace(account_id=owner)))
    client = TestClient(api.app)
    for case in ('available', 'export_revoked', 'display_revoked', 'source_revised', 'after_cutoff', 'restored'):
        current = note.model_copy(deep=True)
        if case == 'export_revoked':
            current.permissions.export = False
        if case == 'display_revoked':
            current.permissions.display = False
        if case == 'source_revised':
            from hashlib import sha256
            current.text += ' Revised.'
            current.content_sha256 = sha256(current.text.encode()).hexdigest()
        if case == 'after_cutoff':
            current.revised_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
        sources.repository[ids[1]] = current
        response = client.get(path)
        assert response.status_code == 200, response.text
        turn = response.json()['content']['turns'][-1]
        answers = [part['answer'] for part in turn['question_parts']]
        assert any('20.00' in ans for ans in answers)
        unchanged = case in ('available', 'export_revoked', 'restored')
        assert any('90.00' in ans for ans in answers) == unchanged
        original = client.get(path + '/sources/' + ids[1] + '/text')
        assert original.status_code == (200 if unchanged else 403)
        export = client.get(path + '/export')
        assert export.status_code == (200 if case in ('available', 'restored') else 403), export.text
        if export.status_code == 200:
            assert export.json()['version']['content']['turns'][-1]['question_parts'] == turn['question_parts']
        if not unchanged:
            assert frozen['financial']['increment_note']['text'] not in response.text
        metadata = client.get(path + '/export/metadata')
        assert metadata.status_code == 200 and frozen['financial']['increment_note']['text'] not in metadata.text
        rows.append({'case': case, 'version_endpoint': response.status_code, 'growth_visible': True,
                     'contribution_visible': unchanged, 'source_text': original.status_code,
                     'full_export': export.status_code, 'metadata_export': metadata.status_code})
print(json.dumps(rows, indent=2))
