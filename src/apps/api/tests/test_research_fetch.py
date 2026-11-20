import base64
from hashlib import sha256
from pathlib import Path
import shutil

import pytest
from pydantic import ValidationError

import app.research_fetch as research_fetch
from app.openai_compatible import BinaryHttpResponse, HttpResponse, _TransportFailure, _TransportTimeout
from app.research_fetch import (
    FetchFailure,
    FetchSourceRequest,
    fetch_official_pdf,
    fetch_official_text,
    fetch_source_document,
)


def request(url="https://www.bea.gov/news/2024/example"):
    return FetchSourceRequest(url=url, title="Official source", permissions={"store": True})


def test_extracts_visible_text_without_scripts_and_records_source_hash():
    def transport(method, url, headers, body, timeout):
        assert method == "GET"
        assert url.startswith("https://www.bea.gov/")
        assert "Authorization" not in headers
        return HttpResponse(200, "<html><nav>navigation</nav><h1>Report</h1><script>bad()</script><p>GDP 2.5%</p></html>",
                            {"content-type": "text/html; charset=utf-8"})
    result = fetch_official_text(request(), transport)
    assert "Report" in result.text and "GDP 2.5%" in result.text
    assert "navigation" not in result.text and "bad()" not in result.text
    assert len(result.response_sha256) == 64
    original = base64.b64decode(result.original_html_base64)
    assert b'<script>bad()</script>' in original
    assert sha256(original).hexdigest() == result.response_sha256


def test_table_extraction_preserves_cell_order_empty_cells_and_bounded_spans():
    body = ('<table><tr><th rowspan="2"><p>Period</p></th><th colspan="2">September</th></tr>'
            '<tr><th>Previous</th><th>Revised</th></tr>'
            '<tr>\n <td>Income</td>\n <td>74.4</td>\n <td>88.2</td>\n</tr>'
            '<tr><td>Other</td><td></td><td><span>7</span><script>ignored</script></td></tr>'
            '<tr><td colspan="999999999999999999999999">Untrusted span</td></tr></table>')
    result = fetch_official_text(request(), lambda *args: HttpResponse(200, body, {"content-type": "text/html"}))
    rows = result.text.splitlines()
    assert rows[0] == "| [rowspan=2]  Period  | [colspan=2] September |"
    assert rows[1] == "| Previous | Revised |"
    assert rows[2] == "| Income | 74.4 | 88.2 |"
    assert rows[3] == "| Other |  | 7 |"
    assert rows[4] == "| Untrusted span |"
    assert "ignored" not in result.text


@pytest.mark.parametrize("url", [
    "https://fred.stlouisfed.org/series/GDP",
    "https://api.stlouisfed.org/fred/series?series_id=GDP",
    "https://127.0.0.1/",
    "https://127.1/",
    "https://2130706433/",
    "https://[::ffff:127.0.0.1]/",
    "https://100.64.0.1/",
    "https://localhost/",
    "https://internal.local/",
    "https://www.sec.gov.evil.test/Archives/edgar/data/1/000000000000000001/foo.htm",
    "https://example.com/#fragment",
    "http://www.bea.gov/",
    "https://user:password@www.bea.gov/",
    "https://www.bea.gov:8443/",
])
def test_rejects_unsupported_or_unsafe_hosts(url):
    with pytest.raises(ValidationError):
        request(url)


def test_accepts_open_public_https_hosts():
    for url in (
        "https://ir.nvidia.com/news/press-release?id=1",
        "https://example.com/reports/q4",
        "https://www.bea.gov/news/2024/example",
    ):
        assert str(request(url).url) == url


def test_redirects_and_timeouts_fail_without_fallback():
    with pytest.raises(FetchFailure, match="source_unavailable"):
        fetch_official_text(request(), lambda *args: HttpResponse(302, "", {"location": "https://127.0.0.1"}))

    def timeout(*args):
        raise _TransportTimeout()

    with pytest.raises(FetchFailure, match="source_timeout"):
        fetch_official_text(request(), timeout)


