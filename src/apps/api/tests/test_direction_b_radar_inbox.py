"""Unit tests for Direction B Thesis Ledger, P1-B Staggered Priority Queue, and Unified Change Inbox."""
from __future__ import annotations

from datetime import date
from typing import Any

from app.research_radar_scheduler import (
    ChangeInboxItem,
    FilingWatchSubscription,
    apply_inbox_decision,
    build_inbox_item_from_v1_signal,
    compute_candidate_priority_tier,
    run_p1b_radar_sweep,
    seen_accessions_for_channel,
    summarize_radar_health,
    upsert_inbox_item_with_folding,
)
from app.research_signals import (
    CompanySignalEnvelope,
    EventRunMetrics,
    FailedAccession,
    RawEventCandidate,
    SecEventAnalysisResult,
    SecEventScanResult,
    SignalProvenance,
    SignalQuote,
    SuppressedEventRecord,
)
from app.research_thesis_ledger import (
    ThesisClaim,
    WatchTrigger,
    build_thesis_draft_from_dossier,
    build_v1_thesis_context,
    confirm_thesis_ledger_version,
    diff_thesis_ledgers,
    evaluate_metric_trigger,
)


def _sample_dossier() -> dict[str, Any]:
    return {
        "dossier_id": "dossier_nvda_q2",
        "entity_id": "cik:0001045810",
        "symbol": "NVDA",
        "cik": "0001045810",
        "company_name": "NVIDIA Corp",
        "period_label": "FY2027 Q2",
        "snapshot": {
            "flags": [
                {"severity": "yellow", "code": "AR_GROWTH_GAP", "summary": "AR grew faster than revenue by 6pp"},
                {"severity": "green", "code": "OCF_QUALITY", "summary": "OCF/NI > 0.9x"},
            ],
            "valuation": {"reverse_dcf": {"implied_5y_fcf_cagr_pct": "18.5"}},
        },
        "bear_cases": [
            {
                "bear_id": "bear_1",
                "thesis_challenge": "Gross margin faces Blackwell ramp cost headwinds.",
                "anchored_evidence_ids": ["card_margin_1"],
                "anchor_kind": "margin_cost",
                "next_quarter_invalidation_trigger": "GAAP gross margin falls below 74.0% in Q3.",
                "severity": "high",
            },
            {
                "bear_id": "bear_2",
                "thesis_challenge": "Receivables outpacing revenue could signal channel stuffing.",
                "anchored_evidence_ids": ["card_cash_1"],
                "anchor_kind": "cash_quality",
                "next_quarter_invalidation_trigger": "AR growth minus revenue growth exceeds +8pp.",
                "severity": "medium",
            },
        ],
        "evidence_cards": [
            {
                "card_id": "card_growth_1",
                "worker_role": "growth_demand",
                "topic_tag": "revenue",
                "finding": "Data Center revenue expanded 78% YoY on GB200 rack shipments.",
                "caveat": "Hyperscaler capex digestion risk in H2.",
            }
        ],
    }


def _make_cand(
    cid: str,
    *,
    cik: str = "0001375365",
    accession: str = "0001375365-24-000036",
    form: str = "8-K",
    items: list[str] | None = None,
    statutory: list[str] | None = None,
) -> RawEventCandidate:
    return RawEventCandidate(
        candidate_id=cid,
        cik=cik,
        symbol="SMCI",
        form=form,
        category="material_8k" if form.startswith("8-K") else "ownership_13dg",
        accession=accession,
        filed_at=date(2024, 10, 30),
        sec_url=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/doc.htm",
        source_id=f"sec:{accession}:primary",
        event_thread_key=f"{cik}:{form}:{accession}",
        items=items or [],
        statutory_critical_items=statutory or [],
        excerpt="Item 4.01 EY resigned as registered public accounting firm.",
    )


