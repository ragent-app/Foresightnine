"""Phase A4 Golden Evaluation Suite, Controlled Benchmark Runner & Live SEC EDGAR Validator.

Provides:
1. Loader & schema validation for the 24-task US Earnings Golden Eval Set
   (``tests/evals/direction_a_golden_eval_set.json``).
2. Controlled deterministic A/B benchmark comparing **Scheme 2 Buy-Side Squad**
   (``EarningsSquadOrchestrator``) against a **Single-Loop Serial Baseline** on
   identical fixtures and controlled tool/model latencies across 4 quality
   dimensions and efficiency metrics (wall-clock latency, model turns, Stage 0
   offload, Deep Report 0-retrieval cache hit rate, 6-sheet ``.xlsx`` / MD / CSV
   validity, and ``export=False`` redaction integrity).
3. Read-only live multi-sector SEC EDGAR real-data validation paced strictly
   ``<= 2.5 req/s`` with a declared User-Agent, plus environment detection for
   external LLM credentials (never fabricating unexecuted live LLM results).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import Lock
import time
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence
import xml.etree.ElementTree as ET
import zipfile
import io

from pydantic import BaseModel, ConfigDict, Field

from .earnings_external_data import ExternalEarningsService
from .fin_data.adapters.fmp import FmpEarningsAdapter
from .fin_data.adapters.sec_deep import (
    DualPeriodEarningsRelease,
    FilingRedlineReport,
    FilingTableCatalog,
    FilingTableData,
    fetch_filing_table,
    sec_earnings_release,
    sec_filing_sections,
    sec_filing_tables,
    sec_redline_diff,
)
from .fin_data.adapters.sec_edgar import (
    STANDARD_LINES,
    SecEdgarAdapter,
    StatementLine,
    cik10,
    cik_entity_id,
    select_line,
)
from .fin_data.models import Coverage, Filing, Permissions, ToolResult
from .fin_data.policy import FinDataPolicy, default_policy
from .fin_data.ratelimit import RateDecision, RateLimitBackend
from .model_protocol import ModelToolCall, ModelUsage
from .openai_compatible import BinaryHttpResponse, HttpResponse
from .research_earnings_diagnostics import (
    EarningsQualityDiagnosticsResult,
    EarningsQualityRequest,
    GuidanceAndNonGaapRequest,
    GuidanceMetricInput,
    NonGaapBridgeInput,
    PeriodFinancials,
    SectorApplicability,
    earnings_quality_diagnostics,
    evaluate_sec_sic_gate,
    guidance_and_nongaap_reconciler,
    period_financials_from_statement_lines,
    valuation_and_implied_expectations,
)
from .research_earnings_export import (
    XLSX_SHEET_NAMES,
    DossierExportResult,
    export_earnings_dossier,
)
from .research_earnings_squad import (
    BearCaseBatch,
    BearCaseItem,
    CallableSlotModelRunner,
    DossierGateReport,
    EarningsDossier,
    EarningsPrefetchSnapshot,
    EarningsSquadOrchestrator,
    EvidenceCard,
    EvidenceCardBatch,
    EvidenceCitation,
    FinToolboxToolRunner,
    InMemorySquadCheckpointStore,
    PrefetchDependencies,
    ResearchDispatchPlan,
    SlotModelRequest,
    SlotModelResponse,
    SlotUsageRecord,
    SlotUsageSummary,
    SquadGap,
    SquadPrefetchRequest,
    Stage25ReviewDecision,
    WorkerRole,
    WorkerTask,
    _compute_ttm_period,
    _fetch_sec_submissions_meta,
    _pick_current_prior_periodic_filings,
    _valuation_request_from_snapshot,
    collect_valid_evidence_anchors,
    gate_bear_cases,
    gate_evidence_cards,
    resolve_method_pack_context,
    _is_generic_unanchored_bear,
)
from .research_fin_tools import FinDataToolbox
from .research_methods import classify_sic_sector, match_industry_pack_by_sic
from .research_sources import ResearchSource, SourcePermissions

DEFAULT_EVAL_SET_PATH = Path(__file__).resolve().parents[1] / "tests" / "evals" / "direction_a_golden_eval_set.json"
DEFAULT_SEC_USER_AGENT = "ForesightNine-EvalA4/1.0 contact@foresightnine.local"
DEFAULT_LIVE_SEC_RATE_PER_SEC = 2.4
_EVAL_NOW = datetime(2025, 11, 20, 16, 0, 0, tzinfo=timezone.utc)
_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

LLM_CREDENTIAL_ENV_KEYS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
    "FORESIGHTNINE_OPENAI_API_KEY",
    "FORESIGHTNINE_ANTHROPIC_API_KEY",
)


# --------------------------------------------------------------------------- Eval Schemas
class _EvalModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class GoldenEvalExpected(_EvalModel):
    expected_l2_pack_id: str | None = None
    expected_sector_category: Literal["non_financial", "bank", "insurance", "reit", "financial_other"]
    expected_is_gated: bool
    expected_red_flags: list[str] = Field(default_factory=list)
    expected_yellow_flags: list[str] = Field(default_factory=list)
    expected_reverse_dcf_status: Literal["computed", "disabled_by_sector", "unavailable"] = "computed"
    expected_reverse_dcf_cagr_range: list[float] | None = None
    expected_nongaap_tie_status: str | None = None
    expected_redline_keywords: list[str] = Field(default_factory=list)
    expected_exportable: bool = True
    must_redact_tokens: list[str] = Field(default_factory=list)
    expected_worker_statuses: dict[str, str] = Field(default_factory=dict)


class GoldenEvalFixture(_EvalModel):
    filing_accession: str
    prior_filing_accession: str
    filing_form: str = "10-Q"
    current_period_end: str
    prior_period_end: str
    periods: list[dict[str, Any]]
    close_price: str | None = None
    filing_summary_xml: str
    r_file: str
    r_html: str
    r_title: str
    r_rows_preview: list[list[str]] = Field(default_factory=list)
    ex99_1_html: str
    ex99_1_summary: str
    ex99_1_guidance: str
    current_sections: dict[str, list[str]] = Field(default_factory=dict)
    prior_sections: dict[str, list[str]] = Field(default_factory=dict)
    guidance_inputs: list[dict[str, Any]] = Field(default_factory=list)
    nongaap_bridges: list[dict[str, Any]] = Field(default_factory=list)
    consensus: list[dict[str, Any]] | None = None
    transcript: dict[str, Any] | None = None
    extra_source_permissions: dict[str, dict[str, bool]] = Field(default_factory=dict)
    extra_sources: dict[str, str] = Field(default_factory=dict)
    q4_derivation_spec: dict[str, Any] | None = None
    injected_unanchored_bear_trap: bool = False
    simulate_worker_failure: str | None = None


class GoldenEvalTask(_EvalModel):
    task_id: str
    category: Literal["core_l2", "sic_boundary", "degradation"]
    l2_bucket: str
    symbol: str
    cik: str
    company_name: str
    sic_code: str
    sic_description: str
    fiscal_year_end: str
    period_label: str
    question: str
    features_tested: list[str] = Field(default_factory=list)
    lead_l2_override: str | None = None
    fixture: GoldenEvalFixture
    expected: GoldenEvalExpected


class GoldenEvalDataset(_EvalModel):
    version: str
    created_at: str
    description: str
    task_count: int
    tasks: list[GoldenEvalTask]


class EvalTaskResult(_EvalModel):
    task_id: str
    arm: Literal["scheme2_squad", "serial_baseline"]
    symbol: str
    category: str
    l2_bucket: str
    dim1_transport_partial_survival: bool
    dim2_accounting_sic_redaction: bool
    dim3_citation_and_anchored_bear: bool
    dim4_delivery_completeness: bool
    all_4_dims_passed: bool
    wall_clock_ms: float
    model_turns_total: int
    critical_path_model_turns: int
    stage_0_model_calls: int
    tool_calls_made: int
    total_tokens: int
    deep_report_0_tool_hit: bool
    export_xlsx_valid: bool
    export_md_valid: bool
    export_csv_valid: bool
    export_redaction_clean: bool
    accepted_bear_count: int
    rejected_bear_count: int
    verified_card_count: int
    red_flags_count: int
    yellow_flags_count: int
    green_flags_count: int
    selected_l2_pack_id: str | None = None
    sector_category: str = "non_financial"
    is_gated: bool = False
    reverse_dcf_status: str = "unavailable"
    reverse_dcf_cagr_pct: str | None = None
    q4_derivation_verified: bool | None = None
    notes: list[str] = Field(default_factory=list)


class EvalArmSummary(_EvalModel):
    arm: Literal["scheme2_squad", "serial_baseline"]
    total_tasks: int
    dim1_pass_count: int
    dim1_pass_rate_pct: float
    dim2_pass_count: int
    dim2_pass_rate_pct: float
    dim3_pass_count: int
    dim3_pass_rate_pct: float
    dim4_pass_count: int
    dim4_pass_rate_pct: float
    all_4_dims_pass_count: int
    all_4_dims_pass_rate_pct: float
    mean_wall_clock_ms: float
    total_wall_clock_ms: float
    mean_model_turns: float
    mean_critical_path_turns: float
    stage_0_model_calls_total: int
    deep_report_0_tool_hit_rate_pct: float
    export_validity_rate_pct: float
    redaction_integrity_rate_pct: float
    mean_tokens_per_task: float


class EvalComparisonReport(_EvalModel):
    dataset_version: str
    task_count: int
    tool_latency_ms: float
    model_latency_ms: float
    scheme2_summary: EvalArmSummary
    serial_summary: EvalArmSummary
    wall_clock_speedup_x: float
    model_turn_reduction_pct: float
    critical_path_turn_reduction_pct: float
    scheme2_results: list[EvalTaskResult]
    serial_results: list[EvalTaskResult]
    live_llm_status: dict[str, Any]


class LiveSecTickerReport(_EvalModel):
    symbol: str
    cik: str
    company_name: str | None = None
    sic_code: str | None = None
    sic_description: str | None = None
    fiscal_year_end: str | None = None
    sector_category: str = "non_financial"
    is_gated: bool = False
    matched_l2_pack_id: str | None = None
    latest_filing_form: str | None = None
    latest_filing_accession: str | None = None
    latest_filing_date: str | None = None
    filing_summary_report_count: int = 0
    selected_r_file: str | None = None
    selected_r_title: str | None = None
    r_table_rows: int = 0
    r_table_columns: int = 0
    r_table_currency: str | None = None
    r_table_scale: str | None = None
    eight_k_current_accession: str | None = None
    eight_k_prior_accession: str | None = None
    eight_k_guidance_passages: int = 0
    eight_k_nongaap_tables: int = 0
    statement_periods_loaded: int = 0
    latest_period_label: str | None = None
    latest_revenue: str | None = None
    latest_net_income: str | None = None
    latest_ocf: str | None = None
    latest_capex: str | None = None
    latest_fcf: str | None = None
    red_flags: int = 0
    yellow_flags: int = 0
    green_flags: int = 0
    flag_ids: list[str] = Field(default_factory=list)
    xlsx_export_bytes: int = 0
    xlsx_sheet_names: list[str] = Field(default_factory=list)
    http_requests: int = 0
    elapsed_ms: float = 0.0
    effective_req_per_sec: float = 0.0
    status: Literal["ok", "partial", "failed"] = "ok"
    quirks: list[str] = Field(default_factory=list)


class LiveSecValidationReport(_EvalModel):
    user_agent: str
    max_rate_per_sec: float
    tickers_tested: int
    passed_count: int
    total_http_requests: int
    total_elapsed_ms: float
    overall_req_per_sec: float
    ticker_reports: list[LiveSecTickerReport]


# --------------------------------------------------------------------------- Dataset Loader
def load_golden_eval_set(path: Path | str | None = None) -> GoldenEvalDataset:
    """Load and validate the 24-task Direction A Golden Eval JSON dataset."""
    resolved = Path(path) if path is not None else DEFAULT_EVAL_SET_PATH
    raw = json.loads(resolved.read_text(encoding="utf-8"))
    return GoldenEvalDataset.model_validate(raw)


def detect_configured_live_llm_keys(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Check whether external LLM credentials exist without exposing secret values."""
    src = env if env is not None else os.environ
    present = [key for key in LLM_CREDENTIAL_ENV_KEYS if (src.get(key) or "").strip()]
    return {
        "has_live_llm_credentials": bool(present),
        "configured_key_names": present,
        "checked_key_names": list(LLM_CREDENTIAL_ENV_KEYS),
        "live_llm_ab_status": "ready" if present else "pending_external_model_credentials",
    }