def test_official_fetch_respects_caller_timeout_budget():
    seen = []

    def transport(method, url, headers, body, timeout):
        seen.append(timeout)
        return HttpResponse(200, 'Official text', {'content-type': 'text/plain'})

    assert fetch_official_text(request(), transport, timeout_seconds=2.5).text == 'Official text'
    assert seen == [2.5]
    for timeout in (0, -1, 16):
        with pytest.raises(FetchFailure, match='source_timeout'):
            fetch_official_text(request(), transport, timeout_seconds=timeout)
    assert seen == [2.5]


def test_official_pdf_fetch_is_bounded_and_preserves_exact_original():
    path = Path(__file__).resolve().parents[3] / 'docs/product/phase-a/231-new-cross-domain-check/bea-2020-historical.pdf'
    if not path.is_file() or not shutil.which('pdftotext') or not shutil.which('prlimit'):
        pytest.skip('retained public PDF or extraction tools unavailable')
    raw = path.read_bytes()
    seen = []

    def transport(method, url, headers, body, timeout):
        seen.append((method, url, timeout))
        return BinaryHttpResponse(200, raw, {'content-type': 'application/pdf'})

    result = fetch_official_pdf(request('https://example.com/news/example.pdf'), timeout_seconds=10, transport=transport)
    assert len(seen) == 1 and seen[0][0] == 'GET' and seen[0][2] <= 10
    assert base64.b64decode(result.original_pdf_base64) == raw
    assert result.response_sha256 == sha256(raw).hexdigest() and 'Personal' in result.text
    with pytest.raises(FetchFailure, match='source_format_unsupported'):
        fetch_official_pdf(request('https://www.bea.gov/news/example.pdf'), transport=lambda *a: BinaryHttpResponse(200, raw, {'content-type': 'text/html'}))
    with pytest.raises(FetchFailure, match='source_invalid'):
        fetch_official_pdf(request('https://www.bea.gov/news/example.pdf'), transport=lambda *a: BinaryHttpResponse(200, b'not pdf', {'content-type': 'application/pdf'}))
    with pytest.raises(FetchFailure, match='source_unavailable'):
        fetch_official_pdf(request('https://www.bea.gov/news/example.pdf'), transport=lambda *a: BinaryHttpResponse(302, b'', {'location': 'https://localhost'}))


def test_safe_html_parser_strips_hidden_dom_and_xbrl_headers():
    html = (
        '<ix:header><xbrl>HIDDEN XBRL 999</xbrl></ix:header>'
        '<p>Visible headline</p>'
        '<div hidden><span>secret attr</span><input></div>'
        '<div style="color:red; DISPLAY : none !important;">secret display none<p>nested hidden</p></div>'
        '<span style="visibility:hidden">secret visibility</span>'
        '<p style="display:block">Visible paragraph</p>'
    )
    doc = fetch_source_document(
        request("https://ir.nvidia.com/news/release"),
        lambda *args: BinaryHttpResponse(200, html.encode("utf-8"), {"content-type": "text/html; charset=utf-8"}),
    )
    assert "Visible headline" in doc.text and "Visible paragraph" in doc.text
    for hidden_text in ("HIDDEN XBRL", "secret attr", "secret display none", "nested hidden", "secret visibility"):
        assert hidden_text not in doc.text


def test_unified_fetch_handles_open_html_text_and_pdf_without_pdf_suffix():
    plain = fetch_source_document(
        request("https://example.org/plain"),
        lambda *a: BinaryHttpResponse(200, b"Plain release text", {"content-type": "text/plain"}),
    )
    assert plain.content_type == "text/plain" and plain.text == "Plain release text"
    assert plain.original_html_base64 is None and plain.original_pdf_base64 is None

    path = Path(__file__).resolve().parents[3] / 'docs/product/phase-a/231-new-cross-domain-check/bea-2020-historical.pdf'
    if not path.is_file() or not shutil.which('pdftotext') or not shutil.which('prlimit'):
        pytest.skip('retained public PDF or extraction tools unavailable')
    raw_pdf = path.read_bytes()
    pdf_doc = fetch_source_document(
        request("https://ir.example.com/download?doc=2024q4"),
        lambda *a: BinaryHttpResponse(200, raw_pdf, {"Content-Type": "application/pdf"}),
    )
    assert pdf_doc.content_type == "application/pdf"
    assert pdf_doc.final_url == "https://ir.example.com/download?doc=2024q4"
    assert base64.b64decode(pdf_doc.original_pdf_base64) == raw_pdf
    assert "Personal" in pdf_doc.text