def _make_signal(
    *,
    cik: str = "0001375365",
    symbol: str = "SMCI",
    accession: str = "0001375365-24-000036",
    disposition: str = "push_alert",
    severity: str = "critical",
    direction: str = "negative",
    thread_key: str = "0001375365:auditor_change:2024-10",
    pillars: list[str] | None = None,
    skeptic_dissent: str | None = None,
    summary: str = "EY resigned as SMCI auditor.",
) -> CompanySignalEnvelope:
    return CompanySignalEnvelope(
        signal_id=f"sig_{accession}",
        cik=cik,
        entity_id=f"cik:{cik}",
        symbol=symbol,
        company_name="Super Micro Computer, Inc.",
        event_subtype="auditor_change",
        disposition=disposition,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        disclosed_at="2024-10-30",
        summary=summary,
        affected_thesis_pillars=pillars or ["cash_quality", "risk"],  # type: ignore[arg-type]
        event_thread_key=thread_key,
        skeptic_dissent=skeptic_dissent,
        verbatim_quotes=[
            SignalQuote(
                source_id=f"sec:{accession}:primary",
                accession=accession,
                quotation="EY resigned",
                verified=True,
            )
        ],
        provenance=SignalProvenance(
            source_id=f"sec:{accession}:primary",
            accession=accession,
            form="8-K",
            sec_url=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/doc.htm",
        ),
    )


def test_thesis_ledger_draft_confirm_diff_and_metric_trigger() -> None:
    draft = build_thesis_draft_from_dossier(
        account_id="acc_1",
        topic_id="topic_nvda",
        dossier=_sample_dossier(),
    )
    assert draft.review_status == "draft"
    assert draft.version == 1
    assert len(draft.claims) == 4  # margin, cash_quality (bears), growth (card), valuation (reverse-dcf)
    assert any("AR_GROWTH_GAP" in f for f in draft.active_red_yellow_flags)

    v1 = confirm_thesis_ledger_version(
        account_id="acc_1",
        topic_id="topic_nvda",
        claims=draft.claims,
        base_draft=draft,
        decision_summary="Confirmed Q2 baseline thesis.",
    )
    assert v1.review_status == "confirmed"
    assert v1.version == 1
    assert v1.confirmed_at is not None

    # V1 thesis context projection
    flags, ctx = build_v1_thesis_context(v1)
    assert flags and "AR_GROWTH_GAP" in flags[0]
    assert ctx and "Thesis Ledger v1" in ctx

    # Deterministic metric trigger evaluation
    trg = WatchTrigger(
        kind="metric_rule",
        description="Gross margin >= 74.0%",
        metric_key="gross_margin_pct",
        operator=">=",
        threshold_decimal="74.0",
    )
    assert evaluate_metric_trigger(trg, {"gross_margin_pct": "72.5"}).last_eval_status == "triggered"
    assert evaluate_metric_trigger(trg, {"gross_margin_pct": "75.1"}).last_eval_status == "cleared"
    assert evaluate_metric_trigger(trg, {}, is_10q_available=False).last_eval_status == "awaiting_10q"

    # Upgrade to v2 and verify auditable diff
    updated_claims = [
        c.model_copy(update={"status": "weakened", "user_note": "Q3 guide cut"}) if c.pillar == "margin" else c
        for c in v1.claims
    ]
    v2 = confirm_thesis_ledger_version(
        account_id="acc_1",
        topic_id="topic_nvda",
        claims=updated_claims,
        previous_confirmed=v1,
        decision_summary="Weakened margin pillar after Q3 guide.",
    )
    assert v2.version == 2
    assert v2.parent_version == 1
    diff = diff_thesis_ledgers(v1, v2)
    modified = [e for e in diff.entries if e.change_kind == "modified"]
    assert len(modified) == 1
    assert modified[0].pillar == "margin"
    assert modified[0].old_status == "active"
    assert modified[0].new_status == "weakened"


