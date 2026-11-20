from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import shutil
from threading import Thread
from time import monotonic

import pytest

import app.research_fetch as research_fetch
from app.openai_compatible import BinaryHttpResponse, _TransportFailure, _default_binary_transport
from app.research_documents import (
    classify_page_text,
    extract_pdf_pages,
    page_for_offset,
    page_map_from_text,
    page_range_to_chars,
)
from app.research_fetch import FetchFailure, FetchSourceRequest, fetch_official_pdf, fetch_source_document

needs_poppler = pytest.mark.skipif(
    not (shutil.which("pdfinfo") and shutil.which("pdftotext") and shutil.which("prlimit")),
    reason="poppler-utils and prlimit are required",
)


def build_pdf(pages: list[str | None], *, padding: int = 0) -> bytes:
    """Tiny PDF writer. A None page has an empty content stream (simulates a scanned page)."""
    objects: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 << /Type /Font /Subtype /Font1 /BaseFont /Helvetica >> >> >> >>".replace(
                "/Subtype /Font1", "/Subtype /Type1").encode())
        stream = b"" if text is None else f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET".encode()
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    if padding:
        objects.append(b"<< /Length %d >>\nstream\n" % padding + b"\x00" * padding + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def write_pdf(tmp_path, pages, **kwargs):
    path = tmp_path / "doc.pdf"
    path.write_bytes(build_pdf(pages, **kwargs))
    return path


@pytest.fixture
def server(monkeypatch):
    import app.openai_compatible as oc
    # Plain-HTTP private targets are only allowed for local gateway hosts; treat loopback as one here.
    monkeypatch.setattr(oc, "_LOCAL_GATEWAY_HOSTS", frozenset({"127.0.0.1"}))
    state = {"body": b"", "type": "application/pdf", "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(state["status"])
            self.send_header("Content-Type", state["type"])
            self.send_header("Content-Length", str(len(state["body"])))
            self.end_headers()
            try:
                self.wfile.write(state["body"])
            except OSError:
                pass

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{httpd.server_address[1]}/doc"
    yield state
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def local_transport(monkeypatch, server):
    """Route the real default transport to the local server (private network allowed)."""
    def transport(method, url, headers, body, timeout, **kwargs):
        return _default_binary_transport(method, server["url"], headers, body, timeout,
                                         allow_private_network=True, **kwargs)
    monkeypatch.setattr(research_fetch, "_default_binary_transport", transport)
    return transport


def req(url="https://docs.example.com/report.pdf"):
    return FetchSourceRequest(url=url, title="Large", permissions={"store": True})


def page_text(document_pages, text, number):
    start, end, _ = document_pages["pages"][number - 1]
    return text[start:end]


# ---- page helpers -------------------------------------------------------------------------

def test_page_map_from_text_and_helpers():
    text = "alpha\fbeta\f \fgamma\f"
    pages = page_map_from_text(text, original_retained=True)
    assert pages["page_count"] == 4 and pages["original_retained"] is True
    assert pages["method"] == "pdftotext_layout" and pages["version"] == 2
    assert [p[2] for p in pages["pages"]] == ["text", "text", "needs_ocr", "text"]
    assert page_text(pages, text, 2) == "beta"
    assert pages["pages"][2][0] == pages["pages"][2][1] == text.index("\f", 11)
    assert page_for_offset(pages, 0) == 1 and page_for_offset(pages, 6) == 2
    assert page_for_offset(pages, text.index("gamma")) == 4
    assert page_for_offset(pages, 10_000) is None
    start, end = page_range_to_chars(pages, 2, 4)
    assert text[start:end].startswith("beta") and text[start:end].endswith("gamma")
    assert page_range_to_chars(pages, 9, 10) is None
    assert page_map_from_text("", original_retained=False)["page_count"] == 0
    assert page_map_from_text("one", original_retained=False)["page_count"] == 1


def test_page_classification():
    assert classify_page_text("  \n ") == "needs_ocr"
    assert classify_page_text("���ab") == "needs_ocr"
    assert classify_page_text("x") == "needs_ocr"
    assert classify_page_text("Revenue 12.5") == "text"


def test_page_range_skips_not_included():
    pages = {"pages": [[0, 3, "text"], [None, None, "failed"], [None, None, "not_included"]]}
    assert page_range_to_chars(pages, 2, 3) is None
    assert page_range_to_chars(pages, 1, 3) == (0, 3)


# ---- extraction ---------------------------------------------------------------------------

@needs_poppler
def test_multi_chunk_parallel_extraction_page_map(tmp_path):
    labels = [f"Page number {i:03d} revenue" for i in range(1, 46)]
    labels[6] = None  # scanned page in the first chunk
    labels[30] = None
    path = write_pdf(tmp_path, labels)
    result = extract_pdf_pages(path, deadline=monotonic() + 120, workers=3)
    dp = result.document_pages
    assert dp["page_count"] == 45 and len(dp["pages"]) == 45
    assert dp["method"] == "pdftotext_layout_paged" and dp["original_retained"] is False
    assert dp["text_limit_reached"] is False and dp["version"] == 2
    statuses = [p[2] for p in dp["pages"]]
    assert statuses[6] == "needs_ocr" and statuses[30] == "needs_ocr"
    assert statuses.count("text") == 43
    for number in range(1, 46):
        if labels[number - 1] is None:
            start, end, _ = dp["pages"][number - 1]
            assert start == end and result.text[start] == "\f"
        else:
            assert f"Page number {number:03d} revenue" in page_text(dp, result.text, number)
    assert result.text.count("\f") == 45
    assert page_for_offset(dp, dp["pages"][44][0]) == 45


@needs_poppler
def test_text_limit_marks_remaining_pages_not_included(tmp_path):
    path = write_pdf(tmp_path, [f"Page {i:03d} body text" for i in range(1, 61)])
    result = extract_pdf_pages(path, deadline=monotonic() + 120, workers=2, max_text_chars=400)
    dp = result.document_pages
    assert dp["text_limit_reached"] is True and len(result.text) <= 400
    statuses = [p[2] for p in dp["pages"]]
    assert statuses[0] == "text" and statuses[-1] == "not_included"
    first_excluded = statuses.index("not_included")
    assert set(statuses[first_excluded:]) == {"not_included"}
    assert all(p[0] is None and p[1] is None for p in dp["pages"][first_excluded:])


@needs_poppler
def test_expired_deadline_marks_pages_failed(tmp_path):
    path = write_pdf(tmp_path, ["a page", "b page"])
    result = extract_pdf_pages(path, deadline=monotonic() - 1)  # no budget left for any chunk
    assert [p[2] for p in result.document_pages["pages"]] == ["failed", "failed"]
    assert result.text == "" and result.document_pages["pages"][0][:2] == [None, None]


# ---- fetch pipeline -----------------------------------------------------------------------

@needs_poppler
def test_large_pdf_streams_and_builds_page_map(server, local_transport):
    pages = [f"Section {i:03d} gross margin" for i in range(1, 31)]
    raw = build_pdf(pages, padding=6_000_000)
    assert len(raw) > research_fetch.MAX_FETCH_BYTES
    server["body"] = raw
    doc = fetch_source_document(req())
    assert doc.original_pdf_base64 is None and doc.content_type == "application/pdf"
    assert doc.pages["page_count"] == 30 and doc.pages["original_retained"] is False
    from hashlib import sha256
    assert doc.response_sha256 == sha256(raw).hexdigest()
    assert "Section 030 gross margin" in page_text(doc.pages, doc.text, 30)


@needs_poppler
def test_small_pdf_keeps_original_and_attaches_page_map(server, local_transport):
    raw = build_pdf(["First page text", "Second page text"])
    server["body"] = raw
    doc = fetch_source_document(req())
    assert doc.original_pdf_base64 is not None
    assert doc.pages["method"] == "pdftotext_layout" and doc.pages["original_retained"] is True
    assert doc.pages["page_count"] == 2
    assert "Second page text" in page_text(doc.pages, doc.text, 2)


@needs_poppler
def test_scanned_pdf_small_and_large_require_ocr(server, local_transport):
    server["body"] = build_pdf([None, None, None])
    with pytest.raises(FetchFailure) as small:
        fetch_source_document(req())
    assert small.value.code == "ocr_required" and "3 pages" in (small.value.detail or "")
    server["body"] = build_pdf([None] * 4, padding=6_000_000)
    with pytest.raises(FetchFailure) as large:
        fetch_source_document(req())
    assert large.value.code == "ocr_required" and "4 pages" in (large.value.detail or "")


@needs_poppler
def test_official_pdf_large_has_no_original(server, local_transport):
    server["body"] = build_pdf(["Alpha"] * 3, padding=5_500_000)
    result = fetch_official_pdf(req())
    assert result.original_pdf_base64 is None and result.pages["page_count"] == 3


# ---- swallowed error / size limits --------------------------------------------------------

def test_oversize_body_without_sink_reports_source_too_large(server, monkeypatch):
    server["body"] = b"x" * 200_000
    server["type"] = "text/plain"

    def transport(method, url, headers, body, timeout, **kwargs):
        return _default_binary_transport(method, server["url"], headers, body, timeout,
                                         allow_private_network=True, max_response_bytes=100_000)
    with pytest.raises(_TransportFailure, match="response too large"):
        transport("GET", "x", {}, b"", 5)
    # Through research_fetch: the message must survive so the failure maps to source_too_large.
    monkeypatch.setattr(research_fetch, "_default_binary_transport", transport)
    with pytest.raises(FetchFailure) as error:
        fetch_source_document(req("https://docs.example.com/big.txt"))
    assert error.value.code == "source_too_large"


def test_oversize_streamed_pdf_reports_source_too_large(server, local_transport, monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LARGE_DOCUMENT_MAX_BYTES", "100000")
    server["body"] = build_pdf(["x"], padding=300_000)
    with pytest.raises(FetchFailure) as error:
        fetch_source_document(req())
    assert error.value.code == "source_too_large" and "100,000" in (error.value.detail or "")


def test_sink_streams_body_and_non_2xx_writes_nothing(server):
    server["body"] = b"y" * 3_000_000
    sink = io.BytesIO()
    response = _default_binary_transport("GET", server["url"], {}, b"", 10, allow_private_network=True,
                                         max_response_bytes=10_000_000, sink=sink)
    assert response.status_code == 200 and response.body == b"" and len(sink.getvalue()) == 3_000_000
    server["status"] = 500
    server["body"] = b"z" * 200_000
    failed_sink = io.BytesIO()
    response = _default_binary_transport("GET", server["url"], {}, b"", 10, allow_private_network=True,
                                         max_response_bytes=10_000_000, sink=failed_sink)
    assert response.status_code == 500 and failed_sink.getvalue() == b""


def test_non_pdf_body_above_five_mb_is_paged_not_rejected(server, local_transport):
    # The 5 MB cap now applies only to in-memory originals; larger text streams to disk and is paged.
    server["type"] = "text/plain"
    server["body"] = b"a" * 5_500_000  # one unbroken line far above the text limit still cannot be paged
    with pytest.raises(FetchFailure) as error:
        fetch_source_document(req("https://docs.example.com/big.txt"))
    assert error.value.code == "source_too_large" and "2,000,000 characters" in (error.value.detail or "")
    server["body"] = b"".join(b"line %07d of the document\n" % i for i in range(200_000))
    assert len(server["body"]) > research_fetch.MAX_FETCH_BYTES
    doc = fetch_source_document(req("https://docs.example.com/big.txt"))
    assert doc.pages["method"] == "html_paged" and doc.pages["page_count"] > 100