def test_supports_over_256kb_up_to_5mb_and_rejects_oversized(monkeypatch):
    # >256 KB raw body and >200,000 extracted chars now succeed cleanly up to 5 MB / 2,000,000 chars.
    large_body = ("Revenue rose 12%. " * 20_000).encode("utf-8")
    assert 256 * 1024 < len(large_body) < 5_000_000
    seen_limits = []

    def fake_binary_transport(method, url, headers, body, timeout, *, max_response_bytes=0, **kwargs):
        seen_limits.append(max_response_bytes)
        return BinaryHttpResponse(200, large_body, {"content-type": "text/plain; charset=utf-8"})

    monkeypatch.setattr(research_fetch, "_default_binary_transport", fake_binary_transport)
    doc = fetch_source_document(request("https://example.com/long-filing"))
    assert seen_limits == [research_fetch.large_document_max_bytes()]
    assert len(doc.text) > 200_000

    # Near 5 MB boundary passes; >5 MB fails with source_too_large.
    near_cap = b"A" * 4_999_000
    # Extracted text limit is 2,000,000 chars; pad with HTML comments/whitespace so raw is ~4.99 MB and text is < 2M chars.
    near_cap_html = b"<p>OK</p><!--" + (b"x" * 4_998_000) + b"-->"
    ok_near_cap = fetch_source_document(
        request("https://example.com/near-cap"),
        lambda *a: BinaryHttpResponse(200, near_cap_html, {"content-type": "text/html"}),
    )
    assert ok_near_cap.text == "OK"

    with pytest.raises(FetchFailure, match="source_too_large"):
        fetch_source_document(
            request("https://example.com/too-big"),
            lambda *a: BinaryHttpResponse(200, near_cap + b"B" * 2000, {"content-type": "text/plain"}),
        )

    def too_large_transport(*a, **kw):
        raise _TransportFailure("response too large")

    with pytest.raises(FetchFailure, match="source_too_large"):
        fetch_source_document(request("https://example.com/stream-too-big"), too_large_transport)

    with pytest.raises(FetchFailure, match="source_too_large"):
        fetch_source_document(
            request("https://example.com/over-text-limit"),
            lambda *a: BinaryHttpResponse(200, b"x" * 2_000_001, {"content-type": "text/plain"}),
        )


def test_decodes_non_utf8_encodings_via_header_meta_and_fallbacks():
    gbk_html = "<p>英伟达营业收入增长 126%</p>".encode("gbk")
    doc_header = fetch_source_document(
        request("https://example.com/gbk"),
        lambda *a: BinaryHttpResponse(200, gbk_html, {"content-type": "text/html; charset=GBK"}),
    )
    assert "英伟达营业收入增长 126%" in doc_header.text
    assert doc_header.response_sha256 == sha256(gbk_html).hexdigest()
    assert base64.b64decode(doc_header.original_html_base64) == gbk_html

    meta_gb_html = b'<meta charset="gb2312"><p>' + "净利润同比增长".encode("gb18030") + b"</p>"
    doc_meta = fetch_source_document(
        request("https://example.com/meta-gb"),
        lambda *a: BinaryHttpResponse(200, meta_gb_html, {"content-type": "text/html"}),
    )
    assert "净利润同比增长" in doc_meta.text

    no_decl_gb = "季度财报摘要".encode("gb18030")
    doc_gb_fallback = fetch_source_document(
        request("https://example.com/no-decl-gb"),
        lambda *a: BinaryHttpResponse(200, no_decl_gb, {"content-type": "text/plain"}),
    )
    assert "季度财报摘要" in doc_gb_fallback.text

    latin1_bytes = b"<p>Caf\xe9 revenue 100</p>"
    doc_latin1 = fetch_source_document(
        request("https://example.com/latin1"),
        lambda *a: BinaryHttpResponse(200, latin1_bytes, {"content-type": "text/html"}),
    )
    assert "Café revenue 100" in doc_latin1.text


