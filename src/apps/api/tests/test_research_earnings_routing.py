"""Unit and FastAPI integration tests for default-entry US earnings auto-routing."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

import app.main as api
from app.fin_data.adapters.sec_edgar import SecEdgarAdapter
from app.fin_data.entity import EntityCandidate, resolve_entity
from app.fin_data.models import EntityRef, ToolResult
from app.openai_compatible import BinaryHttpResponse
from app.research_earnings_routing import derive_sources_mode, detect_us_earnings_squad_route
from app.research_earnings_squad import (
    BearCaseBatch,
    BearCaseItem,
    CallableSlotModelRunner,
    EarningsSquadOrchestrator,
    EvidenceCard,
    EvidenceCardBatch,
    EvidenceCitation,
    PrefetchDependencies,
    ResearchDispatchPlan,
    SlotModelRequest,
    SlotModelResponse,
    Stage25ReviewDecision,
    WorkerTask,
)
from app.research_runs import ResearchRunRequest
from tests.test_a2_squad_api_and_export import _build_rich_dossier, _parse_sse_frames
from tests.test_research_runtime import turn as scripted_turn


@pytest.fixture(autouse=True)
def _clean_routing_api_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    api.reset_state()
    api.set_earnings_squad_runner_hook(None)
    yield
    api.set_earnings_squad_runner_hook(None)
    api.reset_state()


def _register(client: TestClient) -> dict[str, str]:
    resp = client.post(
        "/v1/auth/local/register",
        json={
            "email": f"route-{uuid4().hex[:10]}@example.test",
            "password": "correct horse battery staple!",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _make_sec_adapter_from_tickers(records: list[dict[str, Any]]) -> SecEdgarAdapter:
    payload = {str(idx): row for idx, row in enumerate(records)}
    body = json.dumps(payload).encode("utf-8")

    def transport(method: str, url: str, headers: dict[str, str], req_body: bytes, timeout: float):
        if "company_tickers.json" in url:
            return BinaryHttpResponse(200, body, {"content-type": "application/json"})
        return BinaryHttpResponse(404, b"{}", {"content-type": "application/json"})

    return SecEdgarAdapter(user_agent="ForesightNineTest contact@example.test", transport=transport)


def _install_nvda_aapl_squad_hook(*, ticker: str, cik: str, block_event: threading.Event | None = None):
    seed = _build_rich_dossier(restrict_byok_export=False)
    snap = seed.snapshot.model_copy(
        update={
            "entity_id": f"cik:{cik}",
            "symbol": ticker,
            "cik": cik,
            "company_name": f"{ticker} Test Corp",
        }
    )
    export_sid = snap.prefetched_sources[0].source_id

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        if block_event is not None and mreq.role == "worker_forensics":
            block_event.wait(timeout=3.0)
        if mreq.role == "lead_coordinator" and mreq.stage == "stage_1_plan":
            return SlotModelResponse(
                parsed=ResearchDispatchPlan(
                    selected_l2_pack_id="kpi_semis_hardware",
                    tasks=[
                        WorkerTask(worker_role="worker_segments", objective="Extract segment revenue."),
                        WorkerTask(worker_role="worker_forensics", objective="Inspect earnings quality."),
                        WorkerTask(worker_role="worker_guidance", objective="Compare guidance."),
                    ],
                )
            )
        if mreq.role in ("worker_segments", "worker_forensics", "worker_guidance"):
            return SlotModelResponse(
                parsed=EvidenceCardBatch(
                    task_id=f"task_{mreq.role}",
                    worker_role=mreq.role,
                    cards=[
                        EvidenceCard(
                            card_id=f"card_{mreq.role}_1",
                            worker_role=mreq.role,
                            topic_tag=mreq.role,
                            finding=f"{ticker} verified finding from {mreq.role}.",
                            supporting_fact_ids=["f:rev_q2"],
                            citations=[
                                EvidenceCitation(
                                    source_id=export_sid,
                                    quotation="ACME Q2 FY26 Cloud revenue reached $720M, up 24% YoY",
                                )
                            ],
                        )
                    ],
                )
            )
        if mreq.role == "lead_coordinator" and mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.role == "bear_reviewer":
            anchor = snap.flags[0].flag_id if snap.flags else "card_worker_forensics_1"
            return SlotModelResponse(
                parsed=BearCaseBatch(
                    items=[
                        BearCaseItem(
                            bear_id="bear_route_1",
                            thesis_challenge=f"{ticker} gross margin compression risk on customer concentration.",
                            anchored_evidence_ids=[anchor],
                            anchor_kind="flag",
                            next_quarter_invalidation_trigger=f"{ticker} gross margin expands > 50bps YoY.",
                        )
                    ]
                )
            )
        return SlotModelResponse(content=f"# Buy-Side Memo: {ticker}\n- Auto-routed Scheme 2 dossier.")

    def hook(org_sink=None, checkpoint_store=None, **_kwargs):
        return {
            "orchestrator": EarningsSquadOrchestrator(
                model_runner=CallableSlotModelRunner(model_fn),
                prefetch_deps=PrefetchDependencies(
                    precomputed_snapshot=snap,
                    initial_source_texts={
                        export_sid: "ACME Q2 FY26 Cloud revenue reached $720M, up 24% YoY; total revenue $1,000M."
                    },
                    initial_source_permissions={
                        export_sid: {"store": True, "display": True, "model_input": True, "export": True}
                    },
                ),
                checkpoint_store=checkpoint_store,
                org_sinks=[org_sink] if org_sink else [],
            )
        }

    api.set_earnings_squad_runner_hook(hook)


# --------------------------------------------------------------------------- 1. Intent & Entity Routing Unit Tests
@pytest.mark.parametrize(
    "question,expected_ticker,expected_cik",
    [
        ("分析NVIDIA距今最新的财报", "NVDA", "0001045810"),
        ("分析英伟达最新季报", "NVDA", "0001045810"),
        ("英伟达 (NVDA) 业绩解读与电话会拆解", "NVDA", "0001045810"),
        ("分析苹果最新年报与业绩指引", "AAPL", "0000320193"),
        ("微软 10-Q 财报深度拆解", "MSFT", "0000789019"),
        ("Analyze NVIDIA latest earnings", "NVDA", "0001045810"),
        ("Review Apple Q4 quarterly results and 10-K", "AAPL", "0000320193"),
        ("Deep dive Microsoft annual results and 8-K EX-99.1", "MSFT", "0000789019"),
        ("AAPL Q2 FY26 earnings quality", "AAPL", "0000320193"),
        ("Analyze CIK 0001045810 10-K filing", "NVDA", "0001045810"),
    ],
)
def test_single_company_us_earnings_routes_to_squad(question: str, expected_ticker: str, expected_cik: str):
    decision = detect_us_earnings_squad_route(question)
    assert decision.should_route is True, decision
    assert decision.run_mode == "earnings_squad"
    assert decision.ticker == expected_ticker
    assert decision.cik == expected_cik
    assert decision.entity_id == f"cik:{expected_cik}"
    assert decision.reason == "matched_us_single_company_earnings"


def test_resolve_entity_integration_supports_arbitrary_and_rejects_ambiguous_or_non_us():
    sec = _make_sec_adapter_from_tickers([
        {"cik_str": 1321655, "ticker": "PLTR", "title": "Palantir Technologies Inc."},
        {"cik_str": 1086222, "ticker": "FDX", "title": "FEDEX CORP"},
        {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        {"cik_str": 1418121, "ticker": "APLE", "title": "Apple Hospitality REIT, Inc."},
        {"cik_str": 999001, "ticker": "ALPH", "title": "Alpha Energy Corp"},
        {"cik_str": 999002, "ticker": "ALPT", "title": "Alpha Tech Holdings"},
    ])
    resolver = lambda query: resolve_entity(query, market="US", asset_type="equity", sec=sec)

    # Arbitrary company not in built-in shortcut table resolves via SEC resolve_entity
    pltr = detect_us_earnings_squad_route("Analyze Palantir latest quarterly results", entity_resolver=resolver)
    assert pltr.should_route is True
    assert pltr.ticker == "PLTR"
    assert pltr.cik == "0001321655"

    # FedEx must not be false-excluded by macro "fed" pattern
    fdx = detect_us_earnings_squad_route("Analyze FedEx Q4 earnings", entity_resolver=lambda q: resolve_entity("FDX" if "fedex" in q.lower() else q, market="US", asset_type="equity", sec=sec))
    assert fdx.should_route is True
    assert fdx.ticker == "FDX"

    # Ambiguous entity resolution must refuse auto-routing
    ambiguous = detect_us_earnings_squad_route("Analyze Alpha Q4 earnings", entity_resolver=resolver)
    assert ambiguous.should_route is False
    assert ambiguous.run_mode == "legacy_single_loop"
    assert ambiguous.reason == "ambiguous_entity"

    # Non-US exchange candidate must be rejected even with earnings terms
    hk_resolver = lambda _q: ToolResult(
        status="ok",
        reason="ok",
        data=[
            EntityCandidate(
                entity=EntityRef(
                    entity_id="HKEX:0700",
                    symbol="0700",
                    exchange="HKEX",
                    asset_type="equity",
                    identifiers={"cik": "0001234567"},
                ),
                match_basis="ticker_exact",
                unique=True,
            )
        ],
    )
    non_us = detect_us_earnings_squad_route("Analyze Tencent Q4 earnings", entity_resolver=hk_resolver)
    assert non_us.should_route is False
    assert non_us.reason == "non_us_or_missing_cik"

    # Invalid / short / zero CIK must be rejected
    bad_cik_resolver = lambda _q: [
        EntityCandidate(
            entity=EntityRef(
                entity_id="NASDAQ:ACME",
                symbol="ACME",
                exchange="NASDAQ",
                asset_type="equity",
                identifiers={"cik": "0000000000"},
            ),
            match_basis="ticker_exact",
            unique=True,
        )
    ]
    bad_cik = detect_us_earnings_squad_route("Analyze ACME 10-K earnings", entity_resolver=bad_cik_resolver)
    assert bad_cik.should_route is False
    assert bad_cik.reason == "non_us_or_missing_cik"


@pytest.mark.parametrize(
    "question,expected_reason",
    [
        # Multi-company comparisons
        ("NVIDIA vs AMD 财报对比", "comparison_excluded"),
        ("MSFT versus GOOGL Q4 earnings", "comparison_excluded"),
        ("Compare Apple and Microsoft annual results", "comparison_excluded"),
        ("英伟达和同行比较业绩", "comparison_excluded"),
        ("Apple peers earnings comparison", "comparison_excluded"),
        ("分析苹果和微软最新财报", "multiple_companies"),
        ("Analyze NVDA and AMD Q4 10-Q", "multiple_companies"),
        # Macro / CPI / Fed / GDP
        ("最新美国 CPI 数据与美联储利率决议对美股业绩影响", "macro_excluded"),
        ("Fed FOMC rate decision and GDP inflation report", "macro_excluded"),
        ("Q2 nonfarm payrolls and PCE earnings impact", "macro_excluded"),
        # IPO-day queries
        ("某科技公司 IPO 上市首日表现与招股书", "ipo_excluded"),
        ("Analyze ARM IPO first day of trading and S-1", "ipo_excluded"),
        # Non-US markers (A-share / HK / 6-digit CN codes / suffixes)
        ("分析 A股 600519 贵州茅台最新年报", "non_us_market_excluded"),
        ("分析 600519.SS 最新财报", "non_us_market_excluded"),
        ("000001.SZ 季报业绩解读", "non_us_market_excluded"),
        ("分析 300750 宁德时代最新业绩", "non_us_market_excluded"),
        ("分析 港股 0700.HK 腾讯最新季报", "non_us_market_excluded"),
        ("9988.HK 财报分析", "non_us_market_excluded"),
        ("HKEX:0700 quarterly results", "non_us_market_excluded"),
        ("分析中芯国际 688981.SH 年报", "non_us_market_excluded"),
        # Missing earnings intent or missing company
        ("NVDA FY25 revenue?", "no_earnings_intent"),
        ("What did 10-K say?", "no_company_detected"),
        ("analyze earnings now", "no_company_detected"),
        ("Analyze Q2 FY26 ACME earnings quality", "entity_resolution_unavailable"),
    ],
)
def test_exclusions_keep_legacy_single_loop(question: str, expected_reason: str):
    decision = detect_us_earnings_squad_route(question)
    assert decision.should_route is False, decision
    assert decision.run_mode == "legacy_single_loop"
    assert decision.reason == expected_reason


def test_opt_outs_and_sources_mode_are_respected():
    q = "分析NVIDIA距今最新的财报"

    d_disabled = detect_us_earnings_squad_route(q, allow_auto_squad=False)
    assert d_disabled.should_route is False
    assert d_disabled.reason == "auto_squad_disabled"

    for mode in ("empty", "private_only", "private_upload_only", "local_only", "disabled"):
        d_mode = detect_us_earnings_squad_route(q, sources_mode=mode)
        assert d_mode.should_route is False
        assert d_mode.reason == "external_retrieval_disabled"

    d_empty_sel = detect_us_earnings_squad_route(q, selected_source_ids=[])
    assert d_empty_sel.should_route is False
    assert d_empty_sel.reason == "explicit_empty_sources"

    d_no_official = detect_us_earnings_squad_route(q, auto_fetch_official=False)
    assert d_no_official.should_route is False
    assert d_no_official.reason == "official_fetch_disabled"

    d_no_disc = detect_us_earnings_squad_route(q, discover_sources=False, auto_fetch_official=None)
    assert d_no_disc.should_route is False
    assert d_no_disc.reason == "discovery_and_official_disabled"

    # Turning off web discovery while explicitly allowing official retrieval keeps SEC squad enabled
    d_official_ok = detect_us_earnings_squad_route(q, discover_sources=False, auto_fetch_official=True)
    assert d_official_ok.should_route is True

    # derive_sources_mode maps ResearchRunRequest fields cleanly
    assert derive_sources_mode(ResearchRunRequest(parent_version_id="v1")) == "auto"
    assert derive_sources_mode(ResearchRunRequest(parent_version_id="v1", selected_source_ids=[])) == "empty"
    assert derive_sources_mode(
        ResearchRunRequest(parent_version_id="v1", selected_source_ids=["s1"], auto_fetch_official=False),
        has_attached_sources=True,
    ) == "private_only"


# --------------------------------------------------------------------------- 2. End-to-End FastAPI Auto-Routing Tests
def test_default_run_endpoint_auto_routes_us_earnings_to_squad_and_streams_org_events(monkeypatch: pytest.MonkeyPatch):
    # Ensure legacy single-loop model is NEVER called when auto-routed
    class ForbiddenLegacyProvider:
        name = "forbidden-legacy"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=4000, max_output_tokens=1000)

        def complete(self, *args, **kwargs):
            raise AssertionError("Legacy single-loop complete() must not run for auto-routed earnings squad!")

        def complete_turn(self, *args, **kwargs):
            raise AssertionError("Legacy single-loop complete_turn() must not run for auto-routed earnings squad!")

    monkeypatch.setattr(api, "challenge_provider", ForbiddenLegacyProvider())
    _install_nvda_aapl_squad_hook(ticker="NVDA", cik="0001045810")

    client = TestClient(api.app)
    headers = _register(client)

    topic_resp = client.post(
        "/v2/research/topics",
        headers=headers,
        json={"question": "分析NVIDIA距今最新的财报"},
    )
    assert topic_resp.status_code == 201, topic_resp.text
    topic = topic_resp.json()
    topic_id = topic["topic_id"]
    v0 = topic["versions"][0]["version_id"]

    run_resp = client.post(
        f"/v2/research/topics/{topic_id}/runs",
        headers={**headers, "Idempotency-Key": "auto-route-nvda-1"},
        json={"parent_version_id": v0},
    )
    assert run_resp.status_code == 202, run_resp.text
    run_data = run_resp.json()
    assert run_data["run_mode"] == "earnings_squad"
    assert run_data["squad_run_id"].startswith("sqrun_")
    assert run_data["ticker"] == "NVDA"
    assert run_data["cik"] == "0001045810"
    assert run_data["entity_id"] == "cik:0001045810"
    assert run_data["routing_reason"] == "matched_us_single_company_earnings"
    op_id = run_data["operation"]["operation_id"]

    # Idempotent replay returns identical squad metadata
    replay_resp = client.post(
        f"/v2/research/topics/{topic_id}/runs",
        headers={**headers, "Idempotency-Key": "auto-route-nvda-1"},
        json={"parent_version_id": v0},
    )
    assert replay_resp.status_code == 202
    assert replay_resp.headers["Idempotency-Replayed"] == "true"
    assert replay_resp.json()["squad_run_id"] == run_data["squad_run_id"]
    assert replay_resp.json()["run_mode"] == "earnings_squad"

    # Poll operation completion
    for _ in range(200):
        op_state = client.get(f"/v2/research/topics/{topic_id}/runs/{op_id}", headers=headers).json()["operation"]["status"]
        if op_state in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.01)
    assert op_state == "completed"

    # Persisted on GET topic, GET runs, GET turn-statuses, and GET dossier
    topic_after = client.get(f"/v2/research/topics/{topic_id}", headers=headers).json()
    assert topic_after["run_mode"] == "earnings_squad"
    assert topic_after["squad_run_id"] == run_data["squad_run_id"]
    assert topic_after["ticker"] == "NVDA"
    assert topic_after["cik"] == "0001045810"

    runs_list = client.get(f"/v2/research/topics/{topic_id}/runs", headers=headers).json()
    assert len(runs_list) == 1
    assert runs_list[0]["run_mode"] == "earnings_squad"
    assert runs_list[0]["squad_run_id"] == run_data["squad_run_id"]
    assert runs_list[0]["logical_status"] == "completed"
    assert runs_list[0]["ticker"] == "NVDA"
    assert runs_list[0]["cik"] == "0001045810"

    statuses = client.get("/v2/research/turn-statuses", headers=headers).json()
    assert len(statuses) == 1
    assert statuses[0]["run_mode"] == "earnings_squad"
    assert statuses[0]["squad_run_id"] == run_data["squad_run_id"]
    assert statuses[0]["logical_status"] == "completed"

    dossier_resp = client.get(f"/v2/research/topics/{topic_id}/earnings-squad/dossier", headers=headers)
    assert dossier_resp.status_code == 200, dossier_resp.text
    dos = dossier_resp.json()
    assert dos["status"] == "completed"
    assert dos["run_id"] == run_data["squad_run_id"]
    assert dos["ticker"] == "NVDA"
    assert dos["cik"] == "0001045810"
    assert dos["dossier"]["locked"] is True

    # OrgGraph SSE stream replays structured events + terminal frame
    sse_resp = client.get(f"/v2/research/topics/{topic_id}/earnings-squad/events", headers=headers)
    assert sse_resp.status_code == 200
    frames = _parse_sse_frames(sse_resp.text.splitlines())
    assert any(f.get("event") == "org_graph_event" for f in frames)
    assert frames[-1]["event"] == "run.terminal"
    assert frames[-1]["data"]["operation_status"] == "completed"

    # Progress endpoint also remains coherent without 404
    prog_resp = client.get(f"/v2/research/topics/{topic_id}/runs/{op_id}/progress", headers=headers)
    assert prog_resp.status_code == 200
    assert prog_resp.json()["run_mode"] == "earnings_squad"
    assert prog_resp.json()["logical_status"] == "completed"


def test_default_run_endpoint_cancels_active_auto_routed_squad():
    unblock = threading.Event()
    _install_nvda_aapl_squad_hook(ticker="AAPL", cik="0000320193", block_event=unblock)

    client = TestClient(api.app)
    headers = _register(client)
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Analyze Apple Q4 earnings"}).json()
    topic_id = topic["topic_id"]
    v0 = topic["versions"][0]["version_id"]

    start = client.post(
        f"/v2/research/topics/{topic_id}/runs",
        headers={**headers, "Idempotency-Key": "auto-cancel-aapl"},
        json={"parent_version_id": v0},
    )
    assert start.status_code == 202
    op = start.json()["operation"]
    assert start.json()["ticker"] == "AAPL"
    assert start.json()["cik"] == "0000320193"

    cancel_resp = client.post(
        f"/v1/operations/{op['operation_id']}/cancel",
        headers={**headers, "Idempotency-Key": "cancel-aapl-1"},
        json={"cancel_token": op["cancel_token"]},
    )
    unblock.set()
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["operation"]["status"] == "cancelled"

    dos = client.get(f"/v2/research/topics/{topic_id}/earnings-squad/dossier", headers=headers).json()
    assert dos["status"] == "cancelled"


@pytest.mark.parametrize(
    "question,run_overrides",
    [
        ("NVIDIA vs AMD 财报对比", {}),
        ("分析 A股 600519 贵州茅台最新年报", {}),
        ("分析NVIDIA距今最新的财报", {"allow_auto_squad": False}),
        ("分析NVIDIA距今最新的财报", {"selected_source_ids": []}),
        ("分析NVIDIA距今最新的财报", {"auto_fetch_official": False}),
    ],
)
def test_excluded_and_opted_out_requests_stay_on_legacy_loop(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    run_overrides: dict[str, Any],
):
    client = TestClient(api.app)
    headers = _register(client)
    src_id = client.post(
        "/v2/research/sources",
        headers=headers,
        json={
            "title": "User Note",
            "text": "Sample revenue note 100 USD.",
            "permissions": {"store": True, "display": True, "model_input": True, "export": True},
        },
    ).json()["source_id"]

    class LegacyScriptedProvider:
        name = "legacy-scripted"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)

        def complete(self, messages, tools, **kwargs):
            tool_results = [m for m in messages if m.get("role") == "tool"]
            if not tool_results and run_overrides.get("selected_source_ids") != []:
                return scripted_turn("read_sources", {"reads": [{"source_id": src_id}], "rationale": "read"}, "r1")
            if run_overrides.get("selected_source_ids") == []:
                return scripted_turn(
                    "submit_research",
                    {"report": "No sources selected.", "claims": [], "limitations": ["No source"], "stopping_reason": "answered", "status": "partial"},
                    "s1",
                )
            return scripted_turn(
                "submit_research",
                {
                    "report": "Sample revenue note 100 USD.",
                    "claims": [{"text": "Sample revenue note 100 USD.", "source_id": src_id, "quotation": "Sample revenue note 100 USD.", "role": "support"}],
                    "limitations": [],
                    "stopping_reason": "answered",
                },
                "s1",
            )

    monkeypatch.setattr(api, "challenge_provider", LegacyScriptedProvider())
    monkeypatch.setattr(api, "evidence_provider", SimpleNamespace(supports_project_search=False))
    # Fail if squad hook were ever invoked
    api.set_earnings_squad_runner_hook(lambda **_k: (_ for _ in ()).throw(AssertionError("Squad hook must not be invoked!")))

    topic = client.post(
        "/v2/research/topics",
        headers=headers,
        json={"question": question, "source_ids": [src_id]},
    ).json()
    topic_id = topic["topic_id"]
    v0 = topic["versions"][0]["version_id"]

    run_resp = client.post(
        f"/v2/research/topics/{topic_id}/runs",
        headers={**headers, "Idempotency-Key": f"legacy-{uuid4().hex[:8]}"},
        json={"parent_version_id": v0, **run_overrides},
    )
    assert run_resp.status_code == 202, run_resp.text
    run_data = run_resp.json()
    assert run_data["run_mode"] == "legacy_single_loop"
    assert "squad_run_id" not in run_data

    op_id = run_data["operation"]["operation_id"]
    for _ in range(200):
        status = client.get(f"/v2/research/topics/{topic_id}/runs/{op_id}", headers=headers).json()["operation"]["status"]
        if status in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.01)
    assert status == "completed"

    # No earnings squad dossier exists for legacy runs
    assert client.get(f"/v2/research/topics/{topic_id}/earnings-squad/dossier", headers=headers).status_code == 404


def test_default_run_endpoint_blocks_concurrent_squad_and_resolves_custom_sec_issuer(monkeypatch: pytest.MonkeyPatch):
    sec = _make_sec_adapter_from_tickers([
        {"cik_str": 1321655, "ticker": "PLTR", "title": "Palantir Technologies Inc."},
    ])
    monkeypatch.setenv("FORESIGHTNINE_SEC_USER_AGENT", "ForesightNineTest contact@example.test")
    monkeypatch.setattr(api, "SecEdgarAdapter", lambda user_agent=None: sec)

    unblock = threading.Event()
    _install_nvda_aapl_squad_hook(ticker="PLTR", cik="0001321655", block_event=unblock)

    client = TestClient(api.app)
    headers = _register(client)
    topic = client.post(
        "/v2/research/topics",
        headers=headers,
        json={"question": "Analyze Palantir latest quarterly results"},
    ).json()
    topic_id = topic["topic_id"]
    v0 = topic["versions"][0]["version_id"]

    first = client.post(
        f"/v2/research/topics/{topic_id}/runs",
        headers={**headers, "Idempotency-Key": "pltr-run-1"},
        json={"parent_version_id": v0},
    )
    assert first.status_code == 202, first.text
    first_data = first.json()
    assert first_data["run_mode"] == "earnings_squad"
    assert first_data["ticker"] == "PLTR"
    assert first_data["cik"] == "0001321655"

    try:
        # Starting a second run while auto-routed squad is active must return 409 lifecycle_conflict
        conflict = client.post(
            f"/v2/research/topics/{topic_id}/runs",
            headers={**headers, "Idempotency-Key": "pltr-run-2"},
            json={"parent_version_id": v0},
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error_code"] == "lifecycle_conflict"
    finally:
        unblock.set()

    op_id = first_data["operation"]["operation_id"]
    for _ in range(200):
        status = client.get(f"/v2/research/topics/{topic_id}/runs/{op_id}", headers=headers).json()["operation"]["status"]
        if status in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.01)
    assert status == "completed"
