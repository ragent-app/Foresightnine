"""Site-terms registry and its enforcement in discovery, acquisition and fetch (plan 6.10)."""
import json

import pytest

from app.openai_compatible import BinaryHttpResponse
from app.research_fetch import FetchFailure, FetchSourceRequest, _check_fetch_target, fetch_source_document
from app.research_runtime import TOOL_FAILURE_GUIDANCE, SYSTEM, run_research_agent
from app.research_source_terms import OVERRIDES_ENV, TERMS_RULES, terms_decision
from tests.test_research_runtime import Model, context, turn
from tests.test_research_runtime_s0a import STOP, feedback


@pytest.fixture(autouse=True)
def _no_overrides(monkeypatch):
    monkeypatch.delenv(OVERRIDES_ENV, raising=False)


@pytest.mark.parametrize("url", [
    "https://www.hkexnews.hk/listedco/listconews/sehk/2026/0301/x.pdf",
    "https://www1.hkexnews.hk/a.pdf",
    "https://www.hkex.com.hk/Market-Data",
    "https://HKEXNEWS.HK./x",
])
def test_prohibited_hosts_and_subdomains(url):
    decision = terms_decision(url)
    assert not decision.allowed and decision.status == "prohibited"
    assert decision.reason_code == "hkex_terms_prohibit_automated_access" and decision.evidence_url
    assert "terms do not permit automated access" in decision.message


@pytest.mark.parametrize("url,code", [
    ("https://www.cninfo.com.cn/new/disclosure", "cninfo_terms_unverified"),
    ("https://static.cninfo.com.cn/finalpage/a.PDF", "cninfo_terms_unverified"),
    ("https://www.sse.com.cn/disclosure/", "sse_terms_unverified"),
    ("https://disc.static.szse.cn/download/a.pdf", "szse_terms_unverified"),
    ("https://szse.cn/", "szse_terms_unverified"),
])
def test_unverified_commercial_hosts(url, code):
    decision = terms_decision(url)
    assert not decision.allowed and decision.status == "unverified_commercial" and decision.reason_code == code


@pytest.mark.parametrize("url", [
    "https://www.sec.gov/Archives/x.htm", "https://example.com/hkexnews.hk", "https://nothkexnews.hk.example.com/",
    "https://evilcninfo.com.cn/", "https://ir.example.com.hk/annual.pdf", "https://www.sse.com.cn.example.org/",
])
def test_other_hosts_and_lookalikes_are_not_matched(url):
    assert terms_decision(url).allowed


def test_registry_is_versioned_data():
    assert {rule.host for rule in TERMS_RULES} == {"hkex.com.hk", "hkexnews.hk", "cninfo.com.cn", "sse.com.cn", "szse.cn"}
    assert all(rule.checked_on == "2026-11-20" and rule.evidence_url.startswith("https://") for rule in TERMS_RULES)


def override(monkeypatch, *items):
    monkeypatch.setenv(OVERRIDES_ENV, json.dumps(list(items)))


def test_override_allows_unverified_host_with_reference(monkeypatch):
    override(monkeypatch, {"host": "cninfo.com.cn", "status": "allowed", "permission_reference": "letter-2026-12"})
    assert terms_decision("https://static.cninfo.com.cn/a.pdf").allowed
    assert not terms_decision("https://www.sse.com.cn/a").allowed


def test_override_scope_is_the_named_host_and_below(monkeypatch):
    override(monkeypatch, {"host": "static.cninfo.com.cn", "status": "allowed", "permission_reference": "r"})
    assert terms_decision("https://static.cninfo.com.cn/a.pdf").allowed
    assert not terms_decision("https://www.cninfo.com.cn/a").allowed


def test_override_cannot_unblock_prohibited_without_written_permission(monkeypatch):
    override(monkeypatch, {"host": "hkexnews.hk", "status": "allowed", "permission_reference": "ref"})
    assert not terms_decision("https://www1.hkexnews.hk/a.pdf").allowed
    override(monkeypatch, {"host": "hkexnews.hk", "status": "allowed", "permission_reference": "ref", "written_permission": "yes"})
    assert not terms_decision("https://www1.hkexnews.hk/a.pdf").allowed
    override(monkeypatch, {"host": "hkexnews.hk", "status": "allowed", "written_permission": True})
    assert not terms_decision("https://www1.hkexnews.hk/a.pdf").allowed
    override(monkeypatch, {"host": "hkexnews.hk", "status": "allowed", "permission_reference": "HKEX-2026-001", "written_permission": True})
    assert terms_decision("https://www1.hkexnews.hk/a.pdf").allowed


@pytest.mark.parametrize("raw", ["not json", "{}", "[1, 2]", '[{"host": "cninfo.com.cn", "status": "blocked", "permission_reference": "r"}]',
                                 '[{"host": "", "status": "allowed", "permission_reference": "r"}]'])