def test_p1b_priority_tier_and_amendment_folding_and_decision_flow() -> None:
    c_p0 = _make_cand("c0", items=["4.01"])
    c_p2 = _make_cand("c2", items=["5.02"])
    c_p3 = _make_cand("c3", items=["8.01"])
    assert compute_candidate_priority_tier([c_p0]) == 0
    assert compute_candidate_priority_tier([c_p3], has_new_earnings=True) == 1
    assert compute_candidate_priority_tier([c_p2]) == 2
    assert compute_candidate_priority_tier([c_p3]) == 3

    draft = build_thesis_draft_from_dossier(account_id="acc_1", topic_id="topic_smci", dossier=_sample_dossier())
    v1 = confirm_thesis_ledger_version(account_id="acc_1", topic_id="topic_smci", claims=draft.claims, base_draft=draft)
    sub = FilingWatchSubscription(account_id="acc_1", topic_id="topic_smci", cik="0001375365", symbol="SMCI")

    sig1 = _make_signal(accession="0001375365-24-000036", summary="Initial 4.01 resignation.")
    item1 = build_inbox_item_from_v1_signal(account_id="acc_1", subscription=sub, signal=sig1, ledger=v1)
    assert item1.linked_claim_ids  # matched cash_quality pillar!

    # Same accession -> idempotent no-op
    same_item, is_new = upsert_inbox_item_with_folding(item1, [item1])
    assert not is_new and same_item.inbox_item_id == item1.inbox_item_id

    # Amendment on same event_thread_key -> folds in place & marks superseded
    sig2 = _make_signal(accession="0001375365-24-000040", summary="8-K/A amendment with EX-16.1 letter.")
    item2 = build_inbox_item_from_v1_signal(account_id="acc_1", subscription=sub, signal=sig2, ledger=v1)
    folded, is_new2 = upsert_inbox_item_with_folding(item2, [item1])
    assert not is_new2
    assert folded.inbox_item_id == item1.inbox_item_id
    assert folded.superseded_by_accession == "0001375365-24-000040"
    assert folded.review_state == "unread"

    # keep_original -> NEVER modifies confirmed ledger
    kept_item, maybe_ledger = apply_inbox_decision(
        item=folded, action="keep_original", user_note="Need more facts", confirmed_ledger=v1
    )
    assert kept_item.review_state == "dismissed_with_note"
    assert maybe_ledger is None

    # accept -> atomically produces ThesisLedger v2
    acc_item, v2 = apply_inbox_decision(
        item=folded, action="accept", user_note="Auditor exit weakens cash quality", confirmed_ledger=v1
    )
    assert acc_item.review_state == "accepted"
    assert v2 is not None and v2.version == 2 and acc_item.resulting_ledger_version == 2


