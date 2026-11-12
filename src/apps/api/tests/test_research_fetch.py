import pytest
from pydantic import ValidationError

from app.openai_compatible import HttpResponse, _TransportTimeout
from app.research_fetch import FetchFailure, FetchSourceRequest, fetch_official_text


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


@pytest.mark.parametrize("url", ["https://fred.stlouisfed.org/series/GDP",
    "https://www.bea.gov.evil.example/", "https://127.0.0.1/", "http://www.bea.gov/",
    "https://user:password@www.bea.gov/", "https://www.bea.gov:8443/"])
def test_rejects_unsupported_or_unsafe_hosts(url):
    with pytest.raises(ValidationError):
        request(url)


def test_redirects_and_timeouts_fail_without_fallback():
    with pytest.raises(FetchFailure, match="source_unavailable"):
        fetch_official_text(request(), lambda *args: HttpResponse(302, "", {"location": "https://127.0.0.1"}))
    def timeout(*args):
        raise _TransportTimeout()
    with pytest.raises(FetchFailure, match="source_timeout"):
        fetch_official_text(request(), timeout)
