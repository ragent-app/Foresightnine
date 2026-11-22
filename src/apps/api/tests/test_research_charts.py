from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from hashlib import sha256

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from app import main as api
from app.model_protocol import ModelResponse, ModelToolCall, ModelUsage
from app.research_budget import ResearchExecutionBudget
from app.research_charts import (
    ChartBuildError,
    CreateChartRequest,
    ResearchChart,
    build_research_chart,
)
from app.research_coordinator import ResearchExecutionContext
from app.research_progress import sanitize_event
from app.research_runtime import TOOLS, SYSTEM, run_research_agent
from app.research_sources import ResearchSource, SourcePermissions
from app.research_topics import ResearchContent, ResearchTurn, ResearchVersion, TurnUsage
from app.research_turns import merge_turn, prior_research_input


def _make_source(
    source_id: str,
    text: str,
    *,
    kind: str = "user_supplied",
    title: str = "Source",
    display: bool = True,
    export: bool = True,
    model_input: bool = True,
    is_simulated: bool = False,
    published_at: datetime | None = datetime(2025, 3, 1, tzinfo=timezone.utc),
    provenance: dict | None = None,
) -> ResearchSource:
    return ResearchSource(
        source_id=source_id,
        owner_id="owner-1",
        title=title,
        text=text,
        kind=kind,  # type: ignore[arg-type]
        obtained_at=datetime(2025, 3, 2, tzinfo=timezone.utc),
        published_at=published_at,
        content_sha256=sha256(text.encode("utf-8")).hexdigest(),
        is_simulated=is_simulated,
        permissions=SourcePermissions(
            store=True,
            model_input=model_input,
            display=display,
            export=export,
        ),
        provenance=provenance or {},
    )


def _context() -> ResearchExecutionContext:
    return ResearchExecutionContext(
        ResearchExecutionBudget(None, None, None, float("inf")),
        False,
        lambda: None,
        lambda: None,
        lambda *_: None,
        lambda *_: None,
        lambda: None,
    )


def _tool_turn(name: str, args: dict, ident: str) -> ModelResponse:
    call = ModelToolCall(ident, name, json.dumps(args))
    return ModelResponse(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": ident,
                    "type": "function",
                    "function": {"name": name, "arguments": call.arguments},
                }
            ],
        },
        (call,),
        ModelUsage(1, 1, 2),
        "tool_calls",
    )


class _ScriptedModel:
    def __init__(self, resolver):
        self.resolver = resolver
        self.inputs: list[list[dict]] = []

    def complete(self, messages, tools, **kwargs):
        snapshot = json.loads(json.dumps(messages))
        self.inputs.append(snapshot)
        return self.resolver(len(self.inputs) - 1, snapshot)


def test_build_chart_from_facts_scales_millions_and_billions_deterministically():
    text = "FY2024 revenue was 60,922 million USD. FY2025 revenue reached 130.497 billion USD."
    src = _make_source("s1", text, title="Annual Filing")
    facts = [
        {
            "fact_id": "fact-2024",
            "label": "Revenue",
            "value": "60922",
            "unit": "millions",
            "currency": "USD",
            "period": "FY2024",
            "source_id": "s1",
            "quotation": "60,922 million USD",
            "character_start": text.index("60,922 million USD"),
            "character_end": text.index("60,922 million USD") + len("60,922 million USD"),
        },
        {
            "fact_id": "fact-2025",
            "label": "Revenue",
            "value": "130.497",
            "unit": "billions",
            "currency": "USD",
            "period": "FY2025",
            "source_id": "s1",
            "quotation": "130.497 billion USD",
            "character_start": text.index("130.497 billion USD"),
            "character_end": text.index("130.497 billion USD") + len("130.497 billion USD"),
        },
    ]
    as_of = datetime(2025, 4, 1, tzinfo=timezone.utc)
    req_billions = CreateChartRequest.model_validate(
        {
            "chart_type": "bar",
            "title": "Annual Revenue",
            "x_label": "Fiscal Year",
            "y_axes": [{"id": "left", "label": "Revenue", "unit_preference": "billions", "currency": "USD"}],
            "series": [
                {
                    "name": "Total Revenue",
                    "mark": "bar",
                    "y_axis_id": "left",
                    "binding": {"kind": "facts", "fact_ids": ["fact-2024", "fact-2025"]},
                }
            ],
            "rationale": "Compare revenue across FY2024 and FY2025 on unified scale.",
        }
    )
    chart = build_research_chart(req_billions, facts=facts, calculations=[], sources={"s1": src}, as_of=as_of)
    assert chart.chart_id.startswith("chart-")
    assert chart.axes[0].scale == "billions"
    assert chart.axes[0].currency == "USD"
    assert chart.axes[0].unit == "USD billions"
    pts = chart.series[0].points
    assert [p.x for p in pts] == ["FY2024", "FY2025"]
    assert [p.y for p in pts] == ["60.922", "130.497"]
    assert pts[0].provenance.fact_id == "fact-2024"
    assert pts[0].provenance.source_id == "s1"
    assert "billions" in chart.subtitle
    assert "USD" in chart.subtitle
    assert "换算" in chart.subtitle
    assert "2025-04-01" in chart.subtitle

    # Also test billions -> millions exact Decimal scaling with no float inaccuracy or scientific notation
    req_millions = CreateChartRequest.model_validate(
        {
            "chart_type": "bar",
            "title": "Annual Revenue (Millions)",
            "y_axes": [{"id": "left", "label": "Revenue", "unit_preference": "USD millions"}],
            "series": [
                {
                    "name": "Total Revenue",
                    "mark": "bar",
                    "binding": {"kind": "facts", "fact_ids": ["fact-2024", "fact-2025"]},
                }
            ],
            "rationale": "Verify exact scaling to millions.",
        }
    )
    chart_m = build_research_chart(req_millions, facts=facts, calculations=[], sources={"s1": src})
    assert chart_m.axes[0].scale == "millions"
    assert [p.y for p in chart_m.series[0].points] == ["60922", "130497"]