def test_safe_redirects_follow_up_to_4_hops_and_fail_closed_on_unsafe_targets(monkeypatch):
    seen_urls = []

    def hop_transport(method, url, headers, body, timeout):
        seen_urls.append(url)
        assert "Cookie" not in headers and "Authorization" not in headers
        if len(seen_urls) == 1:
            return BinaryHttpResponse(301, b"", {"Location": "/step2?x=1#section"})
        if len(seen_urls) == 2:
            return BinaryHttpResponse(307, b"", {"Location": "https://cdn.example.com/final.txt"})
        return BinaryHttpResponse(200, b"Final redirected text", {"content-type": "text/plain"})

    doc = fetch_source_document(request("https://example.com/start"), hop_transport)
    assert seen_urls == [
        "https://example.com/start",
        "https://example.com/step2?x=1",
        "https://cdn.example.com/final.txt",
    ]
    assert doc.final_url == "https://cdn.example.com/final.txt"
    assert doc.redirect_count == 2 and doc.text == "Final redirected text"

    # Exceeding 4 redirect hops fails closed.
    hops = {"n": 0}

    def too_many_hops(method, url, headers, body, timeout):
        hops["n"] += 1
        return BinaryHttpResponse(302, b"", {"location": f"https://example.com/hop/{hops['n']}"})

    with pytest.raises(FetchFailure, match="source_unavailable"):
        fetch_source_document(request("https://example.com/hop/0"), too_many_hops)
    assert hops["n"] == 5

    # Unsafe SSRF targets and HTTP downgrades fail closed without contacting hop target.
    for unsafe_target in (
        "https://127.0.0.1/secret",
        "https://127.1/secret",
        "https://2130706433/secret",
        "https://localhost/admin",
        "http://cdn.example.com/downgrade",
        "https://www.sec.gov/Archives/edgar/data/1/000000000000000001/foo.htm",
    ):
        calls = []

        def unsafe_redirect(method, url, headers, body, timeout, target=unsafe_target):
            calls.append(url)
            return BinaryHttpResponse(302, b"", {"location": target})

        with pytest.raises(FetchFailure, match="source_unavailable"):
            fetch_source_document(request("https://example.com/start"), unsafe_redirect)
        assert calls == ["https://example.com/start"]

    # FRED redirect target fails closed with source_blocked_policy.
    fred_calls = []

    def fred_redirect(method, url, headers, body, timeout):
        fred_calls.append(url)
        return BinaryHttpResponse(302, b"", {"location": "https://fred.stlouisfed.org/series/GDP"})

    with pytest.raises(FetchFailure, match="source_blocked_policy"):
        fetch_source_document(request("https://example.com/start"), fred_redirect)
    assert fred_calls == ["https://example.com/start"]

    # Default transport path re-validates hop DNS via _validated_addresses and fails closed on private DNS.
    dns_checked = []

    def fake_dns(host, port, *, allow_private=False):
        dns_checked.append((host, port, allow_private))
        if host == "rebind.example.com":
            raise _TransportFailure("private ip")
        return ("93.184.216.34",)

    def fake_default_binary(method, url, headers, body, timeout, **kwargs):
        return BinaryHttpResponse(302, b"", {"location": "https://rebind.example.com/secret"})

    monkeypatch.setattr(research_fetch, "_validated_addresses", fake_dns)
    monkeypatch.setattr(research_fetch, "_default_binary_transport", fake_default_binary)
    with pytest.raises(FetchFailure, match="source_unavailable"):
        fetch_source_document(request("https://example.com/start"))
    assert dns_checked == [("rebind.example.com", 443, False)]


