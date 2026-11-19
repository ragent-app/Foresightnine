from fastapi.testclient import TestClient

from app.main import app
import app.main as api
from app.research_fetch import FetchedText


def test_source_permissions_and_account_boundary(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)

    def auth(email):
        response = client.post("/v1/auth/local/register", json={"email": email,
            "password": "correct horse battery staple!"})
        assert response.status_code == 201
        return {"Authorization": "Bearer " + response.json()["access_token"]}

    first, second = auth("first@example.test"), auth("second@example.test")
    payload = {"title": "Company report", "text": "Revenue was 100.",
               "permissions": {"store": False}}
    assert client.post("/v2/research/sources", headers=first, json=payload).status_code == 422
    payload["permissions"]["store"] = True
    created = client.post("/v2/research/sources", headers=first, json=payload)
    assert created.status_code == 201, created.text
    source = created.json()
    assert "text" not in source
    assert source["verification"] == "user_declared_not_verified"
    path = "/v2/research/sources/" + source["source_id"]
    assert client.get(path, headers=first).status_code == 200
    assert client.get(path, headers=second).status_code == 404
    assert client.post("/v2/research/topics", headers=second,
                       json={"source_ids": [source["source_id"]]}).status_code == 404
    topic = client.post("/v2/research/topics", headers=first,
                        json={"source_ids": [source["source_id"]]})
    assert topic.status_code == 201
    data = topic.json()
    export = f"/v2/research/topics/{data['topic_id']}/versions/{data['versions'][0]['version_id']}/export"
    assert client.get(export, headers=first).status_code == 403
    assert client.get(export, headers=second).status_code == 404
    assert client.delete("/v1/auth/local/account", headers=first).status_code == 200


