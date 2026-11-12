import base64
import gzip
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from fastapi.testclient import TestClient
import app.main as api


def test_uploaded_html_is_private_and_preserves_provenance(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    client = TestClient(api.app)
    def account():
        response = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'})
        return {'Authorization': 'Bearer ' + response.json()['access_token']}
    owner, other = account(), account()
    raw = b'<html><ix:header><xbrl>SECRET METADATA</xbrl></ix:header><script>do not execute</script><table><tr><th>Revenue</th><th>2024</th></tr><tr><td>Total</td><td>100</td></tr></table></html>'
    payload = {'title': 'Financial report', 'html_base64': base64.b64encode(raw).decode(), 'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}}
    created = client.post('/v2/research/sources/html', headers=owner, json=payload)
    assert created.status_code == 201, created.text
    metadata = created.json()
    assert metadata['retrieval_method'] == 'user_html' and metadata['response_sha256'] == sha256(raw).hexdigest()
    assert 'text' not in metadata and 'original_html_base64' not in metadata and metadata['original_html_available']
    assert 'extraction_disclosure' in metadata
    sid = metadata['source_id']
    topic = client.post('/v2/research/topics', headers=owner, json={'question': 'Revenue?', 'source_ids': [sid]}).json()
    path = f"/v2/research/topics/{topic['topic_id']}/versions/{topic['versions'][0]['version_id']}"
    stored = client.get(path + f'/sources/{sid}/text', headers=owner)
    assert stored.status_code == 200
    text = stored.json()['text']
    assert 'Revenue' in text and '100' in text and '|' in text
    assert 'SECRET METADATA' not in text and 'do not execute' not in text
    assert client.get(path + f'/sources/{sid}/text', headers=other).status_code == 404
    listed = client.get(path + '/sources', headers=owner).json()[0]
    assert listed['extraction_disclosure'] and 'original_html_base64' not in listed
    download = client.get(f'/v2/research/sources/{sid}/original', headers=owner)
    assert download.status_code == 200 and download.content == raw
    assert download.headers['content-type'] == 'application/octet-stream'
    assert download.headers['content-disposition'].startswith('attachment;')
    assert download.headers['x-content-type-options'] == 'nosniff'
    assert client.get(f'/v2/research/sources/{sid}/original', headers=other).status_code == 404
    restricted = client.post('/v2/research/sources/html', headers=owner, json={**payload, 'permissions': {'store': True, 'display': True}}).json()
    assert client.get(f"/v2/research/sources/{restricted['source_id']}/original", headers=owner).status_code == 403
    for data in (b'\xff\xfe', b'<ix:header>unclosed', b''):
        bad = {**payload, 'html_base64': base64.b64encode(data).decode()}
        assert client.post('/v2/research/sources/html', headers=owner, json=bad).status_code == 422
    assert client.post('/v2/research/sources/html', headers=owner, json={**payload, 'permissions': {'store': False}}).status_code == 422


def test_original_cisco_filing_can_be_extracted_with_bounded_upload(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    path = Path(__file__).resolve().parents[3] / 'docs/product/phase-a/60-reserved-full-review/cisco-2024-sec.html.gz'
    raw = gzip.decompress(path.read_bytes())
    client = TestClient(api.app)
    account = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test',
        'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + account['access_token']}
    result = client.post('/v2/research/sources/html', headers=headers, json={'title': 'Cisco',
        'html_base64': base64.b64encode(raw).decode(), 'permissions': {'store': True, 'display': True, 'model_input': True}})
    assert result.status_code == 201, result.text
    assert len(raw) < 5_000_000 and result.json()['response_sha256'] == sha256(raw).hexdigest()
    source_id = result.json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source_id]}).json()
    path = f"/v2/research/topics/{topic['topic_id']}/versions/{topic['versions'][0]['version_id']}/sources/{source_id}/text"
    text = client.get(path, headers=headers).json()['text']
    assert '53,803' in text and '56,998' in text and 'Total revenue' in text