def test_env_allowlist_and_blocklist_policy_and_granular_http_status_codes(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_FETCH_BLOCKED_HOSTS", "blocked.example.com")
    with pytest.raises(FetchFailure, match="source_blocked_policy"):
        fetch_source_document(
            request("https://sub.blocked.example.com/report"),
            lambda *a: pytest.fail("blocked host must not contact transport"),
        )
    with pytest.raises(FetchFailure, match="source_blocked_policy"):
        fetch_source_document(
            request("https://example.com/start"),
            lambda *a: BinaryHttpResponse(302, b"", {"location": "https://blocked.example.com/a"}),
        )

    monkeypatch.delenv("FORESIGHTNINE_FETCH_BLOCKED_HOSTS", raising=False)
    monkeypatch.setenv("FORESIGHTNINE_FETCH_ALLOWED_HOSTS", "allowed.example.com")
    with pytest.raises(FetchFailure, match="source_blocked_policy"):
        fetch_source_document(
            request("https://other.example.com/report"),
            lambda *a: pytest.fail("non-allowlisted host must not contact transport"),
        )
    ok = fetch_source_document(
        request("https://allowed.example.com/report"),
        lambda *a: BinaryHttpResponse(200, b"Allowed content", {"content-type": "text/plain"}),
    )
    assert ok.text == "Allowed content"
    monkeypatch.delenv("FORESIGHTNINE_FETCH_ALLOWED_HOSTS", raising=False)

    for status_code, expected_code in (
        (401, "http_forbidden_or_challenge"),
        (403, "http_forbidden_or_challenge"),
        (404, "not_found"),
        (410, "not_found"),
        (413, "source_too_large"),
        (429, "source_rate_limited"),
        (503, "source_unavailable"),
    ):
        with pytest.raises(FetchFailure, match=expected_code):
            fetch_source_document(
                request("https://example.com/status"),
                lambda *a, code=status_code: BinaryHttpResponse(code, b"", {}),
            )
    with pytest.raises(FetchFailure, match="source_format_unsupported"):
        fetch_source_document(
            request("https://example.com/json"),
            lambda *a: BinaryHttpResponse(200, b"{}", {"content-type": "application/json"}),
        )


def test_validated_addresses_sorts_ipv4_before_ipv6_preserving_public_checks(monkeypatch):
    import app.openai_compatible as openai_compatible

    monkeypatch.setattr(
        openai_compatible.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (10, 1, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0)),
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (10, 1, 6, "", ("2001:4860:4860::8888", 443, 0, 0)),
            (2, 1, 6, "", ("1.1.1.1", 443)),
        ],
    )
    assert openai_compatible._validated_addresses("example.com", 443) == (
        "1.1.1.1",
        "93.184.216.34",
        "2001:4860:4860::8888",
        "2606:2800:220:1:248:1893:25c8:1946",
    )
    monkeypatch.setattr(
        openai_compatible.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (10, 1, 6, "", ("fe80::1", 443, 0, 0)),
        ],
    )
    with pytest.raises(openai_compatible._TransportFailure):
        openai_compatible._validated_addresses("example.com", 443)


@pytest.mark.parametrize("url", [
    "https://api.stlouisfed.org/%66red/series?series_id=GDP",
    "https://api.stlouisfed.org/%2566red/series?series_id=GDP",
    "https://api.stlouisfed.org//fred//series?series_id=GDP",
    "https://api.stlouisfed.org/v1/../FRED/series",
    "https://alfred.stlouisfed.org/series?id=GDP",
    "https://sub.alfred.stlouisfed.org/series?id=GDP",
    "https://ｆｒｅｄ.stlouisfed.org/series/GDP",
    "https://efts.sec.gov/LATEST/search-index",
    "https://sec.gov./Archives/edgar/data/1/000000000000000001/foo.htm",
    "https://data.sec.gov/api/xbrl/companyconcept/CIK0000789019/us-gaap/Revenues.json",
    "https://[64:ff9b::127.0.0.1]/",
    "https://[64:ff9b:1::10.0.0.1]/",
    "https://[100::1]/",
    "https://" + ("a" * 64) + ".com/report",
])
def test_hardened_fred_sec_nat64_and_idna_rejections(url):
    with pytest.raises(ValidationError):
        request(url)


