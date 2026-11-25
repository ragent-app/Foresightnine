"""Step V3a-0 foundation regression & unit tests.

Locks the 3 P0 codebase regressions plus all Step V3a-0 memory/OCR/raw-offset/storage guards:
1. Second radar sweep `seen_v1: list[str]` crash + `periodic_filings` (10-Q / NT 10-Q).
2. `text_partial` hybrid IR PDF page state, prioritized OCR selection, and BlobStore retention.
3. R2-D4 two-level inbox merge (Day-0 8-K `earnings_delta` + delayed 10-Q `call_commitment_verify`).
4. `normalize_with_raw_map` & `map_match_to_raw_span` (ligatures, layout spaces, local window).
5. `invoke_and_parse_with_repair` truncation reset (`reset_conversation=True`).
6. `RetrievalMethod` expansion (`peer_relative_valuation`, `governance_capital_audit`, `user_pptx`, etc.).
7. 45-line / 120+ tag sliced `companyfacts` v4 (`per_share`, FPI currency, split OpEx) & 64KB streaming HTML.
8. `DocumentBlobStore.prune(min_orphan_age_seconds=...)` + `ocr-cache` cleanup + `items_for_account`.
9. Owner-scoped `GET /v2/research/sources/{source_id}/text` (unattached, `as_of`, metadata).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from app.fin_data.adapters.sec_deep import stream_strip_sec_html_file
from app.fin_data.adapters.sec_edgar import STANDARD_LINES
from app.fin_data.adapters.sec_events import _is_seen_for_channel, scan_sec_material_events
from app.fin_data.adapters.sec_events.pacing import RawSubmissionRow, RawSubmissionsResult
from app.main import app, reset_state
from app.research_blob_store import DocumentBlobStore
from app.research_documents import (
    OCR_CANDIDATE_STATUSES,
    TEXT_STATUSES,
    classify_page_text,
    merge_ocr_into_document,
    page_for_offset,
    page_map_from_text,
    prioritize_ocr_pages,
)
from app.research_llm_primitives import (
    find_quotation_span,
    find_quotation_span_with_raw,
    invoke_and_parse_with_repair,
    map_match_to_raw_span,
    normalize_with_raw_map,
)
from app.research_peer_compare import (
    _CURRENT_PROJECTION_VERSION,
    _V2_PROJECTION_LINES,
    _projection_target_tags_by_taxonomy,
    extract_sliced_companyfacts_from_file,
    project_companyfacts_to_snapshot,
)
from app.research_radar_scheduler import (
    ChangeInboxItem,
    FilingWatchSubscription,
    merge_v3a_item_into_inbox,
    run_p1b_radar_sweep,
    upsert_inbox_item_with_status,
)
from app.research_signals import PeriodicFilingRef, SecEventAnalysisResult, SecEventScanResult
from app.research_sources import PAGED_RETRIEVAL_METHODS, ResearchSource, SourcePermissions
from app.retention import prune_document_blob_store
from app.storage import InMemoryDocumentRepository, account_items


def _dummy_perms() -> SourcePermissions:
    return SourcePermissions(store=True, model_input=True, display=True, export=True)


# --------------------------------------------------------------------------- 1. Radar 2nd-sweep & periodic_filings

def test_p0_radar_second_sweep_seen_list_and_periodic_filings() -> None:
    """Locks P0-2 (list seen_accessions must not crash) and P0-3 (periodic_filings + NT 10-Q)."""
    assert _is_seen_for_channel(["0001045810-26-000010"], "0001045810-26-000010") is True
    assert _is_seen_for_channel(["0001045810-26-000010"], "0001045810-26-000099") is False
    assert _is_seen_for_channel({"0001045810-26-000010": {"sec_events": "seen"}}, "0001045810-26-000010") is True

    subs = RawSubmissionsResult(
        cik="0001045810",
        company_name="NVIDIA CORP",
        tickers=["NVDA"],
        exchanges=["Nasdaq"],
        sic="3674",
        is_fpi=False,
        rows=[
            RawSubmissionRow(
                cik="0001045810",
                accession="0001045810-26-000201",
                nodash="000104581026000201",
                form="10-Q",
                filed_at=date(2026, 11, 20),
                period_of_report=date(2026, 10, 26),
                primary_document="nvda-20261026.htm",
                primary_document_url="https://www.sec.gov/Archives/edgar/data/1045810/000104581026000201/nvda-20261026.htm",
            ),
            RawSubmissionRow(
                cik="0001045810",
                accession="0001045810-26-000199",
                nodash="000104581026000199",
                form="NT 10-Q",
                filed_at=date(2026, 11, 18),
                period_of_report=date(2026, 10, 26),
            ),
        ],
        pages_fetched=1,
        earliest_loaded_date=date(2026, 1, 1),
    )

    scan_res, _texts = scan_sec_material_events(
        "0001045810",
        user_agent="ForesightNine/1.0 test@foresightnine.local",
        as_of=date(2026, 11, 25),
        lookback_days=30,
        seen_accessions=["0001045810-26-000010"],  # list form (previously threw AttributeError)
        submissions_override=subs,
        facts_override={"facts": {}},
    )
    forms = {pf.form: pf for pf in scan_res.periodic_filings}
    assert "10-Q" in forms and forms["10-Q"].is_late_notice is False
    assert "NT 10-Q" in forms and forms["NT 10-Q"].is_late_notice is True


# --------------------------------------------------------------------------- 2. R2-D4 two-level merge & Phase 2c

def test_p0_r2d4_two_level_merge_8k_delta_and_delayed_10q_v3a() -> None:
    """Locks P0-3: Day-0 8-K earnings_delta merges with delayed 10-Q V3a despite different accessions."""
    delta_card = ChangeInboxItem(
        account_id="acc_1",
        cik="0001045810",
        symbol="NVDA",
        source_channel="earnings_delta",
        event_thread_key="0001045810:earnings:Q3 FY27",
        accession="0001045810-26-000150",  # Day-0 8-K accession
        headline="NVDA · 8-K · Q3 revenue beat",
        summary="Revenue beat prior guidance.",
        impact_level="medium",
        direction="positive",
        affected_pillars=["growth"],
        disclosed_at="2026-11-18",
        report_date="2026-10-26",
        fiscal_period_key="Q3 FY27",
        has_confirmed_ledger=True,
    )
    v3a_card = ChangeInboxItem(
        account_id="acc_1",
        cik="0001045810",
        symbol="NVDA",
        source_channel="call_commitment_verify",
        event_thread_key="v3a:0001045810:Q3 FY27",
        accession="0001045810-26-000201",  # Different 10-Q accession filed 5 days later!
        headline="采购承诺环比暴增 +42% 与电话会表述矛盾",
        summary="10-Q ASC 440 采购承诺增至 $28B。",
        impact_level="high",
        direction="negative",
        affected_pillars=["margin", "risk"],
        scorecard_diff={"call_audit_url": "/topics/t1?panel=call-audit", "contradiction_count": 2},
        disclosed_at="2026-11-23",
        report_date="2026-10-26",
        fiscal_period_key="Q3 FY27",
        has_confirmed_ledger=True,
    )

    merged, is_new, is_mod = merge_v3a_item_into_inbox(v3a_card, [delta_card], filing_date=date(2026, 11, 23))
    assert is_new is False and is_mod is True
    assert merged.inbox_item_id == delta_card.inbox_item_id
    assert merged.impact_level == "high"  # max(medium, high)
    assert set(merged.affected_pillars) == {"growth", "margin", "risk"}
    assert merged.scorecard_diff is not None and "v3a_call_collision" in merged.scorecard_diff
    assert merged.scorecard_diff["call_audit_url"] == "/topics/t1?panel=call-audit"
    assert "含电话会核验" in merged.summary

    # Idempotent re-merge must report is_mod=False (zero spurious updates)
    again, is_new2, is_mod2 = merge_v3a_item_into_inbox(v3a_card, [merged], filing_date=date(2026, 11, 23))
    assert is_new2 is False and is_mod2 is False and again.inbox_item_id == merged.inbox_item_id

    # If the 8-K delta card was already accepted/closed, delayed 10-Q emits a standalone V3a card
    closed_delta = delta_card.model_copy(update={"review_state": "accepted"})
    standalone, is_new3, is_mod3 = merge_v3a_item_into_inbox(v3a_card, [closed_delta], filing_date=date(2026, 11, 23))
    assert is_new3 is True and is_mod3 is True
    assert standalone.source_channel == "call_commitment_verify"


def test_p0_radar_phase2c_runs_for_standalone_delayed_10q_and_no_spurious_updates() -> None:
    """Standalone delayed 10-Q bypasses L805 early exit, triggers Phase 2c & NT 10-Q alert."""
    sub = FilingWatchSubscription(account_id="acc_1", cik="0001045810", symbol="NVDA")
    inbox: list[ChangeInboxItem] = []
    saved_subs: list[FilingWatchSubscription] = []

    pf_10q = PeriodicFilingRef(
        cik="0001045810",
        accession="0001045810-26-000201",
        form="10-Q",
        filed_at=date(2026, 11, 23),
        period_of_report=date(2026, 10, 26),
    )

    def _scan(_s: FilingWatchSubscription, _seen: list[str]) -> tuple[SecEventScanResult, dict[str, str]]:
        return (
            SecEventScanResult(
                cik="0001045810",
                entity_id="cik:0001045810",
                symbol="NVDA",
                periodic_filings=[pf_10q],
            ),
            {},
        )

    def _v3a_fn(_s: FilingWatchSubscription, pfs: list[PeriodicFilingRef], _lg: object) -> list[ChangeInboxItem]:
        return [
            ChangeInboxItem(
                account_id="acc_1",
                cik="0001045810",
                symbol="NVDA",
                source_channel="call_commitment_verify",
                event_thread_key="v3a:0001045810:Q3",
                accession=pfs[0].accession,
                headline="Q3 10-Q 承诺违背",
                summary="实绩未达口头指引。",
                impact_level="high",
                disclosed_at="2026-11-23",
            )
        ]

    summary = run_p1b_radar_sweep(
        subscriptions=[sub],
        scan_fn=_scan,
        analyze_v1_fn=lambda *a: SecEventAnalysisResult(cik="0001045810", entity_id="cik:0001045810"),
        ledger_loader=lambda _s: None,
        existing_inbox_loader=lambda _acc: list(inbox),
        save_subscription=lambda s: saved_subs.append(s),
        save_inbox_item=lambda it: inbox.append(it),
        call_commitment_verify_fn=_v3a_fn,
    )
    assert summary.l2_queued_count == 1
    assert summary.inbox_items_created == 1
    assert summary.inbox_items_updated == 0
    assert saved_subs[-1].seen_accessions["0001045810-26-000201"]["call_verify"] == "checked"

    # Identical item upsert must not bump updated count
    _persisted, is_new, is_mod = upsert_inbox_item_with_status(inbox[0], inbox)
    assert is_new is False and is_mod is False


# --------------------------------------------------------------------------- 3. text_partial & smart OCR page selection

def test_text_partial_classification_prioritize_and_merge() -> None:
    assert "text_partial" in TEXT_STATUSES and "text_partial" in OCR_CANDIDATE_STATUSES
    # Backward compatibility: default partial_threshold=0 keeps short synthetic text as "text"
    assert classify_page_text("Revenue 12.5") == "text"
    # Hybrid IR PDF mode: short header (<120 chars) becomes "text_partial"
    assert classify_page_text("Q3 FY26 Gross Margin Outlook", partial_threshold=120) == "text_partial"

    pages_txt = [
        "NVIDIA Investor Presentation",                # p1 cover
        "Forward-Looking Statements Safe Harbor",      # p2 boilerplate
        "Product Overview",                            # p3 generic
        "Q3 FY26 Revenue & Gross Margin Guidance 75%", # p4 high value
        "Team Culture",                                # p5 generic
        "Non-GAAP Reconciliation Table $35.1B",        # p6 high value
    ]
    raw_text = "\f".join(pages_txt) + "\f"
    dp = page_map_from_text(raw_text, original_retained=True, partial_threshold=120)
    assert [e[2] for e in dp["pages"]] == ["text_partial"] * 6
    # Preserves header offsets so page_for_offset works even before OCR!
    p4_start = dp["pages"][3][0]
    assert page_for_offset(dp, p4_start) == 4

    picked = prioritize_ocr_pages(raw_text, dp, max_pages=2)
    assert picked == [4, 6]

    fake_ocr = {
        4: SimpleNamespace(
            text="Q3 FY26 Guidance: Revenue $37.5B, Non-GAAP Gross Margin 73.5%",
            error=None, engine="tesseract", engine_version="5.3", langs="eng",
            mean_confidence=92.0, low_confidence_count=0, usage=None, cached=False,
        )
    }
    merged_text, merged_dp = merge_ocr_into_document(raw_text, dp, fake_ocr)
    assert merged_dp["pages"][3][2] == "ocr"
    assert merged_dp["pages"][5][2] == "text_partial"
    assert "$37.5B" in merged_text
    assert "Non-GAAP Reconciliation Table" in merged_text


# --------------------------------------------------------------------------- 4. Zero-drift raw-offset projection

def test_normalize_with_raw_map_ligatures_layout_spaces_and_local_window() -> None:
    raw = (
        "Page 1 summary: Margin was 75% in Q1.\f"
        "Page 2   detailed   table:   Gross   margin   pro\ufb01le   |   **75%**   and   revenue   $   35.1B."
    )
    quote = "Gross margin profile 75% and revenue $35.1B"
    match = find_quotation_span(quote, raw)
    assert match is not None and match.match_space in ("norm_text", "norm_table")
    r_start, r_end = map_match_to_raw_span(match, raw)
    extracted = raw[r_start:r_end]
    assert extracted.startswith("Gross") and extracted.endswith("35.1B")

    # Local window prevents short "75%" from falsely snapping to Page 1!
    p2_start = raw.index("Page 2")
    _m2, loc_s, loc_e = find_quotation_span_with_raw("75%", raw, local_window=(p2_start, len(raw)))
    assert loc_s is not None and loc_e is not None
    assert loc_s >= p2_start and raw[loc_s:loc_e] == "75%"


# --------------------------------------------------------------------------- 5. Truncation compact reset

def test_invoke_and_parse_with_repair_truncation_reset() -> None:
    class _MiniModel(BaseModel):
        model_config = ConfigDict(extra="forbid")
        summary: str

    calls: list[dict] = []

    def _invoke(messages: list[dict], attempt: int, *, reset_conversation: bool = False):
        calls.append({"attempt": attempt, "reset": reset_conversation, "msg_count": len(messages)})
        if attempt == 0:
            return SimpleNamespace(content='{"summary": "truncated...', finish_reason="length")
        return SimpleNamespace(content='{"summary": "compact ok"}', finish_reason="stop")

    parsed, repairs = invoke_and_parse_with_repair(
        invoke_fn=_invoke,
        model_cls=_MiniModel,
        system_prompt="sys",
        user_prompt="usr",
        max_repairs=2,
    )
    assert parsed.summary == "compact ok"
    assert repairs == 1
    assert calls[0] == {"attempt": 0, "reset": False, "msg_count": 2}
    assert calls[1] == {"attempt": 1, "reset": True, "msg_count": 2}


# --------------------------------------------------------------------------- 6. RetrievalMethod & 45-line sliced companyfacts v4

def test_retrieval_methods_and_sliced_companyfacts_v4_and_streaming_html(tmp_path: Path) -> None:
    for method in (
        "user_pptx", "user_docx", "user_text",
        "peer_relative_valuation", "governance_capital_audit", "call_qa_verification",
    ):
        src = ResearchSource(
            source_id=f"s_{method}",
            owner_id="acc_1",
            title=f"Title {method}",
            text="body",
            kind="user_supplied",
            obtained_at=datetime.now(timezone.utc),
            content_sha256="a" * 64,
            retrieval_method=method,  # type: ignore[arg-type]
            permissions=_dummy_perms(),
        )
        assert src.retrieval_method == method
    assert "user_pptx" in PAGED_RETRIEVAL_METHODS

    assert len(STANDARD_LINES) == 55
    assert _CURRENT_PROJECTION_VERSION == 4
    assert len(_V2_PROJECTION_LINES) == 45
    tags_by_tax = _projection_target_tags_by_taxonomy()
    assert len(tags_by_tax["us-gaap"]) + len(tags_by_tax["ifrs-full"]) >= 120

    cf_path = tmp_path / "companyfacts.json"
    cf_doc = {
        "cik": 1045810,
        "facts": {
            "us-gaap": {
                "UnusedConceptWithHugeNoise": {"units": {"USD": [{"end": "2026-01-25", "val": 999}]}},
                "Revenues": {"units": {"KRW": [{"start": "2025-10-27", "end": "2026-01-25", "val": 50000, "filed": "2026-02-20", "form": "10-Q"}]}},
                "EarningsPerShareDiluted": {
                    "units": {
                        "KRW/shares": [{"start": "2025-10-27", "end": "2026-01-25", "val": 1250, "filed": "2026-02-20", "form": "10-Q"}],
                        "pure": [{"start": "2025-10-27", "end": "2026-01-25", "val": 99999}],
                    }
                },
                "RevenueRemainingPerformanceObligation": {"units": {"KRW": [{"end": "2026-01-25", "val": 80000, "filed": "2026-02-20", "form": "10-Q"}]}},
                "UnrecordedUnconditionalPurchaseObligationBalanceSheetAmount": {"units": {"KRW": [{"end": "2026-01-25", "val": 30000, "filed": "2026-02-20", "form": "10-Q"}]}},
                "SellingAndMarketingExpense": {"units": {"KRW": [{"start": "2025-10-27", "end": "2026-01-25", "val": 4000, "filed": "2026-02-20", "form": "10-Q"}]}},
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {"units": {"shares": [{"end": "2026-01-25", "val": 24000000, "filed": "2026-02-20", "form": "10-Q"}]}},
            },
        },
    }
    cf_path.write_text(json.dumps(cf_doc), encoding="utf-8")
    sliced = extract_sliced_companyfacts_from_file(cf_path)
    assert "UnusedConceptWithHugeNoise" not in sliced["facts"]["us-gaap"]
    assert "Revenues" in sliced["facts"]["us-gaap"]
    assert "UnrecordedUnconditionalPurchaseObligationBalanceSheetAmount" in sliced["facts"]["us-gaap"]

    snap = project_companyfacts_to_snapshot(
        cik="0001045810", symbol="KRTEST", company_name="Korea Test", sic="3674",
        sic_description=None, latest_periodic_accession="0001-26-000001", latest_periodic_filed_at="2026-02-20",
        companyfacts_doc=sliced,
    )
    assert snap.currency == "KRW"
    assert snap.projection_version == 4
    assert "KRW/shares" in snap.facts_by_line["eps_diluted"]["EarningsPerShareDiluted"]
    assert "pure" not in snap.facts_by_line["eps_diluted"]["EarningsPerShareDiluted"]
    assert "rpo" in snap.facts_by_line and "purchase_commitments" in snap.facts_by_line
    assert "selling_expense" in snap.facts_by_line

    # 64KB streaming HTMLParser skips <ix:header>, <style>, <script>
    html_path = tmp_path / "10k.htm"
    html_path.write_text(
        "<html><head><style>.x{color:red}</style></head><body>"
        "<ix:header><ix:hidden>SECRET_XBRL_HEADER_NOISE</ix:hidden></ix:header>"
        "<script>var evil = 1;</script>"
        "<div>ITEM 1A. RISK FACTORS</div><table><tr><td>Export controls</td><td>$5.5B</td></tr></table>"
        "</body></html>",
        encoding="utf-8",
    )
    stripped = stream_strip_sec_html_file(html_path, chunk_bytes=32)
    assert "SECRET_XBRL_HEADER_NOISE" not in stripped and "var evil" not in stripped
    assert "ITEM 1A. RISK FACTORS" in stripped and "Export controls | $5.5B" in stripped


# --------------------------------------------------------------------------- 7. BlobStore prune & account_items

def test_blob_store_prune_min_orphan_age_ocr_cache_and_account_items(tmp_path: Path) -> None:
    store = DocumentBlobStore(tmp_path / "blobs")
    live_file = tmp_path / "live.bin"
    old_orphan = tmp_path / "old.bin"
    young_orphan = tmp_path / "young.bin"
    live_file.write_bytes(b"LIVE" * 50)
    old_orphan.write_bytes(b"OLD!" * 50)
    young_orphan.write_bytes(b"NEW!" * 50)

    h_live = store.put_file(live_file)
    h_old = store.put_file(old_orphan)
    h_young = store.put_file(young_orphan)

    now_ts = 1_800_000_000.0
    os.utime(store.path_for(h_old), (now_ts - 7200, now_ts - 7200))
    os.utime(store.path_for(h_young), (now_ts - 120, now_ts - 120))  # 2 min old -> protected by min_orphan_age=3600!

    ocr_dir = store.root / "ocr-cache"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    stale_ocr = ocr_dir / "old_page.json"
    stale_ocr.write_text('{"text":"old"}', encoding="utf-8")
    os.utime(stale_ocr, (now_ts - 20 * 86400, now_ts - 20 * 86400))

    summary = prune_document_blob_store(
        store,
        live_sha256s_fn=lambda: {h_live},
        max_age_seconds=3600.0,
        max_total_bytes=1,  # force over-quota LRU pass too!
        min_orphan_age_seconds=3600.0,
        ocr_cache_max_age_seconds=14 * 86400,
        now=now_ts,
    )
    assert summary.blobs_deleted == 1
    assert store.path_for(h_live) is not None
    assert store.path_for(h_young) is not None  # protected against concurrent-upload race!
    assert store.path_for(h_old) is None
    assert summary.ocr_cache_deleted == 1 and not stale_ocr.exists()

    repo: InMemoryDocumentRepository[dict] = InMemoryDocumentRepository()
    repo.put("k1", {"owner_id": "u1", "v": 1})
    repo.put("k2", {"account_id": "u1", "v": 2})
    repo.put("k3", {"account_id": "u2", "v": 3})
    assert {k for k, _ in account_items(repo, "u1")} == {"k1", "k2"}


# --------------------------------------------------------------------------- 8. Owner-scoped /v2/research/sources/{id}/text

def test_owner_scoped_source_text_endpoint_metadata_and_as_of(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    reset_state()
    client = TestClient(app)
    reg = client.post("/v1/auth/local/register", json={"email": "v3a0@example.com", "password": "Password123!"})
    assert reg.status_code in (200, 201)
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/v2/research/sources",
        headers=headers,
        json={
            "title": "NVDA Q3 Transcript",
            "text": "CFO: Gross margin was 75%.\fSlide 2 details.",
            "published_at": "2026-11-20T00:00:00Z",
            "permissions": {"store": True, "model_input": True, "display": True, "export": True},
        },
    )
    assert created.status_code == 201
    sid = created.json()["source_id"]

    # Accessible directly even when NOT attached to any Topic!
    resp = client.get(f"/v2/research/sources/{sid}/text", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_id"] == sid
    assert body["title"] == "NVDA Q3 Transcript"
    assert "Gross margin was 75%" in body["text"]
    assert body["permissions"]["display"] is True

    # Topic as_of cutoff enforcement when ?topic_id= is supplied
    topic = client.post(
        "/v2/research/topics",
        headers=headers,
        json={"question": "Check NVDA Q2", "as_of": "2026-09-01T00:00:00Z"},
    )
    assert topic.status_code == 201
    tid = topic.json()["topic_id"]
    blocked = client.get(f"/v2/research/sources/{sid}/text", params={"topic_id": tid}, headers=headers)
    assert blocked.status_code == 403
