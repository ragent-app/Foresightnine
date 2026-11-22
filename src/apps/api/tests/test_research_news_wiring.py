from __future__ import annotations

from datetime import date, datetime, timezone
import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as api
from app.fin_data.models import Cost, Coverage, Permissions, Provenance, ToolResult
from app.model_protocol import ModelResponse
from app.news.gdelt import GDELT_ATTRIBUTION, GdeltClient, NewsLead
from app.news.providers import ManagedNewsProvider
from app.news.search import GdeltNewsProvider
from app.openai_compatible import BinaryHttpResponse, HttpResponse
from app.platform_secrets import PlatformSecretStore, configure as configure_platform_secrets, search_secret_name
from app.research_news_tool import (
    DiscoverCallableProvider,
    NEWS_CACHE_DISABLED_ENV,
    NewsSearchCache,
    NewsSearchRequest,
    default_news_providers,
    news_search_cache_key,
    reset_news_search_cache,
    run_news_search,
)
from tests.test_research_runtime import turn


def test_default_news_providers_decouples_gdelt_and_wires_all_managed_providers() -> None:
    assert default_news_providers(None, environ={"FORESIGHTNINE_GDELT_ENABLED": "0"}) == []

    gdelt_only = default_news_providers(None, environ={"FORESIGHTNINE_GDELT_ENABLED": "1"})
    assert [p.name for p in gdelt_only] == ["gdelt"]
    assert isinstance(gdelt_only[0], GdeltNewsProvider)

    expected_ids = ("brave", "tavily", "exa", "parallel", "qianfan", "bocha")
    env = {"FORESIGHTNINE_GDELT_ENABLED": "0", **{search_secret_name(pid): f"key-{pid}" for pid in expected_ids}}
    managed = default_news_providers(None, environ=env)
    assert [p.name for p in managed] == list(expected_ids)
    assert all(isinstance(p, ManagedNewsProvider) for p in managed)

    with_discover = default_news_providers(lambda _q: [], environ={"FORESIGHTNINE_GDELT_ENABLED": "1", search_secret_name("tavily"): "t-key"})
    assert [p.name for p in with_discover] == ["glm-coding-mcp-search", "tavily", "gdelt"]
    assert isinstance(with_discover[0], DiscoverCallableProvider)