def test_build_chart_calculations_combo_dual_axis():
    text = "FY2024: 80M; FY2025: 100M; Growth: 25%."
    src = _make_source("s1", text)
    facts = [
        {
            "fact_id": "f1",
            "label": "Revenue",
            "value": "80",
            "unit": "millions",
            "currency": "USD",
            "period": "FY2024",
            "source_id": "s1",
            "character_start": 8,
            "character_end": 11,
        },
        {
            "fact_id": "f2",
            "label": "Revenue",
            "value": "100",
            "unit": "millions",
            "currency": "USD",
            "period": "FY2025",
            "source_id": "s1",
            "character_start": 20,
            "character_end": 24,
        },
    ]
    calcs = [
        {
            "calculation_id": "calc-1",
            "formula_id": "growth_percent",
            "expression": "(first / second - 1) * 100",
            "inputs": [
                {"fact_id": "f2", "value": "100", "unit": "millions", "currency": "USD", "period": "FY2025"},
                {"fact_id": "f1", "value": "80", "unit": "millions", "currency": "USD", "period": "FY2024"},
            ],
            "value": "25.00",
            "unit": "percent",
            "currency": None,
        }
    ]
    req = CreateChartRequest.model_validate(
        {
            "chart_type": "combo_bar_line",
            "title": "Revenue and YoY Growth",
            "y_axes": [
                {"id": "left", "label": "Revenue", "currency": "USD"},
                {"id": "right", "label": "YoY Growth %"},
            ],
            "series": [
                {
                    "name": "Revenue",
                    "mark": "bar",
                    "y_axis_id": "left",
                    "binding": {"kind": "facts", "fact_ids": ["f1", "f2"]},
                },
                {
                    "name": "YoY Growth",
                    "mark": "line",
                    "y_axis_id": "right",
                    "binding": {"kind": "calculations", "calculation_ids": ["calc-1"]},
                },
            ],
            "rationale": "Combo bar/line combining verified revenue and calculated growth.",
        }
    )
    chart = build_research_chart(req, facts=facts, calculations=calcs, sources={"s1": src})
    assert len(chart.axes) == 2
    assert chart.axes[0].unit == "USD millions"
    assert chart.axes[1].unit == "percent"
    calc_pt = chart.series[1].points[0]
    assert calc_pt.x == "FY2025"
    assert calc_pt.y == "25"
    assert calc_pt.display_value == "25%"
    assert calc_pt.provenance.calculation_id == "calc-1"
    assert calc_pt.provenance.source_id == "s1"


def test_market_data_bar_lines_bind_for_charts_with_exact_line_provenance():
    text = (
        "NVDA@NASDAQ | 2025-01-02 | 1day | O 104 H 106 L 103 C 105.00 V 1000 USD\n"
        "NVDA@NASDAQ | 2025-01-03 | 1day | O 106 H 111 L 105 C 110.00 V 2000 USD\n"
    )
    src = _make_source("md-1", text, kind="market_data", provenance={"currency": "USD"})
    req = CreateChartRequest.model_validate({
        "chart_type": "price_series", "title": "NVDA close",
        "y_axes": [{"id": "left", "label": "Price"}],
        "series": [{"name": "NVDA", "mark": "line", "binding": {"kind": "historical_prices", "source_id": "md-1"}}],
        "rationale": "Plot closes from the market_data source."})
    chart = build_research_chart(req, facts=[], calculations=[], sources={"md-1": src})
    pts = chart.series[0].points
    assert [p.x for p in pts] == ["2025-01-02", "2025-01-03"] and [p.y for p in pts] == ["105", "110"]
    assert chart.axes[0].currency == "USD"
    first = pts[0].provenance
    assert text[first.character_start:first.character_end] == text.splitlines()[0]
    # A non-price line is still rejected: charts bind only to recognised source lines.
    bad = _make_source("md-2", "NVDA@NASDAQ | 2025-01-02 | 1day | free text", kind="market_data", provenance={"currency": "USD"})
    with pytest.raises(ChartBuildError):
        build_research_chart(CreateChartRequest.model_validate({
            "chart_type": "price_series", "title": "x", "y_axes": [{"id": "left", "label": "Price"}],
            "series": [{"name": "N", "mark": "line", "binding": {"kind": "historical_prices", "source_id": "md-2"}}],
            "rationale": "Should fail on a malformed line."}), facts=[], calculations=[], sources={"md-2": bad})


def test_historical_prices_raw_and_normalize_base_100():
    usd_text = (
        "NVDA | 2025-01-03 | daily close | 110.00 USD\n"
        "NVDA | 2025-01-02 | daily close | 105.00 USD\n"
    )
    krw_text = (
        "005930.KS | 2025-01-02 | daily close | 50000 KRW per share (KRW)\n"
        "005930.KS | 2025-01-03 | daily close | 55000 KRW per share (KRW)\n"
    )
    s_usd = _make_source("hp-usd", usd_text, kind="market_data", provenance={"currency": "USD"})
    s_krw = _make_source("hp-krw", krw_text, kind="market_data", provenance={"currency": "KRW"})

    # Raw prices cannot mix USD and KRW on same axis
    mixed_req = CreateChartRequest.model_validate(
        {
            "chart_type": "price_series",
            "title": "Mixed Raw Prices",
            "y_axes": [{"id": "left", "label": "Price"}],
            "series": [
                {
                    "name": "NVDA",
                    "mark": "line",
                    "binding": {"kind": "historical_prices", "source_id": "hp-usd", "normalize_base_100": False},
                },
                {
                    "name": "Samsung",
                    "mark": "line",
                    "binding": {"kind": "historical_prices", "source_id": "hp-krw", "normalize_base_100": False},
                },
            ],
            "rationale": "Should fail closed when mixing currencies without normalization.",
        }
    )
    with pytest.raises(ChartBuildError):
        build_research_chart(mixed_req, facts=[], calculations=[], sources={"hp-usd": s_usd, "hp-krw": s_krw})

    # Normalized base=100 merges across currencies cleanly on single axis and orders ascending by date
    norm_req = CreateChartRequest.model_validate(
        {
            "chart_type": "price_series",
            "title": "Indexed Relative Performance",
            "y_axes": [{"id": "left", "label": "Base 100"}],
            "series": [
                {
                    "name": "NVDA",
                    "mark": "line",
                    "binding": {"kind": "historical_prices", "source_id": "hp-usd", "normalize_base_100": True},
                },
                {
                    "name": "Samsung",
                    "mark": "line",
                    "binding": {"kind": "historical_prices", "source_id": "hp-krw", "normalize_base_100": True},
                },
            ],
            "rationale": "Normalize both to day-1 = 100 for cross-market comparison.",
        }
    )
    chart = build_research_chart(norm_req, facts=[], calculations=[], sources={"hp-usd": s_usd, "hp-krw": s_krw})
    assert chart.axes[0].unit == "index (base=100)"
    assert chart.axes[0].currency is None
    nvda_pts = chart.series[0].points
    assert [p.x for p in nvda_pts] == ["2025-01-02", "2025-01-03"]
    assert nvda_pts[0].y == "100"
    assert Decimal(nvda_pts[1].y) == Decimal("104.7619")
    # Offsets point back to exact source lines (note 2025-01-02 was second line in input!)
    p0 = nvda_pts[0].provenance
    assert usd_text[p0.character_start : p0.character_end] == "NVDA | 2025-01-02 | daily close | 105.00 USD"
    krw_pts = chart.series[1].points
    assert [p.y for p in krw_pts] == ["100", "110"]


