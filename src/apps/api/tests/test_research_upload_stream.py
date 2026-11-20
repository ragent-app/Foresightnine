"""Streaming multipart upload of large documents (PDF / HTML / text) with the shared paged pipeline."""
import json
import os
import shutil
import tracemalloc
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as api
from app.research_upload_stream import ingest_uploaded_file, sniff_upload_kind
from app.research_fetch import FetchFailure
from app.research_sources import SourcePermissions
from tests.test_research_large_documents import build_pdf, needs_poppler
from tests.test_research_large_html import big_html
from tests.test_research_ocr import build_mixed_pdf, needs_tesseract, scanned_page_jpeg

PERMS = {"store": True, "display": True, "model_input": True, "export": True}
BOUNDARY = "----ForesightBoundary7MA4YWxk"


def multipart(file_bytes: bytes | None, *, filename="report.pdf", fields=None, boundary=BOUNDARY) -> bytes:
    fields = {"title": "Annual report", "permissions": json.dumps(PERMS), **(fields or {})}
    out = b""
    for name, value in fields.items():
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode()
    if file_bytes is not None:
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode() + file_bytes + b"\r\n"
    return out + f"--{boundary}--\r\n".encode()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.delenv("FORESIGHTNINE_DOCUMENT_STORE_DIR", raising=False)
    for name in list(os.environ):
        if name.startswith("FORESIGHTNINE_OCR_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client():
    return TestClient(api.app)


def account(client):
    response = client.post("/v1/auth/local/register", json={"email": f"{uuid4().hex}@example.test", "password": "correct horse battery staple!"})
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def post(client, headers, body, *, boundary=BOUNDARY, content_type=None):
    return client.post("/v2/research/sources/upload", headers={**headers, "Content-Type": content_type or f"multipart/form-data; boundary={boundary}"},
                       content=body)


def stored(metadata):
    return api.research_sources.get(metadata["owner_id"], metadata["source_id"])


# ---- memory ----------------------------------------------------------------------------------

def test_endpoint_memory_is_bounded_for_a_120_mb_chunked_upload(client):
    """Drive the ASGI app directly with a lazily generated, chunked (no Content-Length) body."""
    import asyncio

    owner = account(client)
    line = b"Revenue grew 100 million this quarter and margin improved.\n"
    unit = line * ((1 << 20) // len(line) + 1)
    chunks = 120
    head = (f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="title"\r\n\r\nBig text\r\n'
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="permissions"\r\n\r\n{json.dumps(PERMS)}\r\n'
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="big.txt"\r\n\r\n').encode()
    tail = f"\r\n--{BOUNDARY}--\r\n".encode()
    pieces = iter([head, *([unit] * chunks), tail])
    sent = []

    async def receive():
        try:
            return {"type": "http.request", "body": next(pieces), "more_body": True}
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": "/v2/research/sources/upload", "raw_path": b"/v2/research/sources/upload", "query_string": b"",
             "root_path": "", "client": ("127.0.0.1", 1234), "server": ("testserver", 80),
             "headers": [(b"host", b"testserver"), (b"authorization", owner["Authorization"].encode()),
                         (b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode())]}
    tracemalloc.start()
    try:
        asyncio.run(api.app(scope, receive, send))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 201, sent
    assert chunks * len(unit) > 120_000_000 and peak < 60_000_000, f"peak {peak:,} bytes"


# ---- endpoint --------------------------------------------------------------------------------

@needs_poppler
def test_large_pdf_upload_builds_page_map_and_is_account_private(client):
    owner, other = account(client), account(client)
    raw = build_pdf([f"Section {i:03d} gross margin" for i in range(1, 31)], padding=6_000_000)
    assert len(raw) > 5_000_000
    created = post(client, owner, multipart(raw, fields={"is_simulated": "false"}))
    assert created.status_code == 201, created.text
    metadata = created.json()
    assert metadata["retrieval_method"] == "user_pdf" and metadata["response_sha256"] == sha256(raw).hexdigest()
    assert "text" not in metadata and "original_pdf_base64" not in metadata
    assert metadata["permissions"] == PERMS and metadata["kind"] == "user_supplied"
    source = stored(metadata)
    pages = source.provenance["document_pages"]
    assert pages["page_count"] == 30 and pages["original_retained"] is False and source.original_pdf_base64 is None
    start, end, status = pages["pages"][29]
    assert status == "text" and "Section 030 gross margin" in source.text[start:end]
    topic = client.post("/v2/research/topics", headers=owner, json={"question": "Margin?", "source_ids": [metadata["source_id"]]}).json()
    path = f"/v2/research/topics/{topic['topic_id']}/versions/{topic['versions'][0]['version_id']}"
    assert client.get(path + f"/sources/{metadata['source_id']}/text", headers=owner).status_code == 200
    assert client.get(path + f"/sources/{metadata['source_id']}/text", headers=other).status_code == 404
    assert client.get(f"/v2/research/sources/{metadata['source_id']}/original", headers=other).status_code == 404
    # The streaming route does not widen who can read it: a second account never sees the first account's source.
    assert api.research_sources.repository.get(metadata["source_id"]).owner_id == metadata["owner_id"]


@needs_poppler
def test_small_pdf_upload_keeps_original_for_download(client):
    owner = account(client)
    raw = build_pdf(["Revenue 100", "Margin 20"])
    metadata = post(client, owner, multipart(raw)).json()
    assert metadata["original_pdf_available"] is True
    assert client.get(f"/v2/research/sources/{metadata['source_id']}/original", headers=owner).content == raw
    assert stored(metadata).provenance["document_pages"]["page_count"] == 2


def test_large_html_upload_is_paged_and_hidden_text_stays_out(client):
    owner = account(client)
    raw = big_html(hidden_at=6_000, hidden_lines=18_000)
    assert len(raw) > 5_000_000
    created = post(client, owner, multipart(raw, filename="filing.htm"))
    assert created.status_code == 201, created.text
    metadata = created.json()
    assert metadata["retrieval_method"] == "user_html" and metadata["response_content_type"] == "text/html"
    source = stored(metadata)
    pages = source.provenance["document_pages"]
    assert pages["method"] == "html_paged" and pages["original_retained"] is False and source.original_html_base64 is None
    assert "SECRET_HIDDEN" not in source.text and "SECRET_SCRIPT" not in source.text and "Paragraph 039999 revenue grew." in source.text


def test_plain_text_and_gbk_html_uploads(client):
    owner = account(client)
    text = ("营业收入 123 亿元\n" * 400).encode("utf-8")
    metadata = post(client, owner, multipart(text, filename="年报.txt")).json()
    source = stored(metadata)
    assert metadata["response_content_type"] == "text/plain" and "营业收入 123 亿元" in source.text
    assert source.provenance["document_pages"]["original_retained"] is True
    gbk = "<html><body><p>营业收入 456 亿元</p></body></html>".encode("gb18030")
    assert "营业收入 456 亿元" in stored(post(client, owner, multipart(gbk, filename="a.html")).json()).text


@needs_tesseract
def test_scanned_page_in_large_pdf_is_ocrd_locally(client, tmp_path):
    owner = account(client)
    scan = scanned_page_jpeg(tmp_path, "Quarterly revenue 4215 million")
    raw = build_mixed_pdf(["Cover page text", scan, "Closing page text"], padding=5_500_000)
    created = post(client, owner, multipart(raw))
    assert created.status_code == 201, created.text
    source = stored(created.json())
    statuses = [entry[2] for entry in source.provenance["document_pages"]["pages"]]
    assert statuses == ["text", "ocr", "text"]
    assert "4215" in source.text.replace(",", "") and "revenue" in source.text.lower()


@needs_poppler
def test_scanned_pdf_without_ocr_engine_is_rejected_clearly(client, monkeypatch):
    monkeypatch.setattr("app.research_ocr.default_engines", lambda: [])
    owner = account(client)
    response = post(client, owner, multipart(build_pdf([None, None])))
    assert response.status_code == 422 and "OCR" in response.json()["message"]


def test_validation_and_boundary_errors(client):
    owner = account(client)
    assert post(client, {}, multipart(b"x")).status_code == 401
    assert client.post("/v2/research/sources/upload", headers=owner, json={"a": 1}).status_code == 415
    assert post(client, owner, multipart(b"hello", filename="a.txt", fields={"permissions": json.dumps({**PERMS, "store": False})})).status_code == 422
    assert post(client, owner, multipart(b"hello", filename="a.txt", fields={"permissions": "nope"})).status_code == 422
    assert post(client, owner, multipart(None)).status_code == 422                       # no file part
    assert post(client, owner, multipart(b"hello", filename="a.txt", fields={"title": ""})).status_code == 422
    assert post(client, owner, multipart(b"hello", filename="a.txt", fields={"surprise": "1"})).status_code == 422
    assert post(client, owner, multipart(b"hello", filename="a.txt")[:-20]).status_code == 422   # truncated body
    assert post(client, owner, multipart(b"\x00\x01\x02binary", filename="a.bin")).status_code == 422
    assert post(client, owner, multipart(b"not a pdf", filename="a.pdf")).status_code == 422     # content wins over extension
    assert post(client, owner, multipart(b"")).status_code == 422


def test_duplicate_extra_and_oversized_fields_and_multiple_files_are_rejected(client):
    owner = account(client)
    good = multipart(b"hello", filename="a.txt")
    dup_title = good.replace(f"--{BOUNDARY}--".encode(), (f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="title"\r\n\r\nagain\r\n--{BOUNDARY}--').encode())
    assert post(client, owner, dup_title).status_code == 422
    second_file = good.replace(f"--{BOUNDARY}--".encode(), (f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="b.txt"\r\n\r\nmore\r\n--{BOUNDARY}--').encode())
    assert post(client, owner, second_file).status_code == 422
    other_name = good.replace(f"--{BOUNDARY}--".encode(), (f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="attachment"; filename="b.txt"\r\n\r\nmore\r\n--{BOUNDARY}--').encode())
    assert post(client, owner, other_name).status_code == 422
    assert post(client, owner, multipart(b"hello", filename="a.txt", fields={"title": "t" * 70_000})).status_code == 422
    assert post(client, owner, multipart(b"hello", filename="a.txt", fields={"permissions": "{}"})).status_code == 422


def test_authentication_happens_before_the_body_is_read(client):
    import asyncio

    async def receive():
        raise AssertionError("the body must not be read before authentication")

    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": "/v2/research/sources/upload", "raw_path": b"/v2/research/sources/upload", "query_string": b"",
             "root_path": "", "client": ("127.0.0.1", 1), "server": ("testserver", 80),
             "headers": [(b"host", b"testserver"), (b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode())]}
    asyncio.run(api.app(scope, receive, send))
    assert next(m["status"] for m in sent if m["type"] == "http.response.start") == 401


def test_byte_cap_is_enforced_on_the_stream_and_content_length(client, monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LARGE_DOCUMENT_MAX_BYTES", "1000000")
    owner = account(client)
    over = multipart(b"a" * 1_200_000, filename="a.txt")
    assert post(client, owner, over).status_code == 413
    # No Content-Length (chunked): the receive stream is capped by the endpoint itself.
    def chunks():
        for index in range(0, len(over) + 3_000_000, 65536):
            yield (over + b"\0" * 3_000_000)[index:index + 65536] if index < len(over) else b"z" * 65536
    streamed = client.post("/v2/research/sources/upload", headers={**owner, "Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}, content=chunks())
    assert streamed.status_code == 413
    huge = client.post("/v2/research/sources/upload", headers={**owner, "Content-Type": f"multipart/form-data; boundary={BOUNDARY}", "Content-Length": "9999999999"}, content=b"x")
    assert huge.status_code in {400, 413}


def test_base64_endpoints_keep_their_limits(client):
    owner = account(client)
    import base64
    ok = client.post("/v2/research/sources/html", headers=owner, json={"title": "t", "html_base64": base64.b64encode(b"<p>hi</p>").decode(), "permissions": PERMS})
    assert ok.status_code == 201
    too_big = client.post("/v2/research/sources/html", headers=owner, content=b"{" + b" " * 7_000_000 + b"}", )
    assert too_big.status_code in {413, 415}


def test_ingest_without_store_permission_and_sniffing(tmp_path):
    path = tmp_path / "x.bin"
    path.write_bytes(b"%PDF-1.4 junk")
    assert sniff_upload_kind(path, "x.html") == "pdf"
    path.write_bytes(b"<!DOCTYPE html><html></html>")
    assert sniff_upload_kind(path, "x") == "html"
    path.write_bytes(b"\x00\x00\x00")
    with pytest.raises(FetchFailure):
        sniff_upload_kind(path, "x.txt")
    path.write_bytes(b"hello")
    with pytest.raises(FetchFailure) as denied:
        ingest_uploaded_file(path, kind="text", title="t", permissions=SourcePermissions(store=False), sha256_hex="0" * 64, size=5)
    assert denied.value.code == "source_permission_unknown"


def test_fetch_endpoint_reports_terms_block(client):
    owner = account(client)
    response = client.post("/v2/research/sources/fetch", headers=owner, json={"url": "https://www1.hkexnews.hk/listedco/a.pdf", "title": "AR", "permissions": PERMS})
    assert response.status_code == 422 and "terms do not permit automated access" in response.json()["message"]
