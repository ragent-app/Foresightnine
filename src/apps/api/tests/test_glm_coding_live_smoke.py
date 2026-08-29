import json
from pathlib import Path
import sys
from typing import Any

import pytest


API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from app.openai_compatible import HttpResponse  # noqa: E402
from app.phase3 import RUBRIC_CRITERIA  # noqa: E402
from scripts.glm_coding_live_smoke import main  # noqa: E402


TEST_SECRET = "unit-test-only-glm-smoke-secret"


def valid_payload() -> dict[str, Any]:
    return {
        "claims": ["Synthetic result."],
        "objections": ["Synthetic result needs external review."],
        "missing_evidence": ["No private evidence is supplied."],
        "questions": ["What would falsify this synthetic challenge by its deadline?"],
        "rubric_scores": [
            {"criterion": criterion, "score": 3, "rationale": "Synthetic validation rationale."}
            for criterion in RUBRIC_CRITERIA
        ],
        "unresolved_issues": ["This is a smoke-only result."],
        "citation_ids": ["smoke:public"],
        "public_fact_citation_ids": ["smoke:public"],
        "native_search_used": False,
    }


def configure_secret(monkeypatch: pytest.MonkeyPatch, value: str | None = TEST_SECRET) -> None:
    if value is None:
        monkeypatch.delenv("FORESIGHTNINE_GLM_CODING_API_KEY", raising=False)
    else:
        monkeypatch.setenv("FORESIGHTNINE_GLM_CODING_API_KEY", value)
    monkeypatch.delenv("FORESIGHTNINE_GLM_CODING_MODEL", raising=False)


def test_missing_secret_is_blocked_without_transport(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    configure_secret(monkeypatch, None)

    def no_transport(*_args: object) -> HttpResponse:
        raise AssertionError("transport must not be called without the server secret")

    assert main((), transport=no_transport) != 0  # type: ignore[arg-type]
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "model": "glm-5.3",
        "no_network_attempted": True,
        "status": "blocked",
        "validation_summary": {"challenge_draft": False, "result": "secret_missing"},
    }


def test_success_fake_transport_is_redacted_and_uses_fixed_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_secret(monkeypatch)
    calls: list[tuple[str, str, dict[str, str], bytes]] = []

    def transport(method: str, url: str, headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        calls.append((method, url, headers, body))
        response = {"choices": [{"message": {"content": json.dumps(valid_payload()), "reasoning_content": "PRIVATE_REASONING"}}]}
        return HttpResponse(200, json.dumps(response), {"X-Request-ID": "req-smoke-123"})

    assert main((), transport=transport) == 0  # type: ignore[arg-type]
    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert output == {
        "model": "glm-5.3",
        "request_id": "req-smoke-123",
        "status": "passed",
        "validation_summary": {
            "challenge_draft": True,
            "citations_field": True,
            "citations_validated": True,
            "rubric_criteria_exact": True,
            "rubric_items": 10,
        },
    }
    assert TEST_SECRET not in output_text
    assert "PRIVATE_REASONING" not in output_text
    assert "Synthetic result." not in output_text
    assert len(calls) == 1
    assert calls[0][0:2] == ("POST", "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions")
    request_body = json.loads(calls[0][3])
    assert request_body["model"] == "glm-5.3"
    assert "temperature" not in request_body
    assert "top_p" not in request_body


def test_invalid_structured_response_fails_closed(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    configure_secret(monkeypatch)
    marker = "RESPONSE_BODY_MUST_NOT_PRINT"

    def transport(*_args: object) -> HttpResponse:
        return HttpResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps({"marker": marker})}}]}))

    assert main((), transport=transport) != 0  # type: ignore[arg-type]
    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert output["status"] == "failed"
    assert output["validation_summary"] == {"failure_code": "invalid_provider_output", "result": "provider_failure"}
    assert marker not in output_text


def test_5xx_failure_is_classified_without_response_body(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    configure_secret(monkeypatch)
    marker = "UPSTREAM_BODY_MUST_NOT_PRINT"
    calls = 0

    def transport(*_args: object) -> HttpResponse:
        nonlocal calls
        calls += 1
        return HttpResponse(503, marker)

    assert main((), transport=transport) != 0  # type: ignore[arg-type]
    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert output["status"] == "failed"
    assert output["validation_summary"] == {"failure_code": "provider_unavailable", "result": "provider_failure"}
    assert marker not in output_text
    assert calls == 1
