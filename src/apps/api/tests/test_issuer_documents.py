"""issuer_documents: Wikidata identity, robots.txt, bounded IR crawl, terms-blocked hosts, personal (user-initiated) mode."""
import json
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as api
from app import entity_web, issuer_documents
from app.entity_web import WikidataClient, normalize_ticker
from app.issuer_documents import (IssuerCrawler, IssuerDocuments, IssuerDocumentsRequest, crawl_issuer_site, parse_robots)
from app.news.gdelt import RequestPacer
from app.openai_compatible import BinaryHttpResponse
from app.research_fetch import FetchSourceRequest, _check_fetch_target, FetchFailure, fetch_source_document
from app.research_runtime import SYSTEM, TOOL_FAILURE_GUIDANCE, TOOLS, run_research_agent
from app.research_source_terms import OVERRIDES_ENV, terms_decision, user_initiated_scope
from tests.test_research_runtime import Model, context, turn
from tests.test_research_runtime_s0a import STOP, feedback

TODAY = date(2026, 11, 21)
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
PERSONAL = json.dumps([{"host": h, "status": "user_initiated_only",
                        "permission_reference": "personal non-commercial use, user-initiated single documents"}
                       for h in ("cninfo.com.cn", "sse.com.cn", "szse.cn", "hkexnews.hk", "hkex.com.hk")])


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.delenv(OVERRIDES_ENV, raising=False)
    monkeypatch.delenv("FORESIGHTNINE_FETCH_ALLOWED_HOSTS", raising=False)
    entity_web.clear_cache()
    issuer_documents.clear_robots_cache()
    yield
    entity_web.clear_cache()
    issuer_documents.clear_robots_cache()


# ------------------------------------------------------------------------------------------- Wikidata fake

def claim(value, *, rank="normal", qualifiers=None, datatype="string"):
    snak = {"snaktype": "value", "datavalue": {"value": value}}
    out = {"mainsnak": snak, "rank": rank}
    if qualifiers:
        out["qualifiers"] = qualifiers
    return out


def item(qid):
    return {"id": qid, "numeric-id": 1}


def ticker_q(value):
    return {"P249": [{"snaktype": "value", "datavalue": {"value": value}}]}


ENTITIES = {
    "Q1": {"id": "Q1", "labels": {"en": {"value": "Tencent Holdings"}, "zh": {"value": "腾讯控股"}},
           "descriptions": {"en": {"value": "Chinese technology company"}},
           "claims": {"P856": [claim("https://www.tencent.com/"), claim("https://old.example.org/", qualifiers={"P582": [{}]}),
                               claim("https://deprecated.example.org/", rank="deprecated")],
                      "P414": [claim(item("Q100"), qualifiers=ticker_q("0700")), claim(item("Q101"), qualifiers=ticker_q("TCEHY"))]}},
    "Q2": {"id": "Q2", "labels": {"en": {"value": "Tencent Music"}},
           "claims": {"P856": [claim("http://www.tencentmusic.com/")], "P414": [claim(item("Q101"), qualifiers=ticker_q("TME"))]}},
    "Q3": {"id": "Q3", "labels": {"en": {"value": "Kweichow Moutai"}, "zh": {"value": "贵州茅台"}},
           "claims": {"P856": [claim("https://www.moutaichina.com/")], "P414": [claim(item("Q102"), qualifiers=ticker_q("600519"))]}},
    "Q100": {"id": "Q100", "labels": {"en": {"value": "Hong Kong Stock Exchange"}}},
    "Q101": {"id": "Q101", "labels": {"en": {"value": "OTC Markets"}}},
    "Q102": {"id": "Q102", "labels": {"en": {"value": "Shanghai Stock Exchange"}}},
}


class FakeWikidata:
    def __init__(self, search=None):
        self.calls = []
        self.search = search or {"Tencent": ["Q1", "Q2"], "腾讯": ["Q1"], "Moutai": ["Q3"]}

    def __call__(self, method, url, headers, body, timeout, **kwargs):
        self.calls.append((url, headers))
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if parts.netloc == "query.wikidata.org":
            text = query["query"][0]
            qids = ["Q3"] if "600519" in text else (["Q1"] if '"700"' in text else [])
            rows = [{"item": {"value": f"http://www.wikidata.org/entity/{q}"}} for q in qids]
            return BinaryHttpResponse(200, json.dumps({"results": {"bindings": rows}}).encode(), {})
        action = query["action"][0]
        if action == "wbsearchentities":
            ids = next((v for k, v in self.search.items() if k.lower() in query["search"][0].lower()), [])
            return BinaryHttpResponse(200, json.dumps({"search": [{"id": i} for i in ids]}).encode(), {})
        ids = query["ids"][0].split("|")
        return BinaryHttpResponse(200, json.dumps({"entities": {i: ENTITIES[i] for i in ids if i in ENTITIES}}).encode(), {})


