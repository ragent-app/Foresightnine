from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.fin_data.models import Cost, ToolResult
from app.news.gdelt import GDELT_ATTRIBUTION, NewsLead
from app.news.search import (
    AUTHORITY_TIER_WEIGHTS,
    EVENT_TYPES,
    GdeltNewsProvider,
    GlmNewsProvider,
    classify_authority_tier,
    entity_matches,
    news_search,
    tag_news_events,
)

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


def failed(status, reason, message="", retryable=True):
    return ToolResult.failure(status, reason, message, retryable=retryable, cost=Cost(provider_calls=1))


QUOTA_MSG = "您已达到每周/每月使用上限，您的限额将在 2026-11-23 21:28:02 重置。 (quota resets at 2026-11-23T21:28:02+08:00)"


def test_all_providers_limited_reports_quota_with_each_provider_and_reset():
    glm = Fake("glm", failed("failed", "search_quota_exhausted", QUOTA_MSG, retryable=False))
    gd = Fake("gdelt", failed("rate_limited", "gdelt_rate_limited", "GDELT answered HTTP 429", retryable=True), supports=True)
    r = news_search("微软 营收", providers=[glm, gd])
    assert r.reason == "search_quota_exhausted" and r.status == "rate_limited" and r.retryable
    assert "glm: search_quota_exhausted" in r.message and "2026-11-23" in r.message and "gdelt: gdelt_rate_limited" in r.message
    assert r.reason != "all_providers_failed"


def test_only_quota_is_failed_not_retryable_and_only_rate_is_search_rate_limited():
    quota = news_search("q", providers=[Fake("glm", failed("failed", "search_quota_exhausted", QUOTA_MSG, retryable=False))])
    assert (quota.status, quota.reason, quota.retryable) == ("failed", "search_quota_exhausted", False)
    rate = news_search("q", providers=[Fake("glm", failed("rate_limited", "search_rate_limited", "slow down")),
                                       Fake("gdelt", failed("rate_limited", "gdelt_rate_limited", "429"), supports=True)])
    assert (rate.status, rate.reason) == ("rate_limited", "search_rate_limited")


def test_mixed_limit_and_unavailable_keeps_all_providers_failed_with_messages_in_notes():
    r = news_search("q", providers=[Fake("glm", failed("failed", "search_quota_exhausted", QUOTA_MSG, retryable=False)),
                                    Fake("gdelt", failed("failed", "gdelt_unavailable", "down"), supports=True)])
    assert r.reason == "all_providers_failed"
    assert "search_quota_exhausted" in r.message and "2026-11-23" in r.message


def test_glm_adapter_keeps_quota_failure_code_message_and_reset():
    from app.phase3 import ProviderFailure
    error = ProviderFailure("search_quota_exhausted", "limit reached", retryable=False)
    error.reset_at = "2026-11-23T21:28:02+08:00"

    class Boom:
        supports_project_search = True

        def search(self, *a, **k):
            raise error

    r = GlmNewsProvider(Boom()).search("q")
    assert r.status == "failed" and r.reason == "search_quota_exhausted" and not r.retryable
    assert "limit reached" in r.message and "2026-11-23T21:28:02+08:00" in r.message


def test_newest_clusters_kept_when_truncated_by_limit():
    leads = [
        lead("https://alpha.example.com/1", "Alpha Semiconductor Fab Expansion Update", "glm", published_at=datetime(2026, 11, 1, tzinfo=UTC)),
        lead("https://beta.example.com/2", "Beta Copper Mine Output Report", "glm", published_at=datetime(2026, 11, 5, tzinfo=UTC)),
        lead("https://gamma.example.com/3", "Gamma Grid Transformer Shortage Briefing", "glm", published_at=datetime(2026, 11, 10, tzinfo=UTC)),
        lead("https://delta.example.com/4", "Delta Airline Fuel Hedging Results", "glm", published_at=datetime(2026, 11, 15, tzinfo=UTC)),
        lead("https://epsilon.example.com/5", "Epsilon Sovereign Bond Auction Demand", "glm", published_at=datetime(2026, 11, 20, tzinfo=UTC)),
        lead("https://zeta.example.com/6", "Zeta Undated Background Industry Primer", "glm"),
    ]
    r = news_search("market", limit=2, providers=[Fake("glm", ok(leads))])
    assert r.status == "ok"
    assert r.coverage.truncated is True
    assert [g["representative"].url for g in r.data] == [
        "https://epsilon.example.com/5",
        "https://delta.example.com/4",
    ]