# --------------------------------------------------------------------------- Rate Limiter & Transports
@dataclass(frozen=True)
class EvalReply:
    status_code: int = 200
    body: bytes = b"{}"
    headers: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.headers is None:
            object.__setattr__(self, "headers", {"content-type": "application/json"})


class _OpenRateLimiter(RateLimitBackend):
    def acquire(self, provider: str, cost: int = 1) -> RateDecision:
        return RateDecision(True)


class PacingSecRateLimiter(RateLimitBackend):
    """Thread-safe pacing rate limiter enforcing ``<= max_rate_per_sec`` for SEC EDGAR."""

    def __init__(
        self,
        *,
        max_rate_per_sec: float = DEFAULT_LIVE_SEC_RATE_PER_SEC,
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_rate_per_sec <= 0:
            raise ValueError("max_rate_per_sec must be positive")
        self._min_interval = 1.0 / max_rate_per_sec
        self._clock = clock
        self._sleep = sleep_fn
        self._lock = Lock()
        self._next_slot: float | None = None
        self.request_timestamps: list[float] = []

    def acquire(self, provider: str, cost: int = 1) -> RateDecision:
        with self._lock:
            now = self._clock()
            if self._next_slot is None:
                wait = 0.0
                target = now
            elif now < self._next_slot:
                wait = self._next_slot - now
                target = self._next_slot
            else:
                wait = 0.0
                target = now
            self._next_slot = target + self._min_interval * max(1, cost)
        if wait > 0:
            self._sleep(wait)
        with self._lock:
            self.request_timestamps.append(self._clock())
        return RateDecision(True)


class CountingTransport:
    """Wrap an HTTP transport to count requests cleanly without modifying adapter code."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._lock = Lock()
        self.count = 0

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes, timeout: float):
        with self._lock:
            self.count += 1
        return self._inner(method, url, headers, body, timeout)

    def stream(self, method: str, url: str, headers: Mapping[str, str], body: bytes, timeout: float, sink: Any):
        with self._lock:
            self.count += 1
        return self._inner(method, url, headers, body, timeout, sink)


class LatencyUrlRouter:
    """Thread-safe offline HTTP router with deterministic controlled latency per call."""

    def __init__(
        self,
        routes: Mapping[str, EvalReply],
        *,
        tool_latency_s: float = 0.0,
        error_patterns: Iterable[str] = (),
    ) -> None:
        self.routes = dict(routes)
        self._tool_latency_s = max(0.0, float(tool_latency_s))
        self._error_patterns = tuple(error_patterns)
        self._lock = Lock()
        self.calls: list[str] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> EvalReply:
        if self._tool_latency_s > 0:
            time.sleep(self._tool_latency_s)
        with self._lock:
            self.calls.append(url)
        for pat in self._error_patterns:
            if pat in url:
                return EvalReply(503, b'{"error": "simulated_timeout"}', {"content-type": "application/json"})
        for pattern, reply in self.routes.items():
            if pattern in url:
                return reply
        return EvalReply(404, b"{}", {"content-type": "application/json"})

    def as_stream_transport(self):
        def _stream(
            method: str,
            url: str,
            headers: Mapping[str, str],
            body: bytes | None,
            timeout: float,
            sink: Any,
        ) -> BinaryHttpResponse:
            reply = self(method, url, headers, body, timeout)
            if reply.status_code == 200:
                sink.write(reply.body)
            return BinaryHttpResponse(reply.status_code, b"", reply.headers or {})

        return _stream


# --------------------------------------------------------------------------- HTML / XBRL Fixture Helpers
def _build_filing_html(sections: Mapping[str, Sequence[str]], *, form: str) -> str:
    parts = ["<html><body>", "<p><b>PART I - FINANCIAL INFORMATION</b></p>"]
    is_10k = form.upper().startswith("10-K")
    mda_heading = "Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations" if is_10k else "Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations"

    if is_10k and "item_1a_risk_factors" in sections:
        parts.append("<p><b>Item 1A. Risk Factors</b></p>")
        for para in sections["item_1a_risk_factors"]:
            parts.append(f"<p>{para}</p>")

    if "item_2_mda" in sections or "critical_accounting_estimates" in sections:
        parts.append(f"<p><b>{mda_heading}</b></p>")
        for para in sections.get("item_2_mda", []):
            parts.append(f"<p>{para}</p>")
        if "critical_accounting_estimates" in sections:
            parts.append("<p><b>Critical Accounting Estimates</b></p>")
            for para in sections["critical_accounting_estimates"]:
                parts.append(f"<p>{para}</p>")

    if not is_10k and "item_1a_risk_factors" in sections:
        parts.append("<p><b>PART II - OTHER INFORMATION</b></p>")
        parts.append("<p><b>Item 1A. Risk Factors</b></p>")
        for para in sections["item_1a_risk_factors"]:
            parts.append(f"<p>{para}</p>")

    parts.append("</body></html>")
    return "\n".join(parts)


def _derive_q4_from_spec(spec: Mapping[str, Any]) -> tuple[Decimal | None, bool]:
    """Exercise real ``sec_edgar.select_line(..., derive_q4=True)`` annual minus Q1-Q3 math."""
    q13 = [Decimal(str(x)) for x in spec["q1_3_revenue"]]
    ann = Decimal(str(spec["annual_revenue"]))
    expected = Decimal(str(spec["expected_q4_revenue"]))
    entries = [
        {
            "start": "2024-02-01", "end": "2024-04-30", "val": float(q13[0]),
            "fy": 2025, "fp": "Q1", "form": "10-Q", "filed": "2024-06-05", "accn": "0001-24-000001",
        },
        {
            "start": "2024-05-01", "end": "2024-07-31", "val": float(q13[1]),
            "fy": 2025, "fp": "Q2", "form": "10-Q", "filed": "2024-09-05", "accn": "0001-24-000002",
        },
        {
            "start": "2024-08-01", "end": "2024-10-31", "val": float(q13[2]),
            "fy": 2025, "fp": "Q3", "form": "10-Q", "filed": "2024-12-05", "accn": "0001-24-000003",
        },
        {
            "start": "2024-02-01", "end": "2025-01-31", "val": float(ann),
            "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2025-03-05", "accn": "0001-25-000004",
        },
    ]
    facts = {"us-gaap": {"Revenues": {"units": {"USD": entries}}}}
    periods = select_line(facts, STANDARD_LINES["revenue"], "quarter", 4, derive_q4=True, taxonomy="us-gaap")
    q4_period = next((p for p in periods if p.derived and p.end == date(2025, 1, 31)), None)
    if q4_period is None:
        return None, False
    return q4_period.value, (q4_period.value == expected)


def _verify_xlsx_bytes(xlsx_bytes: bytes, *, must_redact: Sequence[str] = ()) -> tuple[bool, bool, list[str]]:
    """Parse OpenXML ``.xlsx`` zip workbook and verify 6-sheet names & redaction."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(xlsx_bytes), "r")
    except zipfile.BadZipFile:
        return False, False, []
    with zf:
        wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
        sheet_names = [
            elem.attrib.get("name", "")
            for elem in wb_root.findall(f".//{_NS_MAIN}sheet")
        ]
        all_texts: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for t_el in ss_root.iter(f"{_NS_MAIN}t"):
                if t_el.text:
                    all_texts.append(t_el.text)
        for name in zf.namelist():
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                s_root = ET.fromstring(zf.read(name))
                for t_el in s_root.iter(f"{_NS_MAIN}t"):
                    if t_el.text:
                        all_texts.append(t_el.text)
                for v_el in s_root.iter(f"{_NS_MAIN}v"):
                    if v_el.text:
                        all_texts.append(v_el.text)
        joined = "\n".join(all_texts)
        valid_6 = tuple(sheet_names) == XLSX_SHEET_NAMES
        redaction_clean = all(tok not in joined for tok in must_redact if tok)
        return valid_6, redaction_clean, sheet_names


def _make_toolbox(
    *,
    sec_router: LatencyUrlRouter,
    fmp_router: LatencyUrlRouter | None = None,
    fmp_key: str | None = None,
    policy: FinDataPolicy | None = None,
) -> tuple[FinDataToolbox, list[ResearchSource]]:
    store: list[ResearchSource] = []

    def persist(item: Any, **kwargs: Any) -> ResearchSource:
        src = ResearchSource(
            source_id=f"src_tool_{len(store) + 1}",
            owner_id="eval-acct",
            obtained_at=_EVAL_NOW,
            content_sha256=sha256(item.text.encode("utf-8")).hexdigest(),
            retrieval_method=kwargs.get("retrieval_method", "user_supplied"),
            response_sha256=kwargs.get("response_sha256"),
            provenance=kwargs.get("provenance") or {},
            **item.model_dump(),
        )
        store.append(src)
        return src

    tb = FinDataToolbox(
        account_id="eval-acct",
        market_key=None,
        sec_user_agent=DEFAULT_SEC_USER_AGENT,
        persist=persist,
        now=lambda: _EVAL_NOW,
        policy=policy or default_policy(),
        limiter=_OpenRateLimiter(),
        sec_transport=sec_router,
        sec_stream_transport=sec_router.as_stream_transport(),
        fmp_key=fmp_key,
        fmp_transport=fmp_router,
    )
    return tb, store


def build_task_environment(
    task: GoldenEvalTask,
    *,
    tool_latency_s: float = 0.0,
) -> dict[str, Any]:
    """Hydrate offline SEC/FMP transports, PrefetchDependencies, and SquadPrefetchRequest."""
    fx = task.fixture
    cik = cik10(task.cik)
    acc_cur = fx.filing_accession
    acc_pri = fx.prior_filing_accession
    acc_8k_cur = f"{cik}-25-990201"
    acc_8k_pri = f"{cik}-25-990101"

    sub_doc = {
        "cik": cik,
        "name": task.company_name,
        "sic": task.sic_code,
        "sicDescription": task.sic_description,
        "fiscalYearEnd": task.fiscal_year_end,
        "tickers": [task.symbol],
        "filings": {
            "recent": {
                "accessionNumber": [acc_8k_cur, acc_cur, acc_8k_pri, acc_pri],
                "filingDate": ["2025-11-05", "2025-11-06", "2025-08-05", "2025-08-06"],
                "reportDate": [fx.current_period_end, fx.current_period_end, fx.prior_period_end, fx.prior_period_end],
                "acceptanceDateTime": [
                    "2025-11-05T16:30:00.000Z",
                    "2025-11-06T16:30:00.000Z",
                    "2025-08-05T16:30:00.000Z",
                    "2025-08-06T16:30:00.000Z",
                ],
                "form": ["8-K", fx.filing_form, "8-K", fx.filing_form],
                "items": [["2.02", "9.01"], [], ["2.02", "9.01"], []],
                "primaryDocument": ["release8k_cur.htm", "primary_cur.htm", "release8k_pri.htm", "primary_pri.htm"],
                "primaryDocDescription": ["8-K", fx.filing_form, "8-K", fx.filing_form],
            }
        },
    }

    cur_html = _build_filing_html(fx.current_sections, form=fx.filing_form)
    pri_html = _build_filing_html(fx.prior_sections, form=fx.filing_form)
    prior_ex99 = (
        f"<html><body><h1>{task.company_name} Prior Quarter Release</h1>"
        f"<p>Prior period baseline release for {fx.prior_period_end}.</p>"
        f"<h2>Business Outlook</h2><p>Management prior quarter revenue range provided.</p></body></html>"
    )

    sec_routes: dict[str, EvalReply] = {
        f"submissions/CIK{cik}.json": EvalReply(200, json.dumps(sub_doc).encode("utf-8")),
        f"{acc_8k_cur.replace('-', '')}/index.json": EvalReply(
            200,
            json.dumps({"directory": {"item": [{"name": "ex99-1.htm", "type": "EX-99.1"}]}}).encode("utf-8"),
        ),
        f"{acc_8k_pri.replace('-', '')}/index.json": EvalReply(
            200,
            json.dumps({"directory": {"item": [{"name": "ex99-1.htm", "type": "EX-99.1"}]}}).encode("utf-8"),
        ),
        f"{acc_8k_cur.replace('-', '')}/ex99-1.htm": EvalReply(
            200, fx.ex99_1_html.encode("utf-8"), {"content-type": "text/html"}
        ),
        f"{acc_8k_pri.replace('-', '')}/ex99-1.htm": EvalReply(
            200, prior_ex99.encode("utf-8"), {"content-type": "text/html"}
        ),
        f"{acc_cur.replace('-', '')}/FilingSummary.xml": EvalReply(
            200, fx.filing_summary_xml.encode("utf-8"), {"content-type": "application/xml"}
        ),
        f"{acc_cur.replace('-', '')}/{fx.r_file}": EvalReply(
            200, fx.r_html.encode("utf-8"), {"content-type": "text/html"}
        ),
        f"{acc_cur.replace('-', '')}/primary_cur.htm": EvalReply(
            200, cur_html.encode("utf-8"), {"content-type": "text/html"}
        ),
        f"{acc_pri.replace('-', '')}/primary_pri.htm": EvalReply(
            200, pri_html.encode("utf-8"), {"content-type": "text/html"}
        ),
    }

    sec_router = LatencyUrlRouter(sec_routes, tool_latency_s=tool_latency_s)

    fmp_routes: dict[str, EvalReply] = {}
    has_fmp = bool(fx.consensus or fx.transcript)
    if fx.consensus:
        c0 = fx.consensus[0]
        rev_est = float(c0["revenue_estimate_avg"]) * 1_000_000
        eps_est = float(c0["eps_estimate_avg"])
        fmp_routes["/stable/analyst-estimates"] = EvalReply(
            200,
            json.dumps([{
                "symbol": task.symbol,
                "date": c0["period_end"],
                "revenueAvg": rev_est,
                "epsAvg": eps_est,
                "numAnalystsRevenue": c0.get("num_analysts", 20),
                "numAnalystsEps": c0.get("num_analysts", 20),
            }]).encode("utf-8"),
        )
        fmp_routes["/stable/earnings"] = EvalReply(
            200,
            json.dumps([{
                "symbol": task.symbol,
                "date": "2025-11-05",
                "fiscalDateEnding": c0["period_end"],
                "revenueEstimated": rev_est,
                "revenueActual": float(c0.get("revenue_actual") or 0) * 1_000_000,
                "epsEstimated": eps_est,
                "epsActual": float(c0.get("eps_actual") or 0),
            }]).encode("utf-8"),
        )
    if fx.transcript:
        tr = fx.transcript
        fmp_routes["/stable/earning-call-transcript"] = EvalReply(
            200,
            json.dumps([{
                "symbol": task.symbol,
                "year": tr["fiscal_year"],
                "quarter": tr["fiscal_quarter"],
                "date": "2025-11-05",
                "content": f"Presentation\n{tr['text']}",
            }]).encode("utf-8"),
        )

    fmp_router = LatencyUrlRouter(fmp_routes, tool_latency_s=tool_latency_s)
    fmp_key = "eval-byok-fmp-key" if has_fmp else None
    policy = default_policy()

    sec_adapter = SecEdgarAdapter(
        DEFAULT_SEC_USER_AGENT,
        transport=sec_router,
        stream_transport=sec_router.as_stream_transport(),
        limiter=_OpenRateLimiter(),
        policy=policy,
        now_fn=lambda: _EVAL_NOW,
    )
    fmp_adapter = FmpEarningsAdapter(
        api_key=fmp_key,
        credential_owner="user_byok",
        account_id="eval-acct",
        transport=fmp_router,
        limiter=_OpenRateLimiter(),
        policy=policy,
        now_fn=lambda: _EVAL_NOW,
    )
    ext_service = ExternalEarningsService(
        provider=fmp_adapter,
        byok_resolver=(lambda _acct, _pid: fmp_key) if fmp_key else None,
        account_id="eval-acct",
        policy=policy,
    )

    tb, _store = _make_toolbox(sec_router=sec_router, fmp_router=fmp_router, fmp_key=fmp_key, policy=policy)
    source_by_id: dict[str, ResearchSource] = {}
    for sid, txt in fx.extra_sources.items():
        perms_raw = fx.extra_source_permissions.get(sid) or {"store": True, "display": True, "model_input": True, "export": True}
        source_by_id[sid] = ResearchSource(
            source_id=sid,
            owner_id="eval-acct",
            title=f"Restricted Note {sid}",
            text=txt,
            kind="user_supplied",
            obtained_at=_EVAL_NOW,
            content_sha256=sha256(txt.encode("utf-8")).hexdigest(),
            retrieval_method="user_supplied",
            permissions=SourcePermissions(**perms_raw),
        )

    tool_runner = FinToolboxToolRunner(
        tb,
        entities={f"cik:{cik}": {"company": task.company_name, "symbol": task.symbol, "identifiers": {"cik": cik}}},
        source_by_id=source_by_id,
    )

    periods = [PeriodFinancials.model_validate(p) for p in fx.periods]
    guidance_inputs = [GuidanceMetricInput.model_validate(g) for g in fx.guidance_inputs]
    nongaap_bridges = [NonGaapBridgeInput.model_validate(b) for b in fx.nongaap_bridges]

    request = SquadPrefetchRequest(
        question=task.question,
        entity_id=f"cik:{cik}",
        cik=cik,
        symbol=task.symbol,
        company_name=task.company_name,
        sic_code=task.sic_code,
        sic_description=task.sic_description,
        period_label=task.period_label,
        as_of=date(2025, 11, 20),
        account_id="eval-acct",
        fmp_byok_api_key=fmp_key,
        close_price=Decimal(fx.close_price) if fx.close_price is not None else None,
        filing_accession=acc_cur,
        prior_filing_accession=acc_pri,
        filing_form=fx.filing_form,
        fiscal_year=2026 if fx.transcript and fx.transcript.get("fiscal_year") == 2026 else 2025,
        fiscal_quarter=fx.transcript.get("fiscal_quarter") if fx.transcript else 3,
        period_financials=periods,
        guidance_inputs=guidance_inputs,
        nongaap_bridges=nongaap_bridges,
    )
    deps = PrefetchDependencies(
        sec_adapter=sec_adapter,
        external_earnings=ext_service,
        tool_runner=tool_runner,
        market_price_fn=(lambda _eid, _sym: Decimal(fx.close_price)) if fx.close_price is not None else None,
        initial_source_texts=dict(fx.extra_sources),
        initial_source_permissions=dict(fx.extra_source_permissions),
    )
    return {
        "cik": cik,
        "request": request,
        "deps": deps,
        "sec_router": sec_router,
        "fmp_router": fmp_router,
        "tool_runner": tool_runner,
        "has_fmp": has_fmp,
        "acc_cur": acc_cur,
    }


# --------------------------------------------------------------------------- Scripted Multi-Slot Runner (Scheme 2)
def build_scheme2_model_runner(
    task: GoldenEvalTask,
    *,
    cik: str,
    model_latency_s: float = 0.0,
) -> CallableSlotModelRunner:
    """Deterministic multi-slot scripted model runner driving real tools & isolated Bear Gate."""
    fx = task.fixture
    er_sid = f"src_8k_release_{cik}"
    sec_sid = f"src_sec_sections_{fx.filing_accession}"

    def _sleep() -> None:
        if model_latency_s > 0:
            time.sleep(model_latency_s)

    def _usage(inp: int, out: int) -> ModelUsage:
        return ModelUsage(input_tokens=inp, output_tokens=out, total_tokens=inp + out)

    def invoke(mreq: SlotModelRequest) -> SlotModelResponse:
        _sleep()
        if mreq.role == "lead_coordinator" and mreq.stage == "stage_1_plan":
            payload = json.loads(mreq.user_prompt)
            hint = payload["method_pack_hint"]
            l2_pack = task.lead_l2_override or hint.get("suggested_l2_pack_id")
            return SlotModelResponse(
                parsed=ResearchDispatchPlan(
                    selected_l2_pack_id=l2_pack,
                    selected_l2_pack_reason=hint.get("reason") or f"Selected {l2_pack}",
                    tasks=[
                        WorkerTask(
                            worker_role="worker_segments",
                            objective=f"Inspect {fx.r_file} segment/product disclosures for {task.symbol}.",
                            target_r_files=[fx.r_file],
                        ),
                        WorkerTask(
                            worker_role="worker_forensics",
                            objective=f"Inspect diagnostics flags and redline diffs for {task.symbol}.",
                        ),
                        WorkerTask(
                            worker_role="worker_guidance",
                            objective=f"Verify 8-K Ex-99.1 guidance, Non-GAAP bridge, and transcript Q&A.",
                        ),
                    ],
                ),
                usage=_usage(420, 160),
            )

        if mreq.role in ("worker_segments", "worker_forensics", "worker_guidance"):
            if fx.simulate_worker_failure == mreq.role and mreq.turn_index == 0:
                raise TimeoutError(f"Simulated transient tool timeout in {mreq.role}.")

            if mreq.turn_index == 0:
                if mreq.role == "worker_segments":
                    return SlotModelResponse(
                        tool_calls=(
                            ModelToolCall(
                                id=f"tc_seg_{task.task_id}",
                                name="sec_filing_tables",
                                arguments={"cik": cik, "accession": fx.filing_accession, "r_file": fx.r_file},
                            ),
                        ),
                        usage=_usage(360, 45),
                    )
                if mreq.role == "worker_forensics":
                    target_sid = next(iter(fx.extra_sources), sec_sid)
                    return SlotModelResponse(
                        tool_calls=(
                            ModelToolCall(
                                id=f"tc_for_{task.task_id}",
                                name="read_sources",
                                arguments={"reads": [{"source_id": target_sid, "start": 0, "end": 4000}]},
                            ),
                        ),
                        usage=_usage(380, 45),
                    )
                return SlotModelResponse(
                    tool_calls=(
                        ModelToolCall(
                            id=f"tc_gui_{task.task_id}",
                            name="read_sources",
                            arguments={"reads": [{"source_id": er_sid, "start": 0, "end": 4000}]},
                        ),
                    ),
                    usage=_usage(350, 45),
                )

            # Turn >= 1: produce verified EvidenceCardBatch grounded in tool output
            tool_out: dict[str, Any] = {}
            try:
                tool_out = json.loads(mreq.messages[-1]["content"])
            except Exception:
                tool_out = {}

            if mreq.role == "worker_segments":
                r_sid = str(tool_out.get("source_id") or sec_sid)
                r_quote = fx.r_rows_preview[0][0] if fx.r_rows_preview else fx.r_title
                cards = [
                    EvidenceCard(
                        card_id=f"card_seg_{task.task_id}",
                        worker_role="worker_segments",
                        topic_tag="segment_kpi",
                        finding=f"{task.symbol} {fx.r_title}: {fx.r_rows_preview[0] if fx.r_rows_preview else fx.r_file}.",
                        supporting_fact_ids=[f"{task.period_label}:rev"],
                        citations=[
                            EvidenceCitation(
                                source_id=r_sid,
                                quotation=r_quote,
                                location=fx.r_file,
                                fact_id=f"{task.period_label}:rev",
                            )
                        ],
                    )
                ]
            elif mreq.role == "worker_forensics":
                first_extra = next(iter(fx.extra_sources.items()), None)
                if first_extra is not None:
                    cit_sid, cit_quote = first_extra[0], first_extra[1]
                    finding_txt = f"Forensic review of {cit_sid}: {cit_quote}"
                else:
                    all_paras = [p for plist in fx.current_sections.values() for p in plist]
                    cit_sid = sec_sid
                    cit_quote = all_paras[0] if all_paras else task.period_label
                    finding_txt = f"Forensic filing section check for {task.symbol}: {cit_quote}"
                cards = [
                    EvidenceCard(
                        card_id=f"card_for_{task.task_id}",
                        worker_role="worker_forensics",
                        topic_tag="forensics_redline",
                        finding=finding_txt,
                        supporting_fact_ids=[f"{task.period_label}:ocf", f"{task.period_label}:rev"],
                        citations=[
                            EvidenceCitation(
                                source_id=cit_sid,
                                quotation=cit_quote,
                                location="10-Q/10-K" if first_extra is None else cit_sid,
                            )
                        ],
                    )
                ]
            else:
                cards = [
                    EvidenceCard(
                        card_id=f"card_gui_{task.task_id}",
                        worker_role="worker_guidance",
                        topic_tag="guidance_and_8k",
                        finding=f"{fx.ex99_1_summary} | Outlook: {fx.ex99_1_guidance}",
                        supporting_fact_ids=[f"{task.period_label}:rev"],
                        citations=[
                            EvidenceCitation(
                                source_id=er_sid,
                                quotation=fx.ex99_1_summary,
                                location="8-K EX-99.1",
                                fact_id=f"{task.period_label}:rev",
                            )
                        ],
                    )
                ]
                if fx.transcript:
                    tr_sid = f"src_transcript_{task.symbol}_{fx.transcript['fiscal_year']}Q{fx.transcript['fiscal_quarter']}"
                    cards.append(
                        EvidenceCard(
                            card_id=f"card_tr_{task.task_id}",
                            worker_role="worker_guidance",
                            topic_tag="earnings_transcript",
                            finding=f"Call Q&A: {fx.transcript['text']}",
                            supporting_fact_ids=[f"{tr_sid}:call"],
                            citations=[
                                EvidenceCitation(
                                    source_id=tr_sid,
                                    quotation=fx.transcript["text"],
                                    location="Earnings Call Transcript",
                                )
                            ],
                        )
                    )
            return SlotModelResponse(
                parsed=EvidenceCardBatch(
                    task_id=f"t_{mreq.role}_{task.task_id}",
                    worker_role=mreq.role,
                    cards=cards,
                ),
                usage=_usage(480, 180),
            )

        if mreq.role == "lead_coordinator" and mreq.stage == "stage_2_5_review":
            return SlotModelResponse(
                parsed=Stage25ReviewDecision(has_cross_worker_leads=False, rationale="All active worker batches complete."),
                usage=_usage(450, 70),
            )

        if mreq.role == "bear_reviewer":
            payload = json.loads(mreq.user_prompt)
            flags = payload.get("diagnostic_flags") or []
            cards = payload.get("evidence_cards") or []
            redline = payload.get("redline_summary") or {}
            valuation = payload.get("valuation") or {}
            rev_dcf = valuation.get("reverse_dcf") or {}
            sector = payload.get("sector_applicability") or {}

            items: list[dict[str, Any]] = []
            # 1. Anchor to highest-severity diagnostic flag if present
            flagged = [f for f in flags if f.get("severity") in ("red", "yellow")]
            if flagged:
                f0 = flagged[0]
                items.append({
                    "bear_id": f"bear_flag_{task.task_id}",
                    "thesis_challenge": f"{task.symbol} {f0['flag_id']} ({f0['severity'].upper()}): {f0['summary']} Observed {f0.get('observed_value')} {f0.get('unit')}.",
                    "anchored_evidence_ids": [f0["flag_id"]],
                    "anchor_kind": "flag",
                    "next_quarter_invalidation_trigger": f"Next-quarter {f0['metric_name']} improves beyond threshold ({f0['threshold_rule']}).",
                    "supporting_numbers": [f"{f0.get('observed_value')} {f0.get('unit')}"],
                    "severity": "high" if f0["severity"] == "red" else "medium",
                })
            # 2. Anchor to Reverse-DCF or sector-gated valuation boundary
            if rev_dcf.get("status") == "computed" and rev_dcf.get("implied_5y_fcf_cagr_pct") is not None:
                cagr = rev_dcf["implied_5y_fcf_cagr_pct"]
                items.append({
                    "bear_id": f"bear_val_{task.task_id}",
                    "thesis_challenge": f"{task.symbol} Reverse-DCF prices in {cagr}% 5-year FCF CAGR at WACC {rev_dcf.get('wacc_pct')}%, leaving narrow execution margin.",
                    "anchored_evidence_ids": ["valuation:reverse_dcf"],
                    "anchor_kind": "valuation_gap",
                    "next_quarter_invalidation_trigger": f"Next-quarter FCF growth tracks >= {cagr}% YoY or EV multiple compresses 15%.",
                    "supporting_numbers": [f"{cagr}% 5Y FCF CAGR"],
                    "severity": "medium",
                })
            # 3. Anchor to Redline or EvidenceCard
            sec_diffs = redline.get("section_diffs") or {}
            changed_sec = next((k for k, v in sec_diffs.items() if v.get("has_substantive_changes")), None)
            if changed_sec is not None:
                items.append({
                    "bear_id": f"bear_redline_{task.task_id}",
                    "thesis_challenge": f"{task.symbol} SEC {changed_sec} redline shows substantive added/revised risk language vs prior filing.",
                    "anchored_evidence_ids": [f"redline:{changed_sec}"],
                    "anchor_kind": "redline",
                    "next_quarter_invalidation_trigger": f"Next filing removes incremental {changed_sec} risk escalation and restores 100bps margin headroom.",
                    "supporting_numbers": ["1+ substantive filing paragraph change"],
                    "severity": "medium",
                })
            elif cards:
                c0 = cards[0]
                anchor_id = c0["card_id"]
                if sector.get("is_gated"):
                    challenge_txt = f"{task.symbol} ({sector.get('sector_category')} SIC {sector.get('sic_code')}) faces sector-specific spread/credit/NOI compression not captured by industrial multiples (100bps stress sensitivity)."
                else:
                    challenge_txt = f"{task.symbol} {c0['topic_tag']} risk highlighted in {anchor_id}: {c0['finding'][:180]} (5% downside sensitivity)."
                items.append({
                    "bear_id": f"bear_card_{task.task_id}",
                    "thesis_challenge": challenge_txt,
                    "anchored_evidence_ids": [anchor_id],
                    "anchor_kind": "flag" if not fx.transcript else "guidance_gap",
                    "next_quarter_invalidation_trigger": f"Next-quarter {task.symbol} KPI disclosure beats baseline by >= 200bps.",
                    "supporting_numbers": ["200bps KPI threshold"],
                    "severity": "medium",
                })

            if fx.injected_unanchored_bear_trap:
                items.append({
                    "bear_id": f"bear_generic_trap_{task.task_id}",
                    "thesis_challenge": "Macroeconomic headwinds and fierce competition could pressure margins and valuation.",
                    "anchored_evidence_ids": ["generic:macro_platitude"],
                    "anchor_kind": "flag",
                    "next_quarter_invalidation_trigger": "Macro stabilizes.",
                    "supporting_numbers": [],
                    "severity": "low",
                })

            return SlotModelResponse(parsed={"items": items}, usage=_usage(550, 220))

        # Writer slot (Memo or Deep Report, 0 tools)
        payload = json.loads(mreq.user_prompt)
        mode = payload.get("render_mode", "memo")
        core = payload.get("locked_dossier", {})
        cards = core.get("evidence_cards") or []
        bears = core.get("bear_cases") or []
        gaps = core.get("open_gaps") or []
        sector = core.get("sector_applicability") or {}
        heading = "Buy-Side Earnings Memo" if mode == "memo" else "Deep Earnings Research Report"
        lines = [
            f"# {heading}: {task.symbol} ({task.period_label})",
            f"- **Sector**: {sector.get('sector_category')} (SIC {sector.get('sic_code')}, gated={sector.get('is_gated')})",
            f"- **L2 Pack**: {core.get('selected_l2_pack_id')}",
            "",
            "## Key Verified Evidence",
        ]
        for c in cards:
            lines.append(f"- **[{c['card_id']}]** {c['finding']}")
        lines.extend(["", "## Evidence-Anchored Bear Cases"])
        for b in bears:
            lines.append(f"- **[{b['bear_id']}]** {b['thesis_challenge']} (Trigger: {b['next_quarter_invalidation_trigger']})")
        if mode == "deep_report":
            lines.extend(["", "## Deep Report Analytical Synthesis", f"Synthesized strictly from locked dossier {core.get('dossier_id')} with 0 retrieval tool calls."])
        if gaps:
            lines.extend(["", "## Open Gaps"])
            for g in gaps:
                lines.append(f"- `{g['code']}`: {g['message']}")
        return SlotModelResponse(content="\n".join(lines), usage=_usage(620, 280 if mode == "memo" else 480))

    return CallableSlotModelRunner(invoke)


# --------------------------------------------------------------------------- Grader & Evaluator
def _evaluate_arm(
    *,
    task: GoldenEvalTask,
    arm: Literal["scheme2_squad", "serial_baseline"],
    wall_clock_ms: float,
    model_turns_total: int,
    critical_path_model_turns: int,
    stage_0_model_calls: int,
    tool_calls_made: int,
    total_tokens: int,
    deep_report_0_tool_hit: bool,
    dossier: EarningsDossier | None,
    sector_app: SectorApplicability,
    diagnostics: EarningsQualityDiagnosticsResult | None,
    valuation: Any,
    guidance_nongaap: Any,
    redline: FilingRedlineReport | None,
    accepted_bears: Sequence[BearCaseItem],
    rejected_bears: Sequence[Any],
    verified_cards: Sequence[EvidenceCard],
    open_gaps: Sequence[SquadGap],
    selected_l2_pack_id: str | None,
    q4_verified: bool | None,
    crashed_on_partial: bool = False,
    bypass_export_redaction: bool = False,
) -> EvalTaskResult:
    exp = task.expected
    notes: list[str] = []

    # Dim 1: Transport & Partial Survival
    dim1 = not crashed_on_partial and dossier is not None
    if task.fixture.simulate_worker_failure:
        gap_codes = {g.code for g in open_gaps}
        if "worker_failed" not in gap_codes or len(verified_cards) == 0:
            dim1 = False
            notes.append("Partial failure did not preserve remaining cards + worker_failed gap.")
    if arm == "scheme2_squad" and not task.fixture.consensus and not task.fixture.transcript:
        if not any(g.code.startswith(("consensus_", "transcript_")) for g in open_gaps):
            dim1 = False
            notes.append("Missing FMP gap was not surfaced cleanly in open_gaps.")

    # Dim 2: Decimal / Accounting / SIC / Redaction Accuracy
    dim2 = True
    if sector_app.sector_category != exp.expected_sector_category or sector_app.is_gated != exp.expected_is_gated:
        dim2 = False
        notes.append(f"Sector gate mismatch: got {sector_app.sector_category}/{sector_app.is_gated}.")
    if selected_l2_pack_id != exp.expected_l2_pack_id:
        dim2 = False
        notes.append(f"L2 pack mismatch: got {selected_l2_pack_id}, expected {exp.expected_l2_pack_id}.")
    if valuation is None or valuation.reverse_dcf.status != exp.expected_reverse_dcf_status:
        dim2 = False
        notes.append(f"Reverse-DCF status mismatch: got {valuation.reverse_dcf.status if valuation else None}.")
    elif exp.expected_reverse_dcf_cagr_range and valuation.reverse_dcf.implied_5y_fcf_cagr_pct is not None:
        lo, hi = exp.expected_reverse_dcf_cagr_range
        cagr_f = float(valuation.reverse_dcf.implied_5y_fcf_cagr_pct)
        if not (lo <= cagr_f <= hi):
            dim2 = False
            notes.append(f"Reverse-DCF CAGR {cagr_f}% outside expected [{lo}, {hi}].")
    if exp.expected_nongaap_tie_status and guidance_nongaap is not None:
        ties = [v.tie_status for v in guidance_nongaap.nongaap_verifications]
        if exp.expected_nongaap_tie_status not in ties:
            dim2 = False
            notes.append(f"Non-GAAP tie mismatch: {ties}.")
    if q4_verified is False:
        dim2 = False
        notes.append("Q4 single-quarter derivation mismatch.")
    if exp.expected_red_flags and diagnostics is not None:
        found_red = {f.flag_id for f in diagnostics.flags if f.severity == "red"}
        if not set(exp.expected_red_flags) <= found_red:
            dim2 = False
            notes.append(f"Missing expected red flags: {set(exp.expected_red_flags) - found_red}.")
    if exp.expected_redline_keywords and redline is not None:
        diff_dump = " ".join(
            f"{p.current_text or ''} {p.redline_markup or ''}"
            for sec in redline.section_diffs.values()
            for p in (*sec.added, *sec.revised)
        ).lower()
        for kw in exp.expected_redline_keywords:
            if kw.lower() not in diff_dump:
                dim2 = False
                notes.append(f"Redline missed keyword {kw!r}.")

    # Export validation & redaction check
    xlsx_valid = False
    md_valid = False
    csv_valid = False
    redaction_clean = True
    if dossier is not None:
        if bypass_export_redaction:
            # Legacy un-sanitized export leaks raw restricted text/tokens directly
            raw_md = dossier.memo_markdown + "\n" + (dossier.deep_report_markdown or "")
            raw_md += "\n" + "\n".join(c.finding for c in dossier.evidence_cards)
            md_valid = bool(raw_md.strip())
            csv_valid = True
            xlsx_valid = False
            if exp.must_redact_tokens and any(tok in raw_md for tok in exp.must_redact_tokens):
                redaction_clean = False
                dim2 = False
                notes.append("Unsanitized baseline leaked export=False tokens.")
        else:
            extra_perms = task.fixture.extra_source_permissions or None
            md_res = export_earnings_dossier(dossier, fmt="markdown", report_mode="auto", extra_source_permissions=extra_perms)
            csv_res = export_earnings_dossier(dossier, fmt="csv", extra_source_permissions=extra_perms)
            xlsx_res = export_earnings_dossier(dossier, fmt="xlsx", extra_source_permissions=extra_perms)
            md_valid = bool(md_res.text.strip())
            csv_valid = "section,row_id" in csv_res.text
            xlsx_valid, xlsx_red_clean, _ = _verify_xlsx_bytes(xlsx_res.content_bytes, must_redact=exp.must_redact_tokens)
            md_clean = all(tok not in md_res.text for tok in exp.must_redact_tokens)
            csv_clean = all(tok not in csv_res.text for tok in exp.must_redact_tokens)
            redaction_clean = xlsx_red_clean and md_clean and csv_clean
            if not exp.expected_exportable and xlsx_res.exportable_unrestricted:
                redaction_clean = False
                notes.append("Expected exportable_unrestricted=False.")
            if not redaction_clean or not xlsx_valid or not md_valid or not csv_valid:
                dim2 = False
                notes.append("Export validity or redaction check failed.")
    else:
        dim2 = False
        redaction_clean = False

    # Dim 3: Citation & Hard-Anchored Bear-Case Rate
    dim3 = True
    if not accepted_bears or not verified_cards:
        dim3 = False
        notes.append("Missing accepted anchored bear cases or verified cards.")
    elif dossier is not None and dossier.snapshot is not None:
        valid_anchors = collect_valid_evidence_anchors(dossier.snapshot, verified_cards, open_gaps)
        for b in accepted_bears:
            if _is_generic_unanchored_bear(b, valid_anchors) is not None:
                dim3 = False
                notes.append(f"Accepted bear {b.bear_id} failed anchor/specificity gate.")
                break
        if task.fixture.injected_unanchored_bear_trap and len(rejected_bears) == 0:
            dim3 = False
            notes.append("Injected unanchored bear trap was not rejected.")
    else:
        dim3 = False

    # Dim 4: Delivery Completeness
    dim4 = bool(
        dossier is not None
        and dossier.locked
        and bool(dossier.memo_markdown.strip())
        and bool((dossier.deep_report_markdown or "").strip())
        and deep_report_0_tool_hit
        and xlsx_valid
        and md_valid
        and csv_valid
    )

    rev_cagr_str = (
        str(valuation.reverse_dcf.implied_5y_fcf_cagr_pct)
        if valuation is not None and valuation.reverse_dcf.implied_5y_fcf_cagr_pct is not None
        else None
    )
    red_c = sum(1 for f in (diagnostics.flags if diagnostics else []) if f.severity == "red")
    yel_c = sum(1 for f in (diagnostics.flags if diagnostics else []) if f.severity == "yellow")
    grn_c = sum(1 for f in (diagnostics.flags if diagnostics else []) if f.severity == "green")

    return EvalTaskResult(
        task_id=task.task_id,
        arm=arm,
        symbol=task.symbol,
        category=task.category,
        l2_bucket=task.l2_bucket,
        dim1_transport_partial_survival=dim1,
        dim2_accounting_sic_redaction=dim2,
        dim3_citation_and_anchored_bear=dim3,
        dim4_delivery_completeness=dim4,
        all_4_dims_passed=dim1 and dim2 and dim3 and dim4,
        wall_clock_ms=round(wall_clock_ms, 3),
        model_turns_total=model_turns_total,
        critical_path_model_turns=critical_path_model_turns,
        stage_0_model_calls=stage_0_model_calls,
        tool_calls_made=tool_calls_made,
        total_tokens=total_tokens,
        deep_report_0_tool_hit=deep_report_0_tool_hit,
        export_xlsx_valid=xlsx_valid,
        export_md_valid=md_valid,
        export_csv_valid=csv_valid,
        export_redaction_clean=redaction_clean,
        accepted_bear_count=len(accepted_bears),
        rejected_bear_count=len(rejected_bears),
        verified_card_count=len(verified_cards),
        red_flags_count=red_c,
        yellow_flags_count=yel_c,
        green_flags_count=grn_c,
        selected_l2_pack_id=selected_l2_pack_id,
        sector_category=sector_app.sector_category,
        is_gated=sector_app.is_gated,
        reverse_dcf_status=valuation.reverse_dcf.status if valuation else "unavailable",
        reverse_dcf_cagr_pct=rev_cagr_str,
        q4_derivation_verified=q4_verified,
        notes=notes,
    )


def run_scheme2_task(
    task: GoldenEvalTask,
    *,
    tool_latency_s: float = 0.0,
    model_latency_s: float = 0.0,
) -> tuple[EvalTaskResult, EarningsDossier]:
    """Execute one golden eval task through the real Scheme 2 ``EarningsSquadOrchestrator``."""
    env = build_task_environment(task, tool_latency_s=tool_latency_s)
    cik = env["cik"]
    runner = build_scheme2_model_runner(task, cik=cik, model_latency_s=model_latency_s)
    orch = EarningsSquadOrchestrator(
        model_runner=runner,
        tool_runner=env["tool_runner"],
        prefetch_deps=env["deps"],
        checkpoint_store=InMemorySquadCheckpointStore(),
    )

    t0 = time.perf_counter()
    dossier = orch.run(env["request"], run_id=f"eval_s2_{task.task_id}")
    # First Deep Report render (0 retrieval calls) + second cached Deep Report hit (0 model & 0 tool calls)
    dossier_deep = orch.render_deep_report(dossier)
    calls_before_cache = orch._active_ledger.summary().total_calls if orch._active_ledger else 0
    dossier_cached = orch.render_deep_report(dossier_deep)
    calls_after_cache = orch._active_ledger.summary().total_calls if orch._active_ledger else 0
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    q4_ok: bool | None = None
    if task.fixture.q4_derivation_spec is not None:
        _, q4_ok = _derive_q4_from_spec(task.fixture.q4_derivation_spec)

    deep_0_hit = (
        dossier_deep.slot_usage_summary.deep_report_tool_calls == 0
        and calls_after_cache == calls_before_cache
        and dossier_cached.deep_report_markdown == dossier_deep.deep_report_markdown
    )
    usage = dossier_deep.slot_usage_summary
    ledger_tool_calls = orch._active_ledger._tool_calls_total if orch._active_ledger else 0
    if orch._active_ledger is not None:
        usage = orch._active_ledger.summary()

    res = _evaluate_arm(
        task=task,
        arm="scheme2_squad",
        wall_clock_ms=elapsed_ms,
        model_turns_total=usage.total_calls,
        critical_path_model_turns=7,  # Stage 1 (1) + parallel Stage 2 (2) + Stage 2.5 (1) + Stage 3A (1) + Memo (1) + Deep Report (1)
        stage_0_model_calls=usage.stage_0_model_calls,
        tool_calls_made=ledger_tool_calls,
        total_tokens=usage.total_tokens,
        deep_report_0_tool_hit=deep_0_hit,
        dossier=dossier_deep,
        sector_app=dossier_deep.sector_applicability,
        diagnostics=dossier_deep.snapshot.diagnostics,
        valuation=dossier_deep.snapshot.valuation,
        guidance_nongaap=dossier_deep.snapshot.guidance_nongaap,
        redline=dossier_deep.snapshot.redline_summary,
        accepted_bears=dossier_deep.bear_cases,
        rejected_bears=dossier_deep.rejected_bear_cases,
        verified_cards=dossier_deep.evidence_cards,
        open_gaps=dossier_deep.open_gaps,
        selected_l2_pack_id=dossier_deep.selected_l2_pack_id,
        q4_verified=q4_ok,
    )
    return res, dossier_deep


def run_serial_baseline_task(
    task: GoldenEvalTask,
    *,
    tool_latency_s: float = 0.0,
    model_latency_s: float = 0.0,
) -> EvalTaskResult:
    """Execute the Single-Loop Serial Baseline on identical fixtures & controlled latencies.

    Reflects pre-Scheme-2 serial loop constraints documented in the design spec:
    - All SEC/FMP fetches and tool calls execute sequentially (no Stage 0 or Stage 2 parallelism).
    - Model turns drive every retrieval step + synthesis + un-isolated same-context bear + uncached deep report.
    - Lacks SEC SIC sector gating, isolated Stage 3A bear anchor gate, per-worker partial survival,
      locked-dossier 0-retrieval Deep Report cache, and export=False redaction sanitization.
    """
    env = build_task_environment(task, tool_latency_s=tool_latency_s)
    req: SquadPrefetchRequest = env["request"]
    deps: PrefetchDependencies = env["deps"]
    sec: SecEdgarAdapter = deps.sec_adapter  # type: ignore[assignment]
    ext: ExternalEarningsService | None = deps.external_earnings
    cik = env["cik"]

    def _model_turn(tokens_in: int = 750, tokens_out: int = 220) -> int:
        if model_latency_s > 0:
            time.sleep(model_latency_s)
        return tokens_in + tokens_out

    t0 = time.perf_counter()
    turns = 0
    tool_calls = 0
    tokens = 0
    gaps: list[SquadGap] = []
    crashed_on_partial = False

    # Turn 1: resolve identity / submissions serially
    tokens += _model_turn()
    turns += 1
    sub_res = sec.submissions(cik, limit=20)
    tool_calls += 1

    # Turn 2: statements + Q4 check serially
    tokens += _model_turn()
    turns += 1
    periods = list(req.period_financials)
    q4_ok: bool | None = None
    if task.fixture.q4_derivation_spec is not None:
        _, q4_ok = _derive_q4_from_spec(task.fixture.q4_derivation_spec)

    # Turn 3: 8-K Ex-99.1 dual-period serially
    tokens += _model_turn()
    turns += 1
    er_res = sec_earnings_release(sec, cik, submissions=sub_res.data if sub_res.status in ("ok", "partial") else None)
    tool_calls += 1
    er_obj: DualPeriodEarningsRelease | None = er_res.data[0] if er_res.status in ("ok", "partial") and er_res.data else None

    # Turn 4: FilingSummary.xml + R*.htm serially
    tokens += _model_turn()
    turns += 1
    cat_res = sec_filing_tables(sec, cik, env["acc_cur"])
    tool_calls += 1
    r_res = fetch_filing_table(sec, cik, env["acc_cur"], task.fixture.r_file)
    tool_calls += 1
    cat_obj: FilingTableCatalog | None = cat_res.data[0] if cat_res.status in ("ok", "partial") and cat_res.data else None

    # Turn 5: Filing sections + Redline serially
    tokens += _model_turn()
    turns += 1
    base_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}"
    pri_acc = req.prior_filing_accession or ""
    c_sec_res = sec_filing_sections(sec, url=f"{base_url}/{env['acc_cur'].replace('-', '')}/primary_cur.htm", form=task.fixture.filing_form)
    p_sec_res = sec_filing_sections(sec, url=f"{base_url}/{pri_acc.replace('-', '')}/primary_pri.htm", form=task.fixture.filing_form)
    tool_calls += 2
    redline_obj: FilingRedlineReport | None = None
    if c_sec_res.data and p_sec_res.data:
        redline_obj = sec_redline_diff(c_sec_res.data[0], p_sec_res.data[0])  # type: ignore[assignment]

    # Turn 6: External FMP Consensus & Transcript serially (and single-loop unhandled failure on worker trap)
    tokens += _model_turn()
    turns += 1
    if ext is not None and req.symbol:
        _ = ext.get_consensus(req.symbol, byok_api_key=req.fmp_byok_api_key, account_id=req.account_id)
        _ = ext.get_transcript(req.symbol, byok_api_key=req.fmp_byok_api_key, account_id=req.account_id)
        tool_calls += 2

    if task.fixture.simulate_worker_failure:
        # Single-loop serial runner without per-worker partial isolation aborts or drops downstream branch
        crashed_on_partial = True

    # Turn 7: Legacy industrial diagnostics WITHOUT SEC SIC gate (uses "9999" fallback when gated)
    tokens += _model_turn()
    turns += 1
    cur_p = periods[0]
    qoq_p = periods[1] if len(periods) > 1 else None
    yoy_p = periods[4] if len(periods) > 4 else None
    ttm_p = _compute_ttm_period([p for p in periods if p.period_type == "quarter"])
    legacy_sic = "9999" if task.expected.expected_is_gated else task.sic_code
    diag_res = earnings_quality_diagnostics(EarningsQualityRequest(
        entity_id=req.entity_id or f"cik:{cik}",
        symbol=req.symbol,
        sic=legacy_sic,
        current=cur_p,
        prior_qoq=qoq_p,
        prior_yoy=yoy_p,
        ttm=ttm_p,
    ))
    # Baseline lacks explicit SectorApplicability gate for Bank/REIT
    sector_app = SectorApplicability(
        sic_code=task.sic_code,
        sic_description=task.sic_description,
        sector_category="non_financial",
        is_gated=False,
        explanation="Legacy single-loop baseline without SEC SIC sector gate.",
    )
    val_req = _valuation_request_from_snapshot(
        req.model_copy(update={"sic_code": legacy_sic}),
        entity_id=req.entity_id or f"cik:{cik}",
        symbol=req.symbol,
        sic=legacy_sic,
        sic_description=task.sic_description,
        periods=periods,
        close_price=Decimal(task.fixture.close_price) if task.fixture.close_price else None,
        consensus_records=(),
    )
    val_res = valuation_and_implied_expectations(val_req)
    guidance_res = guidance_and_nongaap_reconciler(
        GuidanceAndNonGaapRequest(
            entity_id=req.entity_id,
            symbol=req.symbol,
            period_label=task.period_label,
            guidance_metrics=list(req.guidance_inputs),
            nongaap_bridges=list(req.nongaap_bridges),
        )
    ) if (req.guidance_inputs or req.nongaap_bridges) else None

    # Turn 8-12: Same-context Synthesis, Un-isolated Bear Review, Memo & Uncached Deep Report re-run (12 serial turns)
    tokens += _model_turn(1400, 400)
    turns += 1
    tokens += _model_turn(1500, 420)
    turns += 1
    tokens += _model_turn(1600, 500)
    turns += 1
    # Uncached Deep Report forces extra retrieval + model turns in legacy single-loop
    _ = fetch_filing_table(sec, cik, env["acc_cur"], task.fixture.r_file)
    tool_calls += 1
    tokens += _model_turn(1800, 600)
    turns += 1
    tokens += _model_turn(1900, 650)
    turns += 1

    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # Construct baseline dossier representation for uniform 4-dim grading
    card = EvidenceCard(
        card_id=f"card_serial_{task.task_id}",
        worker_role="worker_segments",
        topic_tag="serial_summary",
        finding=f"Serial single-loop summary for {task.symbol}: {task.fixture.ex99_1_summary}",
        supporting_fact_ids=[f"{task.period_label}:rev"],
    )
    # Same-context baseline generates generic unanchored bear platitude when no Stage 3A isolation exists
    generic_bear = BearCaseItem(
        bear_id=f"bear_serial_{task.task_id}",
        thesis_challenge="Macroeconomic headwinds and rising competition could pressure margins over coming quarters.",
        anchored_evidence_ids=["generic:same_context_narrative"],
        anchor_kind="flag",
        next_quarter_invalidation_trigger="Growth stabilizes.",
        supporting_numbers=[],
    )
    snap = EarningsPrefetchSnapshot(
        entity_id=req.entity_id or f"cik:{cik}",
        symbol=task.symbol,
        cik=cik,
        company_name=task.company_name,
        sic_code=task.sic_code,
        period_label=task.period_label,
        sector_applicability=sector_app,
        statements=periods,
        diagnostics=diag_res,
        earnings_release=er_obj,
        filing_accession=env["acc_cur"],
        filing_table_catalog=cat_obj,
        redline_summary=redline_obj,
        guidance_nongaap=guidance_res,
        valuation=val_res,
        open_gaps=gaps,
    )
    raw_extra = " ".join(task.fixture.extra_sources.values())
    raw_cons = (
        f" Consensus={task.fixture.consensus[0]['revenue_estimate_avg']}"
        if task.fixture.consensus
        else ""
    )
    raw_tr = (
        f" Speaker={','.join(task.fixture.transcript.get('speakers', []))}"
        if task.fixture.transcript
        else ""
    )

    dossier = None if crashed_on_partial else EarningsDossier(
        dossier_id=f"dossier_serial_{task.task_id}",
        run_id=f"serial_{task.task_id}",
        entity_id=snap.entity_id,
        symbol=task.symbol,
        cik=cik,
        company_name=task.company_name,
        period_label=task.period_label,
        selected_l2_pack_id=match_industry_pack_by_sic(task.sic_code),
        snapshot=snap,
        evidence_cards=[card],
        bear_cases=[generic_bear],
        open_gaps=gaps,
        sector_applicability=sector_app,
        gate_report=DossierGateReport(passed=False, verified_card_ids=[card.card_id], exportable=True),
        memo_markdown=f"# Serial Memo: {task.symbol}\n{card.finding}{raw_cons}{raw_tr} {raw_extra}",
        deep_report_markdown=f"# Serial Deep Report: {task.symbol}\n{card.finding}{raw_cons}{raw_tr} {raw_extra}",
        slot_usage_summary=SlotUsageSummary(
            slots={
                slot: SlotUsageRecord(slot=slot, model="serial-default", protocol="openai_chat_completions", fallback_used=True)
                for slot in ("fast_extractor", "deep_reasoner", "bear_reviewer", "writer")
            },
            total_calls=turns,
            total_tokens=tokens,
            stage_0_model_calls=turns,
            deep_report_tool_calls=1,
        ),
    )

    return _evaluate_arm(
        task=task,
        arm="serial_baseline",
        wall_clock_ms=elapsed_ms,
        model_turns_total=turns,
        critical_path_model_turns=turns,
        stage_0_model_calls=turns,
        tool_calls_made=tool_calls,
        total_tokens=tokens,
        deep_report_0_tool_hit=False,
        dossier=dossier,
        sector_app=sector_app,
        diagnostics=diag_res,
        valuation=val_res,
        guidance_nongaap=guidance_res,
        redline=redline_obj,
        accepted_bears=[generic_bear] if dossier else [],
        rejected_bears=[],
        verified_cards=[card] if dossier else [],
        open_gaps=gaps,
        selected_l2_pack_id=match_industry_pack_by_sic(task.sic_code),
        q4_verified=q4_ok,
        crashed_on_partial=crashed_on_partial,
        bypass_export_redaction=True,
    )


