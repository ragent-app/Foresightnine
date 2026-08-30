from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.main import app

from .helpers import WORKSPACE_ROOT


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_openapi_snapshot_matches_runtime_and_json_yaml_aliases() -> None:
    json_path = WORKSPACE_ROOT / "packages" / "contracts" / "openapi" / "v1" / "openapi.json"
    yaml_path = WORKSPACE_ROOT / "packages" / "contracts" / "openapi" / "v1" / "openapi.yaml"
    committed = json.loads(json_path.read_text(encoding="utf-8"))
    assert committed == app.openapi()
    assert json_path.read_text(encoding="utf-8") == yaml_path.read_text(encoding="utf-8")


def test_openapi_all_local_refs_resolve() -> None:
    document = app.openapi()
    for node in _walk(document):
        reference = node.get("$ref")
        if not reference:
            continue
        assert reference.startswith("#/")
        target: Any = document
        for token in reference.removeprefix("#/").split("/"):
            target = target[token.replace("~1", "/").replace("~0", "~")]
        assert target is not None


def test_openapi_has_every_phase1_endpoint_operation_error_and_idempotency_contract() -> None:
    document = app.openapi()
    expected = {
        ("get", "/health"),
        ("post", "/v1/sessions"),
        ("post", "/v1/challenges"),
        ("post", "/v1/challenges/{challenge_id}/messages"),
        ("post", "/v1/challenges/{challenge_id}/messages/async"),
        ("post", "/v1/challenges/{challenge_id}/evidence"),
        ("get", "/v1/search/candidates"),
        ("post", "/v1/challenges/{challenge_id}/confirm"),
        ("post", "/v1/worldlines"),
        ("get", "/v1/worldlines/{worldline_id}"),
        ("post", "/v1/probabilities/assess"),
        ("get", "/v1/assets/search"),
        ("get", "/v1/assets/{asset_id}"),
        ("post", "/v1/market-data/manifests"),
        ("post", "/v1/market-data/shards"),
        ("get", "/v1/factor-libraries/{version}/manifest"),
        ("get", "/v1/factor-libraries/{version}/shards"),
        ("post", "/v1/client-computations/{computation_id}/attest"),
        ("post", "/v1/operations/{operation_id}/cancel"),
        ("post", "/v1/operations/{operation_id}/resume"),
        ("get", "/v1/operations/{operation_id}/events"),
        ("get", "/v1/methodology"),
        ("get", "/v1/auth/providers"),
        ("post", "/v1/auth/{provider}/start"),
        ("post", "/v1/auth/oauth/exchange"),
    }
    observed = {(method, path) for path, item in document["paths"].items() for method in item if method in {"get", "post"}}
    assert observed == expected
    operation_ids: list[str] = []
    for path, item in document["paths"].items():
        for method in ("get", "post"):
            if method not in item:
                continue
            operation = item[method]
            operation_ids.append(operation["operationId"])
            assert {"400", "403", "404", "409", "422", "500"} <= set(operation["responses"]), (method, path)
            if method == "post":
                header = [parameter for parameter in operation.get("parameters", []) if parameter.get("name") == "Idempotency-Key"]
                if path in {"/v1/auth/{provider}/start", "/v1/auth/oauth/exchange"}:
                    assert header == []
                else:
                    assert len(header) == 1 and header[0]["required"] is True, path
                assert operation.get("requestBody", {}).get("required") is True, path
    provider_parameter = next(
        parameter
        for parameter in document["paths"]["/v1/auth/{provider}/start"]["post"]["parameters"]
        if parameter["name"] == "provider"
    )
    assert provider_parameter["schema"]["enum"] == ["google", "github"]
    assert len(operation_ids) == len(set(operation_ids))
    event_response = document["paths"]["/v1/operations/{operation_id}/events"]["get"]["responses"]["200"]
    assert "text/event-stream" in event_response["content"]
    assert document["components"]["schemas"]["SseEvent"]["oneOf"]


def test_openapi_exposes_oauth_ticket_exchange_contract() -> None:
    document = app.openapi()
    operation = document["paths"]["/v1/auth/oauth/exchange"]["post"]

    assert "OAuthTicketExchangeRequest" in document["components"]["schemas"]
    assert "OAuthSessionResponse" in document["components"]["schemas"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/OAuthTicketExchangeRequest"
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/OAuthSessionResponse"
    }
