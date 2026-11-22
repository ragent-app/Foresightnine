from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
from time import sleep
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError, create_model
import pytest

import app.main as api
from app.operations import ApiProblem, OperationManager
from app.phase3 import ProviderFailure
from app.providers import FixedClock
from app.research_calculations import CalculationRequest, calculate, calculation_input_status
from app.research_diagnostics import safe_exception_diagnostic
from app.research_recovery import RUNTIME_CHECKPOINT_SCHEMA_VERSION, recovery_status
from app.research_topics import (
    ResearchCalculation,
    ResearchContent,
    ResearchTurn,
    TurnUsage,
)
from app.research_turns import merge_turn
from app.telemetry import telemetry
from tests.test_research_runtime import turn


def _wait_for_operation(manager: OperationManager, operation_id: str) -> str:
    for _ in range(200):
        status = manager._cache_operation(operation_id).status
        if status in {"completed", "failed", "cancelled"}:
            return status
        sleep(0.01)
    raise AssertionError(f"Operation {operation_id} did not terminate in time.")


def test_safe_exception_diagnostic_extracts_sanitized_validation_errors():
    secret_formula = "SECRET_UNTRUSTED_FORMULA"
    secret_extra = "SECRET_EXTRA_VALUE"
    secret_input = "SECRET_NESTED_VALUE"

    with pytest.raises(ValidationError) as exc_info:
        ResearchCalculation.model_validate(
            {
                "calculation_id": "",
                "formula_id": secret_formula,
                "formula_version": 123,
                "expression": None,
                "input_verification": None,
                "input_precision_status": None,
                "inputs": [
                    {"fact_id": "f1", "value": secret_input, "unit": 1, "period": "2025"},
                    {"fact_id": "f2", "value": "80", "unit": "USD", "period": "2024"},
                ],
                "value": "10.0",
                "unit": "USD",
                "rounding": "half_up",
                "decimal_places": 99,
                "unexpected_secret_key": secret_extra,
            }
        )

    diagnostic = safe_exception_diagnostic(exc_info.value)
    assert diagnostic["exception_class"] == "ValidationError"
    entries = diagnostic["validation_errors"]
    assert len(entries) == 5
    for entry in entries:
        assert set(entry) == {"loc", "type"}
        assert len(entry["loc"]) <= 120
        assert len(entry["type"]) <= 64

    # Verify model_validator errors report empty loc + value_error without msg/ctx.
    with pytest.raises(ValidationError) as arity_exc:
        ResearchCalculation.model_validate(
            {
                "calculation_id": "c-arity",
                "formula_id": "growth_percent",
                "formula_version": "1",
                "expression": "x",
                "input_verification": "ok",
                "input_precision_status": "ok",
                "inputs": [
                    {"fact_id": "f1", "value": "10", "unit": "USD", "period": "2025"},
                    {"fact_id": "f2", "value": "8", "unit": "USD", "period": "2024"},
                    {"fact_id": "f3", "value": "6", "unit": "USD", "period": "2023"},
                ],
                "value": "25.00",
                "unit": "percent",
                "rounding": "half_up",
                "decimal_places": 2,
            }
        )
    arity_diag = safe_exception_diagnostic(arity_exc.value)
    assert arity_diag["validation_errors"] == [{"loc": "", "type": "value_error"}]
    assert "requires exactly 2 inputs" not in json.dumps(arity_diag)

    serialized = json.dumps(diagnostic)
    assert secret_formula not in serialized
    assert secret_extra not in serialized
    assert secret_input not in serialized
    assert '"input"' not in serialized
    assert '"ctx"' not in serialized
    assert '"msg"' not in serialized

    # Verify non-ValidationError omits validation_errors and exception text.
    try:
        raise ValueError("SECRET_PLAIN_ERROR_TEXT")
    except ValueError as plain_error:
        plain_diag = safe_exception_diagnostic(plain_error)
    assert plain_diag["exception_class"] == "ValueError"
    assert "validation_errors" not in plain_diag
    assert "SECRET_PLAIN_ERROR_TEXT" not in json.dumps(plain_diag)

    # Verify deeply nested loc (>120 chars) is bounded.
    DeepLeaf = create_model("DeepLeaf", field=(int, ...))
    DeepA = create_model("DeepA", very_long_nested_attribute_name_part_one=(DeepLeaf, ...))
    DeepB = create_model("DeepB", very_long_nested_attribute_name_part_two=(DeepA, ...))
    DeepRoot = create_model("DeepRoot", very_long_nested_attribute_name_part_three=(DeepB, ...))
    with pytest.raises(ValidationError) as deep_exc:
        DeepRoot.model_validate(
            {
                "very_long_nested_attribute_name_part_three": {
                    "very_long_nested_attribute_name_part_two": {
                        "very_long_nested_attribute_name_part_one": {"field": "SECRET_DEEP_VALUE"}
                    }
                }
            }
        )
    deep_diag = safe_exception_diagnostic(deep_exc.value)
    assert len(deep_diag["validation_errors"]) == 1
    assert len(deep_diag["validation_errors"][0]["loc"]) == 120
    assert "SECRET_DEEP_VALUE" not in json.dumps(deep_diag)