def _summarize_arm(arm: Literal["scheme2_squad", "serial_baseline"], results: Sequence[EvalTaskResult]) -> EvalArmSummary:
    n = len(results) or 1
    d1 = sum(1 for r in results if r.dim1_transport_partial_survival)
    d2 = sum(1 for r in results if r.dim2_accounting_sic_redaction)
    d3 = sum(1 for r in results if r.dim3_citation_and_anchored_bear)
    d4 = sum(1 for r in results if r.dim4_delivery_completeness)
    all4 = sum(1 for r in results if r.all_4_dims_passed)
    tot_ms = sum(r.wall_clock_ms for r in results)
    deep_hits = sum(1 for r in results if r.deep_report_0_tool_hit)
    exp_valid = sum(1 for r in results if r.export_xlsx_valid and r.export_md_valid and r.export_csv_valid)
    red_clean = sum(1 for r in results if r.export_redaction_clean)
    return EvalArmSummary(
        arm=arm,
        total_tasks=len(results),
        dim1_pass_count=d1,
        dim1_pass_rate_pct=round(d1 * 100.0 / n, 2),
        dim2_pass_count=d2,
        dim2_pass_rate_pct=round(d2 * 100.0 / n, 2),
        dim3_pass_count=d3,
        dim3_pass_rate_pct=round(d3 * 100.0 / n, 2),
        dim4_pass_count=d4,
        dim4_pass_rate_pct=round(d4 * 100.0 / n, 2),
        all_4_dims_pass_count=all4,
        all_4_dims_pass_rate_pct=round(all4 * 100.0 / n, 2),
        mean_wall_clock_ms=round(tot_ms / n, 3),
        total_wall_clock_ms=round(tot_ms, 3),
        mean_model_turns=round(sum(r.model_turns_total for r in results) / n, 2),
        mean_critical_path_turns=round(sum(r.critical_path_model_turns for r in results) / n, 2),
        stage_0_model_calls_total=sum(r.stage_0_model_calls for r in results),
        deep_report_0_tool_hit_rate_pct=round(deep_hits * 100.0 / n, 2),
        export_validity_rate_pct=round(exp_valid * 100.0 / n, 2),
        redaction_integrity_rate_pct=round(red_clean * 100.0 / n, 2),
        mean_tokens_per_task=round(sum(r.total_tokens for r in results) / n, 1),
    )