def test_is_fred_source_and_idna_and_public_ip_parity(monkeypatch):
    import ipaddress
    import app.openai_compatible as openai_compatible
    from app.research_source_policy import is_fred_source

    assert is_fred_source("https://api.stlouisfed.org/%66red/series")
    assert is_fred_source("https://api.stlouisfed.org//fred/series")
    assert is_fred_source("https://sub.alfred.stlouisfed.org/series")
    assert is_fred_source("https://ｆｒｅｄ.stlouisfed.org/series/GDP")
    assert not is_fred_source("https://api.stlouisfed.org/other")
    assert not is_fred_source("https://" + ("a" * 64) + ".com/")

    # Valid IDNA hostname normalizes cleanly and passes request validation.
    assert request("https://bücher.example.com/report").url
    assert research_fetch.validate_public_https_url("https://bücher.example.com/report") == "xn--bcher-kva.example.com"

    # NAT64 and RFC 6666 discard addresses fail is_public_ip_address and _validated_addresses.
    assert not openai_compatible.is_public_ip_address(ipaddress.ip_address("64:ff9b::127.0.0.1"))
    assert not openai_compatible.is_public_ip_address(ipaddress.ip_address("64:ff9b:1::10.0.0.1"))
    assert not openai_compatible.is_public_ip_address(ipaddress.ip_address("100::1"))
    assert openai_compatible.is_public_ip_address(ipaddress.ip_address("1.1.1.1"))
    monkeypatch.setattr(
        openai_compatible.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(10, 1, 6, "", ("64:ff9b::127.0.0.1", 443, 0, 0))],
    )
    with pytest.raises(openai_compatible._TransportFailure):
        openai_compatible._validated_addresses("nat64.example.com", 443)


def test_safe_html_parser_strips_ix_hidden_template_and_css_comments():
    html = (
        '<ix:hidden><ix:nonFraction>HIDDEN IX</ix:nonFraction></ix:hidden>'
        '<template><p>HIDDEN TEMPLATE</p></template>'
        '<div style="display/*bypass*/:none">HIDDEN COMMENT CSS</div>'
        '<span style="visibility/*a*/:/*b*/hidden">HIDDEN VISIBILITY</span>'
        '<p>Visible result 42</p>'
    )
    doc = fetch_source_document(
        request("https://example.com/clean"),
        lambda *a: BinaryHttpResponse(200, html.encode("utf-8"), {"content-type": "text/html"}),
    )
    assert doc.text == "Visible result 42"


def test_sec_subdomains_and_case_insensitive_redirect_loop_fail_closed():
    for sec_target in ("https://efts.sec.gov/search", "https://sec.gov./Archives/edgar/data/1/000000000000000001/a.htm"):
        calls = []

        def redirect_sec(method, url, headers, body, timeout, target=sec_target):
            calls.append(url)
            return BinaryHttpResponse(302, b"", {"location": target})

        with pytest.raises(FetchFailure, match="source_unavailable"):
            fetch_source_document(request("https://example.com/start"), redirect_sec)
        assert calls == ["https://example.com/start"]

    # Case-insensitive self-redirect must be caught immediately on hop 1.
    loop_calls = []

    def self_redirect_case(method, url, headers, body, timeout):
        loop_calls.append(url)
        return BinaryHttpResponse(302, b"", {"location": "https://EXAMPLE.COM/start"})

    with pytest.raises(FetchFailure, match="source_unavailable"):
        fetch_source_document(request("https://example.com/start"), self_redirect_case)
    assert loop_calls == ["https://example.com/start"]