def test_operations_execute_separates_local_validation_errors_from_provider_failures(caplog):
    manager = OperationManager(FixedClock())
    payload_type = create_model("SyncPayload", content_hash=(str, ...))
    response_type = create_model("SyncResponse", operation=(object, ...), value=(object, ...))

    def _raise_validation_error(_operation_id: str) -> tuple[BaseModel, str]:
        ResearchCalculation.model_validate({"formula_id": "SECRET_BAD_FORMULA"})
        raise AssertionError("unreachable")

    # 1. Local ValidationError in build_result: retryable=False, no provider_failure telemetry.
    telemetry.reset()
    with caplog.at_level(logging.ERROR, logger="app.operations"):
        caplog.clear()
        with pytest.raises(ApiProblem) as exc_info:
            manager.execute(
                endpoint="/v1/research/topics/t1/runs",
                idempotency_key="sync-local-validation",
                request_payload={"case": "build_validation"},
                response_model=response_type,
                result_field="value",
                build_result=_raise_validation_error,
            )
    problem = exc_info.value
    assert problem.status_code == 500
    assert problem.error_code == "internal_error"
    assert problem.retryable is False
    assert "Provider request" not in problem.message
    assert "SECRET_BAD_FORMULA" not in problem.message
    assert telemetry.snapshot()["counters"].get("provider_failure", 0) == 0
    op_id = next(iter(manager.operations))
    last_event = manager.events[op_id][-1]
    assert last_event.event == "operation.error"
    assert last_event.retryable is False
    assert last_event.error.retryable is False
    assert "validation_errors" in caplog.text
    assert "formula_id" in caplog.text
    assert "SECRET_BAD_FORMULA" not in caplog.text

    # 2. Local deterministic error in commit_result: retryable=False, no provider_failure telemetry.
    telemetry.reset()
    with pytest.raises(ApiProblem) as commit_exc:
        manager.execute(
            endpoint="/v1/research/topics/t1/runs",
            idempotency_key="sync-commit-validation",
            request_payload={"case": "commit_validation"},
            response_model=response_type,
            result_field="value",
            build_result=lambda _oid: (payload_type(content_hash="a" * 64), "ref:ok"),
            commit_result=lambda _oid, _res: _raise_validation_error(_oid),
        )
    assert commit_exc.value.status_code == 500
    assert commit_exc.value.retryable is False
    assert telemetry.snapshot()["counters"].get("provider_failure", 0) == 0

    # 3. ProviderFailure keeps record_provider_failure and failure.retryable semantics.
    telemetry.reset()
    with pytest.raises(ApiProblem) as provider_exc:
        manager.execute(
            endpoint="/v1/research/topics/t1/runs",
            idempotency_key="sync-provider-failure",
            request_payload={"case": "provider_retryable"},
            response_model=response_type,
            result_field="value",
            build_result=lambda _oid: (_ for _ in ()).throw(
                ProviderFailure("provider_timeout", "upstream timed out", retryable=True)
            ),
        )
    assert provider_exc.value.status_code == 503
    assert provider_exc.value.retryable is True
    assert provider_exc.value.retry_after == 15
    assert provider_exc.value.message == "Provider request could not produce an accepted result."
    assert telemetry.snapshot()["counters"]["provider_failure"] == 1

    # 4. ApiProblem preserves status/code and does not record provider_failure.
    telemetry.reset()
    with pytest.raises(ApiProblem) as api_exc:
        manager.execute(
            endpoint="/v1/research/topics/t1/runs",
            idempotency_key="sync-api-problem",
            request_payload={"case": "api_problem"},
            response_model=response_type,
            result_field="value",
            build_result=lambda _oid: (payload_type(content_hash="b" * 64), "ref:conflict"),
            commit_result=lambda _oid, _res: (_ for _ in ()).throw(
                ApiProblem(409, "lifecycle_conflict", "Topic version conflict.")
            ),
        )
    assert api_exc.value.status_code == 409
    assert api_exc.value.error_code == "lifecycle_conflict"
    assert telemetry.snapshot()["counters"].get("provider_failure", 0) == 0


