"""End-to-end integration tests for V1 Layer 1 + Layer 2 SEC material events wiring.

Covers:
1. ``FinDataToolbox.sec_material_events`` + ``TOOL_REGISTRY`` / ``TOOL_HANDLERS`` execution
   (CIK resolution via cik / entity_id / ticker, ``categories`` filter, ``as_of`` cutoff,
   ``include_suppressed``, ``scale_anchor``, compact source persistence).
2. ``POST /v2/research/sec-events/scan`` (auth, deterministic scan, ``include_suppressed``,
   ``seen_accessions``, L1 repo caching, 422 validation).
3. ``POST /v2/research/sec-events/analyze`` + ``GET /v2/research/sec-events/analyze/{operation_id}``
   (202 async submit, ``Idempotency-Key`` replay & conflict, polling to completion,
   ``_AccountSlotModelRunner`` + ``resolve_model_slots`` / ``resolve_squad_slots`` across
   ``deep_reasoner`` & ``bear_reviewer``, lazy EX-10.1 attachment drilldown via
   ``fetch_lazy_attachment``, ``SEC_EVENTS_L1_RAW`` & ``SEC_EVENTS_L2_SEMANTIC`` cache hit
   with zero repeat SEC/LLM calls, cross-account 404, and cancellation honoring
   ``operations.cancel_event(operation_id)``).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
from threading import Event
import time
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

import app.main as api
import app.openai_compatible as openai_compatible
from app.fin_data.adapters.sec_events import SecPacingLimiter
from app.model_credentials import ModelConfigUpdate, ModelCredentialCipher
from app.model_protocol import ModelToolCall
from app.openai_compatible import HttpResponse
from app.phase3 import ProviderFailure
from app.research_coverage_map import TOOL_CATEGORY
from app.research_fin_tools import FinDataToolbox, SecMaterialEventsRequest
from app.research_signals import SecEventAnalysisResult, SecEventScanResult
from app.research_sources import ResearchSource, ResearchSources
from app.research_tool_executor import TOOL_HANDLERS, ToolExecutionContext, execute_tool_batch
from app.research_tool_presentation import _GROUPS, available_groups
from app.research_tools_registry import (
    BULK_COMPACT_TOOLS,
    COVERAGE_UPDATING_TOOLS,
    FIN_TOOLS,
    TOOLS,
    TOOL_REGISTRY,
)
from app.storage import InMemoryDocumentRepository
from tests.fin_data.helpers import UA, fixture_bytes
from tests.test_sec_events_layer1 import (
    FakeClock,
    RoutingSecTransport,
    _html_reply,
    _json_reply,
    _make_submissions,
    _xml_reply,
)


def _open_limiter() -> SecPacingLimiter:
    clock = FakeClock(start=1000.0)
    return SecPacingLimiter(rate_per_sec=9.0, burst=1000.0, clock=clock, sleep_fn=clock.sleep)


def _register_user(client: TestClient) -> dict[str, str]:
    resp = client.post(
        "/v1/auth/local/register",
        json={
            "email": f"sec-events-{uuid4().hex[:10]}@example.test",
            "password": "correct horse battery staple!",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _build_fixture_transport() -> RoutingSecTransport:
    """Build deterministic SEC EDGAR URL transport covering tickers, submissions, facts, 8-K, EX-10.1, Form 4."""
    transport = RoutingSecTransport()

    transport.add(
        "https://www.sec.gov/files/company_tickers.json",
        _json_reply({"0": {"cik_str": 320193, "ticker": "ACME", "title": "Acme Corp"}}),
    )

    facts_doc = json.loads(fixture_bytes("sec_companyfacts.json"))
    facts_doc["cik"] = 320193
    facts_doc["entityName"] = "Acme Corp"
    transport.add("https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json", _json_reply(facts_doc))
    transport.add("https://efts.sec.gov/LATEST/search-index", _json_reply({"hits": {"hits": []}}))

    subs = _make_submissions(
        cik="0000320193",
        name="Acme Corp",
        tickers=["ACME"],
        rows=[
            {
                "accession": "0000320193-25-000101",
                "filed_at": "2025-11-12",
                "form": "8-K",
                "primary_doc": "mixed8k.htm",
                "items": ["2.02", "5.02", "8.01", "9.01"],
            },
            {
                "accession": "0000320193-25-000102",
                "filed_at": "2025-11-11",
                "form": "8-K",
                "primary_doc": "earn8k.htm",
                "items": ["2.02", "9.01"],
            },
            {
                "accession": "0000320193-25-000103",
                "filed_at": "2025-11-10",
                "form": "4",
                "primary_doc": "xslF345X05/form4_ceo.xml",
            },
        ],
    )
    transport.add("https://data.sec.gov/submissions/CIK0000320193.json", _json_reply(subs))

    mixed_8k_html = """
    <html><body>
      <p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p>
      <p>Item 2.02 Results of Operations and Financial Condition.</p>
      <p>On November 12, 2025, Acme Corp reported quarterly revenue.</p>
      <p>Item 5.02 Departure of Directors or Certain Officers; Election of Directors.</p>
      <p>Effective November 12, 2025, Chief Financial Officer Alice Smith resigned from the Company. See Exhibit 10.1 separation contract for transition covenants.</p>
      <p>Item 8.01 Other Events.</p>
      <p>Acme Corp announced a $300 million senior buyback authorization.</p>
      <p>SIGNATURES</p>
    </body></html>
    """
    ex10_html = (
        "<html><body><p>SEPARATION AGREEMENT</p>"
        "<p>Under Exhibit 10.1, CFO Alice Smith will receive $4.5M severance and a 12-month non-compete covenant.</p>"
        "</body></html>"
    )
    form4_xml = """<?xml version="1.0"?>
    <ownershipDocument>
      <documentType>4</documentType>
      <periodOfReport>2025-11-10</periodOfReport>
      <aff10b5One>0</aff10b5One>
      <issuer><issuerCik>0000320193</issuerCik><issuerName>Acme Corp</issuerName><issuerTradingSymbol>ACME</issuerTradingSymbol></issuer>
      <reportingOwner>
        <reportingOwnerId><rptOwnerCik>0003333333</rptOwnerCik><rptOwnerName>Bob CEO</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer><officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
      </reportingOwner>
      <nonDerivativeTable>
        <nonDerivativeTransaction>
          <securityTitle><value>Common Stock</value></securityTitle>
          <transactionDate><value>2025-11-10</value></transactionDate>
          <transactionCoding><transactionCode>P</transactionCode><equitySwapInvolved>0</equitySwapInvolved></transactionCoding>
          <transactionAmounts>
            <transactionShares><value>10000</value></transactionShares>
            <transactionPricePerShare><value>50.00</value></transactionPricePerShare>
            <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
          </transactionAmounts>
          <postTransactionAmounts><sharesOwnedFollowingTransaction><value>150000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
          <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>
      </nonDerivativeTable>
    </ownershipDocument>"""

    transport.add("/320193/000032019325000101/mixed8k.htm", _html_reply(mixed_8k_html))
    transport.add(
        "/320193/000032019325000101/index.json",
        _json_reply(
            {
                "directory": {
                    "item": [
                        {"name": "mixed8k.htm", "type": "8-K"},
                        {"name": "ex10_1.htm", "type": "EX-10.1", "description": "Separation Agreement"},
                    ]
                }
            }
        ),
    )
    transport.add("/320193/000032019325000101/ex10_1.htm", _html_reply(ex10_html))
    transport.add("/320193/000032019325000103/form4_ceo.xml", _xml_reply(form4_xml))
    return transport


def _openai_chat_json(model: str, content: str | None, *, tool_calls: list[dict[str, Any]] | None = None) -> HttpResponse:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    finish_reason = "stop"
    if tool_calls:
        msg["tool_calls"] = tool_calls
        finish_reason = "tool_calls"
    payload = {
        "id": f"chatcmpl-{uuid4().hex[:8]}",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 60, "completion_tokens": 25, "total_tokens": 85},
    }
    return HttpResponse(status_code=200, headers={"content-type": "application/json"}, body=json.dumps(payload))


def _poll_analysis_until_terminal(
    client: TestClient,
    headers: dict[str, str],
    operation_id: str,
    *,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/v2/research/sec-events/analyze/{operation_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last["operation"]["status"] in ("completed", "failed", "cancelled"):
            return last
        time.sleep(0.02)
    raise AssertionError(f"Operation {operation_id} did not finish within {timeout_s}s; last={last}")


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    api.reset_state()
    api.set_sec_events_hook(None)
    yield
    api.set_sec_events_hook(None)
    api.reset_state()


# --------------------------------------------------------------------------- 1. Toolbox & Tool Registry Wiring


def test_findata_toolbox_sec_material_events_wiring_and_filtering() -> None:
    assert "sec_material_events" in TOOL_REGISTRY
    assert "sec_material_events" in TOOL_HANDLERS
    assert "sec_material_events" in FIN_TOOLS
    assert "sec_material_events" in BULK_COMPACT_TOOLS
    assert "sec_material_events" in COVERAGE_UPDATING_TOOLS
    assert TOOLS[-1].name == "submit_research"
    assert TOOL_CATEGORY["sec_material_events"] == "filings"
    assert "sec_material_events" in _GROUPS["disclosures"]
    assert "sec_material_events" in available_groups(
        {"user": json.dumps({"available_capabilities": {"filings_available": True}})}
    )["disclosures"]

    transport = _build_fixture_transport()
    repo = InMemoryDocumentRepository()
    sources_store = ResearchSources(repo)
    l1_repo = InMemoryDocumentRepository()

    toolbox = FinDataToolbox(
        account_id="acct_test",
        market_key=None,
        sec_user_agent=UA,
        sec_transport=transport,
        persist=lambda item, **kw: sources_store.create("acct_test", item, **kw),
        now=lambda: datetime(2025, 11, 13, 12, 0, tzinfo=timezone.utc),
        l1_cache=l1_repo,
        sec_events_limiter=_open_limiter(),
    )

    # A) cik resolution via explicit cik + categories=['8k'] + include_suppressed=False
    out_8k = toolbox.sec_material_events(
        SecMaterialEventsRequest(
            cik="320193",
            categories=["8k"],
            lookback_days=30,
            as_of=date(2025, 11, 12),
            include_suppressed=False,
            rationale="Inspect 8-K events.",
        ),
        entities={},
    )
    res_8k = out_8k.result
    assert res_8k["status"] == "ok"
    assert res_8k["cik"] == "0000320193"
    assert len(res_8k["candidates"]) == 1
    assert res_8k["candidates"][0]["accession"] == "0000320193-25-000101"
    assert res_8k["suppressed"] == []
    assert res_8k["scale_anchor"] is not None
    assert "metrics" in res_8k and "open_gaps" in res_8k and "failed" in res_8k
    assert out_8k.source is not None
    assert "Alice Smith resigned" in out_8k.source.text
    # EX-10.1 MUST NOT have been eagerly fetched during Layer 1 scan
    assert transport.call_count_matching("ex10_1.htm") == 0

    # B) entity_id resolution + as_of=2025-11-11 cutoff excludes 2025-11-12 8-K & includes Form 4
    out_cutoff = toolbox.sec_material_events(
        SecMaterialEventsRequest(
            entity_id="NASDAQ:ACME",
            lookback_days=30,
            as_of=date(2025, 11, 11),
            include_suppressed=True,
            rationale="Point-in-time check before 2025-11-12.",
        ),
        entities={"NASDAQ:ACME": {"company": "Acme Corp", "symbol": "ACME", "identifiers": {"cik": "0000320193"}}},
    )
    res_cut = out_cutoff.result
    assert res_cut["cik"] == "0000320193"
    assert {c["form"] for c in res_cut["candidates"]} == {"4"}
    assert all(c["filed_at"] <= "2025-11-11" for c in res_cut["candidates"])
    assert any(s["accession"] == "0000320193-25-000102" for s in res_cut["suppressed"])

    # C) ticker resolution via sec.company_identity hitting company_tickers.json
    out_ticker = toolbox.sec_material_events(
        SecMaterialEventsRequest(
            ticker="ACME",
            categories=["8k", "form4"],
            lookback_days=30,
            as_of=date(2025, 11, 12),
            include_suppressed=True,
        ),
        entities={},
    )
    assert out_ticker.result["cik"] == "0000320193"
    assert transport.call_count_matching("company_tickers.json") == 1
    assert {c["form"] for c in out_ticker.result["candidates"]} == {"8-K", "4"}

    # D) Dispatch through execute_tool_batch end-to-end (0 LLM)
    from types import SimpleNamespace

    exec_sources: list[ResearchSource] = []
    exec_source_map: dict[str, ResearchSource] = {}
    exec_state: dict[str, Any] = {
        "history": [{"results": []}],
        "read_ranges": {},
        "read_receipts": [],
        "entities": {},
        "fin_actions": {},
        "acquired_actions": {},
        "facts": [],
        "charts": [],
        "tables": [],
        "leads": [],
        "events": [],
        "coverage": {},
        "seen": [],
        "search_library_empty": False,
        "rate_limit_retries": [],
        "method_packs": [],
    }
    ctx = ToolExecutionContext(
        question="Scan material SEC events for ACME",
        state=exec_state,
        sources=exec_sources,
        source_by_id=exec_source_map,
        context=SimpleNamespace(ensure_running=lambda: None, cancel_event=lambda: None, usage={"input_tokens": 0}),
        as_of=datetime(2025, 11, 12, 23, 59, tzinfo=timezone.utc),
        seen=set(),
        fin_data=toolbox,
        outcome_builder=lambda *a, **kw: None,
    )
    early, _ = execute_tool_batch(
        ctx,
        [
            ModelToolCall(
                id="call_sec_events_1",
                name="sec_material_events",
                arguments={"ticker": "ACME", "categories": ["8k"], "as_of": "2025-11-12", "rationale": "Agent check."},
            )
        ],
    )
    assert early is None
    tool_out = json.loads(exec_state["history"][-1]["results"][-1]["content"])
    assert tool_out["status"] == "ok"
    assert tool_out["source_id"] in exec_source_map
    assert len(tool_out["candidates"]) == 1


# --------------------------------------------------------------------------- 2. POST /v2/research/sec-events/scan


def test_scan_endpoint_auth_filters_seen_accessions_and_l1_cache() -> None:
    transport = _build_fixture_transport()
    api.set_sec_events_hook({"transport": transport, "user_agent": UA, "limiter": _open_limiter()})
    client = TestClient(api.app)

    # Unauthenticated request rejected
    unauth = client.post("/v2/research/sec-events/scan", json={"cik": "320193"})
    assert unauth.status_code == 401

    headers = _register_user(client)

    # Validation checks
    bad_empty = client.post("/v2/research/sec-events/scan", headers=headers, json={"lookback_days": 30})
    assert bad_empty.status_code == 422
    bad_cat = client.post("/v2/research/sec-events/scan", headers=headers, json={"cik": "320193", "categories": ["badcat"]})
    assert bad_cat.status_code == 422

    # First scan by ticker, exclude suppressed, both 8k & form4
    resp1 = client.post(
        "/v2/research/sec-events/scan",
        headers=headers,
        json={
            "ticker": "ACME",
            "categories": ["8k", "form4"],
            "lookback_days": 30,
            "as_of": "2025-11-12",
            "include_suppressed": False,
        },
    )
    assert resp1.status_code == 200, resp1.text
    scan1 = SecEventScanResult.model_validate(resp1.json())
    assert scan1.cik == "0000320193"
    assert {c.form for c in scan1.target_candidates} == {"8-K", "4"}
    assert scan1.suppressed_events == []
    assert transport.call_count_matching("mixed8k.htm") == 1
    assert transport.call_count_matching("form4_ceo.xml") == 1
    assert transport.call_count_matching("ex10_1.htm") == 0

    # Second scan hits SEC_EVENTS_L1_RAW repo (0 extra filing HTML/XML downloads) & returns suppressed when requested
    resp2 = client.post(
        "/v2/research/sec-events/scan",
        headers=headers,
        json={
            "cik": "0000320193",
            "categories": ["8k", "form4"],
            "lookback_days": 30,
            "as_of": "2025-11-12",
            "include_suppressed": True,
        },
    )
    assert resp2.status_code == 200, resp2.text
    scan2 = SecEventScanResult.model_validate(resp2.json())
    assert transport.call_count_matching("mixed8k.htm") == 1
    assert transport.call_count_matching("form4_ceo.xml") == 1
    assert any(s.accession == "0000320193-25-000102" for s in scan2.suppressed_events)

    # Third scan with seen_accessions skips unchanged accessions cleanly
    resp3 = client.post(
        "/v2/research/sec-events/scan",
        headers=headers,
        json={
            "cik": "0000320193",
            "categories": ["8k", "form4"],
            "lookback_days": 30,
            "as_of": "2025-11-12",
            "seen_accessions": scan2.processed_accessions,
        },
    )
    assert resp3.status_code == 200, resp3.text
    scan3 = SecEventScanResult.model_validate(resp3.json())
    assert scan3.target_candidates == []


# --------------------------------------------------------------------------- 3. POST/GET /v2/research/sec-events/analyze


def test_analyze_endpoint_202_slots_lazy_fetch_idempotency_and_l1_l2_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _build_fixture_transport()
    api.set_sec_events_hook({"transport": transport, "user_agent": UA, "limiter": _open_limiter()})

    llm_calls: list[dict[str, Any]] = []

    def fake_openai_transport(
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: float,
        **_kw: Any,
    ) -> HttpResponse:
        del method, headers, timeout
        req_json = json.loads(body.decode("utf-8"))
        model = str(req_json.get("model") or "")
        messages = req_json.get("messages") or []
        llm_calls.append({"url": url, "model": model, "message_count": len(messages)})

        if model == "model-bear-v1":
            user_text = "\n".join(str(m.get("content") or "") for m in messages)
            import re
            match = re.search(r"candidate_id=(\S+)", user_text)
            cand_id = match.group(1) if match else "cand_unknown"
            return _openai_chat_json(
                model,
                json.dumps(
                    {
                        "candidate_id": cand_id,
                        "verdict": "dissent",
                        "rationale": "CFO transition has a 12-month non-compete reducing near-term disruption.",
                        "refuting_quotes": [],
                        "recommended_disposition": "push_alert",
                    }
                ),
            )

        # deep_reasoner flow:
        # Turn 0: if lazy EX-10.1 exists in user prompt and no tool result yet, trigger read_sources on EX-10.1!
        import re
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        prompt_blob = "\n".join(str(m.get("content") or "") for m in messages)
        if not has_tool_result and "ex10" in prompt_blob:
            lazy_sid_match = re.search(r"LazyAttachments:[^\n]*?([^\s,]+)\([^)]*ex10", prompt_blob)
            if lazy_sid_match:
                lazy_sid = lazy_sid_match.group(1)
                return _openai_chat_json(
                    model,
                    None,
                    tool_calls=[
                        {
                            "id": "call_lazy_ex10",
                            "type": "function",
                            "function": {
                                "name": "read_sources",
                                "arguments": json.dumps({"reads": [{"source_id": lazy_sid}]}),
                            },
                        }
                    ],
                )

        cand_matches = re.findall(
            r"\[CANDIDATE candidate_id=(\S+) accession=(\S+) [^\]]*source_id=(\S+)",
            prompt_blob,
        )
        events_out = []
        for c_id, acc, quote_sid in cand_matches:
            if acc == "0000320193-25-000101":
                events_out.append(
                    {
                        "candidate_id": c_id,
                        "accession": acc,
                        "event_subtype": "leadership_change",
                        "skeptic_counter_check": "Confirmed CFO transition includes $4.5M severance in EX-10.1.",
                        "disposition": "push_alert",
                        "severity": "high",
                        "direction": "negative",
                        "summary": "CFO Alice Smith resigned; EX-10.1 grants $4.5M severance with non-compete.",
                        "thesis_impact": "Governance & management transition oversight required.",
                        "verbatim_quotes": [
                            {"source_id": quote_sid, "quotation": "Chief Financial Officer Alice Smith resigned from the Company"}
                        ],
                    }
                )
            else:
                events_out.append(
                    {
                        "candidate_id": c_id,
                        "accession": acc,
                        "event_subtype": "insider_open_market_trade",
                        "skeptic_counter_check": "CEO discretionary open-market purchase.",
                        "disposition": "log_only",
                        "severity": "medium",
                        "direction": "positive",
                        "summary": "CEO Bob CEO bought 10,000 shares on open market ($500K).",
                        "thesis_impact": "Positive insider alignment.",
                        "verbatim_quotes": [
                            {"source_id": quote_sid, "quotation": "Bob CEO"}
                        ],
                    }
                )
        return _openai_chat_json(
            model,
            json.dumps({"cluster_reasoning_chain": "Analyzed filing cluster.", "events": events_out}),
        )

    monkeypatch.setattr("app.openai_compatible._require_public_dns", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(api.model_settings, "cipher", ModelCredentialCipher(b"p" * 32))
    monkeypatch.setattr(openai_compatible, "_default_transport", fake_openai_transport)
    monkeypatch.setattr(openai_compatible, "_default_stream_transport", fake_openai_transport)

    client = TestClient(api.app)
    headers = _register_user(client)
    other_headers = _register_user(client)
    account, _ = api._authenticated_account_session(headers["Authorization"])

    api.model_settings.update(
        account.account_id,
        ModelConfigUpdate(
            mode="personal",
            protocol="openai_chat_completions",
            base_url="https://llm.example.test/v1",
            model="model-deep-v1",
            api_key="sk-test-personal-1234",
        ),
    )
    stored_cfg = api.model_settings._stored(account.account_id)
    assert stored_cfg is not None
    assert api.model_settings.record_tools_result(stored_cfg, True)

    req_body = {
        "cik": "320193",
        "categories": ["8k", "form4"],
        "lookback_days": 30,
        "as_of": "2025-11-12",
        "include_suppressed": False,
        "slot_overrides": {
            "bear_reviewer": {
                "protocol": "openai_chat_completions",
                "base_url": "https://llm.example.test/v1",
                "model": "model-bear-v1",
                "credential_ref": "account_primary",
                "tools_verified": True,
            }
        },
    }

    # Unauthenticated check
    assert client.post("/v2/research/sec-events/analyze", json=req_body, headers={"Idempotency-Key": "unauth-1"}).status_code == 401

    # 202 async submit
    submit_resp = client.post(
        "/v2/research/sec-events/analyze",
        headers={**headers, "Idempotency-Key": "analyze-idem-1"},
        json=req_body,
    )
    assert submit_resp.status_code == 202, submit_resp.text
    op_id = submit_resp.json()["operation"]["operation_id"]
    assert submit_resp.headers.get("Idempotency-Replayed") == "false"

    # Idempotency replay returns identical operation_id with Idempotency-Replayed: true
    replay_resp = client.post(
        "/v2/research/sec-events/analyze",
        headers={**headers, "Idempotency-Key": "analyze-idem-1"},
        json=req_body,
    )
    assert replay_resp.status_code == 202, replay_resp.text
    assert replay_resp.json()["operation"]["operation_id"] == op_id
    assert replay_resp.headers.get("Idempotency-Replayed") == "true"

    # Idempotency key conflict with different payload returns 409
    conflict_resp = client.post(
        "/v2/research/sec-events/analyze",
        headers={**headers, "Idempotency-Key": "analyze-idem-1"},
        json={**req_body, "lookback_days": 60},
    )
    assert conflict_resp.status_code == 409

    # Poll operation to completion
    polled = _poll_analysis_until_terminal(client, headers, op_id)
    assert polled["operation"]["status"] == "completed", polled
    analysis = SecEventAnalysisResult.model_validate(polled["analysis"])
    assert analysis.cik == "0000320193"
    assert len(analysis.signals) >= 2, f"polled={polled}; llm_calls={llm_calls}"
    assert analysis.suppressed_events == []
    assert analysis.scale_anchor is not None

    # Verify lazy attachment drilldown called fetch_lazy_attachment -> downloaded ex10_1.htm once
    assert transport.call_count_matching("ex10_1.htm") == 1
    assert any("$4.5M severance" in sig.summary or "$4.5M severance" in sig.skeptic_counter_check for sig in analysis.signals)

    # Verify deep_reasoner and bear_reviewer routed to distinct configured models via _AccountSlotModelRunner
    models_used = {c["model"] for c in llm_calls}
    assert "model-deep-v1" in models_used
    assert "model-bear-v1" in models_used
    push_sigs = [s for s in analysis.signals if s.disposition == "push_alert"]
    assert push_sigs and "12-month non-compete" in (push_sigs[0].skeptic_dissent or "")

    # Verify cross-account GET returns 404
    assert client.get(f"/v2/research/sec-events/analyze/{op_id}", headers=other_headers).status_code == 404

    # Verify L1 + L2 cache entries exist and NEW Idempotency-Key gets 100% cache hit (0 extra SEC/LLM calls)
    assert len(list(api.SEC_EVENTS_L1_RAW.items())) > 0
    assert len(list(api.SEC_EVENTS_L2_SEMANTIC.items())) > 0
    llm_calls_before = len(llm_calls)
    sec_8k_before = transport.call_count_matching("mixed8k.htm")
    sec_ex10_before = transport.call_count_matching("ex10_1.htm")

    cached_submit = client.post(
        "/v2/research/sec-events/analyze",
        headers={**headers, "Idempotency-Key": "analyze-idem-2-cache"},
        json=req_body,
    )
    assert cached_submit.status_code == 202, cached_submit.text
    cached_op_id = cached_submit.json()["operation"]["operation_id"]
    assert cached_op_id != op_id

    cached_polled = _poll_analysis_until_terminal(client, headers, cached_op_id)
    assert cached_polled["operation"]["status"] == "completed", cached_polled
    cached_analysis = SecEventAnalysisResult.model_validate(cached_polled["analysis"])
    assert cached_analysis.metrics.l2_cache_hits >= 1
    assert len(llm_calls) == llm_calls_before
    assert transport.call_count_matching("mixed8k.htm") == sec_8k_before
    assert transport.call_count_matching("ex10_1.htm") == sec_ex10_before


def test_analyze_endpoint_respects_cancel_event_and_resolve_squad_slots_fallback() -> None:
    transport = _build_fixture_transport()
    started = Event()
    saw_cancel = Event()

    class _BlockingSlotRunner:
        def __init__(self) -> None:
            self._cancel_event: Any = None

        def bind_runtime(self, *, cancel_event: Any = None, **_kw: Any) -> None:
            self._cancel_event = cancel_event

        def invoke(self, mreq: Any) -> Any:
            del mreq
            started.set()
            assert self._cancel_event is not None
            if self._cancel_event.wait(timeout=5.0):
                saw_cancel.set()
            raise ProviderFailure("provider_cancelled", "Cancelled by caller.", retryable=False)

    # Also assert _resolve_sec_events_runtime falls back to resolve_squad_slots when primary is non-wire
    runner = _BlockingSlotRunner()
    api.set_sec_events_hook({"transport": transport, "user_agent": UA, "limiter": _open_limiter(), "model_runner": runner})

    client = TestClient(api.app)
    headers = _register_user(client)

    # First verify fallback resolve_squad_slots branch directly when no wire primary & no hook runner
    account, _ = api._authenticated_account_session(headers["Authorization"])
    from types import SimpleNamespace
    dummy_req = api.SecEventsAnalyzeApiRequest(cik="320193", categories=["8k"])
    fake_http_req = SimpleNamespace(headers=headers, state=SimpleNamespace(client_ip="127.0.0.1"))
    resolved_runner, filtered_bindings, resolved_slots = api._resolve_sec_events_runtime(
        account=account,
        request=dummy_req,
        http_request=fake_http_req,  # type: ignore[arg-type]
        hook_dict={},
        budget=None,
    )
    assert isinstance(resolved_runner, api._AccountSlotModelRunner)
    assert resolved_slots is None
    assert filtered_bindings is not None and "deep_reasoner" in filtered_bindings
    assert "bear_reviewer" not in filtered_bindings  # fallback bear_reviewer collapses to deep_reasoner

    # Start async operation and cancel it while in flight
    submit = client.post(
        "/v2/research/sec-events/analyze",
        headers={**headers, "Idempotency-Key": "analyze-cancel-1"},
        json={"cik": "320193", "categories": ["8k"], "as_of": "2025-11-12"},
    )
    assert submit.status_code == 202, submit.text
    op = submit.json()["operation"]
    op_id = op["operation_id"]

    assert started.wait(timeout=5.0), "Worker did not invoke runner before timeout"
    cancel_resp = client.post(
        f"/v1/operations/{op_id}/cancel",
        headers={**headers, "Idempotency-Key": "cancel-key-1"},
        json={"cancel_token": op["cancel_token"]},
    )
    assert cancel_resp.status_code == 200, cancel_resp.text
    assert saw_cancel.wait(timeout=5.0), "Runner did not observe operations.cancel_event(operation_id)"

    polled = _poll_analysis_until_terminal(client, headers, op_id)
    assert polled["operation"]["status"] == "cancelled"
    assert "analysis" not in polled or polled["analysis"] is None


def test_platform_runner_429_long_backoff_and_concurrency_gate() -> None:
    """v6.1 P0-2: Platform runner releases concurrency gate before 429 long backoff & respects retry_after."""
    from threading import BoundedSemaphore
    from types import SimpleNamespace
    from app import main as app_main
    from app.phase3 import ProviderFailure
    from app.research_earnings_squad import SlotModelRequest

    slept: list[float] = []
    calls = {"n": 0}
    gate = BoundedSemaphore(1)

    class _Flaky429Adapter:
        def complete_turn(self, _turn: object) -> object:
            calls["n"] += 1
            if calls["n"] == 1:
                err = ProviderFailure("rate_limited", "429 retry_after", retryable=True)
                err.upstream_status = 429
                err.retry_after = 12.0  # type: ignore[attr-defined]
                raise err
            if calls["n"] == 2:
                err = ProviderFailure("rate_limited", "429 schedule", retryable=True)
                err.upstream_status = 429
                raise err
            return SimpleNamespace(
                message={"role": "assistant", "content": '{"ok": true}'},
                tool_calls=(),
                finish_reason="stop",
                usage={"input_tokens": 10, "output_tokens": 5},
            )

    def _check_gate_free_during_sleep(sec: float) -> None:
        # Gate MUST be released before backoff sleep so other threads are not starved
        assert gate.acquire(blocking=False) is True
        gate.release()
        slept.append(sec)

    runner = app_main._AccountSlotModelRunner(
        providers_by_slot={},
        default_provider=_Flaky429Adapter(),
        concurrency_gate=gate,
        rate_limit_backoff_schedule=(5.0, 15.0, 35.0),
        sleeper=_check_gate_free_during_sleep,
    )
    req = SimpleNamespace(
        stage="sec_event_analysis",
        role="event_analyst",
        slot="deep_reasoner",
        system_prompt="sys",
        user_prompt="usr",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}],
        tools=(),
        turn_index=0,
        conversation_id="conv_429_test",
        response_schema=None,
        response_schema_name=None,
        max_output_tokens=None,
    )
    resp = runner.invoke(req)
    assert resp.content == '{"ok": true}'
    assert calls["n"] == 3
    assert len(slept) == 2
    assert slept[0] == 12.0  # respected retry_after
    assert 12.0 <= slept[1] <= 18.0  # 2nd 429 used schedule[1] = 15.0 * [0.85, 1.15]
    # Gate cleanly released after completion
    assert gate.acquire(blocking=False) is True
    gate.release()
