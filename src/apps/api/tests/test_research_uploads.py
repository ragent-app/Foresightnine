import base64
from pathlib import Path
import shutil

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.research_analysis import analyze
from app.research_sources import ResearchSource, SourcePermissions
from app.research_uploads import PdfFailure, PdfUploadRequest, extract_pdf
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
import time


def tiny_pdf() -> bytes:
    """Build two synthetic pages without a PDF-generation dependency."""
    streams = [b"BT /F1 12 Tf 20 150 Td (Revenue 100) Tj ET", b"BT /F1 12 Tf 20 150 Td (Revenue 200) Tj ET"]
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    objects.extend(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream" for stream in streams)
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(pdf)); pdf.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    start = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]: pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(pdf)


def body(raw, *, display=True, export=True):
    return {"title": "Synthetic report", "pdf_base64": base64.b64encode(raw).decode(),
            "permissions": {"store": True, "display": display, "model_input": True, "export": export}}


@pytest.mark.skipif(not shutil.which("prlimit") or not shutil.which("pdftotext"), reason="PDF extraction tools unavailable")
def test_pdf_upload_preserves_original_and_citation_pages(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    reg = client.post("/v1/auth/local/register", json={"email": "pdf@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + reg.json()["access_token"]}
    raw = tiny_pdf()
    uploaded = client.post("/v2/research/sources/pdf", headers=headers, json={**body(raw),
        "event_at": "2023-12-31T00:00:00Z", "published_at": "2024-01-01T00:00:00Z", "is_simulated": True,
        "revised_at": "2024-02-01T00:00:00Z"})
    assert uploaded.status_code == 201, uploaded.text
    item = uploaded.json()
    assert item["retrieval_method"] == "user_pdf"
    assert item["pdf_text_order"] == "layout"
    assert item["is_simulated"] is True
    assert item["event_at"].startswith("2023-12-31")
    assert item["published_at"].startswith("2024-01-01")
    assert item["revised_at"].startswith("2024-02-01")
    assert "original_pdf_base64" not in item and "text" not in item
    source = client.get("/v2/research/sources/" + item["source_id"], headers=headers).json()
    assert "original_pdf_base64" not in source
    assert "Revenue 100" in source["text"] and "Revenue 200" in source["text"]
    original = client.get("/v2/research/sources/" + item["source_id"] + "/original", headers=headers)
    assert original.status_code == 200 and original.content == raw
    document = ResearchSource(source_id="pdf", owner_id="owner", title="Report", text=source["text"],
        kind="user_supplied", obtained_at=datetime.now(timezone.utc), content_sha256="test",
        retrieval_method="user_pdf", permissions=SourcePermissions(store=True, model_input=True, display=True))
    result = analyze("Revenue?", [document], None, lambda _: ({"claims": [{"text": "Second page", "citations": [
        {"source_id": "pdf", "quotation": "Revenue 200"}]}]}, None))
    assert result.claims[0]["citations"][0]["location"]["page"] == 2
    location = result.claims[0]["citations"][0]["location"]
    context = client.get("/v2/research/sources/" + item["source_id"] + "/context", headers=headers,
        params={"start": location["character_start"], "end": location["character_end"]})
    assert context.status_code == 200
    assert context.json()["selected"] == "Revenue 200" and context.json()["page_start"] == 2
    assert "Revenue 100" not in context.json()["before"]
    hidden = client.post("/v2/research/sources/pdf", headers=headers, json=body(raw, export=False)).json()
    assert client.get("/v2/research/sources/" + hidden["source_id"] + "/original", headers=headers).status_code == 403


@pytest.mark.skipif(not shutil.which("prlimit") or not shutil.which("pdftotext"), reason="PDF extraction tools unavailable")
def test_pdf_reading_order_is_explicit_and_keeps_original_evidence(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    reg = client.post("/v1/auth/local/register", json={"email": "pdf-reading@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + reg.json()["access_token"]}
    raw = (Path(__file__).resolve().parents[3] / "docs/product/phase-a/30-cross-domain-evaluation/bea-july-2025.pdf").read_bytes()
    uploaded = client.post("/v2/research/sources/pdf", headers=headers, json={**body(raw), "text_order": "reading"})
    assert uploaded.status_code == 201, uploaded.text
    item = uploaded.json()
    assert item["pdf_text_order"] == "reading"
    source = client.get("/v2/research/sources/" + item["source_id"], headers=headers).json()
    assert client.get("/v2/research/sources/" + item["source_id"] + "/original", headers=headers).content == raw
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "PCE?", "source_ids": [item["source_id"]]}).json()
    path = f"/v2/research/topics/{topic['topic_id']}/versions/{topic['versions'][0]['version_id']}"
    assert client.get(path + "/sources", headers=headers).json()[0]["pdf_text_order"] == "reading"
    assert client.get(path + "/export", headers=headers).json()["source_provenance"][0]["pdf_text_order"] == "reading"
    document = ResearchSource.model_validate({**source, "owner_id": "owner"})
    quote = "From the preceding month, the PCE price index for July increased 0.2 percent."
    result = analyze("2025年7月PCE价格指数较上月和较上年同期变化？", [document], None,
        lambda _: ({"claims": [{"text": "Monthly PCE rose", "citations": [{"source_id": item["source_id"], "quotation": quote}]}]}, 1),
        max_prompt_chars=2055)
    citation = result.claims[0]["citations"][0]
    location = citation["location"]
    assert citation["quotation_match"] == "whitespace_normalized"
    assert source["text"][location["character_start"]:location["character_end"]] == citation["quotation"]
    assert location["page"] == 2


@pytest.mark.skipif(not shutil.which("prlimit") or not shutil.which("pdftotext"), reason="PDF extraction tools unavailable")
def test_pdf_spacing_warning_is_visible_and_marks_research_partial(monkeypatch):
    import app.main as api
    from app.research_sources import pdf_text_spacing_suspect
    from app.research_uploads import _extract_text
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    raw=(Path(__file__).resolve().parents[3]/"docs/product/phase-a/209-cross-domain-frozen/bea-2021-may.pdf").read_bytes()
    reading=_extract_text(raw,text_order="reading");layout=_extract_text(raw,text_order="layout")
    def report(text, method="user_pdf"):
        return ResearchSource(source_id="s",owner_id="owner",title="Report",text=text,kind="user_supplied",
            obtained_at=datetime.now(timezone.utc),content_sha256="hash",retrieval_method=method,
            permissions=SourcePermissions(store=True,display=True,model_input=True,export=True))
    assert pdf_text_spacing_suspect(report(reading))
    assert not pdf_text_spacing_suspect(report(layout))
    assert not pdf_text_spacing_suspect(report(reading,"user_supplied"))
    assert not pdf_text_spacing_suspect(report("中文资料"*1000))
    client=TestClient(app)
    reg=client.post("/v1/auth/local/register",json={"email":f"pdf-spacing-{uuid4().hex}@example.test","password":"correct horse battery staple!"})
    headers={"Authorization":"Bearer "+reg.json()["access_token"]}
    uploaded=client.post("/v2/research/sources/pdf",headers=headers,json={**body(raw),"text_order":"reading"})
    assert uploaded.status_code==201 and uploaded.json()["pdf_text_spacing_warning"] is True
    sid=uploaded.json()["source_id"]
    assert client.get("/v2/research/sources/"+sid,headers=headers).json()["pdf_text_spacing_warning"] is True
    topic=client.post("/v2/research/topics",headers=headers,json={"question":"How did spending change?","source_ids":[sid]}).json()
    path="/v2/research/topics/"+topic["topic_id"]
    sources=client.get(path+"/versions/"+topic["versions"][0]["version_id"]+"/sources",headers=headers).json()
    assert sources[0]["pdf_text_spacing_warning"] is True
    quote="Current-dollarconsumerspendingincreasedinMay,reflectingan increasein servicesthatwas partly\noffsetbyadecreaseingoods."
    class Provider:
        name="pdf-spacing-test"
        policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            return {"claims":[{"text":"Consumer spending changed.","citations":[{"source_id":sid,"quotation":quote}]}]},10
    monkeypatch.setattr(api,"challenge_provider",Provider())
    receipt=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"pdf-spacing"},json={
        "parent_version_id":topic["versions"][0]["version_id"],"require_counterarguments":False}).json()["operation"]
    for _ in range(100):
        status=client.get(path+"/runs/"+receipt["operation_id"],headers=headers).json()["operation"]["status"]
        if status in {"completed","failed"}:break
        time.sleep(.02)
    assert status=="completed"
    version=client.get(path,headers=headers).json()["versions"][-1]
    assert version["content"]["run"]["status"]=="partial"
    assert any("PDF text may have missing word spacing" in x for x in version["content"]["limitations"])
    exported=client.get(path+"/versions/"+version["version_id"]+"/export",headers=headers).json()
    assert exported["source_provenance"][0]["pdf_text_spacing_warning"] is True


@pytest.mark.skipif(not shutil.which("prlimit") or not shutil.which("pdftotext"), reason="PDF extraction tools unavailable")
def test_reextract_pdf_preserves_original_permissions_and_account_boundary(monkeypatch):
    import app.main as api
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client=TestClient(app)
    def auth():
        reg=client.post("/v1/auth/local/register",json={"email":f"reextract-{uuid4().hex}@example.test","password":"correct horse battery staple!"})
        return {"Authorization":"Bearer "+reg.json()["access_token"]}
    owner,other=auth(),auth()
    raw=(Path(__file__).resolve().parents[3]/"docs/product/phase-a/209-cross-domain-frozen/bea-2021-may.pdf").read_bytes()
    uploaded=client.post("/v2/research/sources/pdf",headers=owner,json={**body(raw,export=False),"text_order":"reading",
        "published_at":"2021-06-25T12:30:00Z","event_at":"2021-05-31T00:00:00Z","is_simulated":True})
    assert uploaded.status_code==201
    original=uploaded.json();sid=original["source_id"]
    assert original["original_pdf_available"] and original["pdf_text_spacing_warning"]
    path="/v2/research/sources/"+sid+"/reextract"
    assert client.post(path,headers=other,json={"text_order":"layout"}).status_code==404
    assert client.post(path,headers=owner,json={"text_order":"unknown"}).status_code==422
    response=client.post(path,headers=owner,json={"text_order":"layout"})
    assert response.status_code==201,response.text
    changed=response.json()
    assert changed["source_id"]!=sid and changed["derived_from_source_id"]==sid
    assert changed["pdf_text_order"]=="layout" and not changed.get("pdf_text_spacing_warning")
    assert changed["response_sha256"]==original["response_sha256"]
    assert changed["published_at"]==original["published_at"] and changed["event_at"]==original["event_at"]
    assert changed["permissions"]==original["permissions"] and changed["is_simulated"] is True
    assert "text" not in changed and "original_pdf_base64" not in changed
    assert client.get("/v2/research/sources/"+sid,headers=owner).json()["pdf_text_order"]=="reading"
    assert client.get("/v2/research/sources/"+changed["source_id"]+"/original",headers=owner).status_code==403
    ordinary=client.post("/v2/research/sources",headers=owner,json={"title":"Text","text":"text",
        "permissions":{"store":True}}).json()["source_id"]
    assert client.post("/v2/research/sources/"+ordinary+"/reextract",headers=owner,json={"text_order":"layout"}).status_code==422
    stored=api.research_sources.repository.get(sid)
    api.research_sources.repository.put(sid,stored.model_copy(update={"response_sha256":"0"*64}))
    assert client.post(path,headers=owner,json={"text_order":"layout"}).status_code==422


@pytest.mark.skipif(not shutil.which("prlimit") or not shutil.which("pdftotext"), reason="PDF extraction tools unavailable")
def test_reextract_idempotency_serializes_concurrent_requests(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock
    import app.research_uploads as uploads
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client=TestClient(app)
    def auth():
        account=client.post("/v1/auth/local/register",json={"email":f"reextract-key-{uuid4().hex}@example.test","password":"correct horse battery staple!"}).json()
        return {"Authorization":"Bearer "+account["access_token"]}
    owner,other=auth(),auth()
    original=client.post("/v2/research/sources/pdf",headers=owner,json={**body(tiny_pdf()),"text_order":"reading"}).json()
    foreign=client.post("/v2/research/sources/pdf",headers=other,json={**body(tiny_pdf()),"text_order":"reading"}).json()
    path="/v2/research/sources/"+original["source_id"]+"/reextract"
    call_count=0;guard=Lock();extract=uploads._extract_text
    def counted(*args,**kwargs):
        nonlocal call_count
        with guard:call_count+=1
        return extract(*args,**kwargs)
    monkeypatch.setattr(uploads,"_extract_text",counted)
    request_headers={**owner,"Idempotency-Key":"same-reextract-key"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses=list(pool.map(lambda _:client.post(path,headers=request_headers,json={"text_order":"layout"}),range(4)))
    assert all(r.status_code==201 for r in responses)
    assert len({r.json()["source_id"] for r in responses})==1 and call_count==1
    assert sorted(r.headers["Idempotency-Replayed"] for r in responses)==["false","true","true","true"]
    assert client.post(path,headers=request_headers,json={"text_order":"reading"}).status_code==409
    other_path="/v2/research/sources/"+foreign["source_id"]+"/reextract"
    distinct=client.post(other_path,headers={**other,"Idempotency-Key":"same-reextract-key"},json={"text_order":"layout"})
    assert distinct.status_code==201 and distinct.json()["source_id"]!=responses[0].json()["source_id"]
    assert client.post(path,headers={**owner,"Idempotency-Key":"short"},json={"text_order":"layout"}).status_code==422


def test_pdf_rejects_missing_rights_and_invalid_content():
    with pytest.raises(PdfFailure, match="source_permission_unknown"):
        extract_pdf(PdfUploadRequest(title="x", pdf_base64="eA==", permissions={"store": False}))
    with pytest.raises(PdfFailure, match="invalid_pdf"):
        extract_pdf(PdfUploadRequest(title="x", pdf_base64="eA==", permissions={"store": True}))


@pytest.mark.skipif(not shutil.which('prlimit') or not shutil.which('pdftotext'), reason='PDF extraction tools unavailable')
def test_larger_pdf_upload_is_bounded_and_original_is_recoverable(monkeypatch):
    from app.research_uploads import MAX_PDF_BYTES
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    client = TestClient(app)
    account = client.post('/v1/auth/local/register', json={'email': 'pdf-large@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + account['access_token']}
    # Preserve a valid PDF and inflate it without bringing in external data.
    raw = tiny_pdf() + b' ' * 1_510_000
    response = client.post('/v2/research/sources/pdf', headers=headers, json=body(raw))
    assert response.status_code == 201, response.text
    source_id = response.json()['source_id']
    assert client.get('/v2/research/sources/' + source_id + '/original', headers=headers).content == raw
    too_large = tiny_pdf() + b' ' * MAX_PDF_BYTES
    response = client.post('/v2/research/sources/pdf', headers=headers, json=body(too_large))
    assert response.status_code in (413, 422)