def test_sec_concept_series_orders_ascending_dedups_and_filters_forms():
    sec_lines = [
        "SEC company concept: NVIDIA CORP; CIK 0001045810; taxonomy us-gaap; tag Revenues; label Revenues.",
        "Most recent 5 of 5 matching observations; filing date is not exact public availability time.",
        '{"accn":"0001045810-25-000011","end":"2025-01-26","filed":"2025-02-26","form":"10-K","fp":"FY","fy":2025,"start":"2024-01-29","unit":"USD","val":130497000000}',
        '{"accn":"0001045810-25-000011","end":"2024-01-28","filed":"2025-02-26","form":"10-K","fp":"FY","fy":2024,"start":"2023-01-30","unit":"USD","val":60922000000}',
        '{"accn":"0001045810-24-000020","end":"2024-01-28","filed":"2024-02-21","form":"10-K","fp":"FY","fy":2024,"start":"2023-01-30","unit":"USD","val":60921000000}',
        '{"accn":"0001045810-24-000120","end":"2024-10-27","filed":"2024-11-20","form":"10-Q","fp":"Q3","fy":2025,"start":"2024-07-29","unit":"USD","val":35082000000}',
        '{"accn":"0001045810-23-000010","end":"2023-01-29","filed":"2023-02-24","form":"10-K","fp":"FY","fy":2023,"start":"2022-01-31","unit":"USD","val":26974000000}',
    ]
    sec_text = "\n".join(sec_lines)
    sec_src = _make_source("sec-1", sec_text, kind="official", title="NVDA Revenues")

    req = CreateChartRequest.model_validate(
        {
            "chart_type": "bar",
            "title": "NVDA Annual SEC Revenue",
            "y_axes": [{"id": "left", "label": "Revenue", "unit_preference": "billions"}],
            "series": [
                {
                    "name": "Annual Revenue",
                    "mark": "bar",
                    "binding": {
                        "kind": "sec_concept_series",
                        "source_id": "sec-1",
                        "form_filter": "10-K",
                        "max_points": 2,
                    },
                }
            ],
            "rationale": "Inspect latest 2 annual 10-K revenues in ascending order.",
        }
    )
    chart = build_research_chart(req, facts=[], calculations=[], sources={"sec-1": sec_src})
    pts = chart.series[0].points
    # Latest 2 of 3 annual periods in ascending chronological order (FY2024 -> FY2025), 10-Q excluded,
    # and comparative FY2024 prefers latest filing (60922M filed 2025-02-26 over 60921M filed 2024-02-21).
    assert [p.x for p in pts] == ["FY2024", "FY2025"]
    assert [p.y for p in pts] == ["60.922", "130.497"]
    assert '"filed":"2025-02-26"' in sec_text[pts[0].provenance.character_start : pts[0].provenance.character_end]

    # Check 10-Q filter isolates quarterly point
    q_req = CreateChartRequest.model_validate(
        {
            "chart_type": "line",
            "title": "NVDA Quarterly",
            "y_axes": [{"id": "left", "label": "Revenue", "unit_preference": "billions"}],
            "series": [
                {
                    "name": "Q3 Revenue",
                    "mark": "line",
                    "binding": {"kind": "sec_concept_series", "source_id": "sec-1", "form_filter": "10-Q"},
                }
            ],
            "rationale": "Isolate 10-Q points.",
        }
    )
    q_chart = build_research_chart(q_req, facts=[], calculations=[], sources={"sec-1": sec_src})
    assert [p.x for p in q_chart.series[0].points] == ["FY2025 Q3"]
    assert [p.y for p in q_chart.series[0].points] == ["35.082"]


def test_fail_closed_validation_and_simulated_propagation():
    text = "Rev 100 USD, Margin 25 percent."
    s_ok = _make_source("s-ok", text)
    s_hidden = _make_source("s-hidden", text, display=False)
    s_sim = _make_source("s-sim", text, is_simulated=True)

    facts = [
        {
            "fact_id": "f-usd",
            "label": "Rev",
            "value": "100",
            "unit": "millions",
            "currency": "USD",
            "period": "FY2025",
            "source_id": "s-ok",
        },
        {
            "fact_id": "f-eur",
            "label": "Rev EUR",
            "value": "90",
            "unit": "millions",
            "currency": "EUR",
            "period": "FY2024",
            "source_id": "s-ok",
        },
        {
            "fact_id": "f-pct",
            "label": "Margin",
            "value": "25",
            "unit": "percent",
            "currency": None,
            "period": "FY2025",
            "source_id": "s-ok",
        },
        {
            "fact_id": "f-hidden",
            "label": "Secret",
            "value": "100",
            "unit": "millions",
            "currency": "USD",
            "period": "FY2023",
            "source_id": "s-hidden",
        },
        {
            "fact_id": "f-sim",
            "label": "Sim Rev",
            "value": "110",
            "unit": "millions",
            "currency": "USD",
            "period": "FY2026",
            "source_id": "s-sim",
        },
    ]

    # extra='forbid' blocks raw values array or arbitrary CSS
    with pytest.raises(ValidationError):
        CreateChartRequest.model_validate(
            {
                "chart_type": "bar",
                "title": "Injected",
                "y_axes": [{"id": "left", "label": "Y"}],
                "series": [
                    {
                        "name": "S",
                        "mark": "bar",
                        "color": "#ff0000",
                        "binding": {"kind": "facts", "fact_ids": ["f-usd"]},
                    }
                ],
                "rationale": "r",
            }
        )
    with pytest.raises(ValidationError):
        CreateChartRequest.model_validate(
            {
                "chart_type": "bar",
                "title": "Raw Data",
                "y_axes": [{"id": "left", "label": "Y"}],
                "series": [
                    {
                        "name": "S",
                        "mark": "bar",
                        "binding": {"kind": "raw", "values": [1, 2, 3]},
                    }
                ],
                "rationale": "r",
            }
        )

    def build_for(ids, **kwargs):
        req = CreateChartRequest.model_validate(
            {
                "chart_type": "bar",
                "title": "Check",
                "y_axes": [{"id": "left", "label": "Y"}],
                "series": [{"name": "S", "mark": "bar", "binding": {"kind": "facts", "fact_ids": ids}}],
                "rationale": "r",
                **({k: kwargs.pop(k)} if "replaces_chart_id" in kwargs and (k := "replaces_chart_id") else {}),
            }
        )
        return build_research_chart(req, facts=facts, calculations=[], sources={"s-ok": s_ok, "s-hidden": s_hidden, "s-sim": s_sim}, **kwargs)

    # Incompatible currency on same axis
    with pytest.raises(ChartBuildError):
        build_for(["f-usd", "f-eur"])
    # Incompatible unit family on same axis
    with pytest.raises(ChartBuildError):
        build_for(["f-usd", "f-pct"])
    # Missing fact_id
    with pytest.raises(ChartBuildError):
        build_for(["f-missing"])
    # Missing replaces_chart_id
    with pytest.raises(ChartBuildError):
        build_for(["f-usd"], replaces_chart_id="chart-does-not-exist", existing_charts=[])
    # permissions.display == False
    with pytest.raises(ChartBuildError):
        build_for(["f-hidden"])
    # Simulated rejected by default
    with pytest.raises(ChartBuildError):
        build_for(["f-sim"])
    # Simulated allowed explicitly propagates is_simulated=True
    sim_chart = build_for(["f-sim"], allow_simulated=True)
    assert sim_chart.is_simulated is True
    assert "模拟数据" in sim_chart.subtitle