def test_concurrent_multi_provider_search_preserves_deterministic_merge_order():
    import threading
    import time

    barrier = threading.Barrier(2, timeout=2.0)

    class BarrierProvider:
        def __init__(self, name, result, delay=0.0):
            self.name = name
            self.result = result
            self.delay = delay
            self.supports_time_filter = False

        def search(self, query, **kw):
            barrier.wait()
            if self.delay:
                time.sleep(self.delay)
            return self.result

    p1 = BarrierProvider(
        "first",
        ok([
            lead("https://www.reuters.com/story?utm_source=first", "First provider headline about rates", "first",
                 published_at=datetime(2026, 11, 19, tzinfo=UTC), excerpt="First excerpt"),
            lead("https://only-first.example.com/a", "Unique story from first provider", "first",
                 published_at=datetime(2026, 11, 18, tzinfo=UTC)),
        ]),
        delay=0.05,  # finishes slower than p2, yet must win deterministic merge order
    )
    p2 = BarrierProvider(
        "second",
        ok([
            lead("https://reuters.com/story", "Second provider headline about rates", "second",
                 seen_at=datetime(2026, 11, 19, 12, tzinfo=UTC), domain="reuters.com"),
            lead("https://only-second.example.com/b", "Unique story from second provider", "second",
                 published_at=datetime(2026, 11, 20, tzinfo=UTC)),
        ]),
        delay=0.0,
    )

    r = news_search("rates", providers=[p1, p2])
    assert r.status == "ok"
    assert "Providers answered: first, second." in r.coverage.notes
    all_leads = [m for g in r.data for m in g["members"]]
    merged = next(m for m in all_leads if m.canonical_url == "https://reuters.com/story")
    assert merged.url == "https://www.reuters.com/story?utm_source=first"
    assert merged.excerpt == "First excerpt"
    assert merged.seen_at == datetime(2026, 11, 19, 12, tzinfo=UTC)
    assert merged.providers == ["first", "second"]


def test_concurrent_provider_exception_and_timeout_isolation():
    import threading

    release_hung = threading.Event()

    class ExplodingProvider:
        name = "exploding"
        supports_time_filter = False

        def search(self, query, **kw):
            raise RuntimeError("boom")

    class HungProvider:
        name = "hung"
        supports_time_filter = False

        def search(self, query, **kw):
            release_hung.wait(timeout=2.0)
            return ok([lead("https://hung.example.com/late", "Too late", "hung")])

    fast = Fake("fast", ok([
        lead("https://fast.example.com/news", "Fast provider story arrives cleanly", "fast",
             published_at=datetime(2026, 11, 20, tzinfo=UTC)),
    ]))
    try:
        r = news_search("news", providers=[ExplodingProvider(), HungProvider(), fast], timeout_seconds=0.05)
    finally:
        release_hung.set()

    assert r.status == "partial"
    assert [g["representative"].url for g in r.data] == ["https://fast.example.com/news"]
    notes = " ".join(r.coverage.notes)
    assert "Providers answered: fast." in notes
    assert "Provider exploding failed (provider_exception" in notes
    assert "Provider hung failed (provider_timeout" in notes


