"""Tests for evidence-bound comparison table builder (research_tables.py)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from pydantic import ValidationError
import pytest

from app.research_calculations import CalculationRequest, calculate
from app.research_sources import ResearchSource, SourcePermissions
from app.research_tables import CreateTableRequest, TableBuildError, build_research_table


def _source(
    source_id: str = "src-1",
    *,
    display: bool = True,
    model_input: bool = True,
    is_simulated: bool = False,
    published_at: datetime | None = datetime(2026, 2, 1, tzinfo=timezone.utc),
    text: str = "Revenue was $1,200 million in FY2025 and $1,000 million in FY2024.",
) -> ResearchSource:
    return ResearchSource(
        source_id=source_id,
        owner_id="user-1",
        title="Filing 10-K",
        text=text,
        kind="official",
        is_simulated=is_simulated,
        published_at=published_at,
        obtained_at=datetime(2026, 2, 10, tzinfo=timezone.utc),
        content_sha256="sha-" + source_id,
        retrieval_method="sec_filing_html",
        permissions=SourcePermissions(store=True, model_input=model_input, display=display, export=True),
    )


def _fact(
    fact_id: str,
    *,
    source_id: str = "src-1",
    value: str,
    unit: str,
    currency: str | None = "USD",
    period: str = "FY2025",
    c_start: int = 12,
    c_end: int = 26,
) -> dict:
    return {
        "fact_id": fact_id,
        "label": fact_id,
        "value": value,
        "unit": unit,
        "currency": currency,
        "period": period,
        "source_id": source_id,
        "quotation": "$1,200 million",
        "location": {"character_start": c_start, "character_end": c_end},
    }


def _calc_from_facts(calc_id: str, formula: str, first: dict, second: dict, **extra) -> dict:
    keys = ("fact_id", "value", "unit", "currency", "period")
    req = CalculationRequest.model_validate({
        "formula": formula,
        "first": {k: first[k] for k in keys},
        "second": {k: second[k] for k in keys},
        **extra,
    })
    return {"calculation_id": calc_id, **calculate(req)}


def test_build_comparison_table_with_scale_alignment_and_provenance():
    src_us = _source("src-us", text="AAPL revenue $120 billion; MSFT revenue $90,000 million.")
    src_cn = _source("src-cn", text="Peer Note: FY2025 revenue RMB 200 亿元.")

    f_aapl_25 = _fact("f-aapl-25", source_id="src-us", value="120", unit="USD billions", period="FY2025", c_start=13, c_end=25)
    f_aapl_24 = _fact("f-aapl-24", source_id="src-us", value="100", unit="USD billions", period="FY2024", c_start=13, c_end=25)
    f_msft_25 = _fact("f-msft-25", source_id="src-us", value="90000", unit="USD millions", period="FY2025", c_start=40, c_end=54)
    f_msft_24 = _fact("f-msft-24", source_id="src-us", value="75000", unit="USD millions", period="FY2024", c_start=40, c_end=54)

    c_aapl_g = _calc_from_facts("c-aapl-g", "growth_percent", f_aapl_25, f_aapl_24)
    c_msft_g = _calc_from_facts("c-msft-g", "growth_percent", f_msft_25, f_msft_24)

    req = CreateTableRequest.model_validate({
        "table_type": "peer_comp",
        "title": "FY2025 Peer Revenue Comparison",
        "subtitle": "Aligned to USD billions",
        "columns": [
            {"key": "ticker", "label": "Ticker"},
            {"key": "rev_2025", "label": "FY2025 Revenue", "unit_preference": "USD billions", "currency": "USD"},
            {"key": "yoy", "label": "YoY Growth", "unit_preference": "percent"},
            {"key": "note", "label": "Filing Basis"},
        ],
        "rows": [
            {
                "label": "Apple Inc.",
                "cells": {
                    "ticker": {"kind": "text", "text": "AAPL"},
                    "rev_2025": {"kind": "fact", "fact_id": "f-aapl-25"},
                    "yoy": {"kind": "calculation", "calculation_id": "c-aapl-g"},
                    "note": {"kind": "text", "text": "FY2025 10-K"},
                },
            },
            {
                "label": "Microsoft",
                "cells": {
                    "ticker": {"kind": "text", "text": "MSFT"},
                    "rev_2025": {"kind": "fact", "fact_id": "f-msft-25"},
                    "yoy": {"kind": "calculation", "calculation_id": "c-msft-g"},
                    "note": {"kind": "empty", "display": "—"},
                },
            },
        ],
        "rationale": "Compare revenue levels and YoY growth on a unified USD billions basis.",
    })

    table = build_research_table(
        req,
        facts=[f_aapl_25, f_aapl_24, f_msft_25, f_msft_24],
        calculations=[c_aapl_g, c_msft_g],
        sources={"src-us": src_us, "src-cn": src_cn},
        as_of=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )

    assert table.table_id.startswith("table-")
    assert table.table_type == "peer_comp"
    assert table.source_ids == ["src-us"]
    assert table.is_simulated is False

    # Column auto-alignment and alignment metadata.
    rev_col = table.columns[1]
    assert rev_col.unit == "USD billions"
    assert rev_col.currency == "USD"
    assert rev_col.align == "right"

    # MSFT 90,000 USD millions -> 90 USD billions; exact provenance preserved!
    msft_rev_cell = table.rows[1].cells["rev_2025"]
    assert msft_rev_cell.kind == "fact"
    assert msft_rev_cell.value == "90"
    assert msft_rev_cell.unit == "USD billions"
    assert msft_rev_cell.provenance is not None
    assert msft_rev_cell.provenance.fact_id == "f-msft-25"
    assert msft_rev_cell.provenance.source_id == "src-us"
    assert msft_rev_cell.provenance.character_start == 40
    assert msft_rev_cell.provenance.character_end == 54
    assert msft_rev_cell.provenance.unit == "USD millions"

    # Calculation cell provenance tracks input_fact_ids + unit + period range.
    aapl_yoy_cell = table.rows[0].cells["yoy"]
    assert aapl_yoy_cell.kind == "calculation"
    assert aapl_yoy_cell.value == "20"
    assert aapl_yoy_cell.display_value == "20%"
    assert aapl_yoy_cell.provenance is not None
    assert aapl_yoy_cell.provenance.calculation_id == "c-aapl-g"
    assert aapl_yoy_cell.provenance.input_fact_ids == ["f-aapl-25", "f-aapl-24"]
    assert aapl_yoy_cell.provenance.period == "FY2024 -> FY2025"

    # Markdown output renders GFM table cleanly.
    assert "| Metric | Ticker | FY2025 Revenue | YoY Growth | Filing Basis |" in table.markdown
    assert "| Apple Inc. | AAPL | USD 120 billions | 20% | FY2025 10-K |" in table.markdown
    assert "| Microsoft | MSFT | USD 90 billions | 20% | — |" in table.markdown


def test_build_table_supports_chinese_yuan_scales_and_auto_alignment():
    src = _source("src-cn")
    f1 = _fact("f1", source_id="src-cn", value="2", unit="亿元", currency="CNY", period="FY2025")
    f2 = _fact("f2", source_id="src-cn", value="5000", unit="万元", currency="CNY", period="FY2025")

    req = CreateTableRequest.model_validate({
        "table_type": "financial_summary",
        "title": "Revenue Breakdown",
        "columns": [
            {"key": "segment", "label": "Segment"},
            {"key": "rev", "label": "Revenue", "unit_preference": "亿元"},
        ],
        "rows": [
            {"cells": {"segment": {"kind": "text", "text": "Core"}, "rev": {"kind": "fact", "fact_id": "f1"}}},
            {"cells": {"segment": {"kind": "text", "text": "Cloud"}, "rev": {"kind": "fact", "fact_id": "f2"}}},
        ],
        "rationale": "Align CNY yuan scales for comparison.",
    })
    table = build_research_table(req, facts=[f1, f2], calculations=[], sources={"src-cn": src})
    assert table.rows[0].cells["rev"].value == "2"
    assert table.rows[1].cells["rev"].value == "0.5"
    assert "0.5 亿元" in table.rows[1].cells["rev"].display_value


@pytest.mark.parametrize(
    "bad_text",
    [
        "$120 billion",
        "25.4%",
        "18.5x P/E",
        "Revenue hit 420",
        "约100亿元",
        "123.45",
    ],
)
def test_text_cell_rejects_ungrounded_numbers(bad_text: str):
    with pytest.raises(ValidationError, match="ungrounded numeric quantities"):
        CreateTableRequest.model_validate({
            "table_type": "comparison",
            "title": "Bad Table",
            "columns": [{"key": "col", "label": "Col"}],
            "rows": [{"cells": {"col": {"kind": "text", "text": bad_text}}}],
            "rationale": "Should reject raw numeric text.",
        })


@pytest.mark.parametrize(
    "safe_text",
    [
        "FY2025",
        "2025-Q4",
        "2026年Q1",
        "0700.HK",
        "10-K Item 7",
        "Scope 1",
        "Strong moat",
    ],
)
def test_text_cell_allows_safe_identifiers_and_qualitative_text(safe_text: str):
    req = CreateTableRequest.model_validate({
        "table_type": "comparison",
        "title": "Safe Text",
        "columns": [{"key": "col", "label": "Col"}],
        "rows": [{"cells": {"col": {"kind": "text", "text": safe_text}}}],
        "rationale": "Allow non-quantitative identifiers.",
    })
    assert req.rows[0].cells["col"].text == safe_text


def test_build_table_rejects_cross_currency_unit_mismatch_and_missing_provenance():
    src = _source("src-1")
    f_usd = _fact("f-usd", source_id="src-1", value="100", unit="USD millions", currency="USD", period="FY2025")
    f_eur = _fact("f-eur", source_id="src-1", value="90", unit="EUR millions", currency="EUR", period="FY2025")

    req_cross = CreateTableRequest.model_validate({
        "table_type": "peer_comp",
        "title": "Cross Currency",
        "columns": [{"key": "rev", "label": "Revenue"}],
        "rows": [
            {"cells": {"rev": {"kind": "fact", "fact_id": "f-usd"}}},
            {"cells": {"rev": {"kind": "fact", "fact_id": "f-eur"}}},
        ],
        "rationale": "Check fail closed on mixed currencies.",
    })
    with pytest.raises(TableBuildError, match="mix currencies"):
        build_research_table(req_cross, facts=[f_usd, f_eur], calculations=[], sources={"src-1": src})

    req_missing = CreateTableRequest.model_validate({
        "table_type": "comparison",
        "title": "Missing Fact",
        "columns": [{"key": "rev", "label": "Revenue"}],
        "rows": [{"cells": {"rev": {"kind": "fact", "fact_id": "f-does-not-exist"}}}],
        "rationale": "Check unknown fact fails.",
    })
    with pytest.raises(TableBuildError, match="Unknown fact_id"):
        build_research_table(req_missing, facts=[f_usd], calculations=[], sources={"src-1": src})


def test_build_table_enforces_permissions_as_of_simulation_and_replacement():
    hidden = _source("src-hidden", display=False)
    future = _source("src-future", published_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    sim = _source("src-sim", is_simulated=True)
    ok = _source("src-ok")

    f_hidden = _fact("f-h", source_id="src-hidden", value="10", unit="USD millions")
    f_future = _fact("f-f", source_id="src-future", value="10", unit="USD millions")
    f_sim = _fact("f-s", source_id="src-sim", value="10", unit="USD millions")
    f_ok = _fact("f-ok", source_id="src-ok", value="10", unit="USD millions")

    for bad_fid in ("f-h", "f-f", "f-s"):
        req = CreateTableRequest.model_validate({
            "table_type": "comparison",
            "title": "Guarded",
            "columns": [{"key": "v", "label": "Val"}],
            "rows": [{"cells": {"v": {"kind": "fact", "fact_id": bad_fid}}}],
            "rationale": "Enforce source gate.",
        })
        with pytest.raises(TableBuildError):
            build_research_table(
                req,
                facts=[f_hidden, f_future, f_sim, f_ok],
                calculations=[],
                sources={"src-hidden": hidden, "src-future": future, "src-sim": sim, "src-ok": ok},
                as_of=datetime(2026, 3, 1, tzinfo=timezone.utc),
                allow_simulated=False,
            )

    # Simulated allowed when allow_simulated=True.
    req_sim = CreateTableRequest.model_validate({
        "table_type": "comparison",
        "title": "Simulated Ok",
        "columns": [{"key": "v", "label": "Val"}],
        "rows": [{"cells": {"v": {"kind": "fact", "fact_id": "f-s"}}}],
        "replaces_table_id": "tbl-prev",
        "rationale": "Allow explicit simulation flag + replacement.",
    })
    t_sim = build_research_table(
        req_sim,
        facts=[f_sim],
        calculations=[],
        sources={"src-sim": sim},
        existing_tables=[{"table_id": "tbl-prev"}],
        allow_simulated=True,
    )
    assert t_sim.is_simulated is True
    assert t_sim.replaces_table_id == "tbl-prev"

    # Unknown replaces_table_id fails closed.
    req_bad_replace = req_sim.model_copy(update={"replaces_table_id": "tbl-missing"})
    with pytest.raises(TableBuildError, match="Unknown replaces_table_id"):
        build_research_table(
            req_bad_replace,
            facts=[f_sim],
            calculations=[],
            sources={"src-sim": sim},
            existing_tables=[{"table_id": "tbl-prev"}],
            allow_simulated=True,
        )