def test_slims_heavy_sec_ixbrl_spacer_table_preserving_values_and_spans():
    html = (
        '<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">'
        '<ix:header><ix:hidden>999999</ix:hidden></ix:header>'
        '<p>Consolidated Statements of Income (in millions, except per-share data)</p>'
        '<table>'
        '<tr><td style="width:1%"/><td style="width:30%"/><td colspan="3"/><td/><td/><td colspan="3"/><td/><td/></tr>'
        '<tr><td colspan="3">&#160;</td><td colspan="3"></td>'
        '<th colspan="9">Years Ended</th><td colspan="3"><p></p><br/></td></tr>'
        '<tr><td colspan="3"><span style="display:none">hidden</span></td>'
        '<th colspan="3">January 26, 2025</th><td colspan="3"></td>'
        '<th colspan="3">January 28, 2024</th><td colspan="3"></td></tr>'
        '<tr><td colspan="3">REVENUE:</td><td colspan="3"></td><td colspan="3"></td><td colspan="3"></td></tr>'
        '<tr><td colspan="3">Total revenue</td><td></td><td></td>'
        '<td>$</td><td><ix:nonFraction name="us-gaap:Revenues">130,497</ix:nonFraction></td><td></td>'
        '<td colspan="3"></td><td></td>'
        '<td>$</td><td><ix:nonFraction name="us-gaap:Revenues">60,922</ix:nonFraction></td><td></td><td colspan="3"/></tr>'
        '<tr><td colspan="3">Restructuring and other</td><td></td><td rowspan="2"></td>'
        '<td>$</td><td>(1,234)</td><td></td>'
        '<td colspan="3"></td><td colspan="3"></td><td colspan="3"></td><td></td></tr>'
        '<tr><td colspan="3">Other margin</td><td></td><td colspan="3"></td><td>64.4</td><td>%</td>'
        '<td colspan="3"></td><td></td><td></td><td>62.7</td><td>%</td><td colspan="3"></td></tr>'
        '<tr><td colspan="3"></td><td colspan="3"></td><td colspan="3"></td><td colspan="3"></td></tr>'
        '</table></html>'
    )
    doc = fetch_source_document(
        request("https://ir.example.com/10k"),
        lambda *a: BinaryHttpResponse(200, html.encode("utf-8"), {"content-type": "text/html; charset=utf-8"}),
    )
    assert doc.text.splitlines() == [
        "Consolidated Statements of Income (in millions, except per-share data)",
        "|  | [colspan=9] Years Ended |",
        "|  | [colspan=3] January 26, 2025 |  | [colspan=3] January 28, 2024 |",
        "| [colspan=3] REVENUE: |",
        "| [colspan=3] Total revenue |  | $ | 130,497 |  | $ | 60,922 |",
        "| [colspan=3] Restructuring and other |  | $ | (1,234) |",
        "| [colspan=3] Other margin |  | 64.4 | % |  | 62.7 | % |",
    ]
    # Compare against un-slimmed naive cell emission (13+ empty cells & [colspan] markers per row).
    unslimmed_equivalent_len = (
        len("Consolidated Statements of Income (in millions, except per-share data)\n")
        + len("|  |  | [colspan=3]  |  |  | [colspan=3]  |  |  |\n")
        + len("| [colspan=3]  | [colspan=3]  | [colspan=9] Years Ended | [colspan=3]  |\n")
        + len("| [colspan=3]  | [colspan=3] January 26, 2025 | [colspan=3]  | [colspan=3] January 28, 2024 | [colspan=3]  |\n")
        + len("| [colspan=3] REVENUE: | [colspan=3]  | [colspan=3]  | [colspan=3]  |\n")
        + len("| [colspan=3] Total revenue |  |  | $ | 130,497 |  | [colspan=3]  |  | $ | 60,922 |  | [colspan=3]  |\n")
        + len("| [colspan=3] Restructuring and other |  | [rowspan=2]  | $ | (1,234) |  | [colspan=3]  | [colspan=3]  | [colspan=3]  |  |\n")
        + len("| [colspan=3] Other margin |  | [colspan=3]  | 64.4 | % | [colspan=3]  |  |  | 62.7 | % | [colspan=3]  |\n")
        + len("| [colspan=3]  | [colspan=3]  | [colspan=3]  | [colspan=3]  |")
    )
    assert len(doc.text) < unslimmed_equivalent_len * 0.65
    for expected in ("130,497", "60,922", "(1,234)", "64.4", "62.7", "January 26, 2025", "January 28, 2024", "(in millions"):
        assert expected in doc.text
    assert "999999" not in doc.text and "hidden" not in doc.text