def test_composite_ranking_respects_authority_relevance_as_of_and_recency_mode():
    ref_as_of = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    leads = [
        # Newest, but aggregator and weak match
        lead("https://finance.yahoo.com/news/roundup", "Market wrap for Tuesday", "glm",
             published_at=datetime(2026, 6, 15, 10, 0, tzinfo=UTC), excerpt="Broad markets digest macro news."),
        # Slightly older, official filing matching both query and entity
        lead("https://www.sec.gov/Archives/nvda-10q", "NVDA Form 10-Q Quarterly Revenue Filing", "glm",
             published_at=datetime(2026, 6, 14, 10, 0, tzinfo=UTC), excerpt="NVDA reports data center revenue growth."),
        # Wire agency matching query strongly, same day as official
        lead("https://www.reuters.com/tech/nvda-revenue", "NVDA quarterly revenue jumps on data center demand", "glm",
             published_at=datetime(2026, 6, 14, 11, 0, tzinfo=UTC), excerpt="Reuters coverage of NVDA revenue."),
        # General tech blog matching query, same day (distinct story text so not clustered with Reuters)
        lead("https://techblog.example.com/nvda-post", "NVDA quarterly revenue surges in AI server boom", "glm",
             published_at=datetime(2026, 6, 14, 11, 30, tzinfo=UTC), excerpt="Analysis of NVDA revenue."),
        # Much older official filing (decayed by as_of=2026-06-15)
        lead("https://www.sec.gov/Archives/nvda-old", "NVDA Form 10-Q Quarterly Revenue Filing", "glm",
             published_at=datetime(2026, 2, 1, 10, 0, tzinfo=UTC), excerpt="Older NVDA filing."),
    ]
    assert classify_authority_tier(leads[1]) == "official"
    assert classify_authority_tier(leads[2]) == "wire_agency"
    assert classify_authority_tier(leads[3]) == "general"
    assert classify_authority_tier(leads[0]) == "aggregator"
    assert AUTHORITY_TIER_WEIGHTS["official"] > AUTHORITY_TIER_WEIGHTS["wire_agency"] > AUTHORITY_TIER_WEIGHTS["general"] > AUTHORITY_TIER_WEIGHTS["aggregator"]

    rel = news_search("NVDA quarterly revenue", entities=["NVDA"], as_of=ref_as_of, providers=[Fake("glm", ok(leads))])
    assert rel.status == "ok"
    rel_urls = [g["representative"].url for g in rel.data]
    assert rel_urls[0] == "https://www.sec.gov/Archives/nvda-10q"
    assert rel_urls.index("https://www.reuters.com/tech/nvda-revenue") < rel_urls.index("https://techblog.example.com/nvda-post")
    assert rel_urls.index("https://techblog.example.com/nvda-post") < rel_urls.index("https://finance.yahoo.com/news/roundup")
    assert rel_urls[-1] == "https://www.sec.gov/Archives/nvda-old"

    rec = news_search("NVDA quarterly revenue", entities=["NVDA"], sort_by="recency", as_of=ref_as_of, providers=[Fake("glm", ok(leads))])
    assert [g["representative"].url for g in rec.data] == [
        "https://finance.yahoo.com/news/roundup",
        "https://techblog.example.com/nvda-post",
        "https://www.reuters.com/tech/nvda-revenue",
        "https://www.sec.gov/Archives/nvda-10q",
        "https://www.sec.gov/Archives/nvda-old",
    ]


def test_composite_ranking_deterministic_tie_breaking_and_invalid_sort_by():
    same_dt = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
    l1 = lead("https://b.example.com/story", "Chile copper mine output drops unexpectedly", "glm", published_at=same_dt)
    l2 = lead("https://a.example.com/story", "Peru copper smelter margins expand sharply", "glm", published_at=same_dt)
    res = news_search("copper", providers=[Fake("glm", ok([l1, l2]))])
    assert [g["representative"].canonical_url for g in res.data] == [
        "https://a.example.com/story",
        "https://b.example.com/story",
    ]

    bad = news_search("copper", sort_by="popularity", providers=[Fake("glm", ok([l1]))])  # type: ignore[arg-type]
    assert bad.status == "failed" and bad.reason == "news_sort_invalid"