def test_operations_submit_async_separates_local_validation_errors_from_provider_failures(caplog):
    manager = OperationManager(FixedClock())
    payload_type = create_model("AsyncPayload", content_hash=(str, ...))

    def _raise_validation_error(_operation_id: str) -> tuple[BaseModel, str]:
        ResearchCalculation.model_validate({"formula_id": "SECRET_ASYNC_BAD_FORMULA"})
        raise AssertionError("unreachable")

    # 1. Local ValidationError in async worker: retryable=False, no provider_failure telemetry.
    telemetry.reset()
    with caplog.at_level(logging.ERROR, logger="app.operations"):
        caplog.clear()
        receipt, _ = manager.submit_async(
            endpoint="/v1/research/topics/t1/runs",
            idempotency_key="async-local-validation",
            request_payload={"case": "async_validation"},
            result_field="research",
            build_result=_raise_validation_error,
        )
        assert _wait_for_operation(manager, receipt.operation.operation_id) == "failed"

    err_event = manager.events[receipt.operation.operation_id][-1]
    assert err_event.event == "operation.error"
    assert err_event.retryable is False
    assert err_event.error.retryable is False
    assert err_event.error.error_code == "internal_error"
    assert err_event.error.message == "An internal validation or persistence error prevented saving the result."
    assert telemetry.snapshot()["counters"].get("provider_failure", 0) == 0
    assert "validation_errors" in caplog.text
    assert "formula_id" in caplog.text
    assert "SECRET_ASYNC_BAD_FORMULA" not in caplog.text

    # 2. ApiProblem in async commit callback: preserved and does NOT increment provider_failure.
    telemetry.reset()
    conflict_receipt, _ = manager.submit_async(
        endpoint="/v1/research/topics/t1/runs",
        idempotency_key="async-api-problem",
        request_payload={"case": "async_conflict"},
        result_field="research",
        build_result=lambda _oid: (payload_type(content_hash="c" * 64), "ref:ok"),
        commit_result=lambda _oid, _res: (_ for _ in ()).throw(
            ApiProblem(409, "lifecycle_conflict", "Research changed before save.")
        ),
    )
    assert _wait_for_operation(manager, conflict_receipt.operation.operation_id) == "failed"
    conflict_event = manager.events[conflict_receipt.operation.operation_id][-1]
    assert conflict_event.error.error_code == "lifecycle_conflict"
    assert telemetry.snapshot()["counters"].get("provider_failure", 0) == 0

    # 3. ProviderFailure in async worker: increments provider_failure and respects retryable flag.
    telemetry.reset()
    provider_receipt, _ = manager.submit_async(
        endpoint="/v1/research/topics/t1/runs",
        idempotency_key="async-provider-nonretryable",
        request_payload={"case": "async_provider_nonretryable"},
        result_field="research",
        build_result=lambda _oid: (_ for _ in ()).throw(
            ProviderFailure("invalid_provider_output", "upstream schema invalid", retryable=False)
        ),
    )
    assert _wait_for_operation(manager, provider_receipt.operation.operation_id) == "failed"
    provider_event = manager.events[provider_receipt.operation.operation_id][-1]
    assert provider_event.retryable is False
    assert provider_event.error.message == "Provider request could not produce an accepted result."
    assert telemetry.snapshot()["counters"]["provider_failure"] == 1


