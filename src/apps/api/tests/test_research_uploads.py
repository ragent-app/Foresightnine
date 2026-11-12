import base64
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
    uploaded = client.post("/v2/research/sources/pdf", headers=headers, json=body(raw))
    assert uploaded.status_code == 201, uploaded.text
    item = uploaded.json()
    assert item["retrieval_method"] == "user_pdf"
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


def test_pdf_rejects_missing_rights_and_invalid_content():
    with pytest.raises(PdfFailure, match="source_permission_unknown"):
        extract_pdf(PdfUploadRequest(title="x", pdf_base64="eA==", permissions={"store": False}))
    with pytest.raises(PdfFailure, match="invalid_pdf"):
        extract_pdf(PdfUploadRequest(title="x", pdf_base64="eA==", permissions={"store": True}))
