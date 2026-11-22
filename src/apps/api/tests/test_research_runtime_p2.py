"""Runtime wiring & end-to-end tests for P2 tools: expanded calculate_financials, create_table, analyze_market_series."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace

from app.model_protocol import ModelResponse, ModelToolCall, ModelUsage, canonical_json_dumps, canonicalize_json_value
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext
from app.research_runtime import (
    SYSTEM,
    TOOLS,
    _COVERAGE_UPDATING_TOOLS,
    _PROTECTED_COMPACT_TOOLS,
    run_research_agent,
)
from app.research_sources import ResearchSource, SourcePermissions


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


def _filing_source() -> ResearchSource:
    text = (
        "Apple revenue FY2022 USD 100 billion; FY2025 USD 133.1 billion. "
        "2025-Q1 USD 30 billion, 2025-Q2 USD 31 billion, 2025-Q3 USD 32 billion, "
        "2025-Q4 USD 40100 million."
    )
    return ResearchSource(
        source_id="src-filing",
        owner_id="user-1",
        title="Financial Highlights",
        text=text,
        kind="official",
        published_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        obtained_at=datetime(2026, 2, 5, tzinfo=timezone.utc),
        content_sha256="sha-filing",
        retrieval_method="sec_filing_html",
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
    )


def _market_source() -> ResearchSource:
    days: list[date] = []
    cur = date(2026, 1, 2)
    while len(days) < 30:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    lines = [
        f"AAPL@NASDAQ | {d.isoformat()} | 1d | O {200 + i} H {202 + i} L {199 + i} C {200 + i} V 1000000 USD"
        for i, d in enumerate(days)
    ]
    return ResearchSource(
        source_id="src-mkt-aapl",
        owner_id="user-1",
        title="AAPL Daily Bars",
        text="\n".join(lines),
        kind="market_data",
        published_at=None,
        obtained_at=datetime(2026, 2, 20, tzinfo=timezone.utc),
        content_sha256="sha-mkt",
        retrieval_method="market_data",
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
        provenance={"provider": "yahoo", "exchange": "NASDAQ"},
    )


def test_tools_registry_is_immutable_canonical_and_protects_create_table():
    assert isinstance(TOOLS, tuple)
    names = [t.name for t in TOOLS]
    assert "create_table" in names
    assert "analyze_market_series" in names
    assert names[-1] == "submit_research"
    assert "create_table" in _PROTECTED_COMPACT_TOOLS
    assert "create_table" in _COVERAGE_UPDATING_TOOLS
    assert "analyze_market_series" in _COVERAGE_UPDATING_TOOLS

    # All tool schemas must be deterministically canonicalized (sorted keys) and match round-trip serialization.
    for tool in TOOLS:
        assert tool.input_schema == canonicalize_json_value(tool.input_schema)
        assert json.dumps(tool.input_schema, ensure_ascii=False, separators=(",", ":")) == canonical_json_dumps(tool.input_schema)

    # Stability guard: SYSTEM and serialized TOOLS must not bake calendar years.
    dumped = canonical_json_dumps([t.__dict__ for t in TOOLS]) + SYSTEM
    assert "2025" not in dumped
    assert "2026" not in dumped


def test_runtime_dispatches_expanded_calculations_market_analytics_and_create_table():
    filing = _filing_source()
    mkt = _market_source()

    class ScriptedModel:
        policy = SimpleNamespace(timeout_seconds=10, max_input_tokens=None, max_output_tokens=2000)

        def __init__(self):
            self.step = 0
            self.fact_ids: list[str] = []
            self.calc_ids: list[str] = []
            self.table_id: str | None = None
            self.turns = []

        def estimate_turn_input(self, turn_obj):
            return 100

        def complete_turn(self, turn_obj, **kwargs):
            self.turns.append(turn_obj)
            self.step += 1
            if self.step == 1:
                # Read the filing source before recording facts.
                return ModelResponse(
                    {"role": "assistant", "content": []},
                    (ModelToolCall("c1", "read_sources", {
                        "reads": [{"source_id": "src-filing", "start": 0, "end": len(filing.text)}],
                        "rationale": "Read financial highlights",
                    }),),
                    ModelUsage(10, 5, 15),
                    "tool_use",
                    "tool_calls",
                )
            if self.step == 2:
                facts_payload = [
                    {
                        "label": "Revenue FY2022",
                        "value": 100,
                        "unit": "USD billions",
                        "currency": "USD",
                        "period": "FY2022",
                        "source_id": "src-filing",
                        "quotation": "FY2022 USD 100 billion",
                    },
                    {
                        "label": "Revenue FY2025",
                        "value": 133.1,
                        "unit": "USD billions",
                        "currency": "USD",
                        "period": "FY2025",
                        "source_id": "src-filing",
                        "quotation": "FY2025 USD 133.1 billion",
                    },
                    {
                        "label": "Q1 Revenue",
                        "value": 30,
                        "unit": "USD billions",
                        "currency": "USD",
                        "period": "2025-Q1",
                        "source_id": "src-filing",
                        "quotation": "2025-Q1 USD 30 billion",
                    },
                    {
                        "label": "Q2 Revenue",
                        "value": 31,
                        "unit": "USD billions",
                        "currency": "USD",
                        "period": "2025-Q2",
                        "source_id": "src-filing",
                        "quotation": "2025-Q2 USD 31 billion",
                    },
                    {
                        "label": "Q3 Revenue",
                        "value": 32,
                        "unit": "USD billions",
                        "currency": "USD",
                        "period": "2025-Q3",
                        "source_id": "src-filing",
                        "quotation": "2025-Q3 USD 32 billion",
                    },
                    {
                        "label": "Q4 Revenue",
                        "value": 40100,
                        "unit": "USD millions",
                        "currency": "USD",
                        "period": "2025-Q4",
                        "source_id": "src-filing",
                        "quotation": "2025-Q4 USD 40100 million",
                    },
                ]
                return ModelResponse(
                    {"role": "assistant", "content": []},
                    (ModelToolCall("c2", "record_facts", {"facts": facts_payload, "rationale": "Record annual & quarterly revenues"}),),
                    ModelUsage(10, 5, 15),
                    "tool_use",
                    "tool_calls",
                )
            if self.step == 3:
                rec = json.loads(turn_obj.history[-1].results[0].content)
                self.fact_ids = [item["fact_id"] for item in rec["facts"]]
                return ModelResponse(
                    {"role": "assistant", "content": []},
                    (
                        ModelToolCall("c3a", "calculate_financials", {
                            "calculations": [
                                {
                                    "formula": "cagr",
                                    "first_fact_id": self.fact_ids[1],
                                    "second_fact_id": self.fact_ids[0],
                                    "periods": 3,
                                    "decimal_places": 2,
                                },
                                {
                                    "formula": "ttm_sum",
                                    "fact_ids": self.fact_ids[2:6],
                                    "decimal_places": 2,
                                },
                            ],
                            "rationale": "Compute 3Y CAGR and 2025 TTM revenue across billion/million scales",
                        }),
                        ModelToolCall("c3b", "analyze_market_series", {
                            "source_id": "src-mkt-aapl",
                            "price_basis": "adjusted",
                            "decimal_places": 2,
                        }),
                    ),
                    ModelUsage(10, 5, 15),
                    "tool_use",
                    "tool_calls",
                )
            if self.step == 4:
                calc_res = json.loads(turn_obj.history[-1].results[0].content)
                mkt_res = json.loads(turn_obj.history[-1].results[1].content)
                assert calc_res["calculations"][0]["value"] == "10.00"
                assert calc_res["calculations"][1]["value"] == "133100.00"  # 30B+31B+32B+40,100M in USD millions
                assert mkt_res["status"] == "ok"
                assert mkt_res["bar_count"] == 30
                assert mkt_res["returns"]["5d"] is not None
                self.calc_ids = [item["calculation_id"] for item in calc_res["calculations"]]

                return ModelResponse(
                    {"role": "assistant", "content": []},
                    (ModelToolCall("c4", "create_table", {
                        "table_type": "financial_summary",
                        "title": "Apple Multi-Year Revenue & CAGR",
                        "columns": [
                            {"key": "metric", "label": "Metric"},
                            {"key": "val", "label": "Value", "unit_preference": "USD billions", "currency": "USD"},
                            {"key": "cagr", "label": "3Y CAGR"},
                        ],
                        "rows": [
                            {
                                "label": "FY2025",
                                "cells": {
                                    "metric": {"kind": "text", "text": "Total Revenue"},
                                    "val": {"kind": "fact", "fact_id": self.fact_ids[1]},
                                    "cagr": {"kind": "calculation", "calculation_id": self.calc_ids[0]},
                                },
                            },
                            {
                                "label": "TTM Q1-Q4",
                                "cells": {
                                    "metric": {"kind": "text", "text": "FY2025 Quarterly Sum"},
                                    "val": {"kind": "calculation", "calculation_id": self.calc_ids[1]},
                                    "cagr": {"kind": "empty", "display": "—"},
                                },
                            },
                        ],
                        "rationale": "Summarize revenue levels and CAGR with provenance.",
                    }),),
                    ModelUsage(10, 5, 15),
                    "tool_use",
                    "tool_calls",
                )
            if self.step == 5:
                tbl_res = json.loads(turn_obj.history[-1].results[0].content)
                assert tbl_res["status"] == "created"
                self.table_id = tbl_res["table_id"]
                assert tbl_res["embed_marker"] == f"[[table:{self.table_id}]]"
                assert "133.1" in tbl_res["markdown"]
                return ModelResponse(
                    {"role": "assistant", "content": []},
                    (ModelToolCall("c5", "submit_research", {
                        "report": f"Apple FY2025 revenue reached USD 133.1 billion.\n\n[[table:{self.table_id}]]",
                        "claims": [
                            {
                                "text": "Apple FY2025 revenue was USD 133.1 billion.",
                                "source_id": "src-filing",
                                "quotation": "FY2025 USD 133.1 billion",
                                "role": "support",
                            }
                        ],
                        "limitations": [],
                        "stopping_reason": "answered",
                    }),),
                    ModelUsage(10, 5, 15),
                    "tool_use",
                    "tool_calls",
                )
            raise AssertionError("Unexpected extra step")

    outcome = run_research_agent(
        question="Summarize AAPL multi-year revenue, 3Y CAGR, and recent price momentum.",
        sources=[filing, mkt],
        model=ScriptedModel(),
        context=_context(),
        as_of=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )

    assert outcome.status == "completed"
    assert len(outcome.tables) == 1
    assert outcome.tables[0]["title"] == "Apple Multi-Year Revenue & CAGR"
    # TTM calc (USD 133,100 millions) aligned to column unit_preference "USD billions" -> 133.1!
    assert outcome.tables[0]["rows"][1]["cells"]["val"]["value"] == "133.1"
    assert "src-mkt-aapl" in outcome.source_ids
    assert "src-filing" in outcome.source_ids
    assert any(ev.get("name") == "create_table" and ev.get("status") == "created" for ev in outcome.events)
    assert any(ev.get("name") == "analyze_market_series" and ev.get("status") == "ok" for ev in outcome.events)


def test_runtime_rejects_invalid_table_or_market_analytics_gracefully():
    filing = _filing_source()

    class BadThenSubmitModel:
        policy = SimpleNamespace(timeout_seconds=10, max_input_tokens=None, max_output_tokens=2000)

        def __init__(self):
            self.step = 0

        def estimate_turn_input(self, turn_obj):
            return 100

        def complete_turn(self, turn_obj, **kwargs):
            self.step += 1
            if self.step == 1:
                return ModelResponse(
                    {"role": "assistant", "content": []},
                    (
                        ModelToolCall("b1", "read_sources", {
                            "reads": [{"source_id": "src-filing", "start": 0, "end": 80}],
                            "rationale": "read",
                        }),
                        ModelToolCall("b2", "create_table", {
                            "table_type": "comparison",
                            "title": "Ungrounded",
                            "columns": [{"key": "rev", "label": "Revenue"}],
                            "rows": [{"cells": {"rev": {"kind": "text", "text": "$999 billion"}}}],
                            "rationale": "should fail validation cleanly",
                        }),
                        ModelToolCall("b3", "analyze_market_series", {
                            "source_id": "src-filing",  # official prose, not market_data!
                        }),
                    ),
                    ModelUsage(10, 5, 15),
                    "tool_use",
                    "tool_calls",
                )
            table_err = json.loads(turn_obj.history[-1].results[1].content)
            mkt_err = json.loads(turn_obj.history[-1].results[2].content)
            assert table_err["status"] == "invalid"
            assert mkt_err["status"] == "invalid"
            return ModelResponse(
                {"role": "assistant", "content": []},
                (ModelToolCall("sub", "submit_research", {
                    "report": "Apple revenue FY2025 USD 133.1 billion.",
                    "claims": [
                        {
                            "text": "Apple FY2025 revenue was USD 133.1 billion.",
                            "source_id": "src-filing",
                            "quotation": "FY2025 USD 133.1 billion",
                            "role": "support",
                        }
                    ],
                    "limitations": [],
                    "stopping_reason": "answered",
                }),),
                ModelUsage(10, 5, 15),
                "tool_use",
                "tool_calls",
            )

    outcome = run_research_agent(
        question="Test invalid tool calls recovery.",
        sources=[filing],
        model=BadThenSubmitModel(),
        context=_context(),
    )
    assert outcome.status == "completed"
    assert outcome.tables == []