def test_progress_sanitize_summary_create_chart_whitelists_fields():
    event = sanitize_event(
        {
            "kind": "tool",
            "name": "create_chart",
            "status": "completed",
            "rationale": "Plot revenue",
            "summary": {
                "kind": "create_chart",
                "chart_id": "chart-123",
                "chart_type": "combo_bar_line",
                "title": "Revenue & Growth",
                "series_count": 2,
                "point_count": 6,
                "secret_raw_text": "DO NOT LEAK",
            },
        }
    )
    assert event == {
        "kind": "tool",
        "name": "create_chart",
        "status": "completed",
        "reason": None,
        "rationale": "Plot revenue",
        "denied_count": None,
        "summary": {
            "kind": "create_chart",
            "chart_id": "chart-123",
            "chart_type": "combo_bar_line",
            "title": "Revenue & Growth",
            "series_count": 2,
            "point_count": 6,
        },
    }


def test_runtime_loop_multiturn_replaces_and_continuation_merge():
    src_text = "FY2024 revenue 80 million USD. FY2025 revenue 100 million USD."
    src = _make_source("s1", src_text, title="Income Statement")

    def resolver_turn1(step, messages):
        if step == 0:
            return _tool_turn("read_sources", {"reads": [{"source_id": "s1"}], "rationale": "read source"}, "t1-a")
        if step == 1:
            return _tool_turn(
                "record_facts",
                {
                    "facts": [
                        {
                            "label": "Revenue",
                            "value": 80,
                            "unit": "millions",
                            "currency": "USD",
                            "period": "FY2024",
                            "source_id": "s1",
                            "quotation": "FY2024 revenue 80 million USD.",
                        },
                        {
                            "label": "Revenue",
                            "value": 100,
                            "unit": "millions",
                            "currency": "USD",
                            "period": "FY2025",
                            "source_id": "s1",
                            "quotation": "FY2025 revenue 100 million USD.",
                        },
                    ],
                    "rationale": "record revenues",
                },
                "t1-b",
            )
        if step == 2:
            facts = json.loads([m for m in messages if m.get("role") == "tool"][-1]["content"])["facts"]
            return _tool_turn(
                "create_chart",
                {
                    "chart_type": "bar",
                    "title": "Revenue Growth",
                    "y_axes": [{"id": "left", "label": "Revenue", "currency": "USD"}],
                    "series": [
                        {
                            "name": "Revenue",
                            "mark": "bar",
                            "binding": {"kind": "facts", "fact_ids": [facts[0]["fact_id"], facts[1]["fact_id"]]},
                        }
                    ],
                    "rationale": "Visualize revenue growth.",
                },
                "t1-c",
            )
        chart_res = json.loads([m for m in messages if m.get("role") == "tool"][-1]["content"])
        cid = chart_res["chart_id"]
        return _tool_turn(
            "submit_research",
            {
                "status": "completed",
                "report": f"## Revenue Summary\n\n{chart_res['embed_marker']}\n",
                "claims": [
                    {
                        "text": "Revenue grew from 80M to 100M.",
                        "source_id": "s1",
                        "quotation": "FY2025 revenue 100 million USD.",
                        "role": "support",
                    }
                ],
                "limitations": [],
                "stopping_reason": "answered",
            },
            "t1-d",
        )

    assert any(t.name == "create_chart" for t in TOOLS)
    assert "[[chart:chart_id]]" in SYSTEM

    out1 = run_research_agent(question="Revenue chart?", sources=[src], model=_ScriptedModel(resolver_turn1), context=_context())
    assert out1.status == "completed"
    assert len(out1.charts) == 1
    chart1 = out1.charts[0]
    assert f"[[chart:{chart1['chart_id']}]]" in out1.report
    create_events = [e for e in out1.events if e.get("kind") == "tool" and e.get("name") == "create_chart"]
    assert len(create_events) == 1
    assert create_events[0]["summary"]["chart_id"] == chart1["chart_id"]
    assert create_events[0]["summary"]["point_count"] == 2

    # Merge Turn 1 and test continuation deduplication
    now = datetime.now(timezone.utc)
    turn1 = ResearchTurn(
        turn_id="turn-1",
        index=1,
        question="Revenue chart?",
        question_origin="user",
        operation_ids=["op-1"],
        parent_version_id="v1",
        started_at=now,
        finished_at=now,
        status="completed",
        reason="submitted",
        report=out1.report,
        claim_ids=[c["claim_id"] for c in out1.claims],
        fact_ids=[f["fact_id"] for f in out1.facts],
        chart_ids=[c["chart_id"] for c in out1.charts],
        search_status="not_requested",
        official_fetch_status="not_requested",
        usage=TurnUsage(model_calls=4, search_calls=0, total_tokens=8),
        events=out1.events,
    )
    c1 = merge_turn(
        ResearchContent(),
        turn1,
        claims=out1.claims,
        facts=out1.facts,
        calculations=out1.calculations,
        charts=out1.charts,
        sources=[],
        source_candidates=[],
    )
    # Re-merge same turn_id (continuation): charts must not duplicate & chart_ids union cleanly
    c1_dup = merge_turn(
        c1,
        turn1,
        claims=out1.claims,
        facts=out1.facts,
        calculations=out1.calculations,
        charts=out1.charts,
        sources=[],
        source_candidates=[],
    )
    assert len(c1_dup.charts) == 1
    assert c1_dup.charts[0].turn_id == "turn-1"
    assert c1_dup.turns[-1].chart_ids == [chart1["chart_id"]]

    # Turn 2 replaces chart-1 using prior_research_input
    v2 = ResearchVersion(
        version_id="v2",
        parent_version_id="v1",
        created_at=now,
        created_by="agent",
        review="draft",
        source_ids=["s1"],
        content=c1,
    )
    prior = prior_research_input(v2)
    assert prior is not None
    assert len(prior["charts"]) == 1
    assert prior["charts"][0]["chart_id"] == chart1["chart_id"]

    def resolver_turn2(step, messages):
        if step == 0:
            payload = json.loads(messages[1]["content"])
            prior_facts = payload["prior_research"]["facts"]
            prior_charts = payload["prior_research"]["charts"]
            return _tool_turn(
                "create_chart",
                {
                    "chart_type": "line",
                    "title": "Revenue Trend (Billions)",
                    "y_axes": [{"id": "left", "label": "Revenue", "unit_preference": "billions", "currency": "USD"}],
                    "series": [
                        {
                            "name": "Revenue",
                            "mark": "line",
                            "binding": {"kind": "facts", "fact_ids": [f["fact_id"] for f in prior_facts]},
                        }
                    ],
                    "replaces_chart_id": prior_charts[0]["chart_id"],
                    "rationale": "Update prior chart to line in billions.",
                },
                "t2-a",
            )
        chart_res = json.loads([m for m in messages if m.get("role") == "tool"][-1]["content"])
        return _tool_turn(
            "submit_research",
            {
                "status": "completed",
                "report": f"Updated chart:\n\n{chart_res['embed_marker']}\n",
                "claims": [],
                "limitations": [],
                "stopping_reason": "updated chart",
            },
            "t2-b",
        )

    out2 = run_research_agent(
        question="Convert chart to line in billions",
        sources=[src],
        model=_ScriptedModel(resolver_turn2),
        context=_context(),
        prior=prior,
    )
    assert out2.status == "completed"
    assert len(out2.charts) == 1
    assert out2.charts[0]["replaces_chart_id"] == chart1["chart_id"]
    assert out2.charts[0]["axes"][0]["scale"] == "billions"
    assert [p["y"] for p in out2.charts[0]["series"][0]["points"]] == ["0.08", "0.1"]


