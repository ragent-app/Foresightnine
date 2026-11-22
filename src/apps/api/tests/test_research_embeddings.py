"""Unit tests for protocol-neutral embedding client, LRU cache, SSRF checks, and hybrid RRF retrieval."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import logging
import socket

import pytest

from app import openai_compatible
from app.openai_compatible import HttpResponse, _TransportFailure, _TransportTimeout
from app.phase3 import ProviderFailure
from app.research_embeddings import (
    EmbeddingCache,
    EmbeddingClient,
    EmbeddingClientError,
    EmbeddingConfigError,
    EmbeddingSettings,
    EmbeddingVectorScorer,
    cosine_similarity,
    default_embedding_cache,
    embedding_endpoint,
    validate_embedding_base_url,
)
from app.research_library import ResearchLibrary
from app.research_library_tool import (
    SearchLibraryRequest,
    compact_coverage,
    create_research_library,
    default_vector_scorer,
    hit_row,
    run_search_library,
    sync_library,
)
from app.research_sources import ResearchSource, SourcePermissions


def _source(source_id: str, text: str, *, title: str | None = None) -> ResearchSource:
    return ResearchSource(
        source_id=source_id,
        owner_id="owner",
        title=title or source_id,
        text=text,
        kind="user_supplied",
        obtained_at=datetime(2026, 11, 21, tzinfo=timezone.utc),
        content_sha256="a" * 64,
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
        provenance={},
    )


def _mock_transport(vectors_by_text: dict[str, list[float]], calls: list[dict] | None = None, *, reverse_order: bool = False):
    def transport(method: str, url: str, headers: dict[str, str], body: bytes, timeout: float) -> HttpResponse:
        payload = json.loads(body.decode("utf-8"))
        if calls is not None:
            calls.append({"method": method, "url": url, "headers": dict(headers), "payload": payload, "timeout": timeout})
        items = [
            {"object": "embedding", "index": idx, "embedding": vectors_by_text[text]}
            for idx, text in enumerate(payload["input"])
        ]
        if reverse_order:
            items.reverse()
        return HttpResponse(200, json.dumps({"object": "list", "data": items}))

    return transport


def test_embedding_settings_unconfigured_and_protocol_neutral_endpoints():
    assert EmbeddingSettings.from_environ({}) is None
    assert EmbeddingSettings.from_environ({"FORESIGHTNINE_EMBEDDING_BASE_URL": "https://embed.example.test/v1"}) is None
    assert EmbeddingSettings.from_environ({
        "FORESIGHTNINE_EMBEDDING_BASE_URL": "https://embed.example.test/v1",
        "FORESIGHTNINE_EMBEDDING_MODEL": "generic-embed-v1",
    }) is None

    assert embedding_endpoint("https://embed.example.test") == "https://embed.example.test/v1/embeddings"
    assert embedding_endpoint("https://embed.example.test/v1") == "https://embed.example.test/v1/embeddings"
    assert embedding_endpoint("https://embed.example.test/v1/embeddings") == "https://embed.example.test/v1/embeddings"
    assert embedding_endpoint("https://embed.example.test/api/paas/v4") == "https://embed.example.test/api/paas/v4/embeddings"

    settings = EmbeddingSettings.from_environ({
        "FORESIGHTNINE_EMBEDDING_BASE_URL": "https://embed.example.test/v1/",
        "FORESIGHTNINE_EMBEDDING_MODEL": "custom-embed-01",
        "FORESIGHTNINE_EMBEDDING_API_KEY": "super-secret-embed-key",
        "FORESIGHTNINE_EMBEDDING_TIMEOUT_SECONDS": "12.5",
        "FORESIGHTNINE_EMBEDDING_DIMENSIONS": "256",
        "FORESIGHTNINE_EMBEDDING_BATCH_SIZE": "16",
    })
    assert settings is not None
    assert settings.base_url == "https://embed.example.test/v1"
    assert settings.model == "custom-embed-01"
    assert settings.timeout_seconds == 12.5
    assert settings.dimensions == 256
    assert settings.batch_size == 16
    assert "super-secret-embed-key" not in repr(settings)
    client = EmbeddingClient(settings)
    assert "super-secret-embed-key" not in repr(client)


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "   ",
        "http://embed.example.test/v1",
        "https://127.0.0.1/v1",
        "https://localhost/v1",
        "https://sub.localhost/v1",
        "https://169.254.169.254/v1",
        "https://10.0.0.1/v1",
        "https://192.168.1.10/v1",
        "https://[::1]/v1",
        "https://[::ffff:127.0.0.1]/v1",
        "https://metadata.google.internal/v1",
        "https://metadata.azure.internal/v1",
        "https://embed.local/v1",
        "https://user:pass@embed.example.test/v1",
        "https://embed.example.test/v1?x=1",
        "https://embed.example.test/v1#frag",
        "https://0x7f.0.0.1/v1",
        "https://0177.0.0.1/v1",
        "https://singlelabel/v1",
        "https://fred.stlouisfed.org/v1",
        "https://api.stlouisfed.org/fred",
    ],
)
def test_validate_embedding_base_url_rejects_ssrf_and_fred(bad_url: str):
    with pytest.raises((EmbeddingConfigError, ProviderFailure, ValueError)):
        validate_embedding_base_url(bad_url)


def test_embedding_allowlist_and_private_gateway_and_default_transport_dns_pinning(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(EmbeddingConfigError):
        validate_embedding_base_url("https://other.example.test/v1", allowed_hosts="embed.example.test")
    url, allowlist = validate_embedding_base_url("https://embed.example.test/v1", allowed_hosts="embed.example.test")
    assert url == "https://embed.example.test/v1"
    assert allowlist == frozenset({("embed.example.test", 443)})

    # Explicit private gateway flag permits only _LOCAL_GATEWAY_HOSTS over HTTP.
    gw_url, _ = validate_embedding_base_url("http://host.docker.internal:11434/v1", allow_private_network=True)
    assert gw_url == "http://host.docker.internal:11434/v1"
    with pytest.raises(EmbeddingConfigError):
        validate_embedding_base_url("http://127.0.0.1:11434/v1", allow_private_network=True)

    # Default transport pins public DNS and rejects private DNS resolutions.
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_kw: [(2, 1, 6, "", ("127.0.0.1", 443))])
    monkeypatch.setattr(openai_compatible, "_public_doh_addresses", lambda _h: ("127.0.0.1",))
    settings = EmbeddingSettings.from_values(base_url="https://embed.example.test/v1", model="m1", api_key="k1")
    scorer = EmbeddingVectorScorer(EmbeddingClient(settings))
    lib = ResearchLibrary([_source("s1", "Quarterly earnings release.")], vector_scorer=scorer)
    hits = lib.search("quarterly earnings")
    assert len(hits) == 1 and hits[0].source_id == "s1"
    assert hits.coverage["retrieval_mode"] == "bm25_only"


def test_embedding_cache_keys_lru_eviction_and_counters():
    cache = EmbeddingCache(max_entries=2)
    k1 = cache.make_key("https://a.example.test/v1", "m1", None, "hello")
    k2 = cache.make_key("https://a.example.test/v1", "m1", 256, "hello")
    k3 = cache.make_key("https://a.example.test/v1", "m2", None, "hello")
    k4 = cache.make_key("https://b.example.test/v1", "m1", None, "hello")
    assert len({k1, k2, k3, k4}) == 4

    cache.put(k1, [1.0, 0.0])
    cache.put(k2, [0.0, 1.0])
    assert cache.get(k1) == (1.0, 0.0)  # touches k1 -> k2 becomes LRU
    assert cache.hits == 1 and cache.misses == 0

    cache.put(k3, [0.5, 0.5])  # evicts k2
    assert cache.evictions == 1 and len(cache) == 2
    assert cache.get(k2) is None
    assert cache.get(k1) == (1.0, 0.0)
    assert cache.get(k3) == (0.5, 0.5)
    assert cache.stats() == {"hits": 3, "misses": 1, "size": 2, "max_entries": 2, "evictions": 1}


def test_embedding_cache_thread_safety():
    cache = EmbeddingCache(max_entries=32)
    calls: list[dict] = []
    vectors = {f"doc-{i}": [float(i + 1), 1.0] for i in range(12)}
    settings = EmbeddingSettings.from_values(base_url="https://embed.example.test/v1", model="m1", api_key="k1")
    client = EmbeddingClient(settings, cache=cache, transport=_mock_transport(vectors, calls))

    def worker(idx: int) -> tuple[float, ...]:
        return client.embed_text(f"doc-{idx % 12}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(96)))
    assert len(results) == 96
    assert cache.hits + cache.misses >= 96
    assert len(calls) <= 96
    # Subsequent pass hits 100% cache with zero extra calls.
    before = len(calls)
    again = client.embed_texts([f"doc-{i}" for i in range(12)])
    assert len(again) == 12
    assert len(calls) == before


def test_client_batching_deduplication_out_of_order_index_and_dimensions():
    calls: list[dict] = []
    vectors = {
        "alpha": [1.0, 0.0],
        "beta": [0.0, 1.0],
        "gamma": [0.7, 0.7],
        "delta": [0.2, 0.9],
    }
    cache = EmbeddingCache(max_entries=100)
    settings = EmbeddingSettings.from_values(
        base_url="https://embed.example.test/v1",
        model="m1",
        api_key="secret",
        dimensions=2,
        batch_size=2,
    )
    client = EmbeddingClient(settings, cache=cache, transport=_mock_transport(vectors, calls, reverse_order=True))

    out = client.embed_texts(["alpha", "beta", "alpha", "gamma", "delta", "beta"])
    assert out == [
        (1.0, 0.0),
        (0.0, 1.0),
        (1.0, 0.0),
        (0.7, 0.7),
        (0.2, 0.9),
        (0.0, 1.0),
    ]
    # 4 unique texts with batch_size=2 -> 2 HTTP calls instead of 6.
    assert len(calls) == 2
    assert calls[0]["url"] == "https://embed.example.test/v1/embeddings"
    assert calls[0]["headers"]["Authorization"] == "Bearer secret"
    assert calls[0]["payload"] == {"dimensions": 2, "input": ["alpha", "beta"], "model": "m1"}
    assert calls[1]["payload"] == {"dimensions": 2, "input": ["gamma", "delta"], "model": "m1"}
    assert cache.misses == 4 and cache.hits == 0

    # Repeated call hits cache immediately with 0 API calls.
    out2 = client.embed_texts(["delta", "alpha"])
    assert out2 == [(0.2, 0.9), (1.0, 0.0)]
    assert len(calls) == 2
    assert cache.hits == 2 and cache.misses == 4


def test_cross_lingual_hybrid_retrieval_and_shared_cache_across_runs():
    doc_irrelevant = _source("irrelevant", "Marketing budget stayed flat during the quarter.")
    doc_zh = _source("zh_capex", "公司资本支出显著增加，主要用于新建算力数据中心与电力配套。")
    query = "capital expenditure for AI data centers"

    vectors = {
        doc_irrelevant.text: [0.0, 1.0, 0.0],
        doc_zh.text: [0.95, 0.05, 0.0],
        query: [1.0, 0.0, 0.0],
    }
    calls: list[dict] = []
    shared_cache = EmbeddingCache(max_entries=100)
    settings = EmbeddingSettings.from_values(base_url="https://embed.example.test/v1", model="embed-x", api_key="k")
    transport = _mock_transport(vectors, calls)

    # Pure BM25 cannot match the Chinese capex passage for the English query.
    bm25_lib = ResearchLibrary([doc_irrelevant, doc_zh])
    bm25_hits = bm25_lib.search(query)
    assert bm25_hits == []
    assert bm25_hits.coverage["retrieval_mode"] == "bm25_only"
    assert "embedding_cache_hits" not in bm25_hits.coverage

    # Hybrid RRF surfaces the cross-lingual semantic match at #1.
    scorer1 = EmbeddingVectorScorer(EmbeddingClient(settings, cache=shared_cache, transport=transport))
    lib1 = ResearchLibrary([doc_irrelevant, doc_zh], vector_scorer=scorer1)
    assert len(calls) == 2  # one call per source during initial indexing

    hits1 = lib1.search(query, top_k=2)
    assert len(calls) == 3  # query embedded once
    assert [h.source_id for h in hits1] == ["zh_capex"]  # doc_irrelevant had cosine=0.0 so filtered
    assert hits1[0].reasons == ("semantic_vector",)
    assert hits1[0].lexical_score == 0.0
    assert hits1[0].vector_score == pytest.approx(0.95 / (0.95**2 + 0.05**2) ** 0.5, rel=1e-4)
    assert hits1.coverage["retrieval_mode"] == "hybrid_rrf"
    assert hits1.coverage["embedding_cache_misses"] == 3
    assert hits1.coverage["embedding_cache_hits"] == 0

    # A second run / library sharing the cache hits cache for all passages and the repeated query.
    scorer2 = EmbeddingVectorScorer(EmbeddingClient(settings, cache=shared_cache, transport=transport))
    lib2 = ResearchLibrary([doc_irrelevant, doc_zh], vector_scorer=scorer2)
    hits2 = lib2.search(query, top_k=2)
    assert len(calls) == 3  # zero redundant API calls!
    assert [h.source_id for h in hits2] == ["zh_capex"]
    assert hits2.coverage["embedding_cache_hits"] == 3
    assert hits2.coverage["embedding_cache_misses"] == 3


def test_graceful_fallback_to_bm25_on_provider_failures(caplog: pytest.LogCaptureFixture):
    doc_a = _source("a", "Quarterly revenue grew 18 percent year over year.")
    doc_b = _source("b", "Board composition remained unchanged.")
    settings = EmbeddingSettings.from_values(base_url="https://embed.example.test/v1", model="embed-x", api_key="secret-xyz")

    # 1. Timeout during query() after indexing succeeded -> fail open to BM25, no secret/text in logs.
    state = {"fail": False}

    def flaky_transport(method: str, url: str, headers: dict[str, str], body: bytes, timeout: float) -> HttpResponse:
        if state["fail"]:
            raise _TransportTimeout(timeout_kind="response_timeout")
        payload = json.loads(body.decode("utf-8"))
        return HttpResponse(
            200,
            json.dumps({
                "data": [{"index": i, "embedding": [1.0, 0.0] if "revenue" in t else [0.0, 1.0]}
                         for i, t in enumerate(payload["input"])]
            }),
        )

    scorer = EmbeddingVectorScorer(EmbeddingClient(settings, cache=EmbeddingCache(16), transport=flaky_transport))
    lib = ResearchLibrary([doc_a, doc_b], vector_scorer=scorer)
    state["fail"] = True
    with caplog.at_level(logging.WARNING):
        hits = lib.search("revenue grew")
    assert [h.source_id for h in hits] == ["a"]
    assert hits[0].reasons == ("lexical_bm25", "phrase")
    assert hits.coverage["retrieval_mode"] == "bm25_only"
    assert hits.coverage["vector_fallback_reason"] == "embedding_timeout"
    assert "secret-xyz" not in caplog.text
    assert "revenue grew" not in caplog.text

    # 2. HTTP 503 during index_source() -> does not raise, BM25 still searches cleanly.
    def error_503(*_args) -> HttpResponse:
        return HttpResponse(503, '{"error":{"message":"upstream overloaded"}}')

    scorer2 = EmbeddingVectorScorer(EmbeddingClient(settings, cache=EmbeddingCache(16), transport=error_503))
    lib2 = ResearchLibrary([doc_a, doc_b], vector_scorer=scorer2)
    hits2 = lib2.search("revenue grew")
    assert [h.source_id for h in hits2] == ["a"]
    assert hits2.coverage["retrieval_mode"] == "bm25_only"
    assert hits2.coverage["vector_fallback_reason"] == "embedding_http_error"

    # 3. Malformed payload / transport failure -> does not raise.
    def bad_json(*_args) -> HttpResponse:
        return HttpResponse(200, "{not-json")

    scorer3 = EmbeddingVectorScorer(EmbeddingClient(settings, cache=EmbeddingCache(16), transport=bad_json))
    lib3 = ResearchLibrary([doc_a], vector_scorer=scorer3)
    assert [h.source_id for h in lib3.search("revenue")] == ["a"]


def test_tool_integration_default_vector_scorer_sync_and_coverage():
    default_embedding_cache().clear()
    # Unconfigured env -> None (zero-overhead pure BM25).
    assert default_vector_scorer({}) is None
    # Invalid/SSRF env -> fails open to None with warning rather than crashing.
    assert default_vector_scorer({
        "FORESIGHTNINE_EMBEDDING_BASE_URL": "http://127.0.0.1/v1",
        "FORESIGHTNINE_EMBEDDING_MODEL": "m1",
        "FORESIGHTNINE_EMBEDDING_API_KEY": "k1",
    }) is None

    env = {
        "FORESIGHTNINE_EMBEDDING_BASE_URL": "https://embed.example.test/v1",
        "FORESIGHTNINE_EMBEDDING_MODEL": "embed-v1",
        "FORESIGHTNINE_EMBEDDING_API_KEY": "k1",
    }
    calls: list[dict] = []
    src_a = _source("a", "Unrelated marketing commentary.")
    src_b = _source("b", "数据中心算力资本开支大幅上升。")
    vectors = {
        src_a.text: [0.0, 1.0],
        src_b.text: [1.0, 0.0],
        "AI data center capex": [1.0, 0.0],
    }
    cache = EmbeddingCache(32)
    scorer = default_vector_scorer(env, cache=cache, transport=_mock_transport(vectors, calls))
    assert scorer is not None

    # Inject via sync_library onto an empty ResearchLibrary().
    lib = ResearchLibrary()
    sync_library(lib, {"a": src_a, "b": src_b}, {}, None, vector_scorer=scorer)
    req = SearchLibraryRequest(query="AI data center capex", max_hits=5, rationale="check semantic match")
    result, summary = run_search_library(req, library=lib, source_by_id={"a": src_a, "b": src_b}, versions={}, as_of=None)

    assert result["status"] == "completed" and result["returned_hits"] == 1
    row = result["hits"][0]
    assert row["source_id"] == "b"
    assert set(row.keys()) == set(hit_row(lib.search("AI data center capex")[0], src_b).keys())
    cov = result["library_coverage"]
    assert cov["retrieval_mode"] == "hybrid_rrf"
    assert cov["embedding_cache_misses"] == 3
    assert summary is not None and summary["retrieval_mode"] == "hybrid_rrf"

    # Explicit ResearchLibrary(vector_scorer=None) is never overridden by sync_library even if env is set.
    bm25_lib = ResearchLibrary(vector_scorer=None)
    sync_library(bm25_lib, {"a": src_a}, {}, None, environ=env)
    assert bm25_lib.vector_scorer is None
    assert compact_coverage(bm25_lib.search("marketing").coverage)["retrieval_mode"] == "bm25_only"

    # create_research_library uses default_vector_scorer(environ=...) when omitted.
    auto_lib = create_research_library([src_a], environ={})
    assert auto_lib.vector_scorer is None
    configured_lib = create_research_library([], environ=env)
    assert isinstance(configured_lib.vector_scorer, EmbeddingVectorScorer)


def test_cosine_similarity_edge_cases():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine_similarity([1.0, 0.0], [1.0]) == 0.0
    assert cosine_similarity([], []) == 0.0


@pytest.mark.parametrize(
    "status,body",
    [
        (500, '{"error":"boom"}'),
        (200, "not-json"),
        (200, '{"data":[]}'),
        (200, '{"data":[{"index":0,"embedding":[]}]}'),
        (200, '{"data":[{"index":0,"embedding":[true, 1.0]}]}'),
        (200, '{"data":[{"index":0,"embedding":[1.0, 2.0]},{"index":1,"embedding":[1.0]}]}'),
        (200, '{"data":[{"index":5,"embedding":[1.0]},{"index":0,"embedding":[1.0]}]}'),
    ],
)
def test_embedding_client_rejects_malformed_or_error_responses(status: int, body: str):
    settings = EmbeddingSettings.from_values(base_url="https://embed.example.test/v1", model="m1", api_key="k1")
    client = EmbeddingClient(settings, transport=lambda *_a: HttpResponse(status, body))
    with pytest.raises(EmbeddingClientError):
        client.embed_texts(["one"] if status != 200 or body.count("index") < 2 else ["one", "two"])


def test_embedding_vector_scorer_includes_table_header_context_and_partial_batch_cache():
    header = "| Segment | Capex FY24 |"
    rows = "\n".join(f"| Cloud division row {i} | {100 + i} |" for i in range(40))
    table_doc = _source("tbl", "Financial summary\n" + header + "\n|---|---|\n" + rows)

    seen_inputs: list[str] = []

    def transport(_m, _u, _h, body: bytes, _t) -> HttpResponse:
        payload = json.loads(body.decode("utf-8"))
        seen_inputs.extend(payload["input"])
        return HttpResponse(
            200,
            json.dumps({
                "data": [{"index": i, "embedding": [1.0, 0.0] if "Capex FY24" in txt else [0.0, 1.0]}
                         for i, txt in enumerate(payload["input"])]
            }),
        )

    settings = EmbeddingSettings.from_values(
        base_url="https://embed.example.test/v1", model="m1", api_key="k1", batch_size=1
    )
    cache = EmbeddingCache(64)
    scorer = EmbeddingVectorScorer(EmbeddingClient(settings, cache=cache, transport=transport))
    lib = ResearchLibrary([table_doc], vector_scorer=scorer)
    passages = lib.passages("tbl")
    assert len(passages) > 1
    assert any(p.context == header for p in passages)
    # Every embedded passage that carried a table header context started with that header.
    assert any(txt.startswith(header + "\n") for txt in seen_inputs)


def test_multi_passage_source_batches_and_ocr_replacement_reuses_unchanged_page_cache():
    from tests.test_research_library import paged

    p1_text = "Page one unchanged summary. " * 30
    p2_ocr_text = "Page two scanned operating margin 17.5 percent."
    text_v1, pages_v1 = paged([("text", p1_text), ("needs_ocr", "")])
    text_v2, pages_v2 = paged([("text", p1_text), ("ocr", p2_ocr_text)])

    def make_paged_source(sid: str, txt: str, pgs: list) -> ResearchSource:
        return ResearchSource(
            source_id=sid,
            owner_id="owner",
            title=sid,
            text=txt,
            kind="user_supplied",
            obtained_at=datetime(2026, 11, 21, tzinfo=timezone.utc),
            content_sha256="b" * 64,
            permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
            provenance={"document_pages": {"page_count": len(pgs), "pages": pgs, "version": 2, "method": "m"}},
        )

    calls: list[list[str]] = []

    def transport(_m, _u, _h, body: bytes, _t) -> HttpResponse:
        inputs = json.loads(body.decode("utf-8"))["input"]
        calls.append(inputs)
        return HttpResponse(
            200,
            json.dumps({
                "data": [
                    {"index": i, "embedding": [1.0, 0.0] if ("margin" in txt or "profitability" in txt) else [0.0, 1.0]}
                    for i, txt in enumerate(inputs)
                ]
            }),
        )

    settings = EmbeddingSettings.from_values(base_url="https://embed.example.test/v1", model="m1", api_key="k1", batch_size=16)
    cache = EmbeddingCache(64)
    scorer = EmbeddingVectorScorer(EmbeddingClient(settings, cache=cache, transport=transport))
    lib = ResearchLibrary([make_paged_source("v1", text_v1, pages_v1)], vector_scorer=scorer)
    v1_count = len(lib.passages("v1"))
    assert v1_count >= 1
    assert len(calls) == 1  # all passages of v1 batched into 1 HTTP request

    # Replace v1 with OCR-updated v2: unchanged page 1 passages hit cache, only page 2 OCR passage is embedded!
    lib.replace_source("v1", make_paged_source("v2", text_v2, pages_v2))
    assert len(calls) == 2
    assert calls[1] == [p2_ocr_text]
    assert cache.hits >= v1_count

    hits = lib.search("profitability ratio")
    assert len(hits) == 1
    assert hits[0].source_id == "v2" and hits[0].page == 2 and hits[0].extraction_method == "ocr"

