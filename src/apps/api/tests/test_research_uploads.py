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