def wikidata_client(fake=None):
    fake = fake or FakeWikidata()
    return WikidataClient(transport=fake, min_interval_seconds=0, pacer=RequestPacer()), fake


def test_wikidata_profile_website_tickers_and_labels():
    client, fake = wikidata_client()
    result = client.lookup(name="Tencent", ticker="700", market="HK")
    assert result.status == "ok"
    best = result.profiles[0]
    assert best.qid == "Q1" and best.label_zh == "腾讯控股" and best.label_en == "Tencent Holdings"
    assert best.websites == ("https://www.tencent.com/",)          # ended and deprecated statements are excluded
    hk = next(item for item in best.listings if item.exchange == "HKEX")
    assert hk.tickers == ("0700",) and normalize_ticker("0700") == normalize_ticker("00700") == normalize_ticker("700.HK") == "700"
    assert result.profiles[1].websites == ("https://www.tencentmusic.com/",)       # http is upgraded to https
    agent = fake.calls[0][1]["User-Agent"]
    assert agent.startswith("ForesightNine") and "@" not in agent
    assert all("maxlag=5" in url for url, _ in fake.calls if "wikidata.org/w/api.php" in url)


def test_wikidata_results_are_cached_and_ticker_only_uses_sparql():
    client, fake = wikidata_client()
    client.lookup(name="Moutai", market="CN")
    used = len(fake.calls)
    again, _ = wikidata_client(fake)
    again.lookup(name="Moutai", market="CN")
    assert len(fake.calls) == used                                  # served from the 24 hour cache
    only_ticker = client.lookup(ticker="600519", market="CN")
    assert only_ticker.profiles[0].qid == "Q3" and any("query.wikidata.org" in url for url, _ in fake.calls)


def test_wikidata_failures_are_reported_not_raised():
    def down(*args, **kwargs):
        raise OSError("boom")

    client = WikidataClient(transport=down, min_interval_seconds=0, pacer=RequestPacer())
    result = client.lookup(name="Tencent")
    assert result.status == "failed" and result.reason == "wikidata_unavailable"

    def limited(*args, **kwargs):
        return BinaryHttpResponse(429, b"", {"Retry-After": "7"})

    entity_web.clear_cache()
    limited_result = WikidataClient(transport=limited, min_interval_seconds=0, pacer=RequestPacer()).lookup(name="Other")
    assert limited_result.status == "rate_limited" and limited_result.reason == "wikidata_rate_limited"


# ------------------------------------------------------------------------------------------- robots.txt

ROBOTS = """
User-agent: *
Disallow: /private/
Disallow: /*.json$
Allow: /private/public/
Crawl-delay: 2

User-agent: ForesightNine
Disallow: /secret
Allow: /secret/open
"""


def test_robots_agent_group_wins_over_star_and_longest_match():
    policy = parse_robots(ROBOTS)
    assert not policy.can_fetch("https://x.com/secret/a") and policy.can_fetch("https://x.com/secret/open/a")
    assert policy.can_fetch("https://x.com/private/a")            # the specific group replaces the * group
    assert policy.crawl_delay is None
    star = parse_robots(ROBOTS, agent_token="otherbot")
    assert not star.can_fetch("https://x.com/private/a") and star.can_fetch("https://x.com/private/public/a")
    assert not star.can_fetch("https://x.com/data/list.json") and star.can_fetch("https://x.com/data/list.jsonp")
    assert star.crawl_delay == 2.0
    assert parse_robots("User-agent: *\nDisallow: /\n").can_fetch("https://x.com/") is False
    assert parse_robots("User-agent: *\nDisallow:\n").can_fetch("https://x.com/a") is True
    assert parse_robots("garbage ::: text").can_fetch("https://x.com/a") is True


# ------------------------------------------------------------------------------------------- fake site

