"""Server-only, fail-closed GLM Coding Plan MCP search smoke.

The command deliberately has no provider, endpoint, query, or generation
flags.  The adapter owns the fixed MCP endpoint and reads its credential only
from the server process environment.  Output contains validation metadata
only; source titles, URLs, excerpts, response bodies, and credentials are
never printed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from collections.abc import Mapping, Sequence
from typing import Any


_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.glm_mcp_search import GlmCodingMcpSearchEvidenceProvider  # noqa: E402
from app.openai_compatible import OpenAICompatibleTransport  # noqa: E402
from app.phase3 import ProviderFailure  # noqa: E402


API_KEY_ENV = "FORESIGHTNINE_GLM_CODING_API_KEY"
SEARCH_QUERY = "public macro policy source"
EXIT_OK = 0
EXIT_PROVIDER_FAILURE = 1
EXIT_BLOCKED = 2


def _failure_result(code: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "protocol": "MCP Streamable HTTP",
        "validation_summary": {"result": "provider_failure", "failure_code": code},
    }


def run_smoke(*, transport: OpenAICompatibleTransport | None = None) -> tuple[int, dict[str, Any]]:
    """Run one bounded MCP search and return a redacted ``(exit, result)``."""
    if not os.environ.get(API_KEY_ENV, "").strip():
        return EXIT_BLOCKED, {
            "status": "blocked",
            "protocol": "MCP Streamable HTTP",
            "validation_summary": {"result": "secret_missing"},
            "no_network_attempted": True,
        }

    provider = GlmCodingMcpSearchEvidenceProvider(
        api_key=os.environ.get(API_KEY_ENV),
        transport=transport,
        timeout_seconds=15.0,
        max_attempts=1,
    )
    try:
        candidates = provider.search(SEARCH_QUERY, limit=3)
        return EXIT_OK, {
            "status": "passed",
            "protocol": "MCP Streamable HTTP",
            "candidate_count": len(candidates),
            "all_https": all(candidate.url.startswith("https://") for candidate in candidates),
            "dated_count": sum(candidate.published_at is not None for candidate in candidates),
        }
    except ProviderFailure as error:
        return EXIT_PROVIDER_FAILURE, _failure_result(error.code)
    except Exception:
        # Do not expose library, transport, deployment, or response details.
        return EXIT_PROVIDER_FAILURE, _failure_result("provider_internal_error")


def format_result(result: Mapping[str, Any]) -> str:
    return json.dumps(dict(result), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main(argv: Sequence[str] | None = None, *, transport: OpenAICompatibleTransport | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments:
        print(format_result({
            "status": "blocked",
            "protocol": "MCP Streamable HTTP",
            "validation_summary": {"result": "cli_overrides_not_supported"},
            "no_network_attempted": True,
        }))
        return EXIT_BLOCKED
    exit_code, result = run_smoke(transport=transport)
    print(format_result(result))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