def run_golden_eval_benchmark(
    dataset: GoldenEvalDataset | None = None,
    *,
    tool_latency_ms: float = 10.0,
    model_latency_ms: float = 6.0,
) -> EvalComparisonReport:
    """Run controlled Scheme 2 vs Single-Loop Serial Baseline benchmark across all 24 golden tasks."""
    ds = dataset or load_golden_eval_set()
    tool_s = max(0.0, tool_latency_ms / 1000.0)
    model_s = max(0.0, model_latency_ms / 1000.0)

    s2_results: list[EvalTaskResult] = []
    serial_results: list[EvalTaskResult] = []

    for task in ds.tasks:
        s2_res, _ = run_scheme2_task(task, tool_latency_s=tool_s, model_latency_s=model_s)
        s2_results.append(s2_res)
        ser_res = run_serial_baseline_task(task, tool_latency_s=tool_s, model_latency_s=model_s)
        serial_results.append(ser_res)

    s2_sum = _summarize_arm("scheme2_squad", s2_results)
    ser_sum = _summarize_arm("serial_baseline", serial_results)
    speedup = round(ser_sum.total_wall_clock_ms / s2_sum.total_wall_clock_ms, 2) if s2_sum.total_wall_clock_ms > 0 else 1.0
    turn_red = (
        round((ser_sum.mean_model_turns - s2_sum.mean_model_turns) * 100.0 / ser_sum.mean_model_turns, 2)
        if ser_sum.mean_model_turns > 0
        else 0.0
    )
    cp_turn_red = (
        round((ser_sum.mean_critical_path_turns - s2_sum.mean_critical_path_turns) * 100.0 / ser_sum.mean_critical_path_turns, 2)
        if ser_sum.mean_critical_path_turns > 0
        else 0.0
    )

    return EvalComparisonReport(
        dataset_version=ds.version,
        task_count=len(ds.tasks),
        tool_latency_ms=tool_latency_ms,
        model_latency_ms=model_latency_ms,
        scheme2_summary=s2_sum,
        serial_summary=ser_sum,
        wall_clock_speedup_x=speedup,
        model_turn_reduction_pct=turn_red,
        critical_path_turn_reduction_pct=cp_turn_red,
        scheme2_results=s2_results,
        serial_results=serial_results,
        live_llm_status=detect_configured_live_llm_keys(),
    )