def test_p1b_staggered_sweep_priority_order_zero_llm_quiet_and_failure_isolation() -> None:
    sub_quiet = FilingWatchSubscription(subscription_id="sub_q", account_id="acc_1", cik="0000200406", symbol="JNJ")
    sub_p2 = FilingWatchSubscription(subscription_id="sub_p2", account_id="acc_1", cik="0000829224", symbol="SBUX")
    sub_p0 = FilingWatchSubscription(subscription_id="sub_p0", account_id="acc_1", cik="0001375365", symbol="SMCI")
    sub_err = FilingWatchSubscription(subscription_id="sub_err", account_id="acc_1", cik="0000000999", symbol="FAIL")

    l2_order: list[str] = []
    saved_subs: dict[str, FilingWatchSubscription] = {}
    saved_inbox: list[ChangeInboxItem] = []

    def scan_fn(sub: FilingWatchSubscription, _seen: list[str]) -> tuple[SecEventScanResult, dict[str, str]]:
        if sub.symbol == "FAIL":
            raise RuntimeError("SEC 503 timeout")
        if sub.symbol == "JNJ":
            return (
                SecEventScanResult(
                    cik=sub.cik,
                    entity_id=f"cik:{sub.cik}",
                    symbol="JNJ",
                    as_of=date(2026, 11, 24),
                    target_candidates=[],
                    suppressed_events=[
                        SuppressedEventRecord(
                            accession="0000200406-26-000001",
                            form="4",
                            filed_at=date(2026, 11, 1),
                            category="insider_trade",
                            suppression_reason="routine_10b5_1_plan",
                        )
                    ],
                ),
                {},
            )
        if sub.symbol == "SBUX":
            return (
                SecEventScanResult(
                    cik=sub.cik,
                    entity_id=f"cik:{sub.cik}",
                    symbol="SBUX",
                    as_of=date(2026, 11, 24),
                    target_candidates=[_make_cand("c_sbux", cik=sub.cik, accession="0000829224-24-000010", items=["5.02"])],
                ),
                {},
            )
        return (
            SecEventScanResult(
                cik=sub.cik,
                entity_id=f"cik:{sub.cik}",
                symbol="SMCI",
                as_of=date(2026, 11, 24),
                target_candidates=[_make_cand("c_smci", cik=sub.cik, accession="0001375365-24-000036", items=["4.01"])],
                failed_accessions=[FailedAccession(accession="0001375365-24-000099", form="8-K", error_reason="timeout")],
            ),
            {},
        )

    def analyze_fn(
        sub: FilingWatchSubscription,
        scan: SecEventScanResult,
        _texts: dict[str, str],
        _flags: list[str] | None,
        _ctx: str | None,
    ) -> SecEventAnalysisResult:
        l2_order.append(sub.symbol or sub.cik)
        acc = scan.target_candidates[0].accession
        sig = _make_signal(
            cik=sub.cik,
            symbol=sub.symbol or sub.cik,
            accession=acc,
            disposition="push_alert" if sub.symbol == "SMCI" else "log_only",
        )
        return SecEventAnalysisResult(
            cik=sub.cik,
            entity_id=f"cik:{sub.cik}",
            symbol=sub.symbol,
            as_of=scan.as_of,
            signals=[sig],
            metrics=EventRunMetrics(),
        )

    summary = run_p1b_radar_sweep(
        subscriptions=[sub_quiet, sub_p2, sub_p0, sub_err],
        scan_fn=scan_fn,
        analyze_v1_fn=analyze_fn,
        ledger_loader=lambda _s: None,
        existing_inbox_loader=lambda _acc: saved_inbox,
        save_subscription=lambda s: saved_subs.__setitem__(s.subscription_id, s),
        save_inbox_item=lambda it: saved_inbox.append(it),
    )

    assert summary.subscriptions_checked == 4
    assert summary.l1_zero_change_count == 1  # JNJ exited with 0 LLM
    assert summary.l2_queued_count == 2  # SMCI + SBUX
    assert l2_order == ["SMCI", "SBUX"]  # P0 statutory 4.01 ran BEFORE P2 5.02!
    assert summary.inbox_items_created == 1  # only SMCI push_alert created unread item; SBUX log_only stayed quiet
    assert summary.error_count == 1  # FAIL explicitly marked error
    assert summary.degraded_count == 1  # SMCI had 1 failed L1 accession -> degraded
    assert "0001375365-24-000099" in saved_subs["sub_p0"].failed_accessions
    assert "0001375365-24-000099" not in seen_accessions_for_channel(saved_subs["sub_p0"], "v1")
    assert "0001375365-24-000036" in seen_accessions_for_channel(saved_subs["sub_p0"], "v1")

    health = summarize_radar_health(list(saved_subs.values()), saved_inbox)
    assert health.error_count == 1
    assert health.degraded_count == 1
    assert health.healthy_count == 2
    assert health.unread_inbox_count == 1