HOME = """<html><body><nav>
<a href="/about">About us</a><a href="/investors/">Investor Relations</a><a href="/careers">Careers</a>
<a href="https://www1.hkexnews.hk/listedco/listconews/sehk/2026/1101/x.pdf">HKEX copy of notice</a>
<a href="/shop">Shop</a></nav></body></html>"""
IR = """<html><body><ul>
<li><a href="/files/ar2025.pdf">2025 Annual Report</a> <span>2026-03-20</span></li>
<li><a href="/files/ir2025.pdf">Interim Report 2025</a> <span>20 August 2025</span></li>
<li><a href="/files/circ.pdf">Circular regarding major transaction</a> 2026-06-01</li>
<li><a href="/files/ann0301.pdf">公告：董事会会议召开日期</a> 2026年11月01日</li>
<li><a href="/files/old.pdf">Announcement</a> 2019-01-05</li>
<li><a href="/investors/news/2026/q3-results.html">Third quarter results</a> 2026-11-12</li>
<li><a href="https://static.cninfo.com.cn/finalpage/2026-03-20/1.PDF">Annual report (cninfo)</a> 2026-03-20</li>
<li><a href="/files/brochure.pdf">Brochure</a></li>
</ul><a rel="next" href="/investors/?page=2">Next</a><a href="/contact">Contact</a></body></html>"""
IR2 = """<html><body><ul><li><a href="/files/ann-page2.pdf">Announcement on dividend</a> 2026-09-30</li></ul></body></html>"""


class FakeSite:
    def __init__(self, pages=None, robots="User-agent: *\nDisallow: /careers\nCrawl-delay: 3\n", robots_status=200):
        self.pages = pages or {"https://www.example-co.com/": HOME, "https://www.example-co.com/investors/": IR,
                               "https://www.example-co.com/investors/?page=2": IR2,
                               "https://www.example-co.com/about": "<html><a href='/investors/'>IR</a></html>"}
        self.robots, self.robots_status = robots, robots_status
        self.requests = []

    def __call__(self, method, url, headers, body, timeout, **kwargs):
        self.requests.append(url)
        assert method == "GET" and "ForesightNine" in headers["User-Agent"]
        if url.endswith("/robots.txt"):
            return BinaryHttpResponse(self.robots_status, self.robots.encode(), {"Content-Type": "text/plain"})
        if url in self.pages:
            return BinaryHttpResponse(200, self.pages[url].encode(), {"Content-Type": "text/html; charset=utf-8"})
        return BinaryHttpResponse(404, b"", {})


def crawl(site, **kwargs):
    sleeps = []
    crawler = IssuerCrawler(transport=site, sleeper=sleeps.append, delay_seconds=0, **{k: v for k, v in kwargs.items() if k in ("max_pages",)})
    report = crawl_issuer_site("https://www.example-co.com/", crawler, today=TODAY,
                               **{k: v for k, v in kwargs.items() if k in ("doc_types", "since", "until")})
    return report, sleeps


def test_ir_crawl_extracts_ranks_and_follows_pagination():
    site = FakeSite()
    report, sleeps = crawl(site)
    urls = {d.url: d for d in report.documents}
    assert report.pages_fetched == 3 and report.home_status == "ok"
    assert urls["https://www.example-co.com/files/ar2025.pdf"].doc_type == "annual_report"
    assert urls["https://www.example-co.com/files/ar2025.pdf"].date == date(2026, 3, 20)
    assert urls["https://www.example-co.com/files/ir2025.pdf"].doc_type == "interim_report" and urls["https://www.example-co.com/files/ir2025.pdf"].date == date(2025, 8, 20)
    assert urls["https://www.example-co.com/files/circ.pdf"].doc_type == "circular"
    zh = urls["https://www.example-co.com/files/ann0301.pdf"]
    assert zh.doc_type == "announcement" and zh.language == "zh" and zh.date == date(2026, 11, 1)
    assert "https://www.example-co.com/investors/news/2026/q3-results.html" in urls             # dated HTML document
    assert "https://www.example-co.com/files/ann-page2.pdf" in urls                              # found on the next page
    assert "https://www.example-co.com/careers" not in site.requests                              # not an IR page and disallowed
    assert "https://www.example-co.com/shop" not in site.requests
    # Documents on terms-blocked hosts are listed but never acquirable and never requested.
    blocked = [d for d in report.documents if not d.acquirable]
    assert {d.host for d in blocked} == {"www1.hkexnews.hk", "static.cninfo.com.cn"} and all(d.score < 0 for d in blocked)
    assert not any("hkexnews" in r or "cninfo" in r for r in site.requests)
    assert report.documents[0].doc_type != "other" and all(d.score >= 0 for d in report.documents[:5])
    assert max(sleeps, default=0) <= 3