def _sample_fact(fact_id: str, value: str, period: str, quote: str, start: int) -> dict:
    return {
        "fact_id": fact_id,
        "label": "Revenue",
        "value": value,
        "unit": "USD millions",
        "currency": "USD",
        "period": period,
        "source_id": "src-1",
        "quotation": quote,
        "character_start": start,
        "character_end": start + len(quote),
        "location": {"character_start": start, "character_end": start + len(quote)},
        "verification": "numeric_and_location_match_semantics_not_verified",
    }


def _sample_valid_chart(chart_id: str) -> dict:
    return {
        "chart_id": chart_id,
        "chart_type": "bar",
        "title": "Revenue Trend",
        "subtitle": "",
        "x_label": "Period",
        "axes": [{"id": "left", "label": "Revenue", "unit": "USD millions", "currency": "USD", "scale": "millions"}],
        "series": [
            {
                "name": "Revenue",
                "mark": "bar",
                "y_axis_id": "left",
                "is_projected": False,
                "points": [
                    {
                        "x": "2024",
                        "y": "80",
                        "display_value": "80",
                        "is_projected": False,
                        "provenance": {"fact_id": "fact-2024", "source_id": "src-1", "character_start": 21, "character_end": 41},
                    }
                ],
            }
        ],
        "causal_graph": None,
        "is_simulated": False,
        "has_projected_points": False,
        "replaces_chart_id": None,
        "source_ids": ["src-1"],
    }


def test_merge_turn_isolates_invalid_calculations_and_charts_while_preserving_report_facts_and_claims(caplog):
    f1 = _sample_fact("fact-2025", "100", "2025", "Revenue was 100 USD.", 0)
    f2 = _sample_fact("fact-2024", "80", "2024", "Revenue was 80 USD.", 21)
    valid_calc = {
        "calculation_id": "calc-valid",
        **calculate(
            CalculationRequest(
                formula="growth_percent",
                first={k: f1[k] for k in ("fact_id", "value", "unit", "currency", "period")},
                second={k: f2[k] for k in ("fact_id", "value", "unit", "currency", "period")},
            )
        ),
    }
    valid_calc["input_status"] = calculation_input_status(valid_calc, [f1, f2])
    invalid_calc = {
        **deepcopy(valid_calc),
        "calculation_id": "calc-bad",
        "formula_id": "SECRET_DRIFTED_FORMULA",
    }
    valid_chart = _sample_valid_chart("chart-valid")
    invalid_chart = {
        "chart_id": "chart-bad",
        "chart_type": "causal_graph",
        "title": "SECRET_BROKEN_GRAPH_TITLE",
        # causal_graph is None -> violates ResearchChart._validate_mode
        "causal_graph": None,
        "axes": [],
        "series": [],
        "source_ids": ["src-1"],
    }
    claim = {
        "claim_id": "claim-1",
        "role": "support",
        "text": "Revenue grew from 80M to 100M.",
        "citations": [{"source_id": "src-1", "quotation": "Revenue was 100 USD.", "location": {"character_start": 0, "character_end": 20}}],
        "challenges_claim_ids": [],
    }
    now = datetime.now(timezone.utc)
    turn_obj = ResearchTurn(
        turn_id="turn-1",
        index=1,
        question="How did revenue grow?",
        question_origin="user",
        operation_ids=["op-1"],
        parent_version_id="v0",
        started_at=now,
        finished_at=now,
        status="completed",
        reason="answered",
        report="Completed report preserved despite invalid calculation/chart.",
        claim_ids=["claim-1"],
        fact_ids=["fact-2025", "fact-2024"],
        calculation_ids=["calc-valid", "calc-bad"],
        chart_ids=["chart-valid", "chart-bad"],
        usage=TurnUsage(model_calls=2, search_calls=0, total_tokens=42, usage_status="complete"),
        search_status="not_requested",
        official_fetch_status="not_requested",
    )

    with caplog.at_level(logging.WARNING, logger="app.research_turns"):
        caplog.clear()
        merged = merge_turn(
            ResearchContent(),
            turn_obj,
            claims=[claim],
            facts=[f1, f2],
            calculations=[valid_calc, invalid_calc],
            charts=[valid_chart, invalid_chart],
            sources=[{"source_id": "src-1"}],
            source_candidates=[],
        )

    validated = ResearchContent.model_validate(merged.model_dump())
    assert validated.turns[-1].status == "completed"
    assert validated.turns[-1].report == "Completed report preserved despite invalid calculation/chart."
    assert [item.fact_id for item in validated.facts] == ["fact-2025", "fact-2024"]
    assert [item.claim_id for item in validated.claims] == ["claim-1"]
    assert [item.calculation_id for item in validated.calculations] == ["calc-valid"]
    assert [item.chart_id for item in validated.charts] == ["chart-valid"]
    assert validated.turns[-1].calculation_ids == ["calc-valid"]
    assert validated.turns[-1].chart_ids == ["chart-valid"]

    turn_gap_codes = [g.code for g in validated.turns[-1].gaps]
    content_gap_codes = [g.code for g in validated.gaps]
    assert "calculation_persistence_dropped" in turn_gap_codes
    assert "chart_persistence_dropped" in turn_gap_codes
    assert turn_gap_codes == content_gap_codes

    assert "research_item_persistence_dropped" in caplog.text
    assert "formula_id" in caplog.text
    assert "SECRET_DRIFTED_FORMULA" not in caplog.text
    assert "SECRET_BROKEN_GRAPH_TITLE" not in caplog.text

    # Continuation re-merge drops malformed items without id cleanly and deduplicates gaps.
    continuation_turn = turn_obj.model_copy(deep=True)
    continuation_turn.operation_ids = ["op-2"]
    continuation_turn.calculation_ids = ["calc-valid", "calc-bad-2"]
    merged_cont = merge_turn(
        validated,
        continuation_turn,
        claims=[claim],
        facts=[f1, f2],
        calculations=[valid_calc, {"formula_id": "bad_without_id"}, {**invalid_calc, "calculation_id": "calc-bad-2"}],
        charts=[valid_chart],
        sources=[{"source_id": "src-1"}],
        source_candidates=[],
    )
    assert [item.calculation_id for item in merged_cont.calculations] == ["calc-valid"]
    assert merged_cont.turns[-1].calculation_ids == ["calc-valid"]
    assert [g.code for g in merged_cont.gaps].count("calculation_persistence_dropped") == 1


