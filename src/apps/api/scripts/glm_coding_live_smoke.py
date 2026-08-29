"""Server-only, fail-closed GLM Coding Plan live smoke.

The command intentionally has no provider, endpoint, model, or generation
parameter flags.  The existing GLM adapter owns those values and reads its
credential only from the server process environment.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any


# ``python apps/api/scripts/...`` puts only ``scripts`` on sys.path.  Add the
# API project root so the script uses the repository's existing adapter.
_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.openai_compatible import (  # noqa: E402
    HttpResponse,
    OpenAICompatibleLlmProvider,
    OpenAICompatibleTransport,
    _default_transport,
)
from app.phase3 import (  # noqa: E402
    RUBRIC_CRITERIA,
    ChallengeDraft,
    ChallengeInput,
    ProviderFailure,
    ProviderPolicy,
)


API_KEY_ENV = "FORESIGHTNINE_GLM_CODING_API_KEY"
MODEL = "glm-5.3"
EXIT_OK = 0
EXIT_PROVIDER_FAILURE = 1
EXIT_BLOCKED = 2
_REQUEST_ID_HEADERS = frozenset({"x-request-id", "request-id"})
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


class _RecordingTransport:
    """Capture only a safe request identifier, never response content."""

    def __init__(self, delegate: OpenAICompatibleTransport) -> None:
        self._delegate = delegate
        self.request_id: str | None = None

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HttpResponse:
        response = self._delegate(method, url, headers, body, timeout)
        self.request_id = _safe_request_id(response.headers)
        return response


def _safe_request_id(headers: Mapping[str, str] | None) -> str | None:
    if not isinstance(headers, Mapping):
        return None
    for name, value in headers.items():
        if str(name).lower() not in _REQUEST_ID_HEADERS or not isinstance(value, str):
            continue
        candidate = value.strip()
        if _SAFE_REQUEST_ID.fullmatch(candidate):
            return candidate
    return None


def _challenge_request() -> ChallengeInput:
    """Return a synthetic, non-private challenge with one attached public id."""
    return ChallengeInput(
        operation_id="operation:glm-coding-live-smoke",
        challenge_id="challenge:glm-coding-live-smoke",
        thesis_id="thesis:glm-coding-live-smoke",
        claim="A synthetic smoke challenge remains unresolved until reviewed.",
        turn=1,
        status="CHALLENGING",
        private_evidence_declared=False,
        evidence_ids=("smoke:public",),
        evidence_grades=(("smoke:public", "A"),),
    )


def _validation_summary(draft: ChallengeDraft) -> dict[str, Any]:
    """Re-check the public acceptance boundary before reporting success."""
    if not isinstance(draft, ChallengeDraft):
        raise ProviderFailure("invalid_provider_output", "Smoke result was not a ChallengeDraft.", retryable=False)
    criteria = tuple(score.criterion for score in draft.rubric_scores)
    if (
        len(draft.rubric_scores) != 10
        or set(criteria) != set(RUBRIC_CRITERIA)
        or any(type(score.score) is not int or score.score not in range(5) for score in draft.rubric_scores)
    ):
        raise ProviderFailure("invalid_provider_output", "Smoke result did not contain the ten rubric criteria.", retryable=False)
    if (
        not isinstance(draft.citation_ids, tuple)
        or not isinstance(draft.public_fact_citation_ids, tuple)
        or not draft.citation_ids
    ):
        raise ProviderFailure("citation_mismatch", "Smoke result did not contain validated citations.", retryable=False)
    return {
        "challenge_draft": True,
        "rubric_items": 10,
        "rubric_criteria_exact": True,
        "citations_field": True,
        "citations_validated": True,
    }


def _failure_result(code: str, request_id: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "failed",
        "model": MODEL,
        "validation_summary": {
            "result": "provider_failure",
            "failure_code": code,
        },
    }
    if request_id is not None:
        result["request_id"] = request_id
    return result


def run_smoke(*, transport: OpenAICompatibleTransport | None = None) -> tuple[int, dict[str, Any]]:
    """Run at most one synthetic challenge and return ``(exit_code, result)``."""
    if not os.environ.get(API_KEY_ENV, "").strip():
        return EXIT_BLOCKED, {
            "status": "blocked",
            "model": MODEL,
            "validation_summary": {
                "result": "secret_missing",
                "challenge_draft": False,
            },
            "no_network_attempted": True,
        }

    recorder = _RecordingTransport(transport or _default_transport)
    provider = OpenAICompatibleLlmProvider.from_glm_coding_environment(
        policy=ProviderPolicy(max_attempts=1),
        transport=recorder,
    )
    try:
        draft = provider.generate_challenge(_challenge_request())
        result = {
            "status": "passed",
            "model": MODEL,
            "validation_summary": _validation_summary(draft),
        }
        if recorder.request_id is not None:
            result["request_id"] = recorder.request_id
        return EXIT_OK, result
    except ProviderFailure as error:
        return EXIT_PROVIDER_FAILURE, _failure_result(error.code, recorder.request_id)
    except Exception:
        # Do not expose transport/library text: it may contain response data or
        # deployment details.  A live smoke must fail closed on unknown errors.
        return EXIT_PROVIDER_FAILURE, _failure_result("provider_internal_error", recorder.request_id)


def format_result(result: Mapping[str, Any]) -> str:
    """Serialize only the already-redacted result object as one JSON line."""
    return json.dumps(dict(result), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main(argv: Sequence[str] | None = None, *, transport: OpenAICompatibleTransport | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments:
        print(
            format_result(
                {
                    "status": "blocked",
                    "model": MODEL,
                    "validation_summary": {"result": "cli_overrides_not_supported"},
                    "no_network_attempted": True,
                }
            )
        )
        return EXIT_BLOCKED
    exit_code, result = run_smoke(transport=transport)
    print(format_result(result))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