def test_export_enforces_chart_source_permissions_and_protects_server_charts(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(api.app)
    reg = client.post(
        "/v1/auth/local/register",
        json={"email": "chart-export@example.test", "password": "correct horse battery staple!"},
    ).json()
    headers = {"Authorization": "Bearer " + reg["access_token"]}
    account_id = client.get("/v1/account", headers=headers).json()["account"]["account_id"]

    # Create two sources: s_ok (export=True) and s_noexp (export=False)
    s_ok = client.post(
        "/v2/research/sources",
        headers=headers,
        json={
            "title": "Public Filing",
            "text": "Revenue was 100 USD.",
            "kind": "user_supplied",
            "permissions": {"store": True, "display": True, "model_input": True, "export": True},
        },
    ).json()
    s_noexp = api.research_sources.create(
        account_id,
        api.SourceInput(
            title="Restricted Chart Source",
            text="NVDA | 2025-01-02 | daily close | 120.00 USD",
            kind="market_data",
            permissions=SourcePermissions(store=True, display=True, model_input=True, export=False),
        ),
    )

    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Chart export?", "source_ids": [s_ok["source_id"]]}).json()
    topic_id = topic["topic_id"]
    parent_vid = topic["versions"][-1]["version_id"]

    # Reject forging content.charts via user version edit endpoint
    forged_chart = build_research_chart(
        CreateChartRequest.model_validate(
            {
                "chart_type": "price_series",
                "title": "Prices",
                "y_axes": [{"id": "left", "label": "Price"}],
                "series": [{"name": "NVDA", "mark": "line", "binding": {"kind": "historical_prices", "source_id": s_noexp.source_id}}],
                "rationale": "Plot price series",
            }
        ),
        facts=[],
        calculations=[],
        sources={s_noexp.source_id: s_noexp},
    )
    loaded_topic = api.research_topics.get(account_id, topic_id)
    bad_content = loaded_topic.versions[-1].content.model_copy(deep=True)
    bad_content.charts = [forged_chart]
    res_edit = client.post(
        f"/v2/research/topics/{topic_id}/versions",
        headers=headers,
        json={"parent_version_id": parent_vid, "content": bad_content.model_dump(mode="json"), "review": "draft", "note": ""},
    )
    assert res_edit.status_code == 422

    # Save a server version containing the chart referencing s_noexp -> export MUST return 403
    from app.research_topics import VersionInput

    updated = api.research_topics.add_version(
        account_id,
        topic_id,
        VersionInput(parent_version_id=parent_vid, content=bad_content, review="draft", note=""),
    )
    v_with_chart = updated.versions[-1].version_id
    res_export_blocked = client.get(f"/v2/research/topics/{topic_id}/versions/{v_with_chart}/export", headers=headers)
    assert res_export_blocked.status_code == 403

    # Also test chart referencing exportable displayable source -> export succeeds and includes source provenance
    s_pub_hp = api.research_sources.create(
        account_id,
        api.SourceInput(
            title="Public Market Series",
            text="NVDA | 2025-01-02 | daily close | 120.00 USD",
            kind="market_data",
            permissions=SourcePermissions(store=True, display=True, model_input=True, export=True),
        ),
    )
    pub_chart = build_research_chart(
        CreateChartRequest.model_validate(
            {
                "chart_type": "price_series",
                "title": "Public Prices",
                "y_axes": [{"id": "left", "label": "Price"}],
                "series": [{"name": "NVDA", "mark": "line", "binding": {"kind": "historical_prices", "source_id": s_pub_hp.source_id}}],
                "rationale": "Plot exportable price series",
            }
        ),
        facts=[],
        calculations=[],
        sources={s_pub_hp.source_id: s_pub_hp},
    )
    pub_content = loaded_topic.versions[-1].content.model_copy(deep=True)
    pub_content.charts = [pub_chart]
    updated_pub = api.research_topics.add_version(
        account_id,
        topic_id,
        VersionInput(parent_version_id=v_with_chart, content=pub_content, review="draft", note=""),
    )
    v_pub = updated_pub.versions[-1].version_id
    res_export_ok = client.get(f"/v2/research/topics/{topic_id}/versions/{v_pub}/export", headers=headers)
    assert res_export_ok.status_code == 200
    export_json = res_export_ok.json()
    assert len(export_json["version"]["content"]["charts"]) == 1
    assert any(item["source_id"] == s_pub_hp.source_id for item in export_json["source_provenance"])


def test_causal_graph_binds_claims_forces_unverified_and_downgrades_unbacked_confirmed_nodes():
    src = _make_source("s1", "Capex acceleration boosts GPU foundry utilization.", title="Industry Note")
    src_sim = _make_source("s-sim", "Simulated macro shock.", is_simulated=True)
    claims = [
        {
            "claim_id": "claim-capex",
            "citations": [{"source_id": "s1", "quotation": "Capex acceleration boosts GPU foundry utilization."}],
        },
        {
            "claim_id": "claim-sim",
            "citations": [{"source_id": "s-sim", "quotation": "Simulated macro shock."}],
        },
    ]
    as_of = datetime(2025, 4, 1, tzinfo=timezone.utc)

    # Edge 1 backed by claim-capex -> unverified_assumption False.
    # Edge 2 omits claim_ids (even attempting unverified_assumption=False) -> server forces True.
    # Node 'company' asks for status='confirmed' without any verified edge -> server downgrades to 'assumption'.
    req = CreateChartRequest.model_validate(
        {
            "chart_type": "causal_graph",
            "title": "AI Capex Transmission Chain",
            "causal_graph": {
                "nodes": [
                    {"id": "macro", "label": "Hyperscaler AI Capex", "category": "macro", "status": "confirmed"},
                    {"id": "industry", "label": "Foundry Utilization", "category": "industry", "status": "confirmed"},
                    {
                        "id": "company",
                        "label": "Supplier Gross Margin",
                        "category": "company",
                        "status": "confirmed",
                        "detail": "Assumes HBM supply remains tight",
                    },
                    {"id": "scenario", "label": "Capex Slowdown Case", "category": "scenario", "status": "invalidated"},
                ],
                "edges": [
                    {
                        "from_id": "macro",
                        "to_id": "industry",
                        "relation": "drives wafer demand",
                        "direction": "positive",
                        "claim_ids": ["claim-capex"],
                    },
                    {
                        "from_id": "industry",
                        "to_id": "company",
                        "relation": "supports pricing power",
                        "direction": "conditional",
                        "claim_ids": [],
                        "unverified_assumption": False,
                    },
                    {
                        "from_id": "scenario",
                        "to_id": "company",
                        "relation": "compresses utilization",
                        "direction": "negative",
                        "claim_ids": [],
                    },
                ],
            },
            "rationale": "Show verified and assumed causal links.",
        }
    )
    chart = build_research_chart(req, facts=[], calculations=[], sources={"s1": src}, claims=claims, as_of=as_of)
    assert chart.chart_type == "causal_graph"
    assert chart.axes == [] and chart.series == []
    assert chart.causal_graph is not None
    assert chart.source_ids == ["s1"]
    assert chart.has_projected_points is True
    assert chart.is_simulated is False
    assert "未核实假设" in chart.subtitle
    assert "2025-04-01" in chart.subtitle

    node_status = {n.id: n.status for n in chart.causal_graph.nodes}
    assert node_status["macro"] == "confirmed"
    assert node_status["industry"] == "confirmed"
    # No verified claim-backed edge touches 'company', so 'confirmed' is downgraded to 'assumption'
    assert node_status["company"] == "assumption"
    assert node_status["scenario"] == "invalidated"

    edges = chart.causal_graph.edges
    assert edges[0].unverified_assumption is False
    assert edges[1].unverified_assumption is True
    assert edges[2].unverified_assumption is True

    # Fully verified 2-node causal graph sets has_projected_points=False
    req_verified = CreateChartRequest.model_validate(
        {
            "chart_type": "causal_graph",
            "title": "Verified Chain",
            "causal_graph": {
                "nodes": [
                    {"id": "macro", "label": "Capex", "category": "macro", "status": "confirmed"},
                    {"id": "industry", "label": "Utilization", "category": "industry", "status": "confirmed"},
                ],
                "edges": [
                    {
                        "from_id": "macro",
                        "to_id": "industry",
                        "relation": "lifts demand",
                        "direction": "positive",
                        "claim_ids": ["claim-capex"],
                    }
                ],
            },
            "rationale": "Fully verified 2-node graph.",
        }
    )
    verified_chart = build_research_chart(req_verified, facts=[], calculations=[], sources={"s1": src}, claims=claims)
    assert verified_chart.has_projected_points is False
    assert "已绑定研究引证" in verified_chart.subtitle

    # Simulated source referenced via claim propagates is_simulated=True when allow_simulated=True
    req_sim = CreateChartRequest.model_validate(
        {
            "chart_type": "causal_graph",
            "title": "Sim Chain",
            "causal_graph": {
                "nodes": [
                    {"id": "a", "label": "Shock", "category": "macro", "status": "confirmed"},
                    {"id": "b", "label": "Asset", "category": "asset", "status": "confirmed"},
                ],
                "edges": [
                    {
                        "from_id": "a",
                        "to_id": "b",
                        "relation": "impacts",
                        "direction": "negative",
                        "claim_ids": ["claim-sim"],
                    }
                ],
            },
            "rationale": "Simulated causal graph check.",
        }
    )
    with pytest.raises(ChartBuildError):
        build_research_chart(req_sim, facts=[], calculations=[], sources={"s-sim": src_sim}, claims=claims)
    sim_chart = build_research_chart(
        req_sim, facts=[], calculations=[], sources={"s-sim": src_sim}, claims=claims, allow_simulated=True
    )
    assert sim_chart.is_simulated is True
    assert "模拟数据" in sim_chart.subtitle


def test_causal_graph_fail_closed_structural_and_permission_checks():
    src_ok = _make_source("s-ok", "Verified text.")
    src_hidden = _make_source("s-hidden", "Hidden text.", display=False)
    claims = [
        {"claim_id": "c-ok", "citations": [{"source_id": "s-ok"}]},
        {"claim_id": "c-hidden", "citations": [{"source_id": "s-hidden"}]},
    ]

    # Mutual exclusivity & extra='forbid'
    with pytest.raises(ValidationError):
        CreateChartRequest.model_validate(
            {
                "chart_type": "causal_graph",
                "title": "Missing graph",
                "rationale": "r",
            }
        )
    with pytest.raises(ValidationError):
        CreateChartRequest.model_validate(
            {
                "chart_type": "causal_graph",
                "title": "Mixed numeric + graph",
                "y_axes": [{"id": "left", "label": "Y"}],
                "causal_graph": {
                    "nodes": [
                        {"id": "n1", "label": "A", "category": "macro"},
                        {"id": "n2", "label": "B", "category": "asset"},
                    ],
                    "edges": [{"from_id": "n1", "to_id": "n2", "relation": "leads to"}],
                },
                "rationale": "r",
            }
        )
    with pytest.raises(ValidationError):
        CreateChartRequest.model_validate(
            {
                "chart_type": "bar",
                "title": "Bar with causal_graph",
                "y_axes": [{"id": "left", "label": "Y"}],
                "series": [{"name": "S", "mark": "bar", "binding": {"kind": "facts", "fact_ids": ["f1"]}}],
                "causal_graph": {
                    "nodes": [
                        {"id": "n1", "label": "A", "category": "macro"},
                        {"id": "n2", "label": "B", "category": "asset"},
                    ],
                    "edges": [{"from_id": "n1", "to_id": "n2", "relation": "leads to"}],
                },
                "rationale": "r",
            }
        )
    with pytest.raises(ValidationError):
        ResearchChart.model_validate(
            {
                "chart_id": "chart-bad",
                "chart_type": "causal_graph",
                "title": "Bad",
                "axes": [],
                "series": [],
                "causal_graph": None,
            }
        )

    # > 20 nodes rejected
    with pytest.raises(ValidationError):
        CreateChartRequest.model_validate(
            {
                "chart_type": "causal_graph",
                "title": "Too many nodes",
                "causal_graph": {
                    "nodes": [{"id": f"n{i}", "label": f"Node {i}", "category": "macro"} for i in range(21)],
                    "edges": [{"from_id": f"n{i}", "to_id": f"n{i + 1}", "relation": "next"} for i in range(20)],
                },
                "rationale": "r",
            }
        )

    # Self-loop rejected
    with pytest.raises(ValidationError):
        CreateChartRequest.model_validate(
            {
                "chart_type": "causal_graph",
                "title": "Self loop",
                "causal_graph": {
                    "nodes": [
                        {"id": "n1", "label": "A", "category": "macro"},
                        {"id": "n2", "label": "B", "category": "asset"},
                    ],
                    "edges": [{"from_id": "n1", "to_id": "n1", "relation": "loop"}],
                },
                "rationale": "r",
            }
        )

    # Dangling edge endpoint & isolated/disconnected node rejected
    with pytest.raises(ValidationError):
        CreateChartRequest.model_validate(
            {
                "chart_type": "causal_graph",
                "title": "Missing target",
                "causal_graph": {
                    "nodes": [
                        {"id": "n1", "label": "A", "category": "macro"},
                        {"id": "n2", "label": "B", "category": "asset"},
                    ],
                    "edges": [{"from_id": "n1", "to_id": "n3", "relation": "missing"}],
                },
                "rationale": "r",
            }
        )
    with pytest.raises(ValidationError):
        CreateChartRequest.model_validate(
            {
                "chart_type": "causal_graph",
                "title": "Isolated node",
                "causal_graph": {
                    "nodes": [
                        {"id": "n1", "label": "A", "category": "macro"},
                        {"id": "n2", "label": "B", "category": "asset"},
                        {"id": "n3", "label": "Orphan", "category": "company"},
                    ],
                    "edges": [{"from_id": "n1", "to_id": "n2", "relation": "linked"}],
                },
                "rationale": "r",
            }
        )

    # Unknown claim_id fails closed
    req_unknown_claim = CreateChartRequest.model_validate(
        {
            "chart_type": "causal_graph",
            "title": "Unknown claim",
            "causal_graph": {
                "nodes": [
                    {"id": "n1", "label": "A", "category": "macro"},
                    {"id": "n2", "label": "B", "category": "asset"},
                ],
                "edges": [{"from_id": "n1", "to_id": "n2", "relation": "linked", "claim_ids": ["claim-missing"]}],
            },
            "rationale": "r",
        }
    )
    with pytest.raises(ChartBuildError):
        build_research_chart(req_unknown_claim, facts=[], calculations=[], sources={"s-ok": src_ok}, claims=claims)

    # Cited claim whose source has display=False fails closed
    req_hidden_src = CreateChartRequest.model_validate(
        {
            "chart_type": "causal_graph",
            "title": "Hidden claim source",
            "causal_graph": {
                "nodes": [
                    {"id": "n1", "label": "A", "category": "macro"},
                    {"id": "n2", "label": "B", "category": "asset"},
                ],
                "edges": [{"from_id": "n1", "to_id": "n2", "relation": "linked", "claim_ids": ["c-hidden"]}],
            },
            "rationale": "r",
        }
    )
    with pytest.raises(ChartBuildError):
        build_research_chart(
            req_hidden_src, facts=[], calculations=[], sources={"s-hidden": src_hidden}, claims=claims
        )


def test_causal_graph_runtime_tool_progress_and_export_permissions(monkeypatch):
    src_text = "Power grid constraints delayed hyperscaler datacenter ramp."
    src = _make_source("s1", src_text, title="Grid Report")

    def resolver_turn1(step, messages):
        if step == 0:
            return _tool_turn("read_sources", {"reads": [{"source_id": "s1"}], "rationale": "read"}, "cg-t1-a")
        return _tool_turn(
            "submit_research",
            {
                "status": "completed",
                "report": "Grid constraints delay deployment.",
                "claims": [
                    {
                        "text": "Power grid delays datacenter ramp.",
                        "source_id": "s1",
                        "quotation": "Power grid constraints delayed hyperscaler datacenter ramp.",
                        "role": "support",
                    }
                ],
                "limitations": [],
                "stopping_reason": "answered",
            },
            "cg-t1-b",
        )

    out1 = run_research_agent(question="Grid bottleneck?", sources=[src], model=_ScriptedModel(resolver_turn1), context=_context())
    assert out1.status == "completed" and len(out1.claims) == 1
    now = datetime.now(timezone.utc)
    turn1 = ResearchTurn(
        turn_id="turn-1",
        index=1,
        question="Grid bottleneck?",
        question_origin="user",
        operation_ids=["op-cg-1"],
        parent_version_id="v1",
        started_at=now,
        finished_at=now,
        status="completed",
        reason="submitted",
        report=out1.report,
        claim_ids=[out1.claims[0]["claim_id"]],
        search_status="not_requested",
        official_fetch_status="not_requested",
        usage=TurnUsage(model_calls=2, search_calls=0, total_tokens=4),
        events=out1.events,
    )
    c1 = merge_turn(
        ResearchContent(),
        turn1,
        claims=out1.claims,
        facts=out1.facts,
        calculations=out1.calculations,
        sources=[],
        source_candidates=[],
    )
    v2 = ResearchVersion(
        version_id="v2",
        parent_version_id="v1",
        created_at=now,
        created_by="agent",
        review="draft",
        source_ids=["s1"],
        content=c1,
    )
    prior = prior_research_input(v2)

    def resolver_turn2(step, messages):
        if step == 0:
            prior_claims = json.loads(messages[1]["content"])["prior_research"]["claims"]
            return _tool_turn(
                "create_chart",
                {
                    "chart_type": "causal_graph",
                    "title": "Grid -> Compute -> Cloud Revenue",
                    "causal_graph": {
                        "nodes": [
                            {"id": "grid", "label": "Power Grid Bottleneck", "category": "macro", "status": "confirmed"},
                            {"id": "dc", "label": "Datacenter Ramp Delay", "category": "industry", "status": "confirmed"},
                            {"id": "rev", "label": "Cloud Revenue Recognition", "category": "company", "status": "assumption"},
                        ],
                        "edges": [
                            {
                                "from_id": "grid",
                                "to_id": "dc",
                                "relation": "constrains energization",
                                "direction": "negative",
                                "claim_ids": [prior_claims[0]["claim_id"]],
                            },
                            {
                                "from_id": "dc",
                                "to_id": "rev",
                                "relation": "pushes out billing",
                                "direction": "negative",
                                "claim_ids": [],
                            },
                        ],
                    },
                    "rationale": "Build causal transmission chain.",
                },
                "cg-t2-a",
            )
        res = json.loads([m for m in messages if m.get("role") == "tool"][-1]["content"])
        return _tool_turn(
            "submit_research",
            {
                "status": "completed",
                "report": f"Causal map:\n\n{res['embed_marker']}\n",
                "claims": [],
                "limitations": [],
                "stopping_reason": "causal graph added",
            },
            "cg-t2-b",
        )

    assert "causal_graph" in SYSTEM
    out2 = run_research_agent(
        question="Draw causal transmission map",
        sources=[src],
        model=_ScriptedModel(resolver_turn2),
        context=_context(),
        prior=prior,
    )
    assert out2.status == "completed"
    assert len(out2.charts) == 1
    cg = out2.charts[0]
    assert cg["chart_type"] == "causal_graph"
    assert cg["source_ids"] == ["s1"]
    assert cg["has_projected_points"] is True
    assert "s1" in out2.source_ids
    assert f"[[chart:{cg['chart_id']}]]" in out2.report
    cg_events = [sanitize_event(e) for e in out2.events if e.get("kind") == "tool" and e.get("name") == "create_chart"]
    assert len(cg_events) == 1
    assert cg_events[0]["summary"] == {
        "kind": "create_chart",
        "chart_id": cg["chart_id"],
        "chart_type": "causal_graph",
        "title": "Grid -> Compute -> Cloud Revenue",
        "series_count": 3,
        "point_count": 2,
    }

    # Verify export permission check propagates through causal_graph claim -> source_id
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(api.app)
    reg = client.post(
        "/v1/auth/local/register",
        json={"email": "causal-export@example.test", "password": "correct horse battery staple!"},
    ).json()
    headers = {"Authorization": "Bearer " + reg["access_token"]}
    account_id = client.get("/v1/account", headers=headers).json()["account"]["account_id"]

    s_pub = client.post(
        "/v2/research/sources",
        headers=headers,
        json={
            "title": "Public Note",
            "text": "Public observation.",
            "kind": "user_supplied",
            "permissions": {"store": True, "display": True, "model_input": True, "export": True},
        },
    ).json()
    s_noexp = api.research_sources.create(
        account_id,
        api.SourceInput(
            title="Restricted Note",
            text="Grid note.",
            kind="user_supplied",
            permissions=SourcePermissions(store=True, display=True, model_input=True, export=False),
        ),
    )
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "CG export?", "source_ids": [s_pub["source_id"]]}).json()
    tid = topic["topic_id"]
    pvid = topic["versions"][-1]["version_id"]
    cg_restricted = build_research_chart(
        CreateChartRequest.model_validate(
            {
                "chart_type": "causal_graph",
                "title": "Restricted Claim Link",
                "causal_graph": {
                    "nodes": [
                        {"id": "n1", "label": "Grid", "category": "macro", "status": "confirmed"},
                        {"id": "n2", "label": "Cloud", "category": "company", "status": "confirmed"},
                    ],
                    "edges": [{"from_id": "n1", "to_id": "n2", "relation": "limits", "claim_ids": ["cl-noexp"]}],
                },
                "rationale": "Check export block on causal claim source.",
            }
        ),
        facts=[],
        calculations=[],
        sources={s_noexp.source_id: s_noexp},
        claims=[{"claim_id": "cl-noexp", "citations": [{"source_id": s_noexp.source_id}]}],
    )
    from app.research_topics import VersionInput

    content_with_cg = api.research_topics.get(account_id, tid).versions[-1].content.model_copy(deep=True)
    content_with_cg.charts = [cg_restricted]
    v_cg = api.research_topics.add_version(
        account_id,
        tid,
        VersionInput(parent_version_id=pvid, content=content_with_cg, review="draft", note=""),
    ).versions[-1].version_id
    assert client.get(f"/v2/research/topics/{tid}/versions/{v_cg}/export", headers=headers).status_code == 403