def test_recovery_status_poison_pill_guard_blocks_non_retryable_local_error_until_upgraded():
    now = datetime.now(timezone.utc)
    topic = SimpleNamespace(
        topic_id="t1",
        owner_id="owner1",
        versions=[
            SimpleNamespace(
                version_id="v1",
                created_by="user",
                review="draft",
                content=SimpleNamespace(run={}, turns=[]),
            )
        ],
    )
    base_op = {
        "operation_id": "op1",
        "owner_subject": "account:owner1",
        "endpoint": "/v1/research/topics/t1/runs",
        "status": "failed",
    }
    base_cp = {
        "submission": {"status": "completed", "report": "Ready.", "claims": [], "gaps": [], "stopping_reason": "answered"},
        "_sources": [],
    }
    base_inputs = {
        "owner_id": "owner1",
        "topic_id": "t1",
        "operation_id": "op1",
        "parent_version_id": "v1",
        "runtime_checkpoint": dict(base_cp),
    }

    # 1. Unversioned poison-pill checkpoint with non-retryable local error -> blocked.
    local_err_event = [
        {
            "event": "operation.error",
            "retryable": False,
            "error": {
                "error_code": "internal_error",
                "message": "An internal validation or persistence error prevented saving the result.",
                "retryable": False,
            },
        }
    ]
    blocked_legacy = recovery_status(
        topic=topic,
        operation=base_op,
        inputs=deepcopy(base_inputs),
        owner_id="owner1",
        operation_id="op1",
        now=now,
        source_fingerprints=[],
        provider_matches=True,
        operation_events=local_err_event,
    )
    assert blocked_legacy["recoverable"] is False
    assert blocked_legacy["can_continue"] is False
    assert blocked_legacy["can_deliver"] is False
    assert blocked_legacy["reason"] == "non_retryable_local_error"

    # 2. Current schema version (1) with non-retryable local error on operation.error -> also blocked.
    v1_inputs = deepcopy(base_inputs)
    v1_inputs["runtime_checkpoint"]["checkpoint_schema_version"] = RUNTIME_CHECKPOINT_SCHEMA_VERSION
    blocked_v1 = recovery_status(
        topic=topic,
        operation={**base_op, "error": {"error_code": "internal_error", "retryable": False}},
        inputs=v1_inputs,
        owner_id="owner1",
        operation_id="op1",
        now=now,
        source_fingerprints=[],
        provider_matches=True,
    )
    assert blocked_v1["recoverable"] is False
    assert blocked_v1["can_deliver"] is False
    assert blocked_v1["reason"] == "non_retryable_local_error"

    # 3. Explicitly upgraded checkpoint (version > current/failed or checkpoint_migrated=True) -> allowed.
    upgraded_inputs = deepcopy(v1_inputs)
    upgraded_inputs["runtime_checkpoint"]["checkpoint_schema_version"] = RUNTIME_CHECKPOINT_SCHEMA_VERSION + 1
    upgraded_status = recovery_status(
        topic=topic,
        operation=base_op,
        inputs=upgraded_inputs,
        owner_id="owner1",
        operation_id="op1",
        now=now,
        source_fingerprints=[],
        provider_matches=True,
        operation_events=local_err_event,
    )
    assert upgraded_status["can_deliver"] is True
    assert upgraded_status["can_continue"] is False
    assert upgraded_status["reason"] is None

    migrated_flag_inputs = deepcopy(v1_inputs)
    migrated_flag_inputs["runtime_checkpoint"]["checkpoint_migrated"] = True
    migrated_status = recovery_status(
        topic=topic,
        operation=base_op,
        inputs=migrated_flag_inputs,
        owner_id="owner1",
        operation_id="op1",
        now=now,
        source_fingerprints=[],
        provider_matches=True,
        operation_events=local_err_event,
    )
    assert migrated_status["can_deliver"] is True
    assert migrated_status["reason"] is None

    # 4. Normal retryable interruption or rate-limit/budget/provider/lifecycle_conflict -> NOT blocked.
    for allowed_err in (
        {"error_code": "internal_error", "message": "Transient interruption.", "retryable": True},
        {"error_code": "internal_error", "message": "Provider request could not produce an accepted result.", "retryable": False},
        {"error_code": "rate_limited", "message": "Rate limit hit.", "retryable": False},
        {"error_code": "quota_exceeded", "message": "Budget exceeded.", "retryable": False},
        {"error_code": "provider_timeout", "message": "Timed out.", "retryable": False},
        {"error_code": "lifecycle_conflict", "message": "Research changed before save.", "retryable": False},
    ):
        ok_status = recovery_status(
            topic=topic,
            operation={**base_op, "error": allowed_err},
            inputs=deepcopy(v1_inputs),
            owner_id="owner1",
            operation_id="op1",
            now=now,
            source_fingerprints=[],
            provider_matches=True,
        )
        assert ok_status["can_deliver"] is True, allowed_err
        assert ok_status["reason"] is None, allowed_err