def test_official_fetch_keeps_provenance_and_declared_permissions(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.setattr(api, "_reserve_search_call", lambda request: None)
    monkeypatch.setattr(api, "fetch_official_text", lambda request: FetchedText("GDP was 2.5%.", "a" * 64, "text/html"))
    client = TestClient(app)
    registered = client.post("/v1/auth/local/register", json={"email": "reader@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    fetched = client.post("/v2/research/sources/fetch", headers=headers, json={
        "title": "BEA release", "url": "https://www.bea.gov/news/example",
        "event_at": "2023-12-31T00:00:00Z", "published_at": "2024-01-01T00:00:00Z", "is_simulated": True,
        "revised_at": "2024-02-01T00:00:00Z",
        "permissions": {"store": True, "display": True}})
    assert fetched.status_code == 201, fetched.text
    data = fetched.json()
    assert data["retrieval_method"] == "official_html"
    assert "span markers" in data["extraction_disclosure"]
    assert data["is_simulated"] is True
    assert data["response_sha256"] == "a" * 64
    assert data["verification"] == "user_declared_not_verified"
    assert data["event_at"].startswith("2023-12-31")
    assert data["published_at"].startswith("2024-01-01")
    assert data["revised_at"].startswith("2024-02-01")
    assert data["permissions"]["model_input"] is False
    assert "GDP was 2.5%" in data["text"]
    invalid = client.post("/v2/research/sources/fetch", headers=headers, json={"title": "BEA release",
        "url": "https://www.bea.gov/news/example", "revised_at": "2024-02-01T00:00:00",
        "permissions": {"store": True}})
    assert invalid.status_code == 422


def test_metadata_export_omits_all_research_and_source_text(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    def auth(email):
        response = client.post("/v1/auth/local/register", json={"email": email, "password": "correct horse battery staple!"})
        return {"Authorization": "Bearer " + response.json()["access_token"]}
    first, second = auth("metadata-first@example.test"), auth("metadata-second@example.test")
    source = client.post("/v2/research/sources", headers=first, json={"title": "SECRET_TITLE",
        "text": "SECRET_TEXT 100", "published_at": "2022-01-01T00:00:00Z",
        "permissions": {"store": True, "display": True, "export": False}}).json()
    topic = client.post("/v2/research/topics", headers=first, json={"question": "SECRET_QUESTION",
        "source_ids": [source["source_id"]], "as_of": "2023-01-01T00:00:00Z"}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    saved = client.post(path + "/versions", headers=first, json={"parent_version_id": topic["versions"][0]["version_id"],
        "note": "SECRET_NOTE", "content": {"summary": "SECRET_SUMMARY",
            "facts": [{"fact_id": "f", "label": "SECRET_LABEL", "value": "100", "unit": "USD", "period": "2023",
                       "source_id": source["source_id"], "quotation": "SECRET_TEXT 100", "character_start": 0, "character_end": 15,
                       "location": {"character_start": 0, "character_end": 15}, "verification": "numeric_and_location_match_semantics_not_verified"}],
            "claims": [{"text": "SECRET_CLAIM", "role": "support", "citations": [{"source_id": source["source_id"], "quotation": "SECRET_TEXT 100",
                        "location": {"character_start": 0, "character_end": 15}}]}]}})
    assert saved.status_code == 201, saved.text
    version_id = saved.json()["versions"][-1]["version_id"]
    full = path + "/versions/" + version_id + "/export"
    assert client.get(full, headers=first).status_code == 403
    report = client.get(full + "/metadata", headers=first)
    assert report.status_code == 200 and report.headers["cache-control"] == "no-store"
    assert report.json()["format"] == "metadata_only"
    assert report.json()["tracked_source_count"] == 1
    assert report.json()["fact_count"] == 1 and report.json()["claim_count"] == 1
    assert "SECRET_" not in report.text and source["source_id"] not in report.text
    assert client.get(full + "/metadata", headers=second).status_code == 404
    assert client.get(full + "/metadata").status_code == 401


def test_authorized_full_export_contains_provenance_without_original_or_url_query(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    registration = client.post("/v1/auth/local/register", json={"email": "full-export@example.test",
        "password": "correct horse battery staple!"})
    token = registration.json()["access_token"]
    headers = {"Authorization": "Bearer " + token}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Public report",
        "url": "https://example.org/report?token=SECRET_QUERY", "text": "Revenue was 100. SECRET_REST",
        "published_at": "2023-01-01T00:00:00Z", "permissions": {"store": True, "display": True, "export": True}}).json()
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?",
        "as_of": "2023-01-02T00:00:00Z", "source_ids": [source["source_id"]]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": topic["versions"][0]["version_id"],
        "review": "confirmed", "content": {"summary": "Research result", "claims": [{"text": "Revenue was 100", "role": "support",
          "citations": [{"source_id": source["source_id"], "quotation": "Revenue was 100.",
                         "location": {"character_start": 0, "character_end": 16}}]}]}}).json()
    version = saved["versions"][-1]
    before = datetime.now(timezone.utc)
    response = client.get(path + "/versions/" + version["version_id"] + "/export", headers=headers)
    after = datetime.now(timezone.utc)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["schema_version"] == "1" and result["format"] == "research_json"
    assert before <= datetime.fromisoformat(result["generated_at"].replace("Z", "+00:00")) <= after
    assert result["version"]["review"] == "confirmed" and result["request"]["as_of"] is not None
    assert result["version"]["content"]["claims"][0]["citations"][0]["quotation"] == "Revenue was 100."
    provenance = result["source_provenance"][0]
    assert provenance["source_id"] == source["source_id"] and provenance["content_sha256"] == source["content_sha256"]
    assert provenance["published_at"] is not None and provenance["obtained_at"] is not None
    assert provenance["url"] == "https://example.org/report"
    assert "SECRET_QUERY" not in response.text and "SECRET_REST" not in response.text
    assert token not in response.text and "owner_id" not in response.text and "original_pdf_base64" not in response.text