def _price_chart(source, normalize=False):
    req = CreateChartRequest.model_validate({
        "chart_type": "price_series", "title": "close", "y_axes": [{"id": "left", "label": "Price"}],
        "series": [{"name": "S", "mark": "line",
                    "binding": {"kind": "historical_prices", "source_id": source.source_id, "normalize_base_100": normalize}}],
        "rationale": "Plot closes."})
    return build_research_chart(req, facts=[], calculations=[], sources={source.source_id: source})


@pytest.mark.parametrize("provider_lines, closes, currency", [
    # twelve_data: decimals, volume, ISO currency, no adjustment
    (["AAPL@NASDAQ | 2026-11-19 | 1day | O 270.1 H 273 L 269.5 C 272.25 V 41000000 USD",
      "AAPL@NASDAQ | 2026-11-20 | 1day | O 272 H 275 L 271 C 274.5 V 39000000 USD"], ["272.25", "274.5"], "USD"),
    # tushare: unadjusted prices, ADJF suffix after the currency
    (["600519@SSE | 2026-11-19 | 1day | O 1450.00 H 1470.00 L 1440.00 C 1460.50 V 2300000 CNY ADJF 8.123",
      "600519@SSE | 2026-11-20 | 1day | O 1460.00 H 1475.00 L 1455.00 C 1470.00 V 2100000 CNY ADJF 8.123"], ["1460.5", "1470"], "CNY"),
    # yahoo: split-adjusted close, adj_factor = adjclose / close, missing volume
    (["0700@HKEX | 2026-11-19 | 1day | O 520 H 525 L 518 C 523.5 V - HKD ADJF 0.9876",
      "0700@HKEX | 2026-11-20 | 1day | O 523 H 530 L 521 C 528 V - HKD ADJF 0.9876"], ["523.5", "528"], "HKD"),
    # baostock: derived adj_factor, scientific notation volume
    (["000001@SZSE | 2026-11-19 | 1day | O 11.2 H 11.4 L 11.1 C 11.3 V 1.5E+8 CNY ADJF 1.25",
      "000001@SZSE | 2026-11-20 | 1day | O 11.3 H 11.5 L 11.2 C 11.45 V 1.4E+8 CNY ADJF 1.25"], ["11.3", "11.45"], "CNY"),
])
def test_market_data_provider_line_formats_chart_close(provider_lines, closes, currency):
    src = _make_source("md-p", "\n".join(provider_lines) + "\n", kind="market_data", provenance={"currency": currency})
    chart = _price_chart(src)
    assert [p.y for p in chart.series[0].points] == closes and chart.axes[0].currency == currency