def test_crawl_honors_crawl_delay_and_page_budget_and_date_window():
    site = FakeSite()
    report, sleeps = crawl(site, max_pages=2)
    assert report.pages_fetched == 2 and report.budget_exhausted
    assert not any(r.endswith("?page=2") for r in site.requests)
    site2 = FakeSite()
    clock = {"t": 0.0}
    crawler = IssuerCrawler(transport=site2, clock=lambda: clock["t"], sleeper=lambda s: clock.__setitem__("t", clock["t"] + s), delay_seconds=0)
    crawl_issuer_site("https://www.example-co.com/", crawler, today=TODAY)
    assert clock["t"] >= 3.0                                         # Crawl-delay: 3 between fetches on the same host
    dated, _ = crawl(FakeSite(), since=date(2026, 1, 1), until=date(2026, 12, 31), doc_types=["annual_report"])
    assert all(d.date is None or d.date >= date(2026, 1, 1) for d in dated.documents) and dated.filtered_by_date >= 2
    assert dated.documents[0].doc_type == "annual_report"


def test_robots_disallow_all_and_unreachable_fetch_nothing_but_robots():
    site = FakeSite(robots="User-agent: *\nDisallow: /\n")
    report, _ = crawl(site)
    assert report.home_status == "robots_disallow" and site.requests == ["https://www.example-co.com/robots.txt"]
    issuer_documents.clear_robots_cache()
    server_error = FakeSite(robots_status=503)
    report, _ = crawl(server_error)
    assert report.home_status == "robots_unavailable" and report.home_http_status == 503 and server_error.requests == ["https://www.example-co.com/robots.txt"]
    issuer_documents.clear_robots_cache()
    missing = FakeSite(robots_status=404)
    report, _ = crawl(missing)
    assert report.home_status == "ok" and report.documents


def _outcome_for(transport):
    crawler = IssuerCrawler(transport=transport, delay_seconds=0, sleeper=lambda s: None)
    report = crawl_issuer_site("https://www.example-co.com/", crawler, today=TODAY)
    tool = IssuerDocuments(wikidata=None)
    issuer = {"website": "https://www.example-co.com/"}
    return tool._outcome(IssuerDocumentsRequest(website_url="https://www.example-co.com/", rationale="r"),
                         issuer, "https://www.example-co.com/", report).result


def test_unreachable_robots_is_reported_as_unreachable_not_robots_disallow():
    seen = []

    def transport(method, url, headers, body, timeout, **kwargs):
        seen.append((url, kwargs.get("connect_timeout_seconds")))
        raise OSError("connect timeout")

    result = _outcome_for(transport)
    assert result["status"] == "failed" and result["reason"] == "issuer_site_unreachable" and result["retryable"] is True
    assert "mainland" in result["message"] and seen == [("https://www.example-co.com/robots.txt", 8.0)]


def test_robots_http_refusal_reports_status_and_real_disallow_stays_denied():
    for status in (401, 403, 429, 503):
        issuer_documents.clear_robots_cache()
        result = _outcome_for(FakeSite(robots_status=status))
        assert result["status"] == "failed" and result["reason"] == "issuer_site_robots_unavailable"
        assert result["http_status"] == status and result["retryable"] is True
    issuer_documents.clear_robots_cache()
    result = _outcome_for(FakeSite(robots="User-agent: *\nDisallow: /\n"))
    assert result["status"] == "denied" and result["reason"] == "issuer_site_robots_disallow"


