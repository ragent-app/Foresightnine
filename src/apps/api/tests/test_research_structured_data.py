"""Unit tests for unified structured observations registry (_ADAPTERS) across SEC, CN fin_data, and PDF statements."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json

from app.research_reconciliation import reconcile
from app.research_sources import ResearchSource, SourcePermissions
from app.research_structured_data import _ADAPTERS, extract_structured_observations
from tests.test_research_statement_extraction import CN_INCOME, HK_INCOME, src as pdf_src


def _perms(*, model_input: bool = True, display: bool = True) -> SourcePermissions:
    return SourcePermissions(store=True, model_input=model_input, display=display, export=False)


def _source(
    source_id: str,
    text: str,
    retrieval_method: str,
    *,
    title: str = "Structured Source",
    url: str | None = None,
    provenance: dict | None = None,
    is_simulated: bool = False,
    permissions: SourcePermissions | None = None,
) -> ResearchSource:
    return ResearchSource(
        source_id=source_id,
        owner_id="owner-1",
        title=title,
        text=text,
        url=url,
        kind="official",
        is_simulated=is_simulated,
        obtained_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
        content_sha256=sha256(text.encode("utf-8")).hexdigest(),
        retrieval_method=retrieval_method,  # type: ignore[arg-type]
        permissions=permissions or _perms(),
        provenance=provenance or {},
    )


def test_registry_covers_all_structured_sources():
    assert {"sec_companyconcept", "sec_companyfacts", "structured_financials", "user_pdf", "web_pdf", "extract_statements"} <= set(_ADAPTERS)


def test_sec_companyfacts_fin_data_observations_preserve_exact_provenance():
    text = "\n".join([
        "EXM | income.revenue | us-gaap:Revenues | 2025-01-01..2025-12-31 | fiscal FY2025 | form 10-K filed 2026-02-01 | accn A-25-4 | value 5000 USD",
        "EXM | income.revenue | us-gaap:Revenues | 2025-10-01..2025-12-31 | fiscal 2025-Q4 | form 10-K filed 2026-02-01 | accn A-25-4 | value 1400 USD | derived: FY minus (Q1+Q2+Q3), same tag",
        "EXM | income.eps_diluted | us-gaap:EarningsPerShareDiluted | 2025-01-01..2025-12-31 | fiscal FY2025 | form 10-K filed 2026-02-01 | accn A-25-4 | value 1.25 USD/shares",
        "EXM | income.shares_diluted | us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding | 2025-01-01..2025-12-31 | fiscal FY2025 | form 10-K filed 2026-02-01 | accn A-25-4 | value 4000 shares",
        "EXM | balance.total_assets | us-gaap:Assets | ..2025-12-31 | fiscal FY2025 | form 10-K filed 2026-02-01 | accn A-25-4 | value 9000 USD",
    ]) + "\n"
    src = _source(
        "s-sec",
        text,
        "sec_companyfacts",
        title="EXM SEC XBRL standardized financials",
        provenance={"provider": "sec_edgar", "entity_id": "cik:0000320193", "cik": "0000320193", "taxonomy": "us-gaap"},
    )
    facts, truncated = extract_structured_observations([src])
    assert not truncated and len(facts) == 5
    for fact in facts:
        loc = fact["location"]
        assert src.text[loc["character_start"]:loc["character_end"]] == fact["quotation"]
        assert fact["quotation_match"] == "exact"
        assert fact["extraction_method"] == "sec_companyfacts_row_v1"
        assert fact["verification"] == "structured_source_row_not_independently_verified"

    rev, q4, eps, shs, assets = facts
    assert rev["period"] == "2025-01-01 to 2025-12-31" and rev["currency"] == "USD" and rev["unit"] == "USD"
    assert rev["structured_metadata"]["entity_id"] == "cik:0000320193"
    assert rev["structured_metadata"]["metric_id"] == "us-gaap:Revenues"
    assert rev["structured_metadata"]["standard_line"] == "revenue"
    assert q4["structured_metadata"]["derived"] is True and "FY minus" in q4["structured_metadata"]["derivation"]
    assert eps["unit"] == "USD/shares" and eps["currency"] == "USD"
    assert shs["unit"] == "shares" and shs["currency"] is None
    assert assets["period"] == "2025-12-31" and assets["structured_metadata"]["period_start"] is None


def test_cn_structured_financials_tushare_and_baostock_observations():
    text = "\n".join([
        "600519 | income.revenue | tushare-cas:total_revenue | 2026-01-01..2026-06-30 | fiscal 2026H1 YTD | form 合并报表 filed 2026-08-20 | basis ytd_cumulative | value 260.0 CNY",
        "600519 | income.revenue | tushare-cas:total_revenue | 2026-04-01..2026-06-30 | fiscal 2026Q2 | form 合并报表 filed 2026-08-20 | basis single_quarter_derived | value 140.0 CNY | derived: H1 YTD minus Q1 YTD",
        "600519 | income.roe_avg | baostock:roeAvg | ..2026-09-30 | fiscal 20269M YTD | form BaoStock quarterly data filed 2026-10-25 | basis period_ratio | value 0.08 ratio",
        "600519 | income.revenue | tushare-cas:total_revenue | 2025-01-01..2025-12-31 | fiscal FY2025 | form 合并报表 filed 2026-04-25 | basis ytd_cumulative | value 565.0 CNY | superseded: 2026-03-20:560.0",
    ])
    src = _source(
        "s-cn",
        text,
        "structured_financials",
        title="600519 A-share financials via tushare",
        provenance={"provider": "tushare", "entity_id": "SSE:600519", "taxonomy": "tushare-cas"},
    )
    facts, truncated = extract_structured_observations([src])
    assert not truncated and len(facts) == 4
    h1, q2, roe, fy = facts
    assert h1["currency"] == "CNY" and h1["unit"] == "CNY" and h1["extraction_method"] == "structured_financials_row_v1"
    assert h1["structured_metadata"]["entity_id"] == "SSE:600519"
    assert h1["structured_metadata"]["basis"] == "ytd_cumulative"
    assert q2["structured_metadata"]["derived"] is True and q2["structured_metadata"]["basis"] == "single_quarter_derived"
    assert roe["unit"] == "ratio" and roe["currency"] is None
    assert fy["structured_metadata"]["filing_date"] == "2026-04-25"
    assert src.text[fy["location"]["character_start"]:fy["location"]["character_end"]] == fy["quotation"]


def test_pdf_extract_statements_observations_for_cn_and_hk_reports():
    raw_src, dp = pdf_src(["示例股份有限公司 2025年年度报告", CN_INCOME])
    pdf = _source(
        "s-pdf",
        raw_src.text,
        "user_pdf",
        title="示例股份有限公司 2025年年度报告",
        provenance={"entity_id": "SSE:600519", "document_pages": dp},
    )
    facts, truncated = extract_structured_observations([pdf])
    assert not truncated and len(facts) > 0
    assert all(f["extraction_method"] == "extract_statements_pdf_v1" for f in facts)
    # Every extracted PDF fact must carry exact offsets into pdf.text
    for fact in facts:
        loc = fact["location"]
        assert pdf.text[loc["character_start"]:loc["character_end"]] == fact["quotation"]

    rev_2025 = next(
        f for f in facts
        if f["structured_metadata"]["standard_line"] == "revenue"
        and f["structured_metadata"]["period_end"] == "2025-12-31"
    )
    assert Decimal(rev_2025["value"]) == Decimal("12345678901.23")
    # Duplicate secondary revenue line (其中：营业收入) was not emitted twice
    assert len([f for f in facts if f["structured_metadata"]["standard_line"] == "revenue" and f["structured_metadata"]["period_end"] == "2025-12-31"]) == 1
    # Statement/line canonicalization: operating_profit -> operating_income, net_profit_attributable_parent -> net_income
    op_2025 = next(
        f for f in facts
        if f["structured_metadata"]["statement_line"] == "operating_profit"
        and f["structured_metadata"]["period_end"] == "2025-12-31"
    )
    assert op_2025["structured_metadata"]["standard_line"] == "operating_income"

    # HK English PDF in RMB'000 scales values cleanly to CNY base currency units
    hk_raw, hk_dp = pdf_src([HK_INCOME])
    hk_pdf = _source(
        "s-hk",
        hk_raw.text,
        "web_pdf",
        title="HK 2025 Annual Report",
        provenance={"entity_id": "HKEX:0700", "document_pages": hk_dp},
    )
    hk_facts, _ = extract_structured_observations([hk_pdf])
    hk_rev = next(f for f in hk_facts if f["structured_metadata"]["standard_line"] == "revenue" and f["structured_metadata"]["period_end"] == "2025-12-31")
    assert Decimal(hk_rev["value"]) == Decimal("1234567000") and hk_rev["currency"] == "CNY" and hk_rev["unit"] == "CNY"
    hk_eps = next(f for f in hk_facts if f["structured_metadata"]["standard_line"] == "eps_basic" and f["structured_metadata"]["period_end"] == "2025-12-31")
    assert Decimal(hk_eps["value"]) == Decimal("0.52") and hk_eps["unit"] == "CNY/share" and hk_eps["currency"] == "CNY"


def test_cross_source_interleaving_permission_gates_and_reconciliation():
    # Prepare CN Tushare source + PDF report source for the same company & period with a slight rounding/value discrepancy
    ts_text = "600519 | income.revenue | tushare-cas:total_revenue | 2025-01-01..2025-12-31 | fiscal FY2025 | form 合并报表 filed 2026-04-25 | basis ytd_cumulative | value 12345678901.23 CNY\n"
    ts_src = _source(
        "s-ts",
        ts_text,
        "structured_financials",
        title="600519 Tushare",
        provenance={"provider": "tushare", "entity_id": "SSE:600519", "taxonomy": "tushare-cas"},
    )
    raw_pdf, dp = pdf_src(["600519 贵州茅台 2025年年度报告", CN_INCOME])
    pdf = _source(
        "s-pdf",
        raw_pdf.text,
        "user_pdf",
        title="600519 2025 Annual Report PDF",
        provenance={"entity_id": "SSE:600519", "document_pages": dp},
    )
    simulated = _source("s-sim", ts_text, "structured_financials", is_simulated=True)
    hidden = _source("s-hid", ts_text, "structured_financials", permissions=_perms(display=False))

    combined, truncated = extract_structured_observations([simulated, hidden, ts_src, pdf], max_facts=6)
    assert truncated is True and len(combined) == 6
    # Simulated and non-displayable sources are excluded; remaining sources interleave fairly
    assert all(f["source_id"] in {"s-ts", "s-pdf"} for f in combined)
    assert combined[0]["source_id"] == "s-ts" and combined[1]["source_id"] == "s-pdf"

    # Reconcile extracted CN Tushare & PDF revenue observations directly via research_reconciliation
    raw_cny_pdf, cny_dp = pdf_src(["600519 贵州茅台 2025年年度报告", CN_INCOME.replace("单位：元", "单位：人民币元")])
    cny_pdf = _source(
        "s-cny-pdf",
        raw_cny_pdf.text,
        "user_pdf",
        title="600519 2025 Annual Report PDF",
        provenance={"entity_id": "SSE:600519", "document_pages": cny_dp},
    )
    all_facts, _ = extract_structured_observations([ts_src, cny_pdf], max_facts=50)
    rev_facts = [
        f for f in all_facts
        if f["structured_metadata"]["standard_line"] == "revenue"
        and f["structured_metadata"]["period_end"] == "2025-12-31"
    ]
    assert len(rev_facts) == 2
    rec = reconcile(rev_facts, [ts_src, cny_pdf], default_entity="600519")
    assert rec["stats"]["groups"] == 1 and rec["stats"]["rows"] == 1
    assert rec["rows"][0]["status"] == "consistent"
    assert rec["rows"][0]["metric"] == "revenue"

    # Also test pre-extracted extract_statements payload on user_pdf provenance and extract_statements registry key
    from types import SimpleNamespace
    pre_payload = {
        "statements": [{
            "statement": "income",
            "scope": "consolidated",
            "unit": {"scale": 1000, "scale_label": "thousands", "currency": "USD", "basis": "stated"},
            "columns": [{"period_start": "2025-01-01", "period_end": "2025-12-31", "fiscal_label": "FY2025", "basis": "header_explicit"}],
            "rows": [{
                "standard_line": "revenue",
                "primary": True,
                "label": "Total revenue",
                "value": ["5000000"],
                "page": 1,
                "quotation": "Total revenue   5,000",
                "character_start": 0,
                "character_end": 21,
                "extraction_method": "document_table",
            }],
        }],
    }
    pdf_pre_src = _source(
        "s-pdf-pre",
        "Total revenue   5,000\n",
        "user_pdf",
        title="Pre-extracted PDF statements",
        provenance={"entity_id": "cik:0000320193", "extract_statements": pre_payload},
    )
    pdf_pre_facts, pdf_pre_trunc = extract_structured_observations([pdf_pre_src])
    assert not pdf_pre_trunc and len(pdf_pre_facts) == 1
    assert pdf_pre_facts[0]["value"] == "5000000" and pdf_pre_facts[0]["currency"] == "USD"
    assert pdf_pre_facts[0]["structured_metadata"]["standard_line"] == "revenue"

    duck_pre_src = SimpleNamespace(
        source_id="s-pre",
        title="Direct extract_statements JSON",
        text=json.dumps(pre_payload),
        retrieval_method="extract_statements",
        is_simulated=False,
        permissions=_perms(),
        provenance={"entity_id": "cik:0000320193"},
    )
    pre_facts, pre_trunc = extract_structured_observations([duck_pre_src])  # type: ignore[list-item]
    assert not pre_trunc and len(pre_facts) == 1
    assert pre_facts[0]["value"] == "5000000" and pre_facts[0]["currency"] == "USD"