def test_market_data_without_line_currency_uses_provenance_and_adjf():
    text = "600519@SSE | 2026-11-19 | 1day | O 1 H 2 L 1 C 1.5 V 10 ADJF 2.0\n600519@SSE | 2026-11-20 | 1day | O 1 H 2 L 1 C 1.6 V 10 ADJF 2.0\n"
    chart = _price_chart(_make_source("md-n", text, kind="market_data", provenance={"currency": "CNY"}))
    assert chart.axes[0].currency == "CNY" and len(chart.series[0].points) == 2
    with pytest.raises(ChartBuildError):
        _price_chart(_make_source("md-n2", text, kind="market_data", provenance={}))


def test_market_data_strict_validation_rejects_bad_sources():
    good = "A@SSE | 2026-11-19 | 1day | O 1 H 2 L 1 C 1.5 V 10 CNY"
    cases = {
        "two instruments": good + "\nB@SSE | 2026-11-20 | 1day | O 1 H 2 L 1 C 1.5 V 10 CNY",
        "mixed currency": good + "\nA@SSE | 2026-11-20 | 1day | O 1 H 2 L 1 C 1.5 V 10 USD",
        "bad date": "A@SSE | 2026-13-40 | 1day | O 1 H 2 L 1 C 1.5 V 10 CNY",
        "bad number": "A@SSE | 2026-11-19 | 1day | O 1 H 2 L 1 C abc V 10 CNY",
        "bad adjf": good + " ADJF x",
        "zero close": "A@SSE | 2026-11-19 | 1day | O 1 H 2 L 1 C 0 V 10 CNY",
        "duplicate date": good + "\n" + good,
        "mixed interval": good + "\nA@SSE | 2026-11-26 | 1week | O 1 H 2 L 1 C 1.5 V 10 CNY",
    }
    for name, text in cases.items():
        with pytest.raises(ChartBuildError):
            _price_chart(_make_source("md-bad", text + "\n", kind="market_data", provenance={"currency": "CNY"}))


def test_market_data_normalize_base_100_and_legacy_daily_close_still_work():
    text = "A@SSE | 2026-11-19 | 1day | O 1 H 2 L 1 C 50 V 10 CNY ADJF 2\nA@SSE | 2026-11-20 | 1day | O 1 H 2 L 1 C 55 V 10 CNY ADJF 2\n"
    chart = _price_chart(_make_source("md-b", text, kind="market_data", provenance={"currency": "CNY"}), normalize=True)
    assert [p.y for p in chart.series[0].points] == ["100", "110"]
    legacy = "NVDA | 2025-01-02 | daily close | 105.00 USD\nNVDA | 2025-01-03 | daily close | 110.00 USD\n"
    chart = _price_chart(_make_source("hp-l", legacy, kind="market_data", provenance={"currency": "USD"}))
    assert [p.y for p in chart.series[0].points] == ["105", "110"]