def test_official_html_extraction_disclosure_is_retained_in_version_and_export(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.setattr(api, "_reserve_search_call", lambda request: None)
    monkeypatch.setattr(api, "fetch_official_text", lambda request: FetchedText("| Year | 2024 |", "a" * 64, "text/html"))
    client = TestClient(app)
    registration = client.post("/v1/auth/local/register", json={"email": "html-disclosure@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    source = client.post("/v2/research/sources/fetch", headers=headers, json={"title": "BEA table",
        "url": "https://www.bea.gov/news/example", "permissions": {"store": True, "display": True, "export": True}}).json()
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Year?", "source_ids": [source["source_id"]]}).json()
    path = f"/v2/research/topics/{topic['topic_id']}/versions/{topic['versions'][0]['version_id']}"
    metadata = client.get(path + "/sources", headers=headers).json()[0]
    exported = client.get(path + "/export", headers=headers).json()["source_provenance"][0]
    assert "span markers" in metadata["extraction_disclosure"]
    assert exported["extraction_disclosure"] == metadata["extraction_disclosure"]


def test_source_context_uses_unicode_offsets_and_enforces_display_ownership(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    def auth(email):
        response = client.post("/v1/auth/local/register", json={"email": email, "password": "correct horse battery staple!"})
        return {"Authorization": "Bearer " + response.json()["access_token"]}
    first, second = auth("context-a@example.test"), auth("context-b@example.test")
    text = "前" * 350 + "📈 Revenue 100" + "后" * 350
    item = client.post("/v2/research/sources", headers=first, json={"title": "Report", "text": text,
        "permissions": {"store": True, "display": True, "export": False}}).json()
    path = "/v2/research/sources/" + item["source_id"] + "/context"
    start, end = 350, 350 + len("📈 Revenue 100")
    context = client.get(path, headers=first, params={"start": start, "end": end})
    assert context.status_code == 200 and context.headers["cache-control"] == "no-store"
    data = context.json()
    assert data["selected"] == "📈 Revenue 100" and data["before"] == "前" * 300 and data["after"] == "后" * 300
    assert data["content_sha256"] == item["content_sha256"]
    full_path = "/v2/research/sources/" + item["source_id"]
    full = client.get(full_path, headers=first)
    assert full.status_code == 200 and full.json()["text"] == text and full.headers["cache-control"] == "no-store"
    assert client.get(full_path, headers=second).status_code == 404
    assert client.get(full_path).status_code == 401
    assert client.get(path, headers=second, params={"start": start, "end": end}).status_code == 404
    assert client.get(path, params={"start": start, "end": end}).status_code == 401
    assert client.get(path, headers=first, params={"start": end, "end": start}).status_code == 422
    assert client.get(path, headers=first, params={"start": 0, "end": 1100}).status_code == 422
    hidden = client.post("/v2/research/sources", headers=first, json={"title": "Private", "text": "Secret",
        "permissions": {"store": True, "display": False}}).json()
    assert "text" not in client.get("/v2/research/sources/" + hidden["source_id"], headers=first).json()
    assert client.get("/v2/research/sources/" + hidden["source_id"] + "/context", headers=first,
                      params={"start": 0, "end": 6}).status_code == 403


def test_html_table_context_preserves_offsets_and_permissions(monkeypatch):
    import base64
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    def auth(email):
        response = client.post("/v1/auth/local/register", json={"email": email, "password": "correct horse battery staple!"})
        return {"Authorization": "Bearer " + response.json()["access_token"]}
    first, other = auth("table-context-a@example.test"), auth("table-context-b@example.test")
    html = '<p>In millions</p><table><tr><th>Year ended</th><th>2024</th><th>2023</th></tr><tr><td>Revenue</td><td>100</td><td>90</td></tr></table>'
    upload = client.post('/v2/research/sources/html', headers=first, json={"title": "Report",
        "html_base64": base64.b64encode(html.encode()).decode(), "permissions": {"store": True, "display": True}})
    assert upload.status_code == 201, upload.text
    item = upload.json()
    text = client.get('/v2/research/sources/' + item['source_id'], headers=first).json()['text']
    start = text.index('Revenue')
    path = '/v2/research/sources/' + item['source_id'] + '/context'
    response = client.get(path, headers=first, params={"start": start, "end": start + len('Revenue')})
    assert response.status_code == 200
    table = response.json()['table_context']
    assert 'Year ended' in table['before'] and '2024' in table['before']
    assert table['before'] + table['selected'] + table['after'] == text[table['context_start']:table['context_end']]
    assert not table['truncated_before']
    assert client.get(path, headers=other, params={"start": start, "end": start + 7}).status_code == 404
    structure_path = '/v2/research/sources/' + item['source_id'] + '/table-structure'
    structure = client.get(structure_path, headers=first, params={"start": start, "end": start + 7})
    assert structure.status_code == 200 and structure.json()['status'] == 'available'
    assert structure.json()['rows'][-1]['cells'][0]['text'] == 'Revenue'
    assert client.get(structure_path, headers=other, params={"start": start, "end": start + 7}).status_code == 404
    assert client.get(structure_path, headers=first, params={"start": start, "end": start + 10000}).status_code == 422


def test_table_context_bounded_and_does_not_cross_narrative():
    from app.research_sources import table_context_window
    text = 'Unrelated\n' + ''.join(f'| row {i} | {i} |\n' for i in range(30)) + '| target | 123 |\nAfter'
    start = text.index('target')
    context = table_context_window(text, start, start + len('target'))
    assert context is not None and context['truncated_before']
    assert len(context['before'] + context['selected'] + context['after']) <= 10_000
    assert 'Unrelated' not in context['before'] and '| row 0 |' not in context['before']
    assert table_context_window('ordinary prose', 0, 8) is None
    separate = '| old | 1 |\nNarrative\n| new | 2 |\n'
    pos = separate.index('new')
    assert table_context_window(separate, pos, pos + 3) is None


def test_attach_source_preserves_prior_version_and_rejects_cross_account(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    def auth(email):
        response = client.post("/v1/auth/local/register", json={"email": email,
            "password": "correct horse battery staple!"})
        return {"Authorization": "Bearer " + response.json()["access_token"]}
    first, second = auth("one@example.test"), auth("two@example.test")
    topic = client.post("/v2/research/topics", headers=first, json={"question": "What changed?"}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    parent = topic["versions"][-1]["version_id"]
    source = client.post("/v2/research/sources", headers=first, json={"title": "Evidence", "text": "An observation.",
        "permissions": {"store": True, "display": True}}).json()
    other = client.post("/v2/research/sources", headers=second, json={"title": "Private", "text": "Private text.",
        "permissions": {"store": True}}).json()
    assert client.post(path + "/sources", headers=first, json={"parent_version_id": parent,
        "source_ids": [other["source_id"]]}).status_code == 404
    attached = client.post(path + "/sources", headers=first, json={"parent_version_id": parent,
        "source_ids": [source["source_id"]]})
    assert attached.status_code == 201, attached.text
    versions = attached.json()["versions"]
    assert versions[0]["source_ids"] == []
    assert versions[1]["source_ids"] == [source["source_id"]]
    assert versions[1]["parent_version_id"] == parent
    metadata_path = path + "/versions/" + versions[1]["version_id"] + "/sources"
    metadata = client.get(metadata_path, headers=first)
    assert metadata.status_code == 200
    assert metadata.json()[0]["title"] == "Evidence"
    assert "text" not in metadata.json()[0] and "owner_id" not in metadata.json()[0]
    assert client.get(metadata_path, headers=second).status_code == 404
    assert client.post(path + "/sources", headers=first, json={"parent_version_id": parent,
        "source_ids": [source["source_id"]]}).status_code == 409


def test_user_citations_are_located_and_cannot_reference_unattached_or_hidden_sources(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    def auth(email):
        response = client.post("/v1/auth/local/register", json={"email": email, "password": "correct horse battery staple!"})
        assert response.status_code == 201
        return {"Authorization": "Bearer " + response.json()["access_token"]}
    owner, other = auth("citation-owner@example.test"), auth("citation-other@example.test")
    def create(headers, title, text, display=True):
        response = client.post("/v2/research/sources", headers=headers, json={"title": title, "text": text,
            "permissions": {"store": True, "display": display, "export": True}})
        assert response.status_code == 201
        return response.json()["source_id"]
    visible = create(owner, "Report", "Revenue 100. Profit 20. Revenue 100.")
    hidden = create(owner, "Hidden", "Hidden quote", display=False)
    unattached = create(owner, "Unattached", "Private quote")
    other_source = create(other, "Other account", "Other quote")
    topic = client.post("/v2/research/topics", headers=owner, json={"question": "Review", "source_ids": [visible, hidden]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    parent = topic["versions"][-1]["version_id"]
    def save(citations, parent_version=None, claims=None):
        return client.post(path + "/versions", headers=owner, json={"parent_version_id": parent_version or parent,
            "content": {"claims": claims if claims is not None else [{"text": "User finding", "role": "support", "citations": citations}]}})
    def cite(source_id, quote, start=0, end=None):
        return {"source_id": source_id, "quotation": quote, "location": {"character_start": start, "character_end": start + len(quote) if end is None else end}}
    for citation in [cite(visible, "Not present"), cite(hidden, "Hidden quote"), cite(unattached, "Private quote"),
                     cite(other_source, "Other quote"), cite(visible, " "), cite(visible, "Profit 20", 0),  # Wrong position.
                     cite(visible, "Profit 20", 13, 99)]:
        assert save([citation]).status_code == 422
    assert len(client.get(path, headers=owner).json()["versions"]) == 1
    saved = save([cite(visible, "Profit 20", 13), cite(visible, "Revenue 100", 24)])
    assert saved.status_code == 201, saved.text
    version = saved.json()["versions"][-1]
    claim = version["content"]["claims"][0]
    assert claim["edited_by_user"] is True and claim["turn_id"] is None and claim["claim_id"].startswith("claim-")
    one, second = claim["citations"]
    assert one["location"] == {"character_start": 13, "character_end": 22}
    assert second["location"] == {"character_start": 24, "character_end": 35}
    context = client.get("/v2/research/sources/" + visible + "/context", headers=owner,
                         params={"start": 13, "end": 22})
    assert context.json()["selected"] == "Profit 20"
    exported = client.get(path + "/versions/" + version["version_id"] + "/export", headers=owner)
    assert exported.status_code == 200 and exported.json()["version"]["content"]["claims"][0]["citations"] == claim["citations"]
    # Removing the citations of a finding is allowed; the finding stays a user edit.
    edited = {**claim, "text": "Revised finding", "citations": []}
    updated = save([], version["version_id"], [edited])
    assert updated.status_code == 201
    assert updated.json()["versions"][-1]["content"]["claims"][0]["citations"] == []


def test_new_user_citations_respect_historical_cutoff(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    reg = client.post("/v1/auth/local/register", json={"email": "citation-cutoff@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + reg.json()["access_token"]}
    def create(title, **dates):
        return client.post("/v2/research/sources", headers=headers, json={"title": title, "text": title + " quote",
            "permissions": {"store": True, "display": True}, **dates}).json()["source_id"]
    before = create("before", published_at="2023-01-01T00:00:00Z")
    future = create("future", published_at="2023-03-01T00:00:00Z")
    revised = create("revised", published_at="2023-01-01T00:00:00Z", revised_at="2023-03-01T00:00:00Z")
    unknown = create("unknown")
    microfuture = create("microfuture", published_at="2023-02-01T00:00:00.000001Z")
    simulated = create("simulated", published_at="2023-01-01T00:00:00Z", is_simulated=True)
    hidden = client.post("/v2/research/sources", headers=headers, json={"title": "hidden", "text": "hidden quote",
        "published_at": "2023-01-01T00:00:00Z", "permissions": {"store": True, "display": False}}).json()["source_id"]
    unattached = create("unattached", published_at="2023-01-01T00:00:00Z")
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Before February?",
        "source_ids": [before, future, revised, unknown, microfuture, simulated, hidden], "as_of": "2023-02-01T00:00:00Z"}).json()
    path = "/v2/research/topics/" + topic["topic_id"] + "/versions"
    source_path = path + "/" + topic["versions"][0]["version_id"] + "/sources/"
    visible = client.get(source_path + before + "/text", headers=headers)
    assert visible.status_code == 200 and visible.json()["text"] == "before quote"
    assert visible.headers["cache-control"] == "no-store"
    for source_id in (future, revised, unknown, microfuture, simulated, hidden):
        denied = client.get(source_path + source_id + "/text", headers=headers)
        assert denied.status_code == 403 and "quote" not in denied.text
    assert client.get(source_path + unattached + "/text", headers=headers).status_code == 404
    assert client.get(source_path + before + "/text").status_code == 401
    other = client.post("/v1/auth/local/register", json={"email": "citation-cutoff-other@example.test",
        "password": "correct horse battery staple!"}).json()
    assert client.get(source_path + before + "/text", headers={"Authorization": "Bearer " + other["access_token"]}).status_code == 404
    def save(source_id, quote):
        return client.post(path, headers=headers, json={"parent_version_id": topic["versions"][0]["version_id"],
            "content": {"claims": [{"text": "Finding", "role": "support", "citations": [{"source_id": source_id, "quotation": quote,
                        "location": {"character_start": 0, "character_end": len(quote)}}]}]}})
    for source_id, quote in [(future, "future quote"), (revised, "revised quote"), (unknown, "unknown quote")]:
        assert save(source_id, quote).status_code == 422
    assert save(before, "before quote").status_code == 201


def test_research_text_upload_has_scoped_bounded_body_limit(monkeypatch):
    from hashlib import sha256

    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    session = client.post("/v1/auth/local/register", json={"email": "large-source@example.test",
        "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + session["access_token"]}
    text = "A financial report. " * 25000
    response = client.post("/v2/research/sources", headers=headers, json={"title": "Long filing", "text": text,
        "permissions": {"store": True, "display": True, "model_input": True}})
    assert response.status_code == 201, response.text
    assert response.json()["content_sha256"] == sha256(text.encode()).hexdigest()
    oversized = client.post("/v2/research/sources", headers=headers, json={"title": "Too large", "text": "x" * 2_100_000,
        "permissions": {"store": True}})
    assert oversized.status_code == 413
    unrelated = client.post("/v2/research/sources/fetch", headers=headers, json={"text": "x" * 300_000})
    assert unrelated.status_code == 413


def test_fetched_official_html_original_is_private_permission_gated_attachment(monkeypatch):
    import base64
    from hashlib import sha256
    from uuid import uuid4
    from app.research_fetch import FetchedText
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setattr(api, '_reserve_search_call', lambda request: None)
    raw = b'<html><p>Official text</p><script>do_not_execute()</script></html>'
    monkeypatch.setattr(api, 'fetch_official_text', lambda request: FetchedText('Official text', sha256(raw).hexdigest(),
        'text/html', base64.b64encode(raw).decode()))
    client = TestClient(app)
    def account():
        result = client.post('/v1/auth/local/register', json={'email': f'official-{uuid4().hex}@example.test',
            'password': 'correct horse battery staple!'}).json()
        return {'Authorization': 'Bearer ' + result['access_token']}
    owner, other = account(), account()
    def fetch(export):
        response = client.post('/v2/research/sources/fetch', headers=owner, json={'title': 'Official report',
            'url': 'https://www.bea.gov/news/example', 'permissions': {'store': True, 'display': True, 'export': export}})
        assert response.status_code == 201, response.text
        assert response.json()['original_html_available'] is True
        assert 'original_html_base64' not in response.json()
        return response.json()['source_id']
    available, restricted = fetch(True), fetch(False)
    download = client.get('/v2/research/sources/' + available + '/original', headers=owner)
    assert download.status_code == 200 and download.content == raw
    assert download.headers['content-type'].startswith('application/octet-stream')
    assert download.headers['content-disposition'].startswith('attachment;')
    assert download.headers['x-content-type-options'] == 'nosniff'
    assert client.get('/v2/research/sources/' + restricted + '/original', headers=owner).status_code == 403
    assert client.get('/v2/research/sources/' + available + '/original', headers=other).status_code == 404


def _tiny_pdf_bytes() -> bytes:
    """Build two synthetic PDF pages without external PDF-generation dependencies."""
    streams = [b"BT /F1 12 Tf 20 150 Td (Revenue 100) Tj ET", b"BT /F1 12 Tf 20 150 Td (Revenue 200) Tj ET"]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    objects.extend(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream" for stream in streams)
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    start = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(pdf)


def test_open_web_fetch_supports_html_and_pdf_without_suffix_and_maps_failures(monkeypatch):
    from datetime import datetime, timezone
    from uuid import uuid4
    import app.research_fetch as research_fetch
    from app.openai_compatible import BinaryHttpResponse, _TransportTimeout
    from app.research_sources import ResearchSource, SourcePermissions, pdf_text_spacing_suspect

    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.delenv("FORESIGHTNINE_FETCH_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("FORESIGHTNINE_FETCH_BLOCKED_HOSTS", raising=False)
    monkeypatch.delenv("FORESIGHTNINE_SEC_USER_AGENT", raising=False)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setattr(api, "_reserve_search_call", lambda request: None)

    pdf_bytes = _tiny_pdf_bytes()
    html_bytes = (
        b"<html><p>NVIDIA Q4 summary</p>"
        b"<table><tr><th>Year</th><th>Revenue</th></tr><tr><td>FY25</td><td>130.5B</td></tr></table>"
        b"<div style='display:none'>HIDDEN PROMPT INJECTION</div></html>"
    )

    def fake_transport(method, url, headers, body, timeout, **kwargs):
        if url == "https://ir.example.com/news/q4":
            return BinaryHttpResponse(200, html_bytes, {"content-type": "text/html; charset=utf-8"})
        if url == "https://ir.example.com/download?id=42":
            return BinaryHttpResponse(200, pdf_bytes, {"content-type": "application/pdf"})
        if url == "https://ir.example.com/notes":
            return BinaryHttpResponse(200, b"Plain text notes", {"content-type": "text/plain"})
        if url == "https://www.bea.gov/redirect-out":
            return BinaryHttpResponse(302, b"", {"location": "https://cdn.example.org/mirror.html"})
        if url == "https://cdn.example.org/mirror.html":
            return BinaryHttpResponse(200, b"<html><p>Mirrored content</p></html>", {"content-type": "text/html"})
        if url == "https://blocked-upstream.example.com/403":
            return BinaryHttpResponse(403, b"", {})
        if url == "https://missing.example.com/404":
            return BinaryHttpResponse(404, b"", {})
        if url == "https://ratelimit.example.com/429":
            return BinaryHttpResponse(429, b"", {})
        if url == "https://timeout.example.com/slow":
            raise _TransportTimeout("timed out")
        if url == "https://json.example.com/data":
            return BinaryHttpResponse(200, b'{"ok": true}', {"content-type": "application/json"})
        raise AssertionError(f"Unexpected transport URL: {url}")

    monkeypatch.setattr(research_fetch, "_validated_addresses", lambda host, port, *, allow_private=False: ("93.184.216.34",))
    monkeypatch.setattr(research_fetch, "_default_binary_transport", fake_transport)

    client = TestClient(app)
    reg = client.post("/v1/auth/local/register", json={"email": f"web-fetch-{uuid4().hex}@example.test",
                                                        "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + reg["access_token"]}

    # 1. Open-web HTML fetch (export=False)
    web_html = client.post("/v2/research/sources/fetch", headers=headers, json={
        "title": "NVIDIA IR Q4",
        "url": "https://ir.example.com/news/q4",
        "permissions": {"store": True, "display": True, "model_input": True, "export": False},
    })
    assert web_html.status_code == 201, web_html.text
    html_data = web_html.json()
    assert html_data["kind"] == "user_supplied"
    assert html_data["retrieval_method"] == "web_html"
    assert html_data["original_html_available"] is True
    assert "original_html_base64" not in html_data
    assert "span markers" in html_data["extraction_disclosure"]
    assert "HIDDEN PROMPT INJECTION" not in html_data["text"]
    assert html_data["permissions"]["export"] is False
    assert html_data["provenance"]["final_url"] == "https://ir.example.com/news/q4"
    assert client.get(f"/v2/research/sources/{html_data['source_id']}/original", headers=headers).status_code == 403

    fy25_start = html_data["text"].index("FY25")
    html_ctx = client.get(f"/v2/research/sources/{html_data['source_id']}/context", headers=headers,
                          params={"start": fy25_start, "end": fy25_start + 4})
    assert html_ctx.status_code == 200 and html_ctx.json()["table_context"] is not None

    # 2. Open-web PDF without .pdf suffix (both export=True and export=False)
    web_pdf_exportable = client.post("/v2/research/sources/fetch", headers=headers, json={
        "title": "NVIDIA Filing PDF",
        "url": "https://ir.example.com/download?id=42",
        "permissions": {"store": True, "display": True, "model_input": True, "export": True},
    })
    assert web_pdf_exportable.status_code == 201, web_pdf_exportable.text
    pdf_data = web_pdf_exportable.json()
    assert pdf_data["kind"] == "user_supplied"
    assert pdf_data["retrieval_method"] == "web_pdf"
    assert pdf_data["original_pdf_available"] is True
    assert "original_pdf_base64" not in pdf_data
    assert pdf_data["pdf_text_order"] == "layout"
    assert "Revenue 100" in pdf_data["text"] and "Revenue 200" in pdf_data["text"]

    p2_start = pdf_data["text"].index("Revenue 200")
    pdf_ctx = client.get(f"/v2/research/sources/{pdf_data['source_id']}/context", headers=headers,
                         params={"start": p2_start, "end": p2_start + len("Revenue 200")})
    assert pdf_ctx.status_code == 200
    assert pdf_ctx.json()["page_start"] == 2 and pdf_ctx.json()["page_end"] == 2
    assert "Revenue 100" not in pdf_ctx.json()["before"]

    original_pdf = client.get(f"/v2/research/sources/{pdf_data['source_id']}/original", headers=headers)
    assert original_pdf.status_code == 200 and original_pdf.content == pdf_bytes
    assert original_pdf.headers["content-type"].startswith("application/pdf")
    assert 'filename="source.pdf"' in original_pdf.headers["content-disposition"]

    web_pdf_no_export = client.post("/v2/research/sources/fetch", headers=headers, json={
        "title": "NVIDIA Filing PDF Private",
        "url": "https://ir.example.com/download?id=42",
        "permissions": {"store": True, "display": True, "model_input": True, "export": False},
    })
    assert web_pdf_no_export.status_code == 201
    assert client.get(f"/v2/research/sources/{web_pdf_no_export.json()['source_id']}/original", headers=headers).status_code == 403

    # 3. Plain-text URL and official host redirecting to third-party host
    plain = client.post("/v2/research/sources/fetch", headers=headers, json={
        "title": "Notes", "url": "https://ir.example.com/notes",
        "permissions": {"store": True, "display": True},
    })
    assert plain.status_code == 201
    assert plain.json()["kind"] == "user_supplied" and plain.json()["retrieval_method"] == "web_html"
    assert plain.json()["original_html_available"] is False

    redirected = client.post("/v2/research/sources/fetch", headers=headers, json={
        "title": "Redirected", "url": "https://www.bea.gov/redirect-out",
        "permissions": {"store": True, "display": True, "export": True},
    })
    assert redirected.status_code == 201
    assert redirected.json()["kind"] == "user_supplied" and redirected.json()["retrieval_method"] == "web_html"
    assert redirected.json()["provenance"]["final_url"] == "https://cdn.example.org/mirror.html"

    # 4. pdf_text_spacing_suspect includes web_pdf
    joined_text = ("Joinedwordswithoutspacesbetween " * 50).strip()
    assert pdf_text_spacing_suspect(ResearchSource(
        source_id="s-web-pdf", owner_id="owner", title="Web PDF", text=joined_text, kind="user_supplied",
        obtained_at=datetime.now(timezone.utc), content_sha256="h", retrieval_method="web_pdf",
        permissions=SourcePermissions(store=True, display=True),
    )) is True

    # 5. FetchFailure status mappings (429 / 503 / 422)
    for bad_url, expected_code in (
        ("https://ratelimit.example.com/429", 429),
        ("https://timeout.example.com/slow", 503),
        ("https://blocked-upstream.example.com/403", 422),
        ("https://missing.example.com/404", 422),
        ("https://json.example.com/data", 422),
    ):
        resp = client.post("/v2/research/sources/fetch", headers=headers, json={
            "title": "Bad", "url": bad_url, "permissions": {"store": True},
        })
        assert resp.status_code == expected_code, (bad_url, resp.text)

    monkeypatch.setenv("FORESIGHTNINE_FETCH_BLOCKED_HOSTS", "policy-blocked.example.com")
    blocked_resp = client.post("/v2/research/sources/fetch", headers=headers, json={
        "title": "Policy blocked", "url": "https://policy-blocked.example.com/report", "permissions": {"store": True},
    })
    assert blocked_resp.status_code == 422
    monkeypatch.delenv("FORESIGHTNINE_FETCH_BLOCKED_HOSTS", raising=False)
    monkeypatch.setenv("FORESIGHTNINE_FETCH_ALLOWED_HOSTS", "ir.example.com")
    not_allowed = client.post("/v2/research/sources/fetch", headers=headers, json={
        "title": "Not allowlisted", "url": "https://other.example.com/report", "permissions": {"store": True},
    })
    assert not_allowed.status_code == 422


def test_fetch_routes_trailing_dot_sec_host_and_rejects_sec_subdomains_and_encoded_fred(monkeypatch):
    import base64
    from hashlib import sha256
    from types import SimpleNamespace
    from uuid import uuid4
    import pytest
    from app.operations import ApiProblem
    from app.research_sec import SecFilingResult
    from app.research_sources import SourcePermissions

    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.setenv("FORESIGHTNINE_SEC_USER_AGENT", "Example Research contact@example.test")
    monkeypatch.setattr(api, "_reserve_search_call", lambda request: None)
    monkeypatch.setattr(api, "reserve_sec_request", lambda *_: True)
    monkeypatch.setattr(api, "fetch_source_document", lambda *_: pytest.fail("must not route SEC or FRED URLs to general web fetcher"))

    seen_sec_urls = []
    raw = b"<html><p>SEC trailing dot filing</p></html>"

    def fake_sec_fetch(req, agent):
        seen_sec_urls.append(str(req.url))
        assert agent == "Example Research contact@example.test"
        return SecFilingResult("SEC trailing dot filing", sha256(raw).hexdigest(), base64.b64encode(raw).decode())

    monkeypatch.setattr(api, "fetch_sec_filing", fake_sec_fetch)
    client = TestClient(app)
    reg = client.post("/v1/auth/local/register", json={
        "email": f"sec-edge-{uuid4().hex}@example.test",
        "password": "correct horse battery staple!",
    }).json()
    headers = {"Authorization": "Bearer " + reg["access_token"]}

    canonical_sec_url = "https://www.sec.gov/Archives/edgar/data/858877/000085887724000017/csco-20240727.htm"
    trailing_dot_sec_url = "https://www.sec.gov./Archives/edgar/data/858877/000085887724000017/csco-20240727.htm"
    sec_ok = client.post("/v2/research/sources/fetch", headers=headers, json={
        "title": "SEC Filing Trailing Dot",
        "url": trailing_dot_sec_url,
        "permissions": {"store": True, "display": True, "model_input": True, "export": True},
    })
    assert sec_ok.status_code == 201, sec_ok.text
    sec_data = sec_ok.json()
    assert sec_data["retrieval_method"] == "sec_filing_html"
    assert sec_data["kind"] == "official"
    assert sec_data["url"] == canonical_sec_url
    assert seen_sec_urls == [canonical_sec_url]

    for bad_url in (
        "https://efts.sec.gov/EDGAR/searchedgar/companysearch",
        "https://api.stlouisfed.org/%66red/series/observations",
        "https://sub.alfred.stlouisfed.org/series",
    ):
        resp = client.post("/v2/research/sources/fetch", headers=headers, json={
            "title": "Rejected", "url": bad_url, "permissions": {"store": True},
        })
        assert resp.status_code == 422, (bad_url, resp.text)

    # Also prove direct invocation fail-closes on non-www SEC subdomains before web fallback.
    fake_http_request = SimpleNamespace(headers={"Authorization": headers["Authorization"]})
    unvalidated_efts = SimpleNamespace(
        url="https://efts.sec.gov/EDGAR/searchedgar/companysearch",
        title="EFTS",
        permissions=SourcePermissions(store=True),
        published_at=None,
        event_at=None,
        revised_at=None,
        is_simulated=False,
    )
    with pytest.raises(ApiProblem) as exc_info:
        api.fetch_research_source(unvalidated_efts, fake_http_request)
    assert exc_info.value.status_code == 422
