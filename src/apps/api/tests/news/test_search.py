from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.fin_data.models import Cost, ToolResult
from app.news.gdelt import GDELT_ATTRIBUTION, NewsLead
from app.news.search import GdeltNewsProvider, GlmNewsProvider, news_search

UTC = timezone.utc


class Fake:
    def __init__(self, name, result, supports=False):
        self.name, self.result, self.supports_time_filter, self.calls = name, result, supports, []

    def search(self, query, **kw):
        self.calls.append((query, kw))
        return self.result


def ok(leads):
    return ToolResult(status="ok", reason="ok", data=leads, cost=Cost(provider_calls=1))


def lead(url, title, provider, **kw):
    return NewsLead(url=url, title=title, providers=[provider], **kw)


def test_merge_filter_annotate_cluster():
    glm = Fake("glm", ok([
        lead("https://www.reuters.com/a?utm_source=x", "Fed holds rates steady in November", "glm", published_at=datetime(2026, 11, 19, tzinfo=UTC), published_precision="date"),
        lead("https://old.com/o", "Old story about something else entirely different", "glm", published_at=datetime(2026, 1, 1, tzinfo=UTC), published_precision="date"),
        lead("https://undated.com/u", "Completely unrelated article about oil tanker rates", "glm"),
        lead("https://www.hkexnews.hk/doc", "HKEX announcement of results for the quarter", "glm", published_at=datetime(2026, 11, 18, tzinfo=UTC), published_precision="date"),
    ]))
    gd = Fake("gdelt", ok([
        lead("https://reuters.com/a", "Fed holds rates steady in November", "gdelt", seen_at=datetime(2026, 11, 19, 21, tzinfo=UTC), domain="reuters.com", source_type="news", attribution=[GDELT_ATTRIBUTION]),
        lead("https://sec.gov/x", "SEC statement on climate filings review", "gdelt", seen_at=datetime(2026, 11, 19, 5, tzinfo=UTC), source_type="news", attribution=[GDELT_ATTRIBUTION]),
    ]), supports=True)
    r = news_search("fed rates", since=date(2026, 11, 1), until=date(2026, 11, 20), providers=[glm, gd], entities=["Federal Reserve"])
    assert r.status == "ok"
    leads = [m for g in r.data for m in g["members"]]
    urls = {m.url for m in leads}
    assert "https://old.com/o" not in urls                       # dropped: dated, outside window
    assert "https://undated.com/u" in urls                       # kept: undated, flagged
    merged = next(m for m in leads if m.canonical_url == "https://reuters.com/a")
    assert merged.providers == ["gdelt", "glm"] and merged.published_at and merged.seen_at
    assert GDELT_ATTRIBUTION in merged.attribution and merged.cluster_id
    hk = next(m for m in leads if "hkexnews" in m.url)
    assert hk.acquirable is False and hk.terms_status == "prohibited" and hk.terms_reason
    assert next(m for m in leads if "undated" in m.url).acquirable is True
    assert next(m for m in leads if "sec.gov" in m.url).source_type == "official"
    notes = " ".join(r.coverage.notes)
    assert "dropped 1" in notes and "2 have no known publication date" in notes and GDELT_ATTRIBUTION in notes
    assert "do not support time filtering" in notes and "glm" in notes
    assert gd.calls[0][1]["since"] == date(2026, 11, 1) and '"Federal Reserve"' in gd.calls[0][0]
    r.to_wire()


def test_partial_when_one_provider_fails_and_all_failed():
    bad = Fake("gdelt", ToolResult.failure("rate_limited", "gdelt_rate_limited", retryable=True), supports=True)
    good = Fake("glm", ok([lead("https://a.com/x", "Some headline about markets today", "glm")]))
    r = news_search("markets", providers=[good, bad])
    assert r.status == "partial" and "gdelt" in " ".join(r.coverage.notes) and GDELT_ATTRIBUTION in r.coverage.notes
    r = news_search("markets", providers=[bad])
    assert r.status == "rate_limited" and r.retryable
    assert news_search("markets", providers=[Fake("x", ToolResult.failure("failed", "boom"))]).status == "failed"


def test_empty_and_invalid():
    e = ToolResult(status="empty", reason="no_results")
    assert news_search("markets", providers=[Fake("a", e)]).status == "empty"
    assert news_search("  ", providers=[Fake("a", e)]).reason == "news_query_invalid"
    assert news_search("x", providers=[]).reason == "no_news_provider"


def test_source_type_and_language_filters():
    gd = Fake("gdelt", ok([lead("https://a.com/1", "English headline about central banks", "gdelt", language="en", source_type="news"),
                           lead("https://b.com/2", "中文标题关于央行政策", "gdelt", language="zh", source_type="news"),
                           lead("https://sec.gov/3", "Official filing notice about reporting", "gdelt", language="en", source_type="news")]), supports=True)
    r = news_search("central banks", languages=["zh"], source_types=["news"], providers=[gd])
    urls = {m.url for g in r.data for m in g["members"]}
    assert urls == {"https://b.com/2"}


def test_unresolved_google_redirect_not_acquirable():
    glm = Fake("glm", ok([lead("https://www.google.com/goto?url=abcdefgh", "Redirect only headline from search", "glm")]))
    r = news_search("q", providers=[glm])
    m = r.data[0]["members"][0]
    assert m.unresolved_redirect is True and m.acquirable is False


def test_glm_adapter_with_fake_provider():
    class P:
        supports_project_search = True
        def search(self, query, *, limit):
            return (SimpleNamespace(title="美联储维持利率不变", url="https://a.com/1", excerpt="x", publisher="A", published_at=date(2026, 11, 19), independently_reviewable=True),)
    res = GlmNewsProvider(P()).search("q", limit=5)
    assert res.status == "ok" and res.data[0].published_precision == "date" and res.data[0].language == "zh"

    class Boom:
        supports_project_search = True
        def search(self, query, *, limit):
            from app.phase3 import ProviderFailure
            raise ProviderFailure("search_provider_rate_limited", "slow down", retryable=True)
    assert GlmNewsProvider(Boom()).search("q").status == "rate_limited"

    class Off:
        supports_project_search = False
    assert GlmNewsProvider(Off()).search("q").status == "denied"


def test_gdelt_provider_wraps_client():
    class C:
        def search(self, query, **kw):
            return ToolResult(status="empty", reason="gdelt_no_articles")
    assert GdeltNewsProvider(C()).search("q").status == "empty"