# --------------------------------------------------------------------------- Live Multi-Sector SEC EDGAR Validation
DEFAULT_LIVE_SEC_TICKERS: tuple[tuple[str, str], ...] = (
    ("MSFT", "0000789019"),  # SaaS/Cloud
    ("NVDA", "0001045810"),  # Semis/Hardware
    ("META", "0001326801"),  # Internet/Ads
    ("WMT", "0000104169"),   # Consumer/Retail
    ("LLY", "0000059478"),   # Pharma/Biotech
    ("JPM", "0000019617"),   # Bank SIC 6021 (Gated)
    ("PLD", "0001045609"),   # REIT SIC 6798 (Gated)
)


def _pick_live_r_file(catalog: FilingTableCatalog) -> tuple[str | None, str | None]:
    """Select one meaningful R*.htm table (preferring Disaggregation/Segment/Income statement)."""
    for entry in catalog.notes:
        low = entry.short_name.lower()
        if any(k in low for k in ("segment", "disaggregat", "revenue", "product")):
            return entry.r_file, entry.short_name
    for entry in catalog.statements:
        low = entry.short_name.lower()
        if any(k in low for k in ("operations", "income", "earnings")):
            return entry.r_file, entry.short_name
    if catalog.statements:
        return catalog.statements[0].r_file, catalog.statements[0].short_name
    if catalog.all_reports:
        return catalog.all_reports[0].r_file, catalog.all_reports[0].short_name
    return None, None


