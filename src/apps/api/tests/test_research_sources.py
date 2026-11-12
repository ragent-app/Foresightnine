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
        "note": "SECRET_NOTE", "content": {"summary": "SECRET_SUMMARY", "limitations": ["SECRET_LIMITATION"],
            "facts": [{"fact_id": "f", "label": "SECRET_LABEL", "value": "100", "unit": "USD", "period": "2023",
                       "source_id": source["source_id"], "quotation": "SECRET_TEXT 100"}],
            "claims": [{"text": "SECRET_CLAIM", "citations": [{"source_id": source["source_id"], "quotation": "SECRET_TEXT 100"}]}]}})
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
        "review": "confirmed", "content": {"summary": "Research result", "claims": [{"text": "Revenue was 100",
          "citations": [{"source_id": source["source_id"], "quotation": "Revenue was 100."}]}]}}).json()
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
    assert client.get(path, headers=second, params={"start": start, "end": end}).status_code == 404
    assert client.get(path, params={"start": start, "end": end}).status_code == 401
    assert client.get(path, headers=first, params={"start": end, "end": start}).status_code == 422
    assert client.get(path, headers=first, params={"start": 0, "end": 1100}).status_code == 422
    hidden = client.post("/v2/research/sources", headers=first, json={"title": "Private", "text": "Secret",
        "permissions": {"store": True, "display": False}}).json()
    assert client.get("/v2/research/sources/" + hidden["source_id"] + "/context", headers=first,
                      params={"start": 0, "end": 6}).status_code == 403


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
    def save(citations):
        return client.post(path + "/versions", headers=owner, json={"parent_version_id": parent,
            "content": {"claims": [{"text": "User finding", "citations": citations}]}})
    for source_id, quote in [(visible, "Not present"), (hidden, "Hidden quote"),
                             (unattached, "Private quote"), (other_source, "Other quote"), (visible, " ")]:
        assert save([{"source_id": source_id, "quotation": quote}]).status_code == 422
    assert len(client.get(path, headers=owner).json()["versions"]) == 1
    saved = save([{"source_id": visible, "quotation": "Profit 20", "location": {"character_start": 0, "character_end": 2},
                   "support_status": "verified"}, {"source_id": visible, "quotation": "Revenue 100"}])
    assert saved.status_code == 201, saved.text
    version = saved.json()["versions"][-1]
    claim = version["content"]["claims"][0]
    assert claim["authorship"] == "user" and claim["support_status"] == "not_independently_verified"
    one, multiple = claim["citations"]
    assert one["location"] == {"character_start": 13, "character_end": 22}
    assert one["support_status"] == "not_independently_verified"
    assert multiple["location"]["status"] == "ambiguous" and len(multiple["location"]["matches"]) == 2
    context = client.get("/v2/research/sources/" + visible + "/context", headers=owner,
                         params={"start": 13, "end": 22})
    assert context.json()["selected"] == "Profit 20"
    exported = client.get(path + "/versions/" + version["version_id"] + "/export", headers=owner)
    assert exported.status_code == 200 and exported.json()["version"]["content"]["claims"][0]["citations"] == claim["citations"]
    # Editing the finding retains verified positions; removing citations makes the finding unsupported.
    edited = {**claim, "text": "Revised finding", "citations": []}
    updated = client.post(path + "/versions", headers=owner,
        json={"parent_version_id": version["version_id"], "content": {"claims": [edited]}})
    assert updated.status_code == 201
    assert updated.json()["versions"][-1]["content"]["claims"][0]["support_status"] == "unsupported"


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
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Before February?",
        "source_ids": [before, future, revised, unknown], "as_of": "2023-02-01T00:00:00Z"}).json()
    path = "/v2/research/topics/" + topic["topic_id"] + "/versions"
    def save(source_id, quote):
        return client.post(path, headers=headers, json={"parent_version_id": topic["versions"][0]["version_id"],
            "content": {"claims": [{"text": "Finding", "citations": [{"source_id": source_id, "quotation": quote}]}]}})
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