def test_api_poison_pill_run_sets_checkpoint_version_blocks_recover_loop_and_recovers_after_migration(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    telemetry.reset()

    class Provider:
        name = "p1-poison-pill-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return turn("read_sources", {"reads": [{"source_id": source_id}], "rationale": "read"}, "r1")
            return turn(
                "submit_research",
                {
                    "report": "Revenue was 100 USD.",
                    "claims": [
                        {
                            "text": "Revenue was 100 USD.",
                            "source_id": source_id,
                            "quotation": "Revenue was 100 USD.",
                            "role": "support",
                        }
                    ],
                    "limitations": [],
                    "stopping_reason": "answered",
                },
                "s1",
            )

    provider = Provider()
    monkeypatch.setattr(api, "challenge_provider", provider)
    client = TestClient(api.app)
    auth = client.post(
        "/v1/auth/local/register",
        json={"email": f"{uuid4().hex}@example.test", "password": "correct horse battery staple!"},
    ).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    source_id = client.post(
        "/v2/research/sources",
        headers=headers,
        json={
            "title": "Report",
            "text": "Revenue was 100 USD.",
            "permissions": {"store": True, "display": True, "model_input": True, "export": True},
        },
    ).json()["source_id"]
    topic = client.post(
        "/v2/research/topics",
        headers=headers,
        json={"question": "What was revenue?", "source_ids": [source_id]},
    ).json()
    path = "/v2/research/topics/" + topic["topic_id"]

    # Inject a deterministic ValidationError during merge_turn (after submission checkpoint is saved).
    real_merge_turn = api.merge_turn

    def broken_merge_turn(*args, **kwargs):
        ResearchCalculation.model_validate({"formula_id": "SECRET_DRIFTED_FORMULA"})
        raise AssertionError("unreachable")

    monkeypatch.setattr(api, "merge_turn", broken_merge_turn)

    run_resp = client.post(
        path + "/runs",
        headers={**headers, "Idempotency-Key": uuid4().hex},
        json={"parent_version_id": topic["versions"][0]["version_id"], "discover_sources": False},
    )
    assert run_resp.status_code == 202, run_resp.text
    op_id = run_resp.json()["operation"]["operation_id"]

    for _ in range(200):
        op = client.get(f"{path}/runs/{op_id}", headers=headers).json()["operation"]
        if op["status"] in ("completed", "failed", "cancelled"):
            break
        sleep(0.01)
    assert op["status"] == "failed"

    # Fault domain separation: retryable=False, non-provider message, zero provider_failure telemetry.
    events = api.operations.store.get_events(op_id)
    assert events[-1]["event"] == "operation.error"
    assert events[-1]["retryable"] is False
    assert events[-1]["error"]["retryable"] is False
    assert events[-1]["error"]["error_code"] == "internal_error"
    assert events[-1]["error"]["message"] == "An internal validation or persistence error prevented saving the result."
    assert telemetry.snapshot()["counters"].get("provider_failure", 0) == 0

    stored_inputs = api.RESEARCH_RUN_INPUTS.get(op_id)
    assert stored_inputs["runtime_checkpoint"]["checkpoint_schema_version"] == RUNTIME_CHECKPOINT_SCHEMA_VERSION
    assert stored_inputs["failed_checkpoint_schema_version"] == RUNTIME_CHECKPOINT_SCHEMA_VERSION
    assert "failed_checkpoint_schema_version" not in client.get(f"{path}/runs/{op_id}/inputs", headers=headers).json()

    # Poison-pill guard blocks recovery-status, /recover, and /continue without re-calling provider.
    rec_status = client.get(f"{path}/runs/{op_id}/recovery-status", headers=headers).json()
    assert rec_status["recoverable"] is False
    assert rec_status["can_continue"] is False
    assert rec_status["can_deliver"] is False
    assert rec_status["reason"] == "non_retryable_local_error"

    recover_blocked = client.post(f"{path}/runs/{op_id}/recover", headers={**headers, "Idempotency-Key": uuid4().hex}, json={})
    assert recover_blocked.status_code == 409
    assert recover_blocked.json()["error_code"] == "lifecycle_conflict"

    continue_blocked = client.post(f"{path}/runs/{op_id}/continue", headers={**headers, "Idempotency-Key": uuid4().hex}, json={})
    assert continue_blocked.status_code == 409
    assert continue_blocked.json()["error_code"] == "lifecycle_conflict"
    assert provider.calls == 2

    # Fix the bug and explicitly migrate the checkpoint version -> /recover delivers without calling model again.
    monkeypatch.setattr(api, "merge_turn", real_merge_turn)
    migrated = deepcopy(api.RESEARCH_RUN_INPUTS.get(op_id))
    migrated["runtime_checkpoint"]["checkpoint_schema_version"] = RUNTIME_CHECKPOINT_SCHEMA_VERSION + 1
    api.RESEARCH_RUN_INPUTS.put(op_id, migrated)

    rec_after = client.get(f"{path}/runs/{op_id}/recovery-status", headers=headers).json()
    assert rec_after["can_deliver"] is True
    assert rec_after["can_continue"] is False
    assert rec_after["reason"] is None

    recover_ok = client.post(f"{path}/runs/{op_id}/recover", headers={**headers, "Idempotency-Key": uuid4().hex}, json={})
    assert recover_ok.status_code == 202, recover_ok.text
    recovered_id = recover_ok.json()["operation"]["operation_id"]
    for _ in range(200):
        rec_op = client.get(f"{path}/runs/{recovered_id}", headers=headers).json()["operation"]
        if rec_op["status"] in ("completed", "failed", "cancelled"):
            break
        sleep(0.01)
    assert rec_op["status"] == "completed"
    assert provider.calls == 2
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert content["turns"][-1]["report"] == "Revenue was 100 USD."
    assert len(content["claims"]) == 1