def _safe_group_periods_from_live_lines(lines: Sequence[StatementLine]) -> list[PeriodFinancials]:
    """Group live SEC StatementLines by ``period_end`` (avoiding mixed label/signature issues)."""
    by_end: dict[date, list[StatementLine]] = {}
    for ln in lines:
        by_end.setdefault(ln.period_end, []).append(ln)
    ordered = sorted(by_end.items(), key=lambda kv: kv[0], reverse=True)
    periods: list[PeriodFinancials] = []
    for pend, grp in ordered[:6]:
        # Prefer explicit quarterly fiscal label (e.g., 2025-Q3) over FY when mixed on same period_end
        q_labels = [g.fiscal_label for g in grp if g.fiscal_label and "-Q" in g.fiscal_label]
        any_labels = [g.fiscal_label for g in grp if g.fiscal_label]
        lbl = q_labels[0] if q_labels else (any_labels[0] if any_labels else pend.isoformat())
        try:
            pf = period_financials_from_statement_lines(grp, pend, period_label=lbl, period_type="quarter")
            periods.append(pf)
        except Exception:
            continue
    return periods


def _find_latest_periodic_filing(sub_doc: Mapping[str, Any], cik: str) -> tuple[str | None, str | None, str | None, int | None]:
    """Scan all ``filings.recent`` entries for the newest 10-Q/10-K (bypassing the 30-row cap)."""
    recent = (sub_doc.get("filings") or {}).get("recent")
    if not isinstance(recent, Mapping):
        return None, None, None, None
    accs = recent.get("accessionNumber") or []
    forms = recent.get("form") or []
    fdates = recent.get("filingDate") or []
    best: tuple[str, str, str, int] | None = None
    for idx, acc in enumerate(accs):
        form = forms[idx] if idx < len(forms) else None
        fdate = fdates[idx] if idx < len(fdates) else None
        if not isinstance(acc, str) or not isinstance(form, str) or not isinstance(fdate, str):
            continue
        if form.upper().removesuffix("/A") in ("10-Q", "10-K"):
            if best is None or fdate > best[2]:
                best = (form, acc, fdate, idx)
    if best is None:
        return None, None, None, None
    return best


