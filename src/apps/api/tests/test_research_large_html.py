"""Large HTML / plain text and SEC filings: streamed to disk, parsed incrementally, paged (html_paged)."""
import base64
from hashlib import sha256
from pathlib import Path
import tracemalloc

import pytest

import app.research_fetch as research_fetch
import app.research_sec as research_sec
from app.openai_compatible import HttpResponse, _default_binary_transport
from app.research_documents import HTML_PAGE_CHARS, TextPager, page_for_offset, page_range_to_chars
from app.research_fetch import FetchFailure, FetchSourceRequest, extract_text_pages, fetch_official_text, fetch_source_document
from app.research_sec import SecFailure, fetch_sec_filing
from tests.test_research_large_documents import local_transport, server  # noqa: F401  (fixtures)

SEC_URL = "https://www.sec.gov/Archives/edgar/data/1234567/000123456724000001/s1.htm"
UA = "ForesightNine Test ops@example.com"


def req(url="https://news.example.com/story"):
    return FetchSourceRequest(url=url, title="Large page", permissions={"store": True})


def big_html(*, paragraphs=40_000, hidden_at=None, hidden_lines=0) -> bytes:
    """>5 MB of markup carrying ~1.2M characters of text; optionally a hidden block straddling chunk boundaries."""
    attrs = 'class="article-paragraph" style="margin:0;padding:0;color:#333333;font-family:Georgia,serif;line-height:1.4"'
    out = ["<html><head><meta charset='utf-8'><title>t</title><script>var SECRET_SCRIPT=1;</script></head><body>"]
    for i in range(paragraphs):
        if hidden_at is not None and i == hidden_at:
            out.append('<div style="display:none">')
            out.extend(f"<p>SECRET_HIDDEN line {j} " + "h" * 40 + "</p>" for j in range(hidden_lines))
            out.append("</div>")
        if i % 500 == 0:
            out.append(f"<h2>Section {i // 500}</h2>")
        out.append(f"<p {attrs}>Paragraph {i:06d} revenue grew.</p>")
    out.append("</body></html>")
    return "\n".join(out).encode()


def assert_page_invariants(text: str, pages: dict) -> None:
    included = [p for p in pages["pages"] if p[2] == "text"]
    assert included and pages["method"] == "html_paged" and pages["version"] == 2
    assert pages["page_count"] == len(pages["pages"])
    previous_end = -1
    for start, end, status in included:
        assert start == previous_end + 1                       # contiguous, joined by one newline
        assert start == 0 or text[start - 1] == "\n"           # never starts mid-line
        assert end == len(text) or text[end] == "\n"           # never ends mid-line
        previous_end = end
    assert previous_end == len(text)
    for start, end, _ in included[:-1]:
        assert end - start >= HTML_PAGE_CHARS                  # pages close once they reach ~8,000 characters
    assert max(end - start for start, end, _ in included) < HTML_PAGE_CHARS + 200  # short lines: no runaway page


def test_large_html_streams_pages_and_keeps_hidden_text_out_across_chunk_boundaries(server, local_transport):  # noqa: F811
    # The hidden block starts ~0.8 MB in and is ~2 MB long, so it crosses at least one 1 MB read boundary.
    raw = big_html(hidden_at=6_000, hidden_lines=18_000)
    assert len(raw) > research_fetch.MAX_FETCH_BYTES
    server["type"], server["body"] = "text/html; charset=utf-8", raw
    tracemalloc.start()
    try:
        doc = fetch_source_document(req())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 60_000_000, f"peak {peak:,} bytes"           # bounded: far below the 6 MB x parser blow-up
    assert doc.response_sha256 == sha256(raw).hexdigest() and doc.content_type == "text/html"
    assert doc.original_html_base64 is None and doc.pages["original_retained"] is False
    assert "SECRET_HIDDEN" not in doc.text and "SECRET_SCRIPT" not in doc.text
    assert "Paragraph 039999 revenue grew." in doc.text and "Section 79" in doc.text
    assert_page_invariants(doc.text, doc.pages)
    assert doc.pages["text_limit_reached"] is False and "page_count_complete" not in doc.pages
    # The page contract used by read_pages / outline_source works on the map.
    last = doc.pages["page_count"]
    start, end = page_range_to_chars(doc.pages, last, last)
    assert "Paragraph 039999" in doc.text[start:end] and page_for_offset(doc.pages, start) == last
    first, second = page_range_to_chars(doc.pages, 1, 2)
    assert first == 0 and doc.text[:second].count("\n") > 100