def test_direction_b_api_end_to_end_ledger_subscription_inbox_decision(monkeypatch: Any) -> None:
    """API integration test: draft -> confirm v1 -> subscribe -> health -> inbox -> decide v2."""
    from fastapi.testclient import TestClient
    from app import main as app_main

    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    app_main.reset_state()
    client = TestClient(app_main.app)

    # Register & authenticate test account
    reg = client.post(
        "/v1/auth/local/register",
        json={"email": "dirb@example.test", "password": "correct horse battery staple!"},
    )
    assert reg.status_code in (200, 201), reg.text
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    account = app_main._authenticated_account(f"Bearer {token}")

    # Seed a locked EarningsDossier state for topic_nvda
    topic_id = "topic_nvda_dirb"
    app_main.EARNINGS_SQUAD_STATES.put(
        f"{account.account_id}:{topic_id}:run_1",
        {
            "account_id": account.account_id,
            "topic_id": topic_id,
            "dossier": _sample_dossier(),
        },
    )

    # 1. Generate draft from locked dossier
    r_draft = client.post(f"/v2/research/topics/{topic_id}/thesis-ledger/draft", headers=headers, json={})
    assert r_draft.status_code == 200, r_draft.text
    draft = r_draft.json()
    assert draft["review_status"] == "draft"
    assert len(draft["claims"]) == 4

    # 2. Confirm v1
    r_conf = client.post(
        f"/v2/research/topics/{topic_id}/thesis-ledger/confirm",
        headers=headers,
        json={"claims": draft["claims"], "decision_summary": "Initial Q2 baseline"},
    )
    assert r_conf.status_code == 200, r_conf.text
    v1 = r_conf.json()
    assert v1["version"] == 1 and v1["review_status"] == "confirmed"

    # 3. Create radar subscription (using explicit CIK)
    r_sub = client.post(
        "/v2/research/radar/subscriptions",
        headers=headers,
        json={"cik": "0001045810", "symbol": "NVDA", "topic_id": topic_id},
    )
    assert r_sub.status_code == 200, r_sub.text
    sub = r_sub.json()
    assert sub["cik"] == "0001045810" and sub["enabled"] is True

    # 4. Seed an inbox item linked to this subscription & topic, verify /inbox and /health
    inbox_item = build_inbox_item_from_v1_signal(
        account_id=account.account_id,
        subscription=FilingWatchSubscription.model_validate(sub),
        signal=_make_signal(cik="0001045810", symbol="NVDA", accession="0001045810-26-000123"),
        ledger=app_main._latest_confirmed_ledger(account.account_id, topic_id),
    )
    app_main.CHANGE_INBOX_ITEMS.put(inbox_item.inbox_item_id, inbox_item)

    r_health = client.get("/v2/research/radar/health", headers=headers)
    assert r_health.status_code == 200, r_health.text
    assert r_health.json()["unread_inbox_count"] == 1
    assert r_health.json()["enabled_subscriptions"] == 1

    r_inbox = client.get("/v2/research/inbox?symbol=NVDA&review_state=unread", headers=headers)
    assert r_inbox.status_code == 200, r_inbox.text
    assert r_inbox.json()["unread_count"] == 1
    assert len(r_inbox.json()["items"]) == 1

    # 5. User accepts -> upgrades ThesisLedger to v2!
    r_dec = client.post(
        f"/v2/research/inbox/{inbox_item.inbox_item_id}/decide",
        headers=headers,
        json={"action": "accept", "user_note": "Confirmed impact on cash quality"},
    )
    assert r_dec.status_code == 200, r_dec.text
    dec = r_dec.json()
    assert dec["item"]["review_state"] == "accepted"
    assert dec["item"]["resulting_ledger_version"] == 2
    assert dec["new_ledger"]["version"] == 2

    # 6. Verify ledger history & diff v1 -> v2
    r_hist = client.get(f"/v2/research/topics/{topic_id}/thesis-ledger", headers=headers)
    assert r_hist.status_code == 200, r_hist.text
    hist = r_hist.json()
    assert hist["latest_confirmed"]["version"] == 2
    assert len(hist["versions"]) == 2
    assert hist["latest_diff"]["from_version"] == 1
    assert hist["latest_diff"]["to_version"] == 2


