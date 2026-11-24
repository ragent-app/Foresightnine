"""Phase A4 Golden Evaluation Suite, Controlled A/B Benchmark & Rate-Pacing Tests."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.research_earnings_eval import (
    DEFAULT_LIVE_SEC_TICKERS,
    PacingSecRateLimiter,
    _derive_q4_from_spec,
    _verify_xlsx_bytes,
    detect_configured_live_llm_keys,
    load_golden_eval_set,
    run_golden_eval_benchmark,
    run_live_sec_validation,
    run_scheme2_task,
)
from app.research_earnings_export import XLSX_SHEET_NAMES, export_earnings_dossier


def test_golden_eval_dataset_structure_and_coverage() -> None:
    ds = load_golden_eval_set()
    assert ds.task_count == 24
    assert len(ds.tasks) == 24

    core = [t for t in ds.tasks if t.category == "core_l2"]
    boundaries = [t for t in ds.tasks if t.category == "sic_boundary"]
    degradations = [t for t in ds.tasks if t.category == "degradation"]

    assert len(core) == 20
    assert len(boundaries) == 2
    assert len(degradations) == 2

    buckets: dict[str, list[str]] = {}
    for t in core:
        buckets.setdefault(t.l2_bucket, []).append(t.symbol)

    assert set(buckets.keys()) == {
        "kpi_saas_cloud",
        "kpi_semis_hardware",
        "kpi_internet_ads",
        "kpi_consumer_retail",
        "kpi_pharma_biotech",
    }
    assert set(buckets["kpi_saas_cloud"]) == {"MSFT", "CRM", "NOW", "SNOW"}
    assert set(buckets["kpi_semis_hardware"]) == {"NVDA", "AAPL", "AMD", "AVGO"}
    assert set(buckets["kpi_internet_ads"]) == {"GOOGL", "META", "AMZN", "NFLX"}
    assert set(buckets["kpi_consumer_retail"]) == {"WMT", "COST", "NKE", "SBUX"}
    assert set(buckets["kpi_pharma_biotech"]) == {"LLY", "JNJ", "MRK", "VRTX"}

    assert {t.symbol for t in boundaries} == {"JPM", "PLD"}
    assert any("partial_failure" in t.features_tested for t in degradations)
    assert any("export_redaction" in t.features_tested for t in degradations)


def test_q4_derivation_across_non_calendar_tasks() -> None:
    ds = load_golden_eval_set()
    q4_tasks = [t for t in ds.tasks if t.fixture.q4_derivation_spec is not None]
    assert {t.symbol for t in q4_tasks} == {"SNOW", "NVDA", "AVGO", "WMT"}
    for t in q4_tasks:
        derived, ok = _derive_q4_from_spec(t.fixture.q4_derivation_spec or {})
        assert ok is True, f"Q4 derivation mismatch for {t.task_id}: got {derived}"


def test_unanchored_bear_trap_rejected_by_server_gate() -> None:
    ds = load_golden_eval_set()
    snow_task = next(t for t in ds.tasks if t.task_id == "saas_04_snow")
    res, dossier = run_scheme2_task(snow_task)
    assert res.dim3_citation_and_anchored_bear is True
    assert res.rejected_bear_count >= 1
    assert res.accepted_bear_count >= 1
    assert any(r.bear_id.startswith("bear_generic_trap_") for r in dossier.rejected_bear_cases)
    assert all(not b.bear_id.startswith("bear_generic_trap_") for b in dossier.bear_cases)


def test_financial_and_reit_sic_gates_disable_industrial_metrics() -> None:
    ds = load_golden_eval_set()
    jpm_task = next(t for t in ds.tasks if t.task_id == "boundary_01_jpm")
    pld_task = next(t for t in ds.tasks if t.task_id == "boundary_02_pld")

    for task, expected_cat in ((jpm_task, "bank"), (pld_task, "reit")):
        res, dossier = run_scheme2_task(task)
        assert res.all_4_dims_passed is True
        assert dossier.sector_applicability.is_gated is True
        assert dossier.sector_applicability.sector_category == expected_cat
        assert dossier.selected_l2_pack_id is None
        assert dossier.snapshot.valuation is not None
        assert dossier.snapshot.valuation.reverse_dcf.status == "disabled_by_sector"
        xlsx = export_earnings_dossier(dossier, fmt="xlsx")
        ok, clean, sheet_names = _verify_xlsx_bytes(xlsx.content_bytes)
        assert ok is True and clean is True
        assert tuple(sheet_names) == XLSX_SHEET_NAMES


def test_partial_failure_survival_and_export_false_redaction() -> None:
    ds = load_golden_eval_set()
    part_task = next(t for t in ds.tasks if t.task_id == "degrade_01_fmp_missing_worker_fail")
    part_res, part_dossier = run_scheme2_task(part_task)
    assert part_res.all_4_dims_passed is True
    gap_codes = {g.code for g in part_dossier.open_gaps}
    assert "worker_failed" in gap_codes
    assert any(c.worker_role == "worker_segments" for c in part_dossier.evidence_cards)
    assert any(c.worker_role == "worker_forensics" for c in part_dossier.evidence_cards)

    red_task = next(t for t in ds.tasks if t.task_id == "degrade_02_export_false_redaction")
    red_res, red_dossier = run_scheme2_task(red_task)
    assert red_res.all_4_dims_passed is True
    assert red_dossier.gate_report.exportable is False
    extra_perms = red_task.fixture.extra_source_permissions
    for fmt in ("markdown", "csv", "xlsx"):
        exp = export_earnings_dossier(red_dossier, fmt=fmt, extra_source_permissions=extra_perms)
        assert exp.exportable_unrestricted is False
        if fmt == "xlsx":
            ok, clean, _ = _verify_xlsx_bytes(
                exp.content_bytes, must_redact=red_task.expected.must_redact_tokens
            )
            assert ok is True and clean is True
        else:
            for tok in red_task.expected.must_redact_tokens:
                assert tok not in exp.text


def test_controlled_benchmark_scheme2_beats_serial_baseline() -> None:
    report = run_golden_eval_benchmark(tool_latency_ms=15.0, model_latency_ms=15.0)
    s2 = report.scheme2_summary
    ser = report.serial_summary

    assert report.task_count == 24
    assert s2.dim1_pass_count == 24
    assert s2.dim2_pass_count == 24
    assert s2.dim3_pass_count == 24
    assert s2.dim4_pass_count == 24
    assert s2.all_4_dims_pass_count == 24
    assert s2.stage_0_model_calls_total == 0
    assert s2.deep_report_0_tool_hit_rate_pct == 100.0
    assert s2.export_validity_rate_pct == 100.0
    assert s2.redaction_integrity_rate_pct == 100.0

    # Controlled efficiency & quality comparison vs Single-Loop Serial Baseline
    assert report.wall_clock_speedup_x > 1.2
    assert report.critical_path_turn_reduction_pct > 30.0
    assert ser.stage_0_model_calls_total > 0
    assert ser.deep_report_0_tool_hit_rate_pct == 0.0
    assert ser.all_4_dims_pass_count < s2.all_4_dims_pass_count


def test_pacing_sec_rate_limiter_enforces_rate_cap() -> None:
    fake_now = [0.0]
    sleeps: list[float] = []

    def fake_clock() -> float:
        return fake_now[0]

    def fake_sleep(dt: float) -> None:
        sleeps.append(dt)
        fake_now[0] += dt

    limiter = PacingSecRateLimiter(max_rate_per_sec=2.5, clock=fake_clock, sleep_fn=fake_sleep)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _i: limiter.acquire("sec_edgar"), range(6)))

    assert len(limiter.request_timestamps) == 6
    span = limiter.request_timestamps[-1] - limiter.request_timestamps[0]
    # 6 requests spaced at 0.4s intervals -> >= 2.0s span (<= 2.5 req/s)
    assert span >= 1.99
    assert sum(sleeps) >= 1.99


def test_live_llm_key_detection_never_exposes_secrets() -> None:
    fake_env = {"OPENAI_API_KEY": "sk-secret-do-not-leak-value"}
    status = detect_configured_live_llm_keys(fake_env)
    assert status["has_live_llm_credentials"] is True
    assert status["configured_key_names"] == ["OPENAI_API_KEY"]
    assert "sk-secret" not in str(status)

    empty_status = detect_configured_live_llm_keys({})
    assert empty_status["has_live_llm_credentials"] is False
    assert empty_status["live_llm_ab_status"] == "pending_external_model_credentials"


@pytest.mark.skipif(
    os.environ.get("FORESIGHTNINE_RUN_LIVE_SEC_EVAL") != "1",
    reason="Set FORESIGHTNINE_RUN_LIVE_SEC_EVAL=1 to execute live SEC EDGAR network validation.",
)
def test_live_multi_sector_sec_edgar_validation() -> None:
    live_report = run_live_sec_validation(DEFAULT_LIVE_SEC_TICKERS)
    assert live_report.tickers_tested == 7
    assert live_report.overall_req_per_sec <= 2.5
    assert live_report.passed_count >= 5
    by_sym = {r.symbol: r for r in live_report.ticker_reports}
    assert by_sym["JPM"].is_gated is True
    assert by_sym["JPM"].sector_category == "bank"
    assert by_sym["PLD"].is_gated is True
    assert by_sym["PLD"].sector_category == "reit"