def test_large_html_original_goes_to_blob_store_when_configured(server, local_transport, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("FORESIGHTNINE_DOCUMENT_STORE_DIR", str(tmp_path / "blobs"))
    raw = big_html(paragraphs=36_000)
    assert len(raw) > research_fetch.MAX_FETCH_BYTES
    server["type"], server["body"] = "text/html", raw
    doc = fetch_source_document(req())
    digest = doc.pages["original_blob_sha256"]
    assert doc.original_html_base64 is None and doc.pages["original_retained"] is True
    assert digest == doc.response_sha256 == sha256(raw).hexdigest()
    assert (tmp_path / "blobs" / digest[:2] / digest).read_bytes() == raw


def test_large_plain_text_over_text_limit_marks_remaining_pages_not_included(server, local_transport):  # noqa: F811
    lines = [f"line {i:07d} " + "x" * 36 for i in range(125_000)]          # 49 chars + newline each, ~6.2 MB
    raw = ("\n".join(lines) + "\n").encode()
    assert len(raw) > research_fetch.MAX_FETCH_BYTES
    server["type"], server["body"] = "text/plain; charset=utf-8", raw
    doc = fetch_source_document(req("https://example.com/big.txt"))
    assert len(doc.text) <= research_fetch.MAX_EXTRACTED_TEXT_CHARS and doc.pages["text_limit_reached"] is True
    statuses = [p[2] for p in doc.pages["pages"]]
    assert statuses[0] == "text" and statuses[-1] == "not_included" and "not_included" in statuses
    included = statuses.count("text")
    assert statuses == ["text"] * included + ["not_included"] * (len(statuses) - included)
    assert all(p[0] is None and p[1] is None for p in doc.pages["pages"][included:])
    # ~6.1M characters in total -> roughly 760 pages of ~8,000 characters, only the first ~250 included.
    assert 700 < doc.pages["page_count"] < 800 and 240 < included < 260
    assert doc.pages["original_retained"] is False and "page_count_complete" not in doc.pages
    assert_page_invariants_included_only(doc.text, doc.pages)
    assert page_range_to_chars(doc.pages, doc.pages["page_count"], doc.pages["page_count"]) is None


def assert_page_invariants_included_only(text, pages):
    included = [p for p in pages["pages"] if p[2] == "text"]
    for start, end, _ in included:
        assert start == 0 or text[start - 1] == "\n"
        assert end == len(text) or text[end] == "\n"
    assert included[-1][1] == len(text)


def test_small_html_and_text_get_a_page_map_and_keep_the_inline_original():
    html = b"<html><body><h1>Title</h1><p>Revenue rose 12%.</p><p>Costs fell.</p></body></html>"
    doc = fetch_source_document(req(), lambda *a: __import__("app.openai_compatible", fromlist=["x"]).BinaryHttpResponse(
        200, html, {"content-type": "text/html"}))
    assert doc.text == "Title\nRevenue rose 12%.\nCosts fell."
    assert doc.pages["page_count"] == 1 and doc.pages["pages"] == [[0, len(doc.text), "text"]]
    assert doc.pages["original_retained"] is True and base64.b64decode(doc.original_html_base64) == html
    text = fetch_official_text(req("https://example.com/a.txt"), lambda *a: __import__(
        "app.openai_compatible", fromlist=["x"]).BinaryHttpResponse(200, b"  one\n\n  two  \n\n", {"content-type": "text/plain"}))
    assert text.text == "one\n\n  two" and text.pages["page_count"] == 1 and text.original_html_base64 is None


def test_non_pdf_cap_is_the_large_document_cap(server, local_transport, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FORESIGHTNINE_LARGE_DOCUMENT_MAX_BYTES", "1000000")
    server["type"], server["body"] = "text/html", big_html(paragraphs=10_000)
    assert len(server["body"]) > 1_000_000
    with pytest.raises(FetchFailure) as error:
        fetch_source_document(req())
    assert error.value.code == "source_too_large" and "1,000,000" in (error.value.detail or "")


def test_single_line_longer_than_the_text_limit_is_too_large():
    with pytest.raises(FetchFailure, match="source_too_large"):
        extract_text_pages(b"x" * 2_000_001, "text/plain", is_html=False)
    with pytest.raises(FetchFailure, match="source_invalid"):
        extract_text_pages(b"<html><script>x</script></html>", "text/html", is_html=True)


def test_encoding_fallback_restarts_from_the_file_when_a_late_byte_is_invalid(tmp_path):
    path = tmp_path / "late.html"
    path.write_bytes(b"<p>" + b"a" * 1_500_000 + b"</p><p>" + "营业收入".encode("gbk") + b"</p>")
    text, pages = extract_text_pages(path, "text/html", is_html=True)
    assert text.endswith("营业收入") and pages["page_count"] >= 1


def test_text_pager_never_splits_lines_and_counts_pages_past_the_limit():
    pager = TextPager(max_text_chars=500, page_chars=100)
    for i in range(100):
        pager.add_line(f"line {i:03d} " + "y" * 30)
    text, pages = pager.finish()
    assert pages["text_limit_reached"] is True and len(text) <= 500
    included = [p for p in pages["pages"] if p[2] == "text"]
    for start, end, _ in included:
        assert text[start:end] == text[start:end].strip("\n") and text[start:end].count("\n") >= 1
    assert pages["page_count"] > len(included) and pages["pages"][-1][2] == "not_included"
    long_pager = TextPager(page_chars=100)
    long_pager.add_line("z" * 1000)
    long_pager.add_line("tail")
    long_text, long_pages = long_pager.finish()
    assert long_pages["pages"] == [[0, 1000, "text"], [1001, 1005, "text"]] and long_text[1001:1005] == "tail"


# ---- SEC filings ---------------------------------------------------------------------------

def sec_request():
    return FetchSourceRequest(url=SEC_URL, title="S-1", permissions={"store": True, "display": True, "model_input": True, "export": True})


@pytest.fixture
def sec_local(server, monkeypatch):  # noqa: F811
    seen = []

    def transport(method, url, headers, body, timeout, **kwargs):
        seen.append((url, dict(headers), timeout))
        return _default_binary_transport(method, server["url"], headers, body, timeout, allow_private_network=True, **kwargs)
    monkeypatch.setattr(research_sec, "_default_binary_transport", transport)
    server["type"] = "text/html; charset=utf-8"
    return seen


def test_sec_large_filing_streams_pages_and_retains_original_in_blob_store(server, sec_local, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("FORESIGHTNINE_DOCUMENT_STORE_DIR", str(tmp_path / "blobs"))
    raw = big_html(paragraphs=36_000, hidden_at=5_000, hidden_lines=15_000)
    assert len(raw) > research_fetch.MAX_FETCH_BYTES
    server["body"] = raw
    result = fetch_sec_filing(sec_request(), UA)
    url, headers, _ = sec_local[0]
    assert url == SEC_URL and headers == {"User-Agent": UA, "Accept": "text/html"}   # pacing/identity unchanged
    assert result.original_base64 is None and result.response_sha256 == sha256(raw).hexdigest()
    assert result.pages["original_blob_sha256"] == result.response_sha256 and result.pages["original_retained"] is True
    assert "SECRET_HIDDEN" not in result.text and "Paragraph 035999 revenue grew." in result.text
    assert_page_invariants(result.text, result.pages)


def test_sec_large_filing_without_blob_store_is_not_retained(server, sec_local, monkeypatch):  # noqa: F811
    monkeypatch.delenv("FORESIGHTNINE_DOCUMENT_STORE_DIR", raising=False)
    server["body"] = big_html(paragraphs=36_000)
    result = fetch_sec_filing(sec_request(), UA)
    assert result.original_base64 is None and result.pages["original_retained"] is False
    assert "original_blob_sha256" not in result.pages


def test_sec_small_filing_keeps_inline_original_and_gets_pages():
    raw = b"<html><body><p>Item 1. Business</p><p>Revenue 100</p></body></html>"
    result = fetch_sec_filing(sec_request(), UA, lambda *a: HttpResponse(200, raw.decode(), {"content-type": "text/html"}))
    assert base64.b64decode(result.original_base64) == raw and result.text == "Item 1. Business\nRevenue 100"
    assert result.pages["page_count"] == 1 and result.pages["original_retained"] is True


def test_sec_injected_transport_accepts_more_than_five_mb(monkeypatch, tmp_path):
    monkeypatch.setenv("FORESIGHTNINE_DOCUMENT_STORE_DIR", str(tmp_path / "blobs"))
    raw = big_html(paragraphs=36_000)
    result = fetch_sec_filing(sec_request(), UA, lambda *a: HttpResponse(200, raw.decode(), {"content-type": "text/html"}))
    assert result.original_base64 is None and result.pages["original_blob_sha256"] == sha256(raw).hexdigest()


def test_sec_oversize_over_large_cap_is_invalid(server, sec_local, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FORESIGHTNINE_LARGE_DOCUMENT_MAX_BYTES", "1000000")
    server["body"] = big_html(paragraphs=10_000)
    with pytest.raises(SecFailure, match="source_invalid"):
        fetch_sec_filing(sec_request(), UA)


def test_sec_filing_index_page_url_still_validates():
    index = "https://www.sec.gov/Archives/edgar/data/1234567/000123456724000001/0001234567-24-000001-index.htm"
    request = FetchSourceRequest(url=index, title="Index", permissions={"store": True})
    html = b"<html><body><table><tr><td>1</td><td>s1.htm</td><td>S-1</td></tr></table></body></html>"
    result = fetch_sec_filing(request, UA, lambda *a: HttpResponse(200, html.decode(), {"content-type": "text/html"}))
    assert "s1.htm" in result.text and result.pages["page_count"] == 1