def test_earnings_delta_stage0_and_stage1_grounding_and_quiet_filter() -> None:
    """Step B2: Verify deterministic Stage 0 diff, Stage 1 quotation grounding, and quiet no-change filtering."""
    from types import SimpleNamespace
    from app.research_delta_review import (
        build_inbox_item_from_delta_review,
        compute_delta_stage0,
        run_earnings_delta_review,
    )

    draft = build_thesis_draft_from_dossier(account_id="acc_1", topic_id="topic_nvda", dossier=_sample_dossier())
    v1 = confirm_thesis_ledger_version(account_id="acc_1", topic_id="topic_nvda", claims=draft.claims, base_draft=draft)

    stage0 = compute_delta_stage0(
        cik="0001045810",
        symbol="NVDA",
        accession="0001045810-26-000201",
        form="8-K",
        disclosed_at="2026-11-17",
        period_label="FY2027 Q3",
        ledger=v1,
        prior_snapshot={
            "flags": [{"code": "AR_GROWTH_GAP", "severity": "yellow", "summary": "AR gap 6pp"}],
            "valuation": {"reverse_dcf": {"implied_5y_fcf_cagr_pct": "18.5"}},
            "guidance_nongaap": {"new_guidance": {"revenue_b": "50.0", "gross_margin_pct": "74.5"}},
        },
        new_snapshot={
            "flags": [{"code": "AR_GROWTH_GAP", "severity": "red", "summary": "AR gap widened to 11pp"}],
            "valuation": {"reverse_dcf": {"implied_5y_fcf_cagr_pct": "24.0"}},
        },
        observed_metrics={"revenue_b": "52.5", "gross_margin_pct": "71.2"},
    )
    assert stage0.is_10q_or_10k is False
    assert stage0.reverse_dcf_delta_bps == "550"
    assert stage0.triggered_count == 1  # implied_5y_fcf_cagr_delta_bps >= 500 triggered!
    assert len(stage0.flag_drifts) == 1 and stage0.flag_drifts[0].new_severity == "red"
    assert any("10-Q/10-K" in g for g in stage0.open_gaps)
    verdicts = {r.metric_name: r.verdict for r in stage0.promise_delivery_rows}
    assert verdicts["revenue_b"] == "beat"
    assert verdicts["gross_margin_pct"] == "miss"

    filing_text = (
        "NVIDIA reported Q3 FY27 revenue of $52.5 billion, beating guidance of $50.0 billion. "
        "However, Q4 GAAP gross margin is guided to 71.2%, down 330 bps sequentially on Blackwell rack ramp costs."
    )

    class _FakeDeltaRunner:
        def invoke(self, _req: Any) -> Any:
            return SimpleNamespace(
                content=(
                    '{"overall_impact": "challenges_thesis", '
                    '"headline": "Q3 revenue beat offset by 330bp Q4 gross margin guide cut", '
                    '"promise_delivery_summary": "Revenue beat (+5.0%), gross margin missed (-330bps).", '
                    '"claim_updates": [{"claim_id": "clm_margin_1", "pillar": "margin", "old_status": "active", '
                    '"new_status": "weakened", "rationale": "Q4 gross margin guided to 71.2% below 74.0% floor.", '
                    '"proposed_statement": "Blackwell ramp compressing near-term gross margins to ~71%.", '
                    '"citations": [{"source_id": "sec:0001045810-26-000201:ex99", "accession": "0001045810-26-000201", '
                    '"quotation": "Q4 GAAP gross margin is guided to 71.2%, down 330 bps sequentially"}]}], '
                    '"new_variables": [], "open_gaps": []}'
                ),
                parsed=None,
                usage={"input_tokens": 1200, "output_tokens": 350, "total_tokens": 1550},
            )

    diff = run_earnings_delta_review(
        stage0=stage0,
        filing_text=filing_text,
        source_id="sec:0001045810-26-000201:ex99",
        ledger=v1,
        runner=_FakeDeltaRunner(),
        model_name="glm-5.3-flash",
    )
    assert diff.overall_impact == "challenges_thesis"
    assert diff.verified_quotes == 1 and diff.total_quotes == 1
    assert diff.tokens_used == 1550
    assert any("10-Q/10-K" in g for g in diff.open_gaps)

    sub = FilingWatchSubscription(account_id="acc_1", topic_id="topic_nvda", cik="0001045810", symbol="NVDA")
    inbox_item = build_inbox_item_from_delta_review(account_id="acc_1", subscription=sub, diff=diff)
    assert inbox_item is not None
    assert inbox_item.source_channel == "earnings_delta"
    assert inbox_item.impact_level == "high"
    assert "margin" in inbox_item.affected_pillars
    assert inbox_item.linked_claim_ids == ["clm_margin_1"]

    # Quiet filter: no_material_change + 0 triggers -> None (stays quiet!)
    quiet_diff = diff.model_copy(
        update={
            "overall_impact": "no_material_change",
            "stage0": stage0.model_copy(update={"triggered_count": 0}),
        }
    )
    assert build_inbox_item_from_delta_review(account_id="acc_1", subscription=sub, diff=quiet_diff) is None
