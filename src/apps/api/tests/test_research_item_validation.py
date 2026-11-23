"""Unit and fault-injection tests for tool-boundary item persistence validation."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import app.research_runtime as research_runtime
from app.model_protocol import ModelResponse, ModelToolCall, ModelUsage
from app.research_budget import ResearchExecutionBudget
from app.research_calculations import CalculationRequest, calculate, calculation_input_status
from app.research_charts import CreateChartRequest, build_research_chart
from app.research_coordinator import ResearchExecutionContext
from app.research_item_validator import (
    ItemPersistenceValidationError,
    sanitize_validation_errors,
    validate_calculation,
    validate_chart,
    validate_claim,
    validate_fact,
)
from app.research_sources import ResearchSource, SourcePermissions
from app.research_topics import ResearchCalculation, ResearchContent, ResearchTurn, TurnUsage
from app.research_turns import merge_turn


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


def _make_source(
    source_id: str,
    text: str,
    *,
    provenance: dict[str, Any] | None = None,
) -> ResearchSource:
    return ResearchSource(
        source_id=source_id,
        owner_id="user-1",
        title="Filing Source",
        text=text,
        kind="official",
        published_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        obtained_at=datetime(2026, 2, 5, tzinfo=timezone.utc),
        content_sha256=sha256(text.encode("utf-8")).hexdigest(),
        retrieval_method="sec_filing_html",
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
        provenance=provenance or {},
    )


def _tool_response(calls: tuple[ModelToolCall, ...]) -> ModelResponse:
    return ModelResponse(
        {"role": "assistant", "content": []},
        calls,
        ModelUsage(10, 5, 15),
        "tool_use",
        "tool_calls",
    )


def _stop_response() -> ModelResponse:
    return ModelResponse({"role": "assistant", "content": "done"}, (), ModelUsage(10, 5, 15), "end_turn", "stop")


class _ScriptedModel:
    policy = SimpleNamespace(timeout_seconds=10, max_input_tokens=None, max_output_tokens=2000)

    def __init__(self, handler):
        self.handler = handler
        self.step = 0
        self.turns = []

    def estimate_turn_input(self, turn_obj):
        return 100

    def complete_turn(self, turn_obj, **kwargs):
        self.turns.append(turn_obj)
        step = self.step
        self.step += 1
        return self.handler(step, turn_obj)


@pytest.mark.parametrize(
    ("calc_payload", "expected_formula"),
    [
        (
            {
                "formula": "growth_percent",
                "first": {"fact_id": "f2", "value": "120", "unit": "USD millions", "currency": "USD", "period": "2025"},
                "second": {"fact_id": "f1", "value": "100", "unit": "USD millions", "currency": "USD", "period": "2024"},
            },
            "growth_percent",
        ),
        (
            {
                "formula": "margin_percent",
                "first": {"fact_id": "f1", "value": "30", "unit": "USD millions", "currency": "USD", "period": "2025"},
                "second": {"fact_id": "f2", "value": "100", "unit": "USD millions", "currency": "USD", "period": "2025"},
            },
            "margin_percent",
        ),
        (
            {
                "formula": "difference",
                "first": {"fact_id": "f2", "value": "120", "unit": "USD millions", "currency": "USD", "period": "2025"},
                "second": {"fact_id": "f1", "value": "100", "unit": "USD millions", "currency": "USD", "period": "2024"},
            },
            "difference",
        ),
        (
            {
                "formula": "ratio",
                "first": {"fact_id": "f2", "value": "150", "unit": "USD millions", "currency": "USD", "period": "2025"},
                "second": {"fact_id": "f1", "value": "100", "unit": "USD millions", "currency": "USD", "period": "2025"},
            },
            "ratio",
        ),
        (
            {
                "formula": "cagr",
                "first": {"fact_id": "f2", "value": "133.1", "unit": "USD billions", "currency": "USD", "period": "FY2025"},
                "second": {"fact_id": "f1", "value": "100", "unit": "USD billions", "currency": "USD", "period": "FY2022"},
                "periods": "3",
            },
            "cagr",
        ),
        (
            {
                "formula": "sum",
                "facts": [
                    {"fact_id": "f1", "value": "10", "unit": "USD billions", "currency": "USD", "period": "2025-Q1"},
                    {"fact_id": "f2", "value": "15000", "unit": "USD millions", "currency": "USD", "period": "2025-Q2"},
                ],
            },
            "sum",
        ),
        (
            {
                "formula": "subtract",
                "facts": [
                    {"fact_id": "f1", "value": "100", "unit": "USD millions", "currency": "USD", "period": "2025"},
                    {"fact_id": "f2", "value": "30", "unit": "USD millions", "currency": "USD", "period": "2025"},
                    {"fact_id": "f3", "value": "20", "unit": "USD millions", "currency": "USD", "period": "2025"},
                ],
            },
            "subtract",
        ),
        (
            {
                "formula": "weighted_average",
                "facts": [
                    {"fact_id": "f1", "value": "10", "unit": "percent", "currency": None, "period": "2024"},
                    {"fact_id": "f2", "value": "20", "unit": "percent", "currency": None, "period": "2025"},
                ],
                "weights": ["1", "3"],
            },
            "weighted_average",
        ),
        (
            {
                "formula": "ttm_sum",
                "facts": [
                    {"fact_id": "q1", "value": "30", "unit": "USD billions", "currency": "USD", "period": "2025-Q1"},
                    {"fact_id": "q2", "value": "31", "unit": "USD billions", "currency": "USD", "period": "2025-Q2"},
                    {"fact_id": "q3", "value": "32", "unit": "USD billions", "currency": "USD", "period": "2025-Q3"},
                    {"fact_id": "q4", "value": "40100", "unit": "USD millions", "currency": "USD", "period": "2025-Q4"},
                ],
            },
            "ttm_sum",
        ),
    ],
)
def test_validate_calculation_accepts_all_nine_formulas_without_mutating_input(calc_payload, expected_formula):
    req = CalculationRequest.model_validate(calc_payload)
    row = {"calculation_id": "calc-1", **calculate(req)}
    facts_list = [calc_payload[k] for k in ("first", "second") if k in calc_payload] + list(calc_payload.get("facts") or [])
    row["input_status"] = calculation_input_status(row, facts_list)
    snapshot = json.loads(json.dumps(row))

    validated = validate_calculation(row)
    assert validated.formula_id == expected_formula
    assert validated.turn_id == "turn-validation"
    assert row == snapshot
    assert "turn_id" not in row

    validated_with_real_turn = validate_calculation(row, turn_id="turn-real-123")
    assert validated_with_real_turn.turn_id == "turn-real-123"
    assert "turn_id" not in row


def test_validate_fact_chart_and_claim_accept_valid_dicts_without_mutating():
    text = "Revenue was 100 USD in FY2024 and 120 USD in FY2025."
    src = _make_source("s1", text)
    fact_dict = {
        "fact_id": "fact-1",
        "label": "Revenue",
        "value": "100",
        "unit": "USD",
        "currency": "USD",
        "period": "FY2024",
        "source_id": "s1",
        "quotation": "100 USD in FY2024",
        "character_start": text.index("100 USD in FY2024"),
        "character_end": text.index("100 USD in FY2024") + len("100 USD in FY2024"),
        "location": {
            "character_start": text.index("100 USD in FY2024"),
            "character_end": text.index("100 USD in FY2024") + len("100 USD in FY2024"),
        },
        "verification": "numeric_and_location_match_semantics_not_verified",
    }
    fact_snap = json.loads(json.dumps(fact_dict))
    assert validate_fact(fact_dict).fact_id == "fact-1"
    assert fact_dict == fact_snap

    chart_req = CreateChartRequest.model_validate(
        {
            "chart_type": "bar",
            "title": "Revenue",
            "x_label": "Year",
            "y_axes": [{"id": "left", "label": "Revenue", "unit_preference": "ones", "currency": "USD"}],
            "series": [
                {
                    "name": "Rev",
                    "mark": "bar",
                    "y_axis_id": "left",
                    "binding": {"kind": "facts", "fact_ids": ["fact-1"]},
                }
            ],
            "rationale": "Show revenue",
        }
    )
    chart_obj = build_research_chart(
        chart_req,
        facts=[fact_dict],
        calculations=[],
        sources={"s1": src},
        as_of=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    chart_dict = chart_obj.model_dump(mode="json")
    chart_snap = json.loads(json.dumps(chart_dict))
    assert validate_chart(chart_dict).chart_id == chart_obj.chart_id
    assert chart_dict == chart_snap

    claim_dict = {
        "claim_id": "claim-1",
        "role": "support",
        "text": "Revenue was 100 USD in FY2024.",
        "citations": [
            {
                "source_id": "s1",
                "quotation": "100 USD in FY2024",
                "location": {
                    "character_start": fact_dict["character_start"],
                    "character_end": fact_dict["character_end"],
                },
            }
        ],
        "challenges_claim_ids": [],
    }
    claim_snap = json.loads(json.dumps(claim_dict))
    assert validate_claim(claim_dict).claim_id == "claim-1"
    assert claim_dict == claim_snap


def test_validator_rejects_invalid_items_and_sanitizes_errors():
    secret = "TOP_SECRET_RAW_INPUT_VALUE"
    try:
        ResearchCalculation.model_validate(
            {
                "calculation_id": "calc-bad",
                "formula_id": secret,
                "formula_version": "1",
                "expression": "a/b",
                "input_verification": "not_verified_by_calculator",
                "input_precision_status": "not_established",
                "inputs": [
                    {"fact_id": "f1", "value": "10", "unit": "USD", "currency": "USD", "period": "2024"},
                    {"fact_id": "f2", "value": "20", "unit": "USD", "currency": "USD", "period": "2025"},
                    {"fact_id": "f3", "value": "30", "unit": "USD", "currency": "USD", "period": "2026"},
                ],
                "value": "2.0",
                "unit": "ratio",
                "rounding": "half_up",
                "decimal_places": 2,
                "unexpected_extra": secret,
            }
        )
        pytest.fail("Expected ValidationError")
    except ValidationError as err:
        sanitized = sanitize_validation_errors(err)
        assert sanitized == [
            {"loc": "formula_id", "type": "literal_error"},
            {"loc": "unexpected_extra", "type": "extra_forbidden"},
        ]
        assert secret not in json.dumps(sanitized)

    with pytest.raises(ItemPersistenceValidationError) as calc_exc:
        validate_calculation({"formula_id": secret, "inputs": []}, item_index=1)
    assert isinstance(calc_exc.value, ValueError)
    assert calc_exc.value.item_kind == "calculation"
    assert calc_exc.value.item_index == 1
    assert secret not in calc_exc.value.sanitized_message
    assert secret not in json.dumps(calc_exc.value.validation_errors)

    with pytest.raises(ItemPersistenceValidationError) as fact_exc:
        validate_fact(
            {
                "fact_id": "fact-bad",
                "label": "Revenue",
                "value": "100",
                "unit": "USD",
                "period": "2025",
                "source_id": "s1",
                "quotation": secret,
                "character_start": 10,
                "character_end": 5,
                "location": {"character_start": 10, "character_end": 5},
                "verification": "ok",
            }
        )
    assert fact_exc.value.item_kind == "fact"
    assert secret not in fact_exc.value.sanitized_message
    assert secret not in json.dumps(fact_exc.value.validation_errors)

    with pytest.raises(ItemPersistenceValidationError) as chart_exc:
        validate_chart({"chart_id": "chart-bad", "chart_type": secret, "title": secret})
    assert chart_exc.value.item_kind == "chart"
    assert secret not in chart_exc.value.sanitized_message
    assert secret not in json.dumps(chart_exc.value.validation_errors)

    with pytest.raises(ItemPersistenceValidationError) as claim_exc:
        validate_claim("not-a-dict-" + secret)
    assert claim_exc.value.item_kind == "claim"
    assert secret not in claim_exc.value.sanitized_message
    assert claim_exc.value.validation_errors == [{"loc": "", "type": "model_type"}]


def test_runtime_rejects_invalid_calculation_at_tool_boundary(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="app.research_runtime")
    secret = "SECRET_POISON_CALC_INPUT"
    real_calculate = research_runtime.calculate
    call_count = 0

    def poisoned_calculate(request):
        nonlocal call_count
        call_count += 1
        out = real_calculate(request)
        if call_count == 2:
            # First calculation in batch is valid; second injects a poison-pill schema error.
            return {**out, "formula_id": secret, "poison_extra": secret}
        return out

    monkeypatch.setattr(research_runtime, "calculate", poisoned_calculate)
    src = _make_source("s1", "100 USD. 120 USD.")

    initial_state = {
        "user": "Q",
        "history": [],
        "events": [],
        "read_ranges": [],
        "facts": [
            {"fact_id": "f1", "value": "100", "unit": "USD", "currency": "USD", "period": "2024",
             "source_id": src.source_id, "quotation": "100 USD.", "character_start": 0, "character_end": 8},
            {"fact_id": "f2", "value": "120", "unit": "USD", "currency": "USD", "period": "2025",
             "source_id": src.source_id, "quotation": "120 USD.", "character_start": 9, "character_end": 17},
        ],
        "calculations": [],
        "charts": [],
        "tables": [],
        "pending": None,
    }

    def handler(step, turn_obj):
        if step == 0:
            return _tool_response(
                (
                    ModelToolCall(
                        "c1",
                        "calculate_financials",
                        {
                            "calculations": [
                                {"formula": "growth_percent", "first_fact_id": "f2", "second_fact_id": "f1"},
                                {"formula": "ratio", "first_fact_id": "f2", "second_fact_id": "f1"},
                            ],
                            "rationale": "Compute growth and ratio",
                        },
                    ),
                )
            )
        return _stop_response()

    model = _ScriptedModel(handler)
    checkpoints: list[dict[str, Any]] = []
    outcome = research_runtime.run_research_agent(
        question="Q",
        sources=[src],
        model=model,
        context=_context(),
        initial_state=initial_state,
        checkpoint=lambda s: checkpoints.append(json.loads(json.dumps(s))),
    )

    assert outcome.calculations == []
    assert checkpoints and all(cp["calculations"] == [] for cp in checkpoints)
    feedback = json.loads(model.turns[-1].history[-1].results[0].content)
    assert feedback["status"] == "invalid"
    assert feedback["error"] == "persistence_validation_failed"
    assert feedback["item_kind"] == "calculation"
    assert feedback["validation_errors"] == [
        {"loc": "formula_id", "type": "literal_error"},
        {"loc": "poison_extra", "type": "extra_forbidden"},
    ]
    assert secret not in json.dumps(feedback)
    assert "research_tool_item_validation_failed" in caplog.text
    assert "item_kind=calculation" in caplog.text
    assert secret not in caplog.text


def test_runtime_rejects_invalid_fact_at_tool_boundary(caplog):
    caplog.set_level(logging.WARNING, logger="app.research_runtime")
    secret = "SECRET_POISON_FACT_QUOTE"
    text = f"FY2024 revenue was 100 USD. FY2025 revenue was 120 USD {secret}."
    # Page 1 covers valid first sentence (text), page 2 covers OCR sentence with corrupted non-str ocr_engine.
    split = text.index("FY2025")
    src = _make_source(
        "s1",
        text,
        provenance={
            "document_pages": {
                "pages": [[0, split, "text"], [split, len(text), "ocr"]],
                "ocr": {"2": {"engine": 987654, "mean_confidence": 95.0}},
            }
        },
    )

    def handler(step, turn_obj):
        if step == 0:
            return _tool_response(
                (
                    ModelToolCall(
                        "c1",
                        "read_sources",
                        {"reads": [{"source_id": "s1", "start": 0, "end": len(text)}], "rationale": "Read source"},
                    ),
                )
            )
        if step == 1:
            return _tool_response(
                (
                    ModelToolCall(
                        "c2",
                        "record_facts",
                        {
                            "facts": [
                                {
                                    "label": "Revenue FY2024",
                                    "value": 100,
                                    "unit": "USD",
                                    "currency": "USD",
                                    "period": "FY2024",
                                    "source_id": "s1",
                                    "quotation": "FY2024 revenue was 100 USD.",
                                },
                                {
                                    "label": "Revenue FY2025",
                                    "value": 120,
                                    "unit": "USD",
                                    "currency": "USD",
                                    "period": "FY2025",
                                    "source_id": "s1",
                                    "quotation": f"FY2025 revenue was 120 USD {secret}.",
                                },
                            ],
                            "rationale": "Record facts",
                        },
                    ),
                )
            )
        return _stop_response()

    model = _ScriptedModel(handler)
    checkpoints: list[dict[str, Any]] = []
    outcome = research_runtime.run_research_agent(
        question="Q",
        sources=[src],
        model=model,
        context=_context(),
        checkpoint=lambda s: checkpoints.append(json.loads(json.dumps(s))),
    )

    assert outcome.facts == []
    assert checkpoints and all(cp["facts"] == [] for cp in checkpoints)
    feedback = json.loads(model.turns[-1].history[-1].results[0].content)
    assert feedback["status"] == "invalid"
    assert feedback["error"] == "persistence_validation_failed"
    assert feedback["item_kind"] == "fact"
    assert feedback["validation_errors"] == [{"loc": "ocr_engine", "type": "string_type"}]
    assert secret not in json.dumps(feedback)
    assert "987654" not in json.dumps(feedback)
    assert "research_tool_item_validation_failed" in caplog.text
    assert "item_kind=fact" in caplog.text
    assert secret not in caplog.text


def test_runtime_rejects_invalid_chart_at_tool_boundary(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="app.research_runtime")
    secret = "SECRET_POISON_CHART_VALUE"
    text = "Revenue was 100 USD in FY2024."
    src = _make_source("s1", text)

    def poisoned_build_chart(*args, **kwargs):
        return {
            "chart_id": "chart-poison",
            "chart_type": secret,
            "title": secret,
            "axes": [],
            "series": [],
        }

    monkeypatch.setattr(research_runtime, "build_research_chart", poisoned_build_chart)

    initial_state = {
        "user": "Q",
        "history": [],
        "events": [],
        "read_ranges": [{"source_id": "s1", "start": 0, "end": len(text)}],
        "facts": [
            {
                "fact_id": "f1",
                "label": "Revenue",
                "value": "100",
                "unit": "USD",
                "currency": "USD",
                "period": "FY2024",
                "source_id": "s1",
                "quotation": "100 USD in FY2024",
                "character_start": text.index("100 USD in FY2024"),
                "character_end": text.index("100 USD in FY2024") + len("100 USD in FY2024"),
                "location": {
                    "character_start": text.index("100 USD in FY2024"),
                    "character_end": text.index("100 USD in FY2024") + len("100 USD in FY2024"),
                },
                "verification": "numeric_and_location_match_semantics_not_verified",
            }
        ],
        "calculations": [],
        "charts": [],
        "tables": [],
        "pending": None,
    }

    def handler(step, turn_obj):
        if step == 0:
            return _tool_response(
                (
                    ModelToolCall(
                        "c1",
                        "create_chart",
                        {
                            "chart_type": "bar",
                            "title": "Revenue",
                            "x_label": "Period",
                            "y_axes": [{"id": "left", "label": "Revenue", "unit_preference": "ones", "currency": "USD"}],
                            "series": [
                                {
                                    "name": "Rev",
                                    "mark": "bar",
                                    "y_axis_id": "left",
                                    "binding": {"kind": "facts", "fact_ids": ["f1"]},
                                }
                            ],
                            "rationale": "Chart revenue",
                        },
                    ),
                )
            )
        return _stop_response()

    model = _ScriptedModel(handler)
    checkpoints: list[dict[str, Any]] = []
    outcome = research_runtime.run_research_agent(
        question="Q",
        sources=[src],
        model=model,
        context=_context(),
        initial_state=initial_state,
        checkpoint=lambda s: checkpoints.append(json.loads(json.dumps(s))),
    )

    assert outcome.charts == []
    assert checkpoints and all(cp["charts"] == [] for cp in checkpoints)
    feedback = json.loads(model.turns[-1].history[-1].results[0].content)
    assert feedback["status"] == "invalid"
    assert feedback["error"] == "persistence_validation_failed"
    assert feedback["item_kind"] == "chart"
    assert any(err["loc"] == "chart_type" and err["type"] == "literal_error" for err in feedback["validation_errors"])
    assert secret not in json.dumps(feedback)
    assert "research_tool_item_validation_failed" in caplog.text
    assert "item_kind=chart" in caplog.text
    assert secret not in caplog.text


def test_runtime_rejects_invalid_claim_at_submit_boundary_and_allows_recovery(caplog):
    caplog.set_level(logging.WARNING, logger="app.research_runtime")
    secret = "SECRET_POISON_CLAIM_TEXT"
    long_source_id = "s" * 101  # Passes SubmitRequest Claim.source_id (no max_length), fails ResearchClaim Citation (max_length=100).
    long_quote = f"Long source statement {secret}."
    long_src = _make_source(long_source_id, long_quote)
    ok_quote = "Valid source statement."
    ok_src = _make_source("s-ok", ok_quote)

    def handler(step, turn_obj):
        if step == 0:
            return _tool_response(
                (
                    ModelToolCall(
                        "c1",
                        "read_sources",
                        {
                            "reads": [
                                {"source_id": long_source_id, "start": 0, "end": len(long_quote)},
                                {"source_id": "s-ok", "start": 0, "end": len(ok_quote)},
                            ],
                            "rationale": "Read both sources",
                        },
                    ),
                )
            )
        if step == 1:
            return _tool_response(
                (
                    ModelToolCall(
                        "c2",
                        "submit_research",
                        {
                            "report": "## Summary\nFinding.",
                            "claims": [
                                {
                                    "text": f"Claim {secret}",
                                    "source_id": long_source_id,
                                    "quotation": long_quote,
                                    "role": "support",
                                }
                            ],
                            "limitations": [],
                            "stopping_reason": "complete",
                            "status": "completed",
                        },
                    ),
                )
            )
        if step == 2:
            # Verify tool boundary rejected the oversized source_id before saving submission.
            feedback = json.loads(turn_obj.history[-1].results[0].content)
            assert feedback["status"] == "invalid"
            assert feedback["error"] == "persistence_validation_failed"
            assert feedback["item_kind"] == "claim"
            assert feedback["validation_errors"] == [{"loc": "citations.0.source_id", "type": "string_too_long"}]
            assert secret not in json.dumps(feedback)
            assert long_source_id not in json.dumps(feedback)
            return _tool_response(
                (
                    ModelToolCall(
                        "c3",
                        "submit_research",
                        {
                            "report": "## Summary\nValid finding.",
                            "claims": [
                                {
                                    "text": "Valid finding.",
                                    "source_id": "s-ok",
                                    "quotation": ok_quote,
                                    "role": "support",
                                }
                            ],
                            "limitations": [],
                            "stopping_reason": "complete",
                            "status": "completed",
                        },
                    ),
                )
            )
        pytest.fail("Unexpected step")

    model = _ScriptedModel(handler)
    checkpoints: list[dict[str, Any]] = []
    outcome = research_runtime.run_research_agent(
        question="What was the valid finding?",
        sources=[long_src, ok_src],
        model=model,
        context=_context(),
        checkpoint=lambda s: checkpoints.append(json.loads(json.dumps(s))),
    )

    assert outcome.status == "completed"
    assert len(outcome.claims) == 1
    assert outcome.claims[0]["citations"][0]["source_id"] == "s-ok"
    assert "turn_id" not in outcome.claims[0]
    # Earlier checkpoints before step 2 never held an invalid submission.
    for cp in checkpoints[:-1]:
        assert cp.get("submission") is None
    assert "research_tool_item_validation_failed" in caplog.text
    assert "item_kind=claim" in caplog.text
    assert secret not in caplog.text
    assert long_source_id not in caplog.text


def test_runtime_valid_multi_formula_fact_chart_claim_flow_passes_seamlessly():
    text = (
        "Revenue FY2022 was 100 billion USD; FY2025 was 133.1 billion USD. "
        "2025-Q1 USD 30 billion, 2025-Q2 USD 31 billion, 2025-Q3 USD 32 billion, 2025-Q4 USD 40100 million."
    )
    src = _make_source("src-1", text)

    def handler(step, turn_obj):
        if step == 0:
            return _tool_response(
                (
                    ModelToolCall(
                        "c1",
                        "read_sources",
                        {"reads": [{"source_id": "src-1", "start": 0, "end": len(text)}], "rationale": "Read source"},
                    ),
                )
            )
        if step == 1:
            return _tool_response(
                (
                    ModelToolCall(
                        "c2",
                        "record_facts",
                        {
                            "facts": [
                                {
                                    "label": "Revenue FY2022",
                                    "value": 100,
                                    "unit": "USD billions",
                                    "currency": "USD",
                                    "period": "FY2022",
                                    "source_id": "src-1",
                                    "quotation": "FY2022 was 100 billion USD",
                                },
                                {
                                    "label": "Revenue FY2025",
                                    "value": 133.1,
                                    "unit": "USD billions",
                                    "currency": "USD",
                                    "period": "FY2025",
                                    "source_id": "src-1",
                                    "quotation": "FY2025 was 133.1 billion USD",
                                },
                                {
                                    "label": "Q1 Revenue",
                                    "value": 30,
                                    "unit": "USD billions",
                                    "currency": "USD",
                                    "period": "2025-Q1",
                                    "source_id": "src-1",
                                    "quotation": "2025-Q1 USD 30 billion",
                                },
                                {
                                    "label": "Q2 Revenue",
                                    "value": 31,
                                    "unit": "USD billions",
                                    "currency": "USD",
                                    "period": "2025-Q2",
                                    "source_id": "src-1",
                                    "quotation": "2025-Q2 USD 31 billion",
                                },
                                {
                                    "label": "Q3 Revenue",
                                    "value": 32,
                                    "unit": "USD billions",
                                    "currency": "USD",
                                    "period": "2025-Q3",
                                    "source_id": "src-1",
                                    "quotation": "2025-Q3 USD 32 billion",
                                },
                                {
                                    "label": "Q4 Revenue",
                                    "value": 40100,
                                    "unit": "USD millions",
                                    "currency": "USD",
                                    "period": "2025-Q4",
                                    "source_id": "src-1",
                                    "quotation": "2025-Q4 USD 40100 million",
                                },
                            ],
                            "rationale": "Record annual and quarterly figures",
                        },
                    ),
                )
            )
        if step == 2:
            rec = json.loads(turn_obj.history[-1].results[0].content)
            fids = [item["fact_id"] for item in rec["facts"]]
            return _tool_response(
                (
                    ModelToolCall(
                        "c3",
                        "calculate_financials",
                        {
                            "calculations": [
                                {"formula": "ratio", "first_fact_id": fids[1], "second_fact_id": fids[0]},
                                {"formula": "cagr", "first_fact_id": fids[1], "second_fact_id": fids[0], "periods": 3},
                                {"formula": "weighted_average", "fact_ids": fids[:2], "weights": [1, 3]},
                                {"formula": "ttm_sum", "fact_ids": fids[2:6]},
                                {"formula": "growth_percent", "fact_ids": [fids[1], fids[0]]},
                            ],
                            "rationale": "Run binary and multi-fact calculations",
                        },
                    ),
                )
            )
        if step == 3:
            calc_res = json.loads(turn_obj.history[-1].results[0].content)
            assert [c["formula_id"] for c in calc_res["calculations"]] == ["ratio", "cagr", "weighted_average", "ttm_sum", "growth_percent"]
            fids = [c["inputs"][0]["fact_id"] for c in calc_res["calculations"][:2]]
            return _tool_response(
                (
                    ModelToolCall(
                        "c4",
                        "create_chart",
                        {
                            "chart_type": "bar",
                            "title": "Revenue Trajectory",
                            "x_label": "Period",
                            "y_axes": [{"id": "left", "label": "Revenue", "unit_preference": "billions", "currency": "USD"}],
                            "series": [
                                {
                                    "name": "Annual Revenue",
                                    "mark": "bar",
                                    "y_axis_id": "left",
                                    "binding": {"kind": "facts", "fact_ids": [fids[1], fids[0]]},
                                }
                            ],
                            "rationale": "Visualize annual revenue",
                        },
                    ),
                )
            )
        if step == 4:
            chart_res = json.loads(turn_obj.history[-1].results[0].content)
            return _tool_response(
                (
                    ModelToolCall(
                        "c5",
                        "submit_research",
                        {
                            "report": f"## Summary\nRevenue grew to 133.1B.\n\n{chart_res['embed_marker']}",
                            "claims": [
                                {
                                    "text": "FY2025 revenue reached 133.1 billion USD.",
                                    "source_id": "src-1",
                                    "quotation": "FY2025 was 133.1 billion USD",
                                    "role": "support",
                                }
                            ],
                            "limitations": [],
                            "stopping_reason": "complete",
                            "status": "completed",
                        },
                    ),
                )
            )
        pytest.fail("Unexpected step")

    outcome = research_runtime.run_research_agent(
        question="Analyze revenue growth",
        sources=[src],
        model=_ScriptedModel(handler),
        context=_context(),
    )
    assert outcome.status == "completed"
    assert len(outcome.facts) == 6
    assert len(outcome.calculations) == 5
    assert len(outcome.charts) == 1
    assert len(outcome.claims) == 1
    for item in [*outcome.facts, *outcome.calculations, *outcome.claims]:
        assert "turn_id" not in item
    for chart_item in outcome.charts:
        assert chart_item.get("turn_id") is None

    turn = ResearchTurn(
        turn_id="turn-1",
        index=1,
        question="Analyze revenue growth",
        question_origin="user",
        operation_ids=["op-1"],
        parent_version_id="ver-0",
        started_at=datetime(2026, 2, 10, tzinfo=timezone.utc),
        finished_at=datetime(2026, 2, 10, 0, 1, tzinfo=timezone.utc),
        status="completed",
        reason=outcome.reason,
        report=outcome.report,
        claim_ids=[c["claim_id"] for c in outcome.claims],
        fact_ids=[f["fact_id"] for f in outcome.facts],
        calculation_ids=[c["calculation_id"] for c in outcome.calculations],
        chart_ids=[c["chart_id"] for c in outcome.charts],
        usage=TurnUsage(model_calls=5, search_calls=0, total_tokens=75),
        search_status="not_run",
        official_fetch_status="not_run",
    )
    merged = merge_turn(
        ResearchContent(),
        turn,
        claims=outcome.claims,
        facts=outcome.facts,
        calculations=outcome.calculations,
        charts=outcome.charts,
        sources=[],
        source_candidates=[],
    )
    assert len(merged.calculations) == 5
    assert len(merged.facts) == 6
    assert len(merged.charts) == 1
    assert len(merged.claims) == 1