def test_word_boundary_and_cjk_entity_matching():
    assert entity_matches("AI", "AI chip demand surges in Q3")
    assert entity_matches("AI", "C3.ai reports quarterly results")
    assert entity_matches("AI", "人工智能AI赛道持续升温")
    assert not entity_matches("AI", "Analysts said claims on Taiwan rail gained momentum")

    assert entity_matches("ON", "ON Semiconductor beats expectations")
    assert entity_matches("ON", "美股ON半导体盘前上涨")
    assert not entity_matches("ON", "Company announced billion-dollar deal on Monday")
    assert not entity_matches("ON", "On Tuesday markets paused")

    assert entity_matches("比亚迪", "比亚迪公布第三季度财报")
    assert entity_matches("Federal Reserve", "The Federal Reserve held rates steady")

    glm = Fake("glm", ok([
        lead("https://a.example.com/1", "Analysts said Taiwan rail gained", "glm"),
        lead("https://a.example.com/2", "人工智能AI芯片龙头发布业绩", "glm"),
        lead("https://a.example.com/3", "Report on Monday billion-dollar company", "glm"),
        lead("https://a.example.com/4", "(NASDAQ: ON) beats Q3 revenue", "glm"),
    ]))
    r = news_search("said", entities=["AI", "ON"], providers=[glm])
    by_url = {m.url: m.matched_entities for g in r.data for m in g["members"]}
    assert by_url["https://a.example.com/1"] == []
    assert by_url["https://a.example.com/2"] == ["AI"]
    assert by_url["https://a.example.com/3"] == []
    assert by_url["https://a.example.com/4"] == ["ON"]
    # Because "AI" does not match inside "said", "AI" is appended to provider query.
    sent_query = glm.calls[0][0]
    assert "AI" in sent_query and "ON" in sent_query


def test_bilingual_event_type_tagging():
    en_cases = [
        ("Apple reports Q4 earnings beat estimates", "EPS exceeded consensus", ["earnings"]),
        ("Nvidia raises full-year revenue guidance", None, ["guidance"]),
        ("Microsoft agrees to buy gaming studio in all-cash acquisition", None, ["m_and_a"]),
        ("SEC fines brokerage firm over compliance failures", None, ["regulatory"]),
        ("Federal Reserve cuts interest rates as inflation cools", None, ["macro"]),
        ("Morgan Stanley upgrades stock and raises price target", None, ["rating_change"]),
        ("Board approves $10B share repurchase and special dividend", None, ["corporate_action"]),
    ]
    zh_cases = [
        ("公司公布2026年三季报，营业收入与净利润双增长", None, ["earnings"]),
        ("管理层上调全年业绩指引并展望需求复苏", None, ["guidance"]),
        ("某科技巨头宣布重大资产重组及并购方案", None, ["m_and_a"]),
        ("证监会就信息披露违规向公司出具警示函并立案调查", None, ["regulatory"]),
        ("美联储宣布降息25个基点，通胀与CPI走势受关注", None, ["macro"]),
        ("券商发布研报上调评级至买入并调高目标价", None, ["rating_change"]),
        ("公司董事会批准股份回购及年度分红派息方案", None, ["corporate_action"]),
    ]
    seen_types: set[str] = set()
    for title, excerpt, expected in en_cases + zh_cases:
        tags = tag_news_events(title, excerpt)
        assert tags == expected, (title, tags)
        seen_types.update(tags)
    assert seen_types == set(EVENT_TYPES)
    assert tag_news_events("Apple Q4 earnings beat estimates and raises full-year guidance") == ["earnings", "guidance"]
    assert tag_news_events("Coastal city enjoys mild sunny weekend weather", "Local festival draws crowds.") == []

    # Cluster aggregates member event tags in canonical order
    glm = Fake("glm", ok([
        lead("https://a.example.com/1", "Acme Q3 earnings beat consensus", "glm", published_at=datetime(2026, 11, 20, tzinfo=UTC)),
        lead("https://b.example.com/2", "Acme Q3 earnings beat consensus", "glm",
             excerpt="Management also raises full-year guidance.", published_at=datetime(2026, 11, 20, tzinfo=UTC)),
    ]))
    res = news_search("Acme", providers=[glm])
    assert res.data[0]["event_types"] == ["earnings", "guidance"]