def test_malformed_overrides_are_ignored(monkeypatch, raw):
    monkeypatch.setenv(OVERRIDES_ENV, raw)
    assert not terms_decision("https://www.cninfo.com.cn/a").allowed


def test_fetch_target_blocked_with_terms_code_and_override(monkeypatch):
    with pytest.raises(FetchFailure) as blocked:
        _check_fetch_target("https://www1.hkexnews.hk/a.pdf", is_redirect=False, check_dns=False)
    assert blocked.value.code == "source_blocked_terms" and "upload" in (blocked.value.detail or "")
    with pytest.raises(FetchFailure) as redirected:
        _check_fetch_target("https://static.cninfo.com.cn/a.pdf", is_redirect=True, check_dns=False)
    assert redirected.value.code == "source_blocked_terms"
    override(monkeypatch, {"host": "cninfo.com.cn", "status": "allowed", "permission_reference": "r"})
    assert _check_fetch_target("https://static.cninfo.com.cn/a.pdf", is_redirect=False, check_dns=False)


def fetch_request(url):
    return FetchSourceRequest(url=url, title="Report", permissions={"store": True})


def test_no_network_request_for_blocked_start_url():
    calls = []

    def transport(*args, **kwargs):
        calls.append(args)
        raise AssertionError("must not be called")

    with pytest.raises(FetchFailure) as error:
        fetch_source_document(fetch_request("https://www.hkexnews.hk/a.pdf"), transport)
    assert error.value.code == "source_blocked_terms" and not calls


def test_redirect_into_blocked_host_stops_before_second_request():
    calls = []

    def transport(method, url, headers, body, timeout, **kwargs):
        calls.append(url)
        return BinaryHttpResponse(302, b"", {"Location": "https://static.cninfo.com.cn/final.pdf"})

    with pytest.raises(FetchFailure) as error:
        fetch_source_document(fetch_request("https://ir.example.com/report.pdf"), transport)
    assert error.value.code == "source_blocked_terms"
    assert calls == ["https://ir.example.com/report.pdf"]


LEADS = [{"title": "HK annual report", "url": "https://www1.hkexnews.hk/listedco/a.pdf"},
         {"title": "IR copy", "url": "https://ir.example.com.hk/annual.pdf"},
         {"title": "A-share report", "url": "https://static.cninfo.com.cn/finalpage/a.PDF"}]


def test_discovery_keeps_blocked_leads_visible_but_not_acquirable():
    m = Model([turn("news_search", {"query": "q", "rationale": "r"}, "d1"), STOP])
    run_research_agent(question="Q", sources=[], model=m, context=context(), discover=lambda q: LEADS)
    fb = feedback(m)
    by_host = {lead["url"].split("/")[2]: lead for lead in fb["leads"]}
    assert by_host["www1.hkexnews.hk"]["acquirable"] is False and by_host["www1.hkexnews.hk"]["terms_status"] == "prohibited"
    assert by_host["static.cninfo.com.cn"]["terms_status"] == "unverified_commercial"
    assert "acquirable" not in by_host["ir.example.com.hk"] and "terms_status" not in by_host["ir.example.com.hk"]
    assert fb["terms_blocked_count"] == 2 and fb["message"] == TOOL_FAILURE_GUIDANCE["source_blocked_terms"]


def test_acquire_of_blocked_lead_is_denied_without_calling_acquire():
    calls = []
    m = Model([turn("news_search", {"query": "q", "rationale": "r"}, "d1"),
               turn("acquire_sources", {"urls": [LEADS[0]["url"], LEADS[1]["url"]], "rationale": "r"}, "a1"), STOP])
    run_research_agent(question="Q", sources=[], model=m, context=context(), discover=lambda q: LEADS,
                       acquire=lambda url, title: calls.append(url) or (None, {"status": "failed"}))
    fb = json.loads([msg for msg in m.inputs[2] if msg.get("role") == "tool"][-1]["content"])
    blocked = next(row for row in fb["results"] if "hkexnews" in row["url"])
    assert blocked["status"] == "denied_or_unavailable" and blocked["reason"] == "source_blocked_terms"
    assert blocked["external_state"] == "not_started" and blocked["message"] == TOOL_FAILURE_GUIDANCE["source_blocked_terms"]
    assert calls == [LEADS[1]["url"]]


def test_guidance_text_and_static_prompt():
    text = TOOL_FAILURE_GUIDANCE["source_blocked_terms"]
    assert "terms do not permit automated access" in text and "upload" in text and "SEC filing" in text
    assert "automated access" not in SYSTEM  # the static prompt is unchanged; the rule is conveyed per result