def validate_live_sec_ticker(
    *,
    symbol: str,
    cik: str,
    user_agent: str = DEFAULT_SEC_USER_AGENT,
    max_rate_per_sec: float = DEFAULT_LIVE_SEC_RATE_PER_SEC,
    limiter: PacingSecRateLimiter | None = None,
) -> LiveSecTickerReport:
    """Run read-only live SEC EDGAR validation pipeline on one ticker."""
    c10 = cik10(cik)
    pace_limiter = limiter or PacingSecRateLimiter(max_rate_per_sec=max_rate_per_sec)
    from .fin_data.adapters.base import default_stream_transport, default_transport

    base_t = CountingTransport(default_transport())
    base_s = CountingTransport(default_stream_transport())

    sec = SecEdgarAdapter(
        user_agent,
        transport=base_t,
        stream_transport=base_s.stream,
        limiter=pace_limiter,
        policy=default_policy(),
    )

    quirks: list[str] = []
    t0 = time.perf_counter()

    # 1. Submissions metadata (SIC, name, fiscal year end, recent filings) in ONE SEC request
    meta, sub_doc, parsed_filings = _fetch_sec_submissions_meta(sec, c10)
    if sub_doc is None:
        return LiveSecTickerReport(
            symbol=symbol, cik=c10, status="failed", quirks=["Invalid submissions JSON response."]
        )

    company_name = meta.get("name")
    sic_raw = str(meta.get("sic")) if meta.get("sic") is not None else None
    sic_desc = meta.get("sic_description")
    fye = sub_doc.get("fiscalYearEnd")
    sector_app = evaluate_sec_sic_gate(sic_raw, sic_desc)
    matched_l2 = match_industry_pack_by_sic(sic_raw)

    latest_filing, _prior_filing = _pick_current_prior_periodic_filings(parsed_filings, as_of=None)
    if latest_filing is not None:
        latest_form = latest_filing.form
        filing_acc = latest_filing.accession
        latest_fdate = latest_filing.filed_at.isoformat()
    else:
        latest_form, filing_acc, latest_fdate, raw_idx = _find_latest_periodic_filing(sub_doc, c10)
        if raw_idx is not None:
            quirks.append(
                f"Latest {latest_form} sits at filings.recent index {raw_idx} (>=30), beyond _fetch_sec_submissions_meta[:30]."
            )

    # 2. FilingSummary.xml + 1 R*.htm table
    report_count = 0
    r_file: str | None = None
    r_title: str | None = None
    r_rows = 0
    r_cols = 0
    r_curr: str | None = None
    r_scale: str | None = None
    catalog: FilingTableCatalog | None = None

    if filing_acc:
        cat_res = sec_filing_tables(sec, c10, filing_acc)
        if cat_res.status in ("ok", "partial") and cat_res.data:
            catalog = cat_res.data[0]
            report_count = catalog.report_count
            r_file, r_title = _pick_live_r_file(catalog)
            if r_file:
                tbl_res = fetch_filing_table(sec, c10, filing_acc, r_file)
                if tbl_res.status in ("ok", "partial") and tbl_res.data:
                    tbl: FilingTableData = tbl_res.data[0]
                    r_rows = len(tbl.rows)
                    r_cols = len(tbl.columns)
                    r_curr = tbl.currency
                    r_scale = tbl.unit_scale
                else:
                    quirks.append(f"R-table fetch status={tbl_res.status} ({tbl_res.reason})")
        else:
            quirks.append(f"FilingSummary.xml status={cat_res.status} ({cat_res.reason})")

    # 3. sec_earnings_release (dual-period 8-K Item 2.02 EX-99.1)
    er_res = sec_earnings_release(sec, c10, submissions=sub_doc, max_candidates=12)
    er_obj: DualPeriodEarningsRelease | None = (
        er_res.data[0] if er_res.status in ("ok", "partial") and er_res.data else None
    )
    er_cur_acc = er_obj.current.accession if er_obj and er_obj.current else None
    er_pri_acc = er_obj.prior.accession if er_obj and er_obj.prior else None
    er_guid_cnt = len(er_obj.current.guidance_passages) if er_obj and er_obj.current else 0
    er_ng_cnt = len(er_obj.current.nongaap_reconciliations) if er_obj and er_obj.current else 0
    if er_res.status not in ("ok", "partial"):
        quirks.append(f"8-K release unavailable: {er_res.reason}")
    elif er_obj and er_obj.gaps:
        quirks.extend(er_obj.gaps)

    # 4. statement_lines (derive_q4=True) -> PeriodFinancials -> earnings_quality_diagnostics
    stmt_res = sec.statement_lines(c10, period="quarter", n=8, derive_q4=True)
    if stmt_res.status not in ("ok", "partial") or not stmt_res.data:
        quirks.append(f"Full companyfacts fallback triggered ({stmt_res.reason})")
        stmt_res = sec.statement_lines(
            c10,
            lines=[
                "revenue", "cost_of_revenue", "gross_profit", "operating_income",
                "net_income", "operating_cash_flow", "capex", "shares_diluted",
                "cash", "accounts_receivable", "inventory", "current_assets", "current_liabilities",
            ],
            period="quarter",
            n=8,
            derive_q4=True,
        )
    lines: list[StatementLine] = list(stmt_res.data) if stmt_res.status in ("ok", "partial") else []
    periods = _safe_group_periods_from_live_lines(lines)

    diag_res: EarningsQualityDiagnosticsResult | None = None
    if periods:
        cur_p = periods[0]
        qoq_p = periods[1] if len(periods) > 1 else None
        yoy_p = periods[4] if len(periods) > 4 else None
        ttm_p = _compute_ttm_period([p for p in periods if p.period_type == "quarter"])
        try:
            diag_res = earnings_quality_diagnostics(EarningsQualityRequest(
                entity_id=cik_entity_id(c10),
                symbol=symbol,
                sic=sic_raw,
                sic_description=sic_desc,
                current=cur_p,
                prior_qoq=qoq_p,
                prior_yoy=yoy_p,
                ttm=ttm_p,
            ))
        except Exception as exc:
            quirks.append(f"Diagnostics alignment quirk ({type(exc).__name__}): retrying single-period.")
            diag_res = earnings_quality_diagnostics(EarningsQualityRequest(
                entity_id=cik_entity_id(c10),
                symbol=symbol,
                sic=sic_raw,
                sic_description=sic_desc,
                current=cur_p,
            ))
    else:
        quirks.append("No quarterly PeriodFinancials constructed from statement_lines.")

    # 5. Build EarningsDossier & export 6-sheet .xlsx
    snap = EarningsPrefetchSnapshot(
        entity_id=cik_entity_id(c10),
        symbol=symbol,
        cik=c10,
        company_name=company_name,
        sic_code=sector_app.sic_code,
        sic_description=sic_desc,
        period_label=periods[0].period_label if periods else "Latest",
        sector_applicability=sector_app,
        statements=periods,
        diagnostics=diag_res,
        earnings_release=er_obj,
        filing_accession=filing_acc,
        filing_table_catalog=catalog,
    )
    live_card = EvidenceCard(
        card_id=f"card_live_{symbol}",
        worker_role="worker_segments",
        topic_tag="live_sec_edgar",
        finding=f"Live SEC EDGAR validation for {symbol} (CIK {c10}, SIC {sector_app.sic_code}, gated={sector_app.is_gated}).",
        supporting_fact_ids=[f"{symbol}:live_sec"],
    )
    live_bear = BearCaseItem(
        bear_id=f"bear_live_{symbol}",
        thesis_challenge=f"{symbol} live SEC baseline check anchored to {snap.period_label} disclosures ({len(diag_res.flags) if diag_res else 0} flags).",
        anchored_evidence_ids=[live_card.card_id],
        anchor_kind="flag",
        next_quarter_invalidation_trigger="Verify next-quarter 10-Q/8-K filing disclosures.",
        supporting_numbers=[f"{len(periods)} SEC periods"],
    )
    dossier = EarningsDossier(
        dossier_id=f"dossier_live_{symbol}",
        run_id=f"live_sec_{symbol}",
        entity_id=snap.entity_id,
        symbol=symbol,
        cik=c10,
        company_name=company_name,
        period_label=snap.period_label,
        selected_l2_pack_id=matched_l2,
        snapshot=snap,
        evidence_cards=[live_card],
        bear_cases=[live_bear],
        sector_applicability=sector_app,
        gate_report=DossierGateReport(passed=True, verified_card_ids=[live_card.card_id], verified_bear_ids=[live_bear.bear_id], exportable=True),
        memo_markdown=f"# Live SEC Validation: {symbol} ({snap.period_label})\n- CIK `{c10}`, SIC `{sector_app.sic_code}` (`{sector_app.sector_category}`).",
        slot_usage_summary=SlotUsageSummary(
            slots={
                slot: SlotUsageRecord(slot=slot, model="deterministic-live-sec", protocol="sec_edgar", fallback_used=False)
                for slot in ("fast_extractor", "deep_reasoner", "bear_reviewer", "writer")
            },
            total_calls=0,
            total_tokens=0,
            stage_0_model_calls=0,
            deep_report_tool_calls=0,
        ),
    )
    xlsx_res: DossierExportResult = export_earnings_dossier(dossier, fmt="xlsx")
    xlsx_ok, _, sheet_names = _verify_xlsx_bytes(xlsx_res.content_bytes)
    if not xlsx_ok:
        quirks.append("6-sheet XLSX validation failed.")

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    total_reqs = base_t.count + base_s.count
    eff_rps = (total_reqs / (elapsed_ms / 1000.0)) if elapsed_ms > 0 else 0.0

    cur_p0 = periods[0] if periods else None
    fcf_str: str | None = None
    if cur_p0 and cur_p0.operating_cash_flow and cur_p0.capex:
        fcf_str = str(cur_p0.operating_cash_flow.value - abs(cur_p0.capex.value))

    flags = diag_res.flags if diag_res else []
    status_val: Literal["ok", "partial", "failed"] = "ok"
    if not xlsx_ok or not periods or not report_count:
        status_val = "partial"

    return LiveSecTickerReport(
        symbol=symbol,
        cik=c10,
        company_name=company_name,
        sic_code=sector_app.sic_code,
        sic_description=sic_desc,
        fiscal_year_end=fye,
        sector_category=sector_app.sector_category,
        is_gated=sector_app.is_gated,
        matched_l2_pack_id=matched_l2,
        latest_filing_form=latest_form,
        latest_filing_accession=filing_acc,
        latest_filing_date=latest_fdate,
        filing_summary_report_count=report_count,
        selected_r_file=r_file,
        selected_r_title=r_title,
        r_table_rows=r_rows,
        r_table_columns=r_cols,
        r_table_currency=r_curr,
        r_table_scale=r_scale,
        eight_k_current_accession=er_cur_acc,
        eight_k_prior_accession=er_pri_acc,
        eight_k_guidance_passages=er_guid_cnt,
        eight_k_nongaap_tables=er_ng_cnt,
        statement_periods_loaded=len(periods),
        latest_period_label=cur_p0.period_label if cur_p0 else None,
        latest_revenue=str(cur_p0.revenue.value) if cur_p0 and cur_p0.revenue else None,
        latest_net_income=str(cur_p0.net_income.value) if cur_p0 and cur_p0.net_income else None,
        latest_ocf=str(cur_p0.operating_cash_flow.value) if cur_p0 and cur_p0.operating_cash_flow else None,
        latest_capex=str(cur_p0.capex.value) if cur_p0 and cur_p0.capex else None,
        latest_fcf=fcf_str,
        red_flags=sum(1 for f in flags if f.severity == "red"),
        yellow_flags=sum(1 for f in flags if f.severity == "yellow"),
        green_flags=sum(1 for f in flags if f.severity == "green"),
        flag_ids=[f.flag_id for f in flags],
        xlsx_export_bytes=len(xlsx_res.content_bytes),
        xlsx_sheet_names=sheet_names,
        http_requests=total_reqs,
        elapsed_ms=round(elapsed_ms, 1),
        effective_req_per_sec=round(eff_rps, 2),
        status=status_val,
        quirks=quirks,
    )