def test_connect_timeout_env_is_applied(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_ISSUER_CONNECT_TIMEOUT_SECONDS", "3")
    seen = []

    def transport(method, url, headers, body, timeout, **kwargs):
        seen.append(kwargs.get("connect_timeout_seconds"))
        raise OSError("x")

    _outcome_for(transport)
    assert seen == [3.0]


def test_blocked_issuer_site_is_never_fetched():
    site = FakeSite()
    crawler = IssuerCrawler(transport=site, sleeper=lambda s: None, delay_seconds=0)
    report = crawl_issuer_site("https://www.hkexnews.hk/index.htm", crawler, today=TODAY)
    assert report.home_status == "blocked" and site.requests == []
    cn = crawl_issuer_site("https://www.cninfo.com.cn/", IssuerCrawler(transport=site, delay_seconds=0), today=TODAY)
    assert cn.home_status == "blocked" and site.requests == []


def test_redirect_into_blocked_host_is_not_followed():
    requests = []

    def transport(method, url, headers, body, timeout, **kwargs):
        requests.append(url)
        if url.endswith("robots.txt"):
            return BinaryHttpResponse(404, b"", {})
        return BinaryHttpResponse(302, b"", {"Location": "https://www.hkexnews.hk/x"})

    report = crawl_issuer_site("https://www.example-co.com/", IssuerCrawler(transport=transport, delay_seconds=0), today=TODAY)
    assert report.home_status == "blocked" and not any("hkexnews" in r for r in requests)


def test_unknown_transport_outcome_is_recorded_without_retry():
    attempts = []

    def transport(method, url, headers, body, timeout, **kwargs):
        attempts.append(url)
        if url.endswith("robots.txt"):
            return BinaryHttpResponse(404, b"", {})
        raise OSError("connection reset")

    report = crawl_issuer_site("https://www.example-co.com/", IssuerCrawler(transport=transport, delay_seconds=0), today=TODAY)
    assert report.home_status == "unreachable" and attempts.count("https://www.example-co.com/") == 1


# ------------------------------------------------------------------------------------------- the tool in the run loop

class Router:
    """One transport for Wikidata and the issuer site."""

    def __init__(self, site=None, wikidata=None):
        self.site, self.wikidata = site or FakeSite(), wikidata or FakeWikidata()
        self.requests = self.site.requests

    def __call__(self, method, url, headers, body, timeout, **kwargs):
        target = self.wikidata if urlsplit(url).netloc.endswith("wikidata.org") else self.site
        return target(method, url, headers, body, timeout, **kwargs)


def tool(router=None):
    router = router or Router()
    return IssuerDocuments(wikidata=WikidataClient(transport=router, min_interval_seconds=0, pacer=RequestPacer()),
                           transport=router, now=lambda: NOW, sleeper=lambda s: None, delay_seconds=0), router


def test_tool_returns_ranked_leads_and_acquire_sources_can_fetch_them():
    issuer, router = tool()
    acquired = []
    m = Model([turn("issuer_documents", {"entity_name": "Tencent", "ticker": "0700", "market": "HK", "doc_types": ["annual_report"],
                                         "rationale": "find the HK annual report"}, "i1"),
               turn("acquire_sources", {"urls": ["https://www.tencent.com/files/ar2025.pdf",
                                                  "https://static.cninfo.com.cn/finalpage/2026-03-20/1.PDF"], "rationale": "read it"}, "a1"), STOP])
    router.wikidata.search = {"Tencent": ["Q1"]}
    # The fake site is served under the website host Wikidata reports for Q1.
    router.site.pages = {k.replace("www.example-co.com", "www.tencent.com"): v for k, v in FakeSite().pages.items()}
    run_research_agent(question="Tencent annual report?", sources=[], model=m, context=context(), issuer_docs=issuer,
                       acquire=lambda url, title: acquired.append(url) or (None, {"status": "failed"}))
    first = feedback(m, 1)
    assert first["status"] == "ok" and first["issuer"]["website"] == "https://www.tencent.com/"
    assert first["issuer"]["website_source"] == "wikidata" and "Wikidata" in first["attribution"]
    docs = {d["url"]: d for d in first["documents"]}
    assert docs["https://www.tencent.com/files/ar2025.pdf"]["doc_type"] == "annual_report"
    assert next(iter(docs)) == "https://www.tencent.com/files/ar2025.pdf"      # requested type ranks first
    assert docs["https://static.cninfo.com.cn/finalpage/2026-03-20/1.PDF"]["acquirable"] is False
    second = feedback(m, 2)
    rows = {r["url"]: r for r in second["results"]}
    assert acquired == ["https://www.tencent.com/files/ar2025.pdf"]            # the acquirable lead reaches acquire
    assert rows["https://static.cninfo.com.cn/finalpage/2026-03-20/1.PDF"]["reason"] == "source_blocked_terms"
    assert not any("cninfo" in r or "hkexnews" in r for r in router.requests)


def test_tool_lead_is_acquirable_through_acquire_sources():
    issuer, router = tool()
    router.wikidata.search = {"Tencent": ["Q1"]}
    router.site.pages = {k.replace("www.example-co.com", "www.tencent.com"): v for k, v in FakeSite().pages.items()}
    seen = []
    m = Model([turn("issuer_documents", {"entity_name": "Tencent", "rationale": "r"}, "i1"),
               turn("acquire_sources", {"urls": ["https://www.tencent.com/files/ar2025.pdf"], "rationale": "r"}, "a1"), STOP])
    run_research_agent(question="Q", sources=[], model=m, context=context(), issuer_docs=issuer,
                       acquire=lambda url, title: seen.append((url, title)) or (None, {"status": "failed"}))
    assert seen == [("https://www.tencent.com/files/ar2025.pdf", "2025 Annual Report")]


def test_ambiguous_unknown_and_missing_websites_do_not_crawl():
    issuer, router = tool()
    router.wikidata.search = {"Tencent": ["Q1", "Q2"]}
    m = Model([turn("issuer_documents", {"entity_name": "Tencent", "rationale": "r"}, "i1"),
               turn("issuer_documents", {"entity_name": "Tencent", "website_url": "https://evil.example.net/", "rationale": "r"}, "i2"), STOP])
    state = {}
    run_research_agent(question="Q", sources=[], model=m, context=context(), issuer_docs=issuer, checkpoint=lambda s: state.update(s))
    first, second = feedback(m, 1), feedback(m, 2)
    assert first["reason"] == "issuer_website_ambiguous" and len(first["candidates"]) == 2
    assert first["message"] and second["reason"] == "website_not_known"
    assert router.site.requests == []
    assert "www.tencent.com" in state["issuer_site_hosts"]
    # A website candidate returned earlier (or written by the user) is accepted.
    router.site.pages = {k.replace("www.example-co.com", "www.tencent.com"): v for k, v in FakeSite().pages.items()}
    m2 = Model([turn("issuer_documents", {"website_url": "https://www.tencent.com/", "rationale": "r"}, "i1"), STOP])
    run_research_agent(question="Look at https://www.tencent.com/ for filings", sources=[], model=m2, context=context(), issuer_docs=issuer)
    assert feedback(m2)["status"] == "ok" and feedback(m2)["issuer"]["website_source"] == "known_in_run"
    none_found = Router(wikidata=FakeWikidata({"Nothing": []}))
    nothing = tool(none_found)[0]
    m3 = Model([turn("issuer_documents", {"entity_name": "Nothing Corp", "rationale": "r"}, "i1"), STOP])
    run_research_agent(question="Q", sources=[], model=m3, context=context(), issuer_docs=nothing)
    assert feedback(m3)["status"] == "empty" and feedback(m3)["reason"] == "issuer_website_not_found"


def test_duplicate_call_reuses_result_without_a_second_crawl():
    issuer, router = tool()
    router.wikidata.search = {"Tencent": ["Q1"]}
    router.site.pages = {k.replace("www.example-co.com", "www.tencent.com"): v for k, v in FakeSite().pages.items()}
    args = {"entity_name": "Tencent", "rationale": "r"}
    m = Model([turn("issuer_documents", args, "i1"), turn("issuer_documents", {**args, "rationale": "again"}, "i2"), STOP])
    run_research_agent(question="Q", sources=[], model=m, context=context(), issuer_docs=issuer)
    again = feedback(m, 2)
    home_requests = [r for r in router.site.requests if r == "https://www.tencent.com/"]
    assert again["status"] == "duplicate_action" and len(home_requests) == 1


def test_capability_flag_tool_registration_and_unavailable_result():
    names = [t.name for t in TOOLS]
    assert names.index("filings") < names.index("issuer_documents") < names.index("submit_research") and names[-1] == "submit_research"
    assert "issuer_documents" in SYSTEM and "HKEXnews" in SYSTEM and "2026" not in SYSTEM
    for reason in ("issuer_website_not_found", "issuer_site_robots_disallow", "issuer_no_documents_found", "source_user_initiated_only"):
        assert reason in TOOL_FAILURE_GUIDANCE
    issuer, _ = tool()
    m = Model([STOP])
    run_research_agent(question="Q", sources=[], model=m, context=context(), issuer_docs=issuer)
    assert json.loads(m.inputs[0][1]["content"])["available_capabilities"]["issuer_documents_available"] is True
    m = Model([turn("issuer_documents", {"entity_name": "X", "rationale": "r"}, "i1"), STOP])
    run_research_agent(question="Q", sources=[], model=m, context=context())
    assert json.loads(m.inputs[0][1]["content"])["available_capabilities"]["issuer_documents_available"] is False
    unavailable = feedback(m)
    assert unavailable["status"] == "unavailable" and unavailable["reason"] == "issuer_documents_unavailable" and unavailable["message"]
    invalid = Model([turn("issuer_documents", {"rationale": "r"}, "i1"), STOP])
    run_research_agent(question="Q", sources=[], model=invalid, context=context(), issuer_docs=issuer)
    assert feedback(invalid)["status"] == "invalid"


def test_request_schema():
    with pytest.raises(ValueError):
        IssuerDocumentsRequest(rationale="r")
    with pytest.raises(ValueError):
        IssuerDocumentsRequest(entity_name="A", since=date(2026, 2, 1), until=date(2026, 1, 1), rationale="r")
    schema = IssuerDocumentsRequest.model_json_schema()
    assert set(schema["properties"]) >= {"entity_name", "entity_id", "ticker", "market", "since", "until", "doc_types", "limit", "rationale"}


# ------------------------------------------------------------------------------------------- user_initiated_only

CNINFO = "https://static.cninfo.com.cn/finalpage/2026-03-20/1.PDF"
SSE = "https://static.sse.com.cn/disclosure/listedinfo/announcement/c/new/2026-03-20/600519_2025_n.pdf"
HKEX = "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0320/x.pdf"


def test_personal_mode_decision_matrix(monkeypatch):
    monkeypatch.setenv(OVERRIDES_ENV, PERSONAL)
    agent = terms_decision(CNINFO)
    assert not agent.allowed and agent.status == "user_initiated_only" and agent.reason_code == "source_user_initiated_only"
    user = terms_decision(CNINFO, initiator="user")
    assert user.allowed and user.status == "user_initiated_only"
    assert terms_decision(SSE, initiator="user").allowed and terms_decision("https://www.szse.cn/a", initiator="user").allowed
    with user_initiated_scope(CNINFO):
        assert terms_decision(CNINFO).allowed and terms_decision("https://disc.static.cninfo.com.cn/x").allowed
        assert not terms_decision(SSE).allowed                      # the scope covers the named site family only
    assert not terms_decision(CNINFO).allowed
    # HKEX stays prohibited whatever the mode or initiator.
    for initiator in (None, "user"):
        decision = terms_decision(HKEX, initiator=initiator)
        assert not decision.allowed and decision.status == "prohibited"
    with user_initiated_scope(HKEX):
        assert not terms_decision(HKEX).allowed


def test_personal_mode_needs_a_reference_and_is_off_by_default(monkeypatch):
    assert not terms_decision(CNINFO, initiator="user").allowed
    monkeypatch.setenv(OVERRIDES_ENV, json.dumps([{"host": "cninfo.com.cn", "status": "user_initiated_only"}]))
    assert not terms_decision(CNINFO, initiator="user").allowed
    monkeypatch.setenv(OVERRIDES_ENV, json.dumps([{"host": "hkexnews.hk", "status": "user_initiated_only", "permission_reference": "x"}]))
    assert not terms_decision(HKEX, initiator="user").allowed


def test_fetch_target_check_follows_the_user_scope(monkeypatch):
    monkeypatch.setenv(OVERRIDES_ENV, PERSONAL)
    with pytest.raises(FetchFailure) as error:
        _check_fetch_target(CNINFO, is_redirect=False, check_dns=False)
    assert error.value.code == "source_blocked_terms" and "single document" in error.value.detail
    with user_initiated_scope(CNINFO):
        assert _check_fetch_target(CNINFO, is_redirect=False, check_dns=False) == "static.cninfo.com.cn"
    with user_initiated_scope(HKEX):
        with pytest.raises(FetchFailure):
            _check_fetch_target(HKEX, is_redirect=False, check_dns=False)


def test_question_urls_are_user_leads_acquired_once_while_agent_leads_stay_blocked(monkeypatch):
    monkeypatch.setenv(OVERRIDES_ENV, PERSONAL)
    inside = []

    def acquire(url, title):
        inside.append((url, terms_decision(url).allowed))
        return None, {"status": "failed"}

    discovered = [{"title": "Agent found", "url": "https://static.cninfo.com.cn/finalpage/2026-03-21/2.PDF"}]
    question = f"请阅读这份年报 {CNINFO}，以及 {HKEX} 和 https://www.cninfo.com.cn/new/x 。"
    m = Model([turn("news_search", {"query": "q", "rationale": "r"}, "d1"),
               turn("acquire_sources", {"urls": [CNINFO, discovered[0]["url"], HKEX], "rationale": "r"}, "a1"), STOP])
    state = {}
    run_research_agent(question=question, sources=[], model=m, context=context(), discover=lambda q: discovered, acquire=acquire,
                       checkpoint=lambda s: state.update(s))
    rows = {r["url"]: r for r in feedback(m, 2)["results"]}
    assert inside == [(CNINFO, True)]                                 # acquired once, inside the user scope
    assert rows[discovered[0]["url"]]["reason"] == "source_user_initiated_only"
    assert rows[HKEX]["reason"] == "source_blocked_terms" and rows[HKEX]["external_state"] == "not_started"
    assert rows[discovered[0]["url"]]["message"] == TOOL_FAILURE_GUIDANCE["source_user_initiated_only"]
    user_leads = {lead["url"]: lead for lead in state["leads"] if lead.get("initiator") == "user"}
    assert CNINFO in user_leads and user_leads[CNINFO].get("acquirable") is not False
    assert user_leads[HKEX]["acquirable"] is False


def test_question_urls_without_personal_mode_stay_blocked():
    inside = []
    m = Model([turn("acquire_sources", {"urls": [CNINFO], "rationale": "r"}, "a1"), STOP])
    run_research_agent(question=f"Read {CNINFO}", sources=[], model=m, context=context(),
                       acquire=lambda u, t: inside.append(u) or (None, {"status": "failed"}))
    row = feedback(m)["results"][0]
    assert row["reason"] == "source_blocked_terms" and inside == []


@pytest.fixture
def client():
    return TestClient(api.app)


def owner(client):
    response = client.post("/v1/auth/local/register", json={"email": f"{uuid4().hex}@example.test", "password": "correct horse battery staple!"})
    return {"Authorization": "Bearer " + response.json()["access_token"]}


PERMS = {"store": True, "display": True, "model_input": True, "export": True}


def pdf_transport(method, url, headers, body, timeout, **kwargs):
    return BinaryHttpResponse(200, "公告正文：本公司2025年度营业收入为100亿元。".encode(), {"Content-Type": "text/plain; charset=utf-8"})


def test_manual_fetch_of_a_pasted_cninfo_url_is_allowed_in_personal_mode(client, monkeypatch):
    headers = owner(client)
    real = api.fetch_source_document
    monkeypatch.setattr(api, "fetch_source_document", lambda request: real(request, pdf_transport))
    blocked = client.post("/v2/research/sources/fetch", headers=headers, json={"url": CNINFO, "title": "AR", "permissions": PERMS})
    assert blocked.status_code == 422 and "terms do not permit automated access" in blocked.json()["message"]
    monkeypatch.setenv(OVERRIDES_ENV, PERSONAL)
    allowed = client.post("/v2/research/sources/fetch", headers=headers, json={"url": CNINFO, "title": "AR", "permissions": PERMS})
    assert allowed.status_code == 201, allowed.text
    stored = api.research_sources.get(allowed.json()["owner_id"], allowed.json()["source_id"])
    assert stored.provenance["retrieval_initiator"] == "user" and stored.provenance["terms_mode"] == "user_initiated_only"
    assert "personal" in stored.provenance["permission_reference"]
    hkex = client.post("/v2/research/sources/fetch", headers=headers, json={"url": HKEX, "title": "AR", "permissions": PERMS})
    assert hkex.status_code == 422 and "terms do not permit automated access" in hkex.json()["message"]
    # Ordinary sites carry no personal-mode provenance.
    plain = client.post("/v2/research/sources/fetch", headers=headers, json={"url": "https://ir.example.com/a.txt", "title": "T", "permissions": PERMS})
    assert plain.status_code == 201
    assert "retrieval_initiator" not in api.research_sources.get(plain.json()["owner_id"], plain.json()["source_id"]).provenance


def test_manual_fetch_scope_does_not_leak_to_later_requests(client, monkeypatch):
    monkeypatch.setenv(OVERRIDES_ENV, PERSONAL)
    real = api.fetch_source_document
    monkeypatch.setattr(api, "fetch_source_document", lambda request: real(request, pdf_transport))
    client.post("/v2/research/sources/fetch", headers=owner(client), json={"url": CNINFO, "title": "AR", "permissions": PERMS})
    assert not terms_decision(CNINFO).allowed
    with pytest.raises(FetchFailure):
        fetch_source_document(FetchSourceRequest(url=CNINFO, title="x", permissions={"store": True}), pdf_transport)


def test_time_budget_stops_the_crawl_and_robots_are_cached():
    site = FakeSite()
    ticks = iter(range(0, 1000))
    crawler = IssuerCrawler(transport=site, clock=lambda: float(next(ticks)), sleeper=lambda s: None, delay_seconds=0, deadline=3.0)
    report = crawl_issuer_site("https://www.example-co.com/", crawler, today=TODAY)
    assert report.time_exhausted and report.pages_fetched <= 1
    issuer_documents.clear_robots_cache()
    full = FakeSite()
    crawl(full)
    crawl(full)
    assert sum(1 for r in full.requests if r.endswith("/robots.txt")) == 1          # cached for 24 hours across crawls