def test_table_slimming_handles_nested_tables_malformed_tags_and_provenance_trace():
    from datetime import datetime, timezone
    from decimal import Decimal
    from app.research_sources import ResearchSource, SourcePermissions
    from app.research_table_provenance import annual_table_value_alignment, trace_html_table
    from app.research_uploads import HtmlUploadRequest, extract_uploaded_html

    malformed = (
        '<table>'
        '<tr><td><table><tr><td></td><td colspan="2"></td></tr></table></td><td>Keep</td><td></td></tr>'
        '<tr><td><table><tr><td>Nested</td></tr></table></td><td>90</td></tr>'
        '<tr><td>First<td>Second</td><td></td><td></td></tr>'
        '<tr><td>OpenA</td><tr><td>OpenB</td></tr>'
        '<tr><td></td><td></td>'
        '</table>'
    )
    doc = fetch_official_text(
        request("https://www.bea.gov/news/2024/malformed"),
        lambda *a: HttpResponse(200, malformed, {"content-type": "text/html"}),
    )
    assert doc.text.splitlines() == [
        "|  | Keep |",
        "|",
        "| Nested |",
        "| 90 |",
        "| First | Second |",
        "| OpenA |",
        "| OpenB |",
    ]

    # A document containing only an empty spacer table produces no extractable text.
    with pytest.raises(FetchFailure, match="source_invalid"):
        fetch_official_text(
            request("https://www.bea.gov/news/2024/empty-table"),
            lambda *a: HttpResponse(200, '<table><tr><td></td><td colspan="4">&#160;</td></tr></table>', {"content-type": "text/html"}),
        )

    sec_html = (
        '<html><table>'
        '<tr><td colspan="3"/><td colspan="3"/><td colspan="3"/><td colspan="3"/><td colspan="3"/></tr>'
        '<tr><td colspan="3"></td><td colspan="12">Years Ended</td></tr>'
        '<tr><td colspan="3"></td><td colspan="3">July 27, 2024</td><td colspan="3"></td><td colspan="3">July 29, 2023</td><td colspan="3"></td></tr>'
        '<tr><td colspan="3">Revenue</td><td>$</td><td>53,803</td><td></td><td colspan="3"></td><td>$</td><td>56,998</td><td></td><td colspan="3"></td></tr>'
        '</table></html>'
    )
    raw = sec_html.encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    perms = SourcePermissions(store=True, display=True)
    text, raw_hash = extract_uploaded_html(HtmlUploadRequest(title="SEC", html_base64=encoded, permissions=perms))
    source = ResearchSource(
        source_id="s-sec",
        owner_id="o",
        title="SEC",
        text=text,
        kind="official",
        obtained_at=datetime.now(timezone.utc),
        content_sha256=sha256(text.encode("utf-8")).hexdigest(),
        retrieval_method="sec_filing_html",
        response_sha256=raw_hash,
        original_html_base64=encoded,
        permissions=perms,
    )
    start = text.index("53,803")
    traced = trace_html_table(source, start, start + len("53,803"))
    assert traced is not None
    alignment = annual_table_value_alignment({"status": "available", **traced}, Decimal("53803"), "FY2024 (year ended July 27, 2024)")
    assert alignment is not None and alignment["status"] == "matched"


def test_extract_html_suppresses_boilerplate_containers_while_preserving_main_article_and_tables():
    html = (
        '<html>'
        '<header role="banner"><span>Site Banner</span><nav><ul><li>Markets</li><li>News</li></ul></nav></header>'
        '<div role="navigation"><div>Mega menu <span>Portfolio</span></div></div>'
        '<div role="NAVIGATION"><p>Mixed case nav</p></div>'
        '<main>'
        '  <article>'
        '    <h1>Nasdaq Market Analysis</h1>'
        '    <p>Nasdaq rose 1.5% on earnings.</p>'
        '    <footer>Share buttons and copyright</footer>'
        '  </article>'
        '  <aside><div role="complementary">Trending tickers and ads</div></aside>'
        '</main>'
        '<footer role="contentinfo">'
        '  <p>Terms of service disclaimer</p>'
        '  <table><tr><th>Metric</th><th>FY25</th></tr><tr><td>Revenue</td><td>130,497</td></tr></table>'
        '</footer>'
        '<div style="display:none"><table><tr><td>Hidden table</td></tr></table></div>'
        '</html>'
    )
    doc = fetch_source_document(
        request("https://finance.example.com/quote/ixic"),
        lambda *a: BinaryHttpResponse(200, html.encode("utf-8"), {"content-type": "text/html; charset=utf-8"}),
    )
    assert "Nasdaq Market Analysis" in doc.text
    assert "Nasdaq rose 1.5% on earnings." in doc.text
    assert "| Metric | FY25 |" in doc.text
    assert "| Revenue | 130,497 |" in doc.text
    for boilerplate in (
        "Site Banner",
        "Markets",
        "News",
        "Mega menu",
        "Portfolio",
        "Mixed case nav",
        "Share buttons and copyright",
        "Trending tickers and ads",
        "Terms of service disclaimer",
        "Hidden table",
    ):
        assert boilerplate not in doc.text