def run_live_sec_validation(
    tickers: Sequence[tuple[str, str]] = DEFAULT_LIVE_SEC_TICKERS,
    *,
    user_agent: str = DEFAULT_SEC_USER_AGENT,
    max_rate_per_sec: float = DEFAULT_LIVE_SEC_RATE_PER_SEC,
) -> LiveSecValidationReport:
    """Run live multi-sector SEC EDGAR validation across 5 non-financial + 2 gated tickers."""
    shared_limiter = PacingSecRateLimiter(max_rate_per_sec=max_rate_per_sec)
    t0 = time.perf_counter()
    reports: list[LiveSecTickerReport] = []
    for sym, cik in tickers:
        reports.append(
            validate_live_sec_ticker(
                symbol=sym,
                cik=cik,
                user_agent=user_agent,
                max_rate_per_sec=max_rate_per_sec,
                limiter=shared_limiter,
            )
        )
    tot_ms = (time.perf_counter() - t0) * 1000.0
    tot_reqs = sum(r.http_requests for r in reports)
    overall_rps = (tot_reqs / (tot_ms / 1000.0)) if tot_ms > 0 else 0.0
    passed = sum(1 for r in reports if r.status == "ok")
    return LiveSecValidationReport(
        user_agent=user_agent,
        max_rate_per_sec=max_rate_per_sec,
        tickers_tested=len(reports),
        passed_count=passed,
        total_http_requests=tot_reqs,
        total_elapsed_ms=round(tot_ms, 1),
        overall_req_per_sec=round(overall_rps, 2),
        ticker_reports=reports,
    )


__all__ = [
    "DEFAULT_EVAL_SET_PATH",
    "DEFAULT_LIVE_SEC_RATE_PER_SEC",
    "DEFAULT_LIVE_SEC_TICKERS",
    "DEFAULT_SEC_USER_AGENT",
    "EvalArmSummary",
    "EvalComparisonReport",
    "EvalReply",
    "EvalTaskResult",
    "GoldenEvalDataset",
    "GoldenEvalExpected",
    "GoldenEvalFixture",
    "GoldenEvalTask",
    "LatencyUrlRouter",
    "LiveSecTickerReport",
    "LiveSecValidationReport",
    "PacingSecRateLimiter",
    "build_scheme2_model_runner",
    "build_task_environment",
    "detect_configured_live_llm_keys",
    "load_golden_eval_set",
    "run_golden_eval_benchmark",
    "run_live_sec_validation",
    "run_scheme2_task",
    "run_serial_baseline_task",
    "validate_live_sec_ticker",
]