def test_default_news_providers_reads_platform_secret_store_and_tolerates_store_failure(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_GDELT_ENABLED", "0")
    monkeypatch.delenv(search_secret_name("brave"), raising=False)
    monkeypatch.setenv(search_secret_name("tavily"), "env-tavily-key")

    class FakeRepo:
        def __init__(self, records=None, fail=False):
            self._records = dict(records or {})
            self._fail = fail

        def get(self, name):
            if self._fail:
                raise RuntimeError("db unavailable")
            return self._records.get(name)

        def put(self, name, value):
            self._records[name] = value

        def delete(self, name):
            self._records.pop(name, None)

    from app.market_credentials import MarketCredentialCipher

    cipher = MarketCredentialCipher(b"k" * 32)
    store = PlatformSecretStore(FakeRepo(), {}, cipher)
    store.set(search_secret_name("brave"), "db-brave-key", actor="admin")
    configure_platform_secrets(store)
    try:
        providers = default_news_providers(None)
        assert [(p.name, p.api_key) for p in providers] == [
            ("brave", "db-brave-key"),
            ("tavily", "env-tavily-key"),
        ]

        broken_store = PlatformSecretStore(FakeRepo(fail=True), {}, None)
        configure_platform_secrets(broken_store)
        fallback = default_news_providers(None)
        assert [(p.name, p.api_key) for p in fallback] == [("tavily", "env-tavily-key")]
    finally:
        configure_platform_secrets(api.platform_secret_store)


def test_managed_news_provider_executes_via_run_news_search() -> None:
    def fake_transport(method, url, headers, body, timeout):
        assert method == "GET"
        assert "api.search.brave.com" in url
        assert headers["X-Subscription-Token"] == "brave-test-key"
        return HttpResponse(200, json.dumps({
            "web": {"results": [
                {"title": "Managed Brave Lead", "url": "https://example.org/brave-news", "description": "Summary", "page_age": "2026-11-20T12:00:00Z"},
            ]}
        }))

    providers = default_news_providers(
        None,
        environ={"FORESIGHTNINE_GDELT_ENABLED": "0", search_secret_name("brave"): "brave-test-key"},
        managed_transport=fake_transport,
    )
    outcome = run_news_search(NewsSearchRequest(query="brave news", rationale="check"), providers=providers)
    assert outcome.result["status"] == "completed"
    assert [lead["url"] for lead in outcome.result["leads"]] == ["https://example.org/brave-news"]
    assert outcome.result["leads"][0]["providers"] == ["brave"]


def test_main_wires_keyless_gdelt_and_managed_providers_without_glm(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.setattr(api, "evidence_provider", SimpleNamespace(supports_project_search=False))
    monkeypatch.setattr(api, "_reserve_search_call", lambda *_: None)

    seen_caps: list[dict] = []

    class Provider:
        name = "keyless-search-wiring-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)

        def complete(self, messages, tools, **kwargs):
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            if not results:
                payload = json.loads(messages[1]["content"])
                seen_caps.append(payload["available_capabilities"])
                return turn("news_search", {"query": "central bank policy", "rationale": "Find news leads."}, "n1")
            assert results[0]["status"] == "completed"
            assert results[0]["leads"]
            return turn("submit_research", {
                "report": "Found news leads without GLM.",
                "claims": [],
                "limitations": ["Leads were not acquired."],
                "stopping_reason": "leads_only",
                "status": "partial",
            }, "s1")

    monkeypatch.setattr(api, "challenge_provider", Provider())

    # 1. Keyless GDELT enabled with GLM discover unconfigured.
    monkeypatch.setenv("FORESIGHTNINE_GDELT_ENABLED", "1")
    monkeypatch.delenv(search_secret_name("brave"), raising=False)

    def gdelt_transport(method, url, headers, body, timeout, **kwargs):
        return BinaryHttpResponse(200, json.dumps({"articles": [{
            "url": "https://www.reuters.com/world/central-bank",
            "title": "Central bank policy update",
            "seendate": "20261120T100000Z",
            "domain": "reuters.com",
            "language": "English",
            "sourcecountry": "United States",
        }]}).encode())

    monkeypatch.setattr(
        "app.news.gdelt._default_transport_ref",
        lambda: gdelt_transport,
    )
    orig_gdelt_init = GdeltClient.__init__
    monkeypatch.setattr(
        GdeltClient,
        "__init__",
        lambda self, **kw: orig_gdelt_init(self, transport=gdelt_transport, min_interval_seconds=0, **kw),
    )

    client = TestClient(api.app)
    auth = client.post("/v1/auth/local/register", json={"email": f"{uuid4().hex}@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}

    caps_resp = client.get("/v2/research/capabilities", headers=headers)
    assert caps_resp.status_code == 200 and caps_resp.json()["search_available"] is True

    topic = client.post("/v2/research/topics", headers=headers, json={"question": "What happened with central bank policy?"}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    run1 = client.post(path + "/runs", headers={**headers, "Idempotency-Key": uuid4().hex}, json={"parent_version_id": topic["versions"][0]["version_id"]})
    assert run1.status_code == 202, run1.text
    op1 = run1.json()["operation"]["operation_id"]
    for _ in range(200):
        st = client.get(f"{path}/runs/{op1}", headers=headers).json()["operation"]
        if st["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.01)
    assert st["status"] == "completed", st
    assert seen_caps[-1]["news_search_available"] is True
    assert seen_caps[-1]["news_search_providers"] == ["gdelt"]
    v1 = client.get(path, headers=headers).json()["versions"][-1]
    assert v1["content"]["turns"][-1]["attributions"][0]["text"] == GDELT_ATTRIBUTION
    assert v1["content"]["turns"][-1]["search_status"] == "completed"

    # 2. Managed provider (Brave) with GDELT disabled and GLM discover unconfigured.
    monkeypatch.setenv("FORESIGHTNINE_GDELT_ENABLED", "0")
    monkeypatch.setenv(search_secret_name("brave"), "brave-run-key")
    monkeypatch.setattr(
        "app.news.providers._default_transport",
        lambda method, url, hdrs, body, timeout: HttpResponse(200, json.dumps({
            "web": {"results": [{"title": "Brave Result", "url": "https://example.com/brave-run", "description": "Desc"}]}
        })),
    )

    run2 = client.post(path + "/runs", headers={**headers, "Idempotency-Key": uuid4().hex}, json={
        "parent_version_id": v1["version_id"],
        "question_override": "Check managed search provider",
    })
    assert run2.status_code == 202, run2.text
    op2 = run2.json()["operation"]["operation_id"]
    for _ in range(200):
        st2 = client.get(f"{path}/runs/{op2}", headers=headers).json()["operation"]
        if st2["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.01)
    assert st2["status"] == "completed", st2
    assert seen_caps[-1]["news_search_available"] is True
    assert seen_caps[-1]["news_search_providers"] == ["brave"]

    # 3. Explicit discover_sources=False still suppresses all news_providers.
    class DisabledCheckProvider:
        name = "disabled-search-check"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)

        def complete(self, messages, tools, **kwargs):
            payload = json.loads(messages[1]["content"])
            seen_caps.append(payload["available_capabilities"])
            return ModelResponse({"role": "assistant", "content": "done"}, (), None, "stop")

    monkeypatch.setattr(api, "challenge_provider", DisabledCheckProvider())
    v2 = client.get(path, headers=headers).json()["versions"][-1]
    run3 = client.post(path + "/runs", headers={**headers, "Idempotency-Key": uuid4().hex}, json={
        "parent_version_id": v2["version_id"],
        "question_override": "Do not search",
        "discover_sources": False,
    })
    assert run3.status_code == 202, run3.text
    op3 = run3.json()["operation"]["operation_id"]
    for _ in range(200):
        st3 = client.get(f"{path}/runs/{op3}", headers=headers).json()["operation"]
        if st3["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.01)
    assert st3["status"] == "completed", st3
    assert seen_caps[-1]["news_search_available"] is False
    assert "news_search_providers" not in seen_caps[-1]


def test_run_news_search_sort_by_and_bilingual_event_tags():
    utc = timezone.utc
    req_default = NewsSearchRequest(query="NVDA earnings", rationale="check")
    assert req_default.sort_by == "relevance"
    with pytest.raises(ValueError):
        NewsSearchRequest(query="NVDA", sort_by="invalid", rationale="check")  # type: ignore[arg-type]

    class FakeProvider:
        name = "fake"
        supports_time_filter = False

        def search(self, query, *, since=None, until=None, languages=None, limit=10):
            return ToolResult(
                status="ok",
                reason="ok",
                cost=Cost(provider_calls=1),
                data=[
                    NewsLead(
                        url="https://finance.yahoo.com/news/wrap",
                        title="Daily market roundup",
                        excerpt="Stocks mixed today.",
                        published_at=datetime(2026, 11, 20, 12, 0, tzinfo=utc),
                        providers=["fake"],
                    ),
                    NewsLead(
                        url="https://www.sec.gov/Archives/nvda-10q",
                        title="NVDA Form 10-Q quarterly earnings and guidance",
                        excerpt="NVDA raises full-year outlook.",
                        published_at=datetime(2026, 11, 19, 12, 0, tzinfo=utc),
                        providers=["fake"],
                    ),
                    NewsLead(
                        url="https://news.example.cn/pboc-cut",
                        title="央行宣布降息并发布监管通知",
                        excerpt="货币政策委员会召开会议。",
                        published_at=datetime(2026, 11, 18, 12, 0, tzinfo=utc),
                        providers=["fake"],
                    ),
                ],
            )

    rel = run_news_search(
        req_default,
        providers=[FakeProvider()],
        entities=["NVDA"],
        as_of=datetime(2026, 11, 20, 18, 0, tzinfo=utc),
        use_cache=False,
    )
    assert rel.cache_hit is False and rel.result["cache_hit"] is False
    assert rel.result["leads"][0]["url"] == "https://www.sec.gov/Archives/nvda-10q"
    assert rel.result["leads"][0]["event_types"] == ["earnings", "guidance"]
    assert rel.result["leads"][0]["matched_entities"] == ["NVDA"]
    assert rel.result["clusters"][0]["event_types"] == ["earnings", "guidance"]
    zh_lead = next(l for l in rel.result["leads"] if "pboc-cut" in l["url"])
    assert zh_lead["event_types"] == ["regulatory", "macro"]

    rec = run_news_search(
        NewsSearchRequest(query="NVDA earnings", sort_by="recency", rationale="newest"),
        providers=[FakeProvider()],
        as_of=datetime(2026, 11, 20, 18, 0, tzinfo=utc),
        use_cache=False,
    )
    assert [l["url"] for l in rec.result["leads"]] == [
        "https://finance.yahoo.com/news/wrap",
        "https://www.sec.gov/Archives/nvda-10q",
        "https://news.example.cn/pboc-cut",
    ]


def test_news_search_cache_hit_miss_ttl_lru_and_isolation(monkeypatch):
    utc = timezone.utc
    now_box = [100.0]
    cache = NewsSearchCache(maxsize=2, ttl_seconds=30.0, clock=lambda: now_box[0])
    calls = []

    class CountingProvider:
        def __init__(self, name="fake"):
            self.name = name
            self.supports_time_filter = False
            self.resets = 0

        def reset(self):
            self.resets += 1

        def search(self, query, *, since=None, until=None, languages=None, limit=10):
            calls.append((self.name, query, since, until, tuple(languages or ()), limit))
            return ToolResult(
                status="ok",
                reason="ok",
                cost=Cost(provider_calls=1),
                data=[NewsLead(url="https://example.com/1", title="Apple earnings beat", providers=[self.name])],
            )

    provider = CountingProvider()
    req1 = NewsSearchRequest(query="  Apple   earnings ", languages=["zh", "en"], limit=10, rationale="first")
    req2 = NewsSearchRequest(query="Apple earnings", languages=["en", "zh"], limit=10, rationale="retry different rationale")

    # Canonical key normalizes whitespace and sorts entities/languages.
    k1 = news_search_cache_key(query=req1.query, entities=["NVDA", "AI"], languages=req1.languages, provider_names=["fake"])
    k2 = news_search_cache_key(query=req2.query, entities=["AI", "NVDA"], languages=req2.languages, provider_names=["fake"])
    assert k1 == k2

    out1 = run_news_search(req1, providers=[provider], entities=["NVDA", "AI"], as_of=datetime(2026, 11, 20, 9, 0, tzinfo=utc), cache=cache)
    assert out1.cache_hit is False and out1.result["cache_hit"] is False
    assert len(calls) == 1 and provider.resets == 1

    # Mutate returned output to verify cached state cannot be corrupted.
    out1.result["leads"].append({"url": "https://corrupted.example.com"})
    out1.lead_rows.clear()

    # Second call with canonically equivalent args hits cache without invoking provider.
    out2 = run_news_search(req2, providers=[provider], entities=["AI", "NVDA"], as_of=datetime(2026, 11, 20, 18, 0, tzinfo=utc), cache=cache)
    assert out2.cache_hit is True and out2.result["cache_hit"] is True
    assert len(calls) == 1 and provider.resets == 1
    assert [l["url"] for l in out2.result["leads"]] == ["https://example.com/1"]
    assert len(out2.lead_rows) == 1

    # Different sort_by / provider_names / limit misses cache.
    out_rec = run_news_search(
        NewsSearchRequest(query="Apple earnings", languages=["en", "zh"], sort_by="recency", rationale="recency"),
        providers=[provider],
        entities=["AI", "NVDA"],
        as_of=datetime(2026, 11, 20, 18, 0, tzinfo=utc),
        cache=cache,
    )
    assert out_rec.cache_hit is False and len(calls) == 2

    # Refresh req1 (k_req1) in LRU, then insert 3rd key -> evicts req_rec (LRU).
    out2_again = run_news_search(req2, providers=[provider], entities=["AI", "NVDA"], as_of=datetime(2026, 11, 20, 18, 0, tzinfo=utc), cache=cache)
    assert out2_again.cache_hit is True and len(calls) == 2

    out_other = run_news_search(
        NewsSearchRequest(query="Other query", rationale="other"),
        providers=[provider],
        cache=cache,
    )
    assert out_other.cache_hit is False and len(calls) == 3 and len(cache) == 2

    # req1 is still cached; out_rec was evicted so it misses.
    assert run_news_search(req2, providers=[provider], entities=["AI", "NVDA"], as_of=datetime(2026, 11, 20, 18, 0, tzinfo=utc), cache=cache).cache_hit is True
    assert run_news_search(
        NewsSearchRequest(query="Apple earnings", languages=["en", "zh"], sort_by="recency", rationale="recency"),
        providers=[provider],
        entities=["AI", "NVDA"],
        as_of=datetime(2026, 11, 20, 18, 0, tzinfo=utc),
        cache=cache,
    ).cache_hit is False
    assert len(calls) == 4

    # TTL expiration re-queries provider cleanly.
    now_box[0] += 31.0
    out_expired = run_news_search(
        NewsSearchRequest(query="Apple earnings", languages=["en", "zh"], sort_by="recency", rationale="expired"),
        providers=[provider],
        entities=["AI", "NVDA"],
        as_of=datetime(2026, 11, 20, 18, 0, tzinfo=utc),
        cache=cache,
    )
    assert out_expired.cache_hit is False and len(calls) == 5

    # Bypass via use_cache=False and env var, plus default cache reset.
    assert run_news_search(
        NewsSearchRequest(query="Apple earnings", languages=["en", "zh"], sort_by="recency", rationale="bypass"),
        providers=[provider],
        entities=["AI", "NVDA"],
        as_of=datetime(2026, 11, 20, 18, 0, tzinfo=utc),
        cache=cache,
        use_cache=False,
    ).cache_hit is False
    assert len(calls) == 6

    monkeypatch.setenv(NEWS_CACHE_DISABLED_ENV, "1")
    assert run_news_search(
        NewsSearchRequest(query="Apple earnings", languages=["en", "zh"], sort_by="recency", rationale="env-bypass"),
        providers=[provider],
        entities=["AI", "NVDA"],
        as_of=datetime(2026, 11, 20, 18, 0, tzinfo=utc),
        cache=cache,
    ).cache_hit is False
    assert len(calls) == 7
    monkeypatch.delenv(NEWS_CACHE_DISABLED_ENV)

    # Default cache can be populated and reset cleanly.
    reset_news_search_cache()
    d1 = run_news_search(NewsSearchRequest(query="Default cache query", rationale="1"), providers=[provider])
    d2 = run_news_search(NewsSearchRequest(query="Default cache query", rationale="2"), providers=[provider])
    assert d1.cache_hit is False and d2.cache_hit is True and len(calls) == 8
    reset_news_search_cache()
    d3 = run_news_search(NewsSearchRequest(query="Default cache query", rationale="3"), providers=[provider])
    assert d3.cache_hit is False and len(calls) == 9


def test_news_search_cache_never_caches_failures_or_partial_and_is_thread_safe():
    class FlakyProvider:
        name = "flaky"
        supports_time_filter = False

        def __init__(self):
            self.count = 0

        def search(self, query, *, since=None, until=None, languages=None, limit=10):
            self.count += 1
            if self.count == 1:
                return ToolResult.failure("rate_limited", "search_rate_limited", "slow down", retryable=True)
            return ToolResult(status="ok", reason="ok", cost=Cost(provider_calls=1),
                              data=[NewsLead(url="https://example.com/ok", title="Recovered lead", providers=["flaky"])])

    cache = NewsSearchCache(maxsize=16, ttl_seconds=60.0)
    flaky = FlakyProvider()
    r1 = run_news_search(NewsSearchRequest(query="retryable query", rationale="1"), providers=[flaky], cache=cache)
    assert r1.result["status"] == "rate_limited" and r1.cache_hit is False
    r2 = run_news_search(NewsSearchRequest(query="retryable query", rationale="2"), providers=[flaky], cache=cache)
    assert r2.result["status"] == "completed" and r2.cache_hit is False and flaky.count == 2
    r3 = run_news_search(NewsSearchRequest(query="retryable query", rationale="3"), providers=[flaky], cache=cache)
    assert r3.cache_hit is True and flaky.count == 2

    # Partial result (one provider ok, one failed) is also not cached.
    bad = SimpleNamespace(
        name="bad",
        supports_time_filter=False,
        search=lambda *a, **k: ToolResult.failure("failed", "boom"),
    )
    good_calls = [0]

    def good_search(*a, **k):
        good_calls[0] += 1
        return ToolResult(status="ok", reason="ok", cost=Cost(provider_calls=1),
                          data=[NewsLead(url="https://example.com/p", title="Partial lead", providers=["good"])])

    good = SimpleNamespace(name="good", supports_time_filter=False, search=good_search)
    p1 = run_news_search(NewsSearchRequest(query="partial query", rationale="1"), providers=[good, bad], cache=cache)
    p2 = run_news_search(NewsSearchRequest(query="partial query", rationale="2"), providers=[good, bad], cache=cache)
    assert p1.result["providers_partial"] is True and p1.cache_hit is False
    assert p2.cache_hit is False and good_calls[0] == 2

    # Concurrent threads reading/writing NewsSearchCache do not raise or lose consistency.
    concurrent_cache = NewsSearchCache(maxsize=8, ttl_seconds=60.0)
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            for step in range(25):
                q = f"q-{(idx + step) % 10}"
                res = run_news_search(
                    NewsSearchRequest(query=q, rationale="thread"),
                    providers=[SimpleNamespace(
                        name="thr",
                        supports_time_filter=False,
                        search=lambda qq, **kw: ToolResult(
                            status="ok", reason="ok", cost=Cost(provider_calls=1),
                            data=[NewsLead(url=f"https://example.com/{qq}", title=qq, providers=["thr"])],
                        ),
                    )],
                    cache=concurrent_cache,
                )
                assert res.result["status"] == "completed"
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(concurrent_cache) <= 8
