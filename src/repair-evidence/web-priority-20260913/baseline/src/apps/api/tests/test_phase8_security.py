from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main as app_main
from app.abuse import AbuseControl, AbuseDecision
from app.retention import RetentionPolicy, cleanup_research_state
from app.security import (
    AcceptCaptchaVerifier,
    RejectCaptchaVerifier,
    TrustedProxyConfig,
    TrustedProxyResolver,
    UnavailableCaptchaVerifier,
    SecurityBoundaryMiddleware,
    SecurityConfigurationError,
    TrustedProxyResolver,
    TurnstileCaptchaVerifier,
    build_captcha_verifier,
    build_security_config,
)
from app.telemetry import telemetry
from .helpers import CLIENT, create_session, idem


def test_rate_limit_uses_peer_when_forwarded_header_is_untrusted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_main.abuse_control.ip, "limit", 1)
    first = CLIENT.post("/v1/sessions", json={}, headers={**idem("spoof-one"), "X-Forwarded-For": "198.51.100.1"})
    second = CLIENT.post("/v1/sessions", json={}, headers={**idem("spoof-two"), "X-Forwarded-For": "198.51.100.2"})
    assert first.status_code == 201
    assert second.status_code == 429
    assert second.headers["Retry-After"].isdigit()
    assert "198.51.100.2" not in second.text


def test_browser_oauth_routes_share_the_ip_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_main.abuse_control.ip, "limit", 1)
    first = CLIENT.get("/oauth/callback", params={"code": "code", "state": "state"})
    second = CLIENT.get("/oauth/callback", params={"code": "code", "state": "state-2"})
    assert first.status_code == 400
    assert second.status_code == 429
    assert second.json()["error_code"] == "rate_limited"


def test_trusted_proxy_chain_only_applies_when_peer_is_configured() -> None:
    request = SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.5"),
        headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.4"},
    )
    resolver = TrustedProxyResolver(TrustedProxyConfig())
    assert resolver.resolve(request) == "10.0.0.5"
    trusted = TrustedProxyResolver(TrustedProxyConfig(networks=(__import__("ipaddress").ip_network("10.0.0.0/8"),)))
    assert trusted.resolve(request) == "198.51.100.7"


@pytest.mark.parametrize("verifier", [RejectCaptchaVerifier(), UnavailableCaptchaVerifier()])
def test_captcha_denial_is_public_fail_closed_before_provider(monkeypatch: pytest.MonkeyPatch, verifier: object) -> None:
    session = create_session("captcha-session-key")
    calls = 0

    class Provider:
        name = "test-provider"

        def generate_challenge(self, request: object) -> object:
            nonlocal calls
            calls += 1
            raise AssertionError("provider must not be invoked")

    monkeypatch.setattr(app_main, "captcha_verifier", verifier)
    monkeypatch.setattr(app_main, "challenge_provider", Provider())
    response = CLIENT.post(
        "/v1/challenges",
        json={
            "session_id": session["session"]["session_id"],
            "thesis": "A thesis that should be denied before provider work.",
            "capital": 1000,
            "base_currency": "USD",
            "user_private_evidence_claims": False,
        },
        headers=idem("captcha-denial-key"),
    )
    assert response.status_code == 403
    assert response.headers["Retry-After"] == "60"
    assert response.json()["error_code"] == "captcha_required"
    assert calls == 0


def test_model_quota_reserves_once_and_idempotency_replay_does_not_invoke_again(monkeypatch: pytest.MonkeyPatch) -> None:
    session = create_session("quota-session-key")
    monkeypatch.setattr(app_main.abuse_control.model, "limit", 1)
    delegate = app_main.challenge_provider
    calls = 0

    class Provider:
        name = "quota-test-provider"

        def generate_challenge(self, request: object) -> object:
            nonlocal calls
            calls += 1
            return delegate.generate_challenge(request)

    monkeypatch.setattr(app_main, "challenge_provider", Provider())
    body = {
        "session_id": session["session"]["session_id"],
        "thesis": "A quota test thesis with a deterministic provider call.",
        "capital": 1000,
        "base_currency": "USD",
        "user_private_evidence_claims": False,
    }
    first = CLIENT.post("/v1/challenges", json=body, headers=idem("quota-replay-key"))
    replay = CLIENT.post("/v1/challenges", json=body, headers=idem("quota-replay-key"))
    assert first.status_code == replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert first.json() == replay.json()
    assert calls == 1


def test_search_provider_call_consumes_the_shared_daily_quota_before_external_work(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    class SearchProvider:
        name = "quota-search-provider"
        supports_project_search = True

        def search(self, query: str, *, limit: int = 5) -> tuple[SimpleNamespace, ...]:
            nonlocal calls
            calls += 1
            return (SimpleNamespace(
                title="Dated public source", url="https://example.com/source", excerpt="Reviewable excerpt.",
                publisher="Example", published_at=date(2026, 8, 1), independently_reviewable=True,
            ),)

    monkeypatch.setattr(app_main, "evidence_provider", SearchProvider())
    monkeypatch.setattr(app_main.abuse_control.model, "limit", 1)

    first = CLIENT.get("/v1/search/candidates", params={"query": "public source", "limit": 1})
    second = CLIENT.get("/v1/search/candidates", params={"query": "public source", "limit": 1})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error_code"] == "quota_exceeded"
    assert "quota" not in second.json()["message"].lower() or "search" in second.json()["message"].lower()
    assert calls == 1


def test_search_quota_backend_failure_blocks_external_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnavailableQuota:
        def reserve(self, reservation_id: str, subjects: object) -> AbuseDecision:
            return AbuseDecision.unavailable(6)

    class Backend:
        model = UnavailableQuota()

    calls = 0

    class SearchProvider:
        name = "must-not-run"
        supports_project_search = True

        def search(self, query: str, *, limit: int = 5) -> tuple[SimpleNamespace, ...]:
            nonlocal calls
            calls += 1
            return ()

    previous_backend = app_main.abuse_control
    previous_provider = app_main.evidence_provider
    try:
        app_main.abuse_control = Backend()
        app_main.evidence_provider = SearchProvider()
        with pytest.raises(app_main.ApiProblem) as raised:
            app_main._search_candidates("public source", 1, SimpleNamespace(state=SimpleNamespace(client_ip="198.51.100.10")))
    finally:
        app_main.abuse_control = previous_backend
        app_main.evidence_provider = previous_provider

    assert raised.value.status_code == 503
    assert raised.value.error_code == "abuse_backend_unavailable"
    assert raised.value.retry_after == 6
    assert calls == 0


def test_model_quota_has_a_global_daily_bucket_across_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_main.abuse_control.model, "limit", 1)
    first_session = create_session("global-quota-session-one")
    second_session = create_session("global-quota-session-two")
    body = {
        "thesis": "A global quota test thesis with a deterministic provider call.",
        "capital": 1000,
        "base_currency": "USD",
        "user_private_evidence_claims": False,
    }
    first = CLIENT.post(
        "/v1/challenges",
        json={**body, "session_id": first_session["session"]["session_id"]},
        headers=idem("global-quota-one"),
    )
    second = CLIENT.post(
        "/v1/challenges",
        json={**body, "session_id": second_session["session"]["session_id"]},
        headers=idem("global-quota-two"),
    )
    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json()["error_code"] == "quota_exceeded"
    assert second.headers["Retry-After"].isdigit()


def test_unavailable_redis_request_backend_maps_to_retryable_503() -> None:
    class UnavailableBackend:
        class Model:
            def reserve(self, reservation_id: str, subjects: object) -> AbuseDecision:
                return AbuseDecision.unavailable()

        model = Model()

        def check_request(self, *, client_ip: str, session_id: str | None = None) -> AbuseDecision:
            return AbuseDecision.unavailable(7)

        def reset(self) -> None:
            return None

    mini = FastAPI()

    @mini.get("/v1/probe")
    def probe() -> dict[str, bool]:
        return {"ok": True}

    mini.add_middleware(
        SecurityBoundaryMiddleware,
        config=build_security_config(),
        abuse=UnavailableBackend(),
        captcha=AcceptCaptchaVerifier(),
        resolver=TrustedProxyResolver(),
    )
    response = TestClient(mini).get("/v1/probe")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "7"
    assert response.json()["error_code"] == "abuse_backend_unavailable"
    assert response.json()["retryable"] is True


def test_unavailable_quota_backend_maps_to_retryable_api_problem_before_provider() -> None:
    class UnavailableQuota:
        def reserve(self, reservation_id: str, subjects: object) -> AbuseDecision:
            return AbuseDecision.unavailable(5)

    class Backend:
        model = UnavailableQuota()

    previous = app_main.abuse_control
    try:
        app_main.abuse_control = Backend()
        with pytest.raises(app_main.ApiProblem) as raised:
            app_main._reserve_model_call(SimpleNamespace(state=SimpleNamespace(client_ip="198.51.100.10")), "operation-id")
    finally:
        app_main.abuse_control = previous
    assert raised.value.status_code == 503
    assert raised.value.error_code == "abuse_backend_unavailable"
    assert raised.value.retryable is True
    assert raised.value.retry_after == 5


def test_async_model_quota_rejection_keeps_http_semantics_before_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    session = create_session("async-quota-session")
    monkeypatch.setattr(app_main.abuse_control.model, "limit", 1)
    created = CLIENT.post(
        "/v1/challenges",
        json={
            "session_id": session["session"]["session_id"],
            "thesis": "An async quota test thesis with a deterministic provider call.",
            "capital": 1000,
            "base_currency": "USD",
            "user_private_evidence_claims": False,
        },
        headers=idem("async-quota-create"),
    )
    assert created.status_code == 201
    challenge_id = created.json()["challenge"]["challenge_id"]
    rejected = CLIENT.post(
        f"/v1/challenges/{challenge_id}/messages/async",
        json={"message": "This request must be rejected before a 202 receipt."},
        headers=idem("async-quota-rejected"),
    )
    assert rejected.status_code == 429
    assert rejected.json()["error_code"] == "quota_exceeded"
    assert rejected.headers["Retry-After"].isdigit()
    assert not any(operation.endpoint.endswith("/messages/async") for operation in app_main.operations.operations.values())


def test_unconfigured_production_captcha_factory_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_ENV", "production")
    monkeypatch.delenv("FORESIGHTNINE_CAPTCHA_MODE", raising=False)
    config = build_security_config()
    assert config.require_captcha is True
    assert isinstance(build_captcha_verifier(config), UnavailableCaptchaVerifier)
    assert isinstance(build_captcha_verifier(config), UnavailableCaptchaVerifier)


def test_turnstile_verifier_is_server_side_and_fail_closed() -> None:
    calls: list[str] = []

    def fake_request(token: str, _request: object) -> object:
        calls.append(token)
        if token == "accepted-token":
            return {"success": True, "hostname": "staging.foresightnine.com"}
        if token == "wrong-host-token":
            return {"success": True, "hostname": "evil.example.test"}
        if token == "provider-rejected-token":
            return {"success": False, "error-codes": ["invalid-input-response"]}
        raise RuntimeError("provider unavailable")

    verifier = TurnstileCaptchaVerifier("server-only-secret", expected_hostname="staging.foresightnine.com", request_json=fake_request)
    request = SimpleNamespace()
    assert verifier.verify("accepted-token", request) is True
    assert verifier.verify("wrong-host-token", request) is False
    assert verifier.verify("provider-rejected-token", request) is False
    assert verifier.verify("provider-error-token", request) is None
    assert verifier.verify(None, request) is False
    assert verifier.verify("x" * 4097, request) is False
    assert calls == ["accepted-token", "wrong-host-token", "provider-rejected-token", "provider-error-token"]


def test_turnstile_configuration_selects_the_real_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_ENV", "production")
    monkeypatch.setenv("FORESIGHTNINE_CAPTCHA_PROVIDER", "turnstile")
    monkeypatch.setenv("FORESIGHTNINE_CAPTCHA_SECRET", "server-only-secret")
    monkeypatch.setenv("FORESIGHTNINE_CAPTCHA_HOSTNAME", "staging.foresightnine.com")
    monkeypatch.delenv("FORESIGHTNINE_CAPTCHA_MODE", raising=False)
    verifier = build_captcha_verifier(build_security_config())
    assert isinstance(verifier, TurnstileCaptchaVerifier)


def test_hardened_security_rejects_unsafe_cors_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_ENV", "production")
    monkeypatch.setenv("FORESIGHTNINE_CORS_ORIGINS", "http://staging.example.test")
    with pytest.raises(SecurityConfigurationError):
        build_security_config()

    monkeypatch.setenv("FORESIGHTNINE_CORS_ORIGINS", "https://app.example.test/")
    assert build_security_config().allowed_origins == ("https://app.example.test",)


def test_body_content_type_and_security_headers_are_bounded() -> None:
    wrong_type = CLIENT.post("/v1/sessions", content="{}", headers={**idem("wrong-content-type"), "Content-Type": "text/plain"})
    assert wrong_type.status_code == 415
    assert wrong_type.json()["error_code"] == "unsupported_media_type"
    oversized = CLIENT.post(
        "/v1/sessions", content="{" + "x" * (256 * 1024) + "}",
        headers={**idem("oversized-body"), "Content-Type": "application/json"},
    )
    assert oversized.status_code == 413
    ok = CLIENT.get("/health")
    assert ok.status_code == 200
    assert ok.headers["X-Content-Type-Options"] == "nosniff"
    assert ok.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'none'" in ok.headers["Content-Security-Policy"]


class _OversizedChunkedBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"{" + b"x" * 200_000
        yield b"x" * 100_000 + b"}"


async def _post_oversized_chunked_body() -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_main.app), base_url="http://test") as client:
        return await client.post(
            "/v1/sessions",
            content=_OversizedChunkedBody(),
            headers={"Content-Type": "application/json", "Idempotency-Key": "chunked-body-test"},
        )


def test_chunked_body_is_rejected_while_streaming_without_content_length() -> None:
    response = asyncio.run(_post_oversized_chunked_body())
    assert response.request.headers.get("transfer-encoding") == "chunked"
    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert response.json()["error_code"] == "request_too_large"


def test_retention_cascades_expired_session_but_preserves_active_session_work() -> None:
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    sessions = {"session:expired": SimpleNamespace(expires_at=now - timedelta(seconds=1)), "session:active": SimpleNamespace(expires_at=now - timedelta(seconds=1))}
    theses = {
        "thesis:expired": SimpleNamespace(session_id="session:expired"),
        "thesis:active": SimpleNamespace(session_id="session:active"),
    }
    challenges = {
        "challenge:expired": SimpleNamespace(thesis_id="thesis:expired"),
        "challenge:active": SimpleNamespace(thesis_id="thesis:active"),
    }
    turns = {"challenge:expired": 2, "challenge:active": 2}
    evidence = {"evidence:expired": SimpleNamespace(challenge_id="challenge:expired")}
    worldlines = {"worldline:expired": object()}
    links = {"worldline:expired": "challenge:expired"}
    manifests = {"manifest:expired": SimpleNamespace(created_at=now - timedelta(days=2))}
    report = cleanup_research_state(
        sessions=sessions, theses=theses, challenges=challenges, challenge_turns=turns,
        evidence=evidence, worldlines=worldlines, market_manifests=manifests, worldline_challenges=links,
        active_session_ids={"session:active"}, now=now,
    )
    assert report.expired_sessions == 1
    assert "session:expired" not in sessions and "thesis:expired" not in theses
    assert "challenge:expired" not in challenges and "evidence:expired" not in evidence
    assert "worldline:expired" not in worldlines
    assert report.removed_market_manifests == 1 and "manifest:expired" not in manifests
    assert "session:active" in sessions and "thesis:active" in theses


def test_application_retention_uses_progressing_lifecycle_clock() -> None:
    session = create_session("runtime-retention-session")
    session_id = session["session"]["session_id"]
    assert app_main.lifecycle_clock.now() >= app_main.SESSIONS[session_id].created_at
    assert session_id in app_main.SESSIONS
    app_main.SESSIONS[session_id] = app_main.SESSIONS[session_id].model_copy(
        update={"expires_at": app_main.lifecycle_clock.now() - timedelta(seconds=1)},
    )
    report = app_main.cleanup_expired_state()
    assert report.expired_sessions == 1
    assert session_id not in app_main.SESSIONS


def test_historical_market_shard_records_redacted_freshness_denial() -> None:
    manifest_response = CLIENT.post(
        "/v1/market-data/manifests",
        json={
            "asset_ids": ["ETF:SPY"],
            "from": "2026-08-01T00:00:00Z",
            "to": "2026-08-10T00:00:00Z",
            "field_set": ["close", "currency", "as_of"],
        },
        headers=idem("freshness-telemetry-manifest"),
    )
    assert manifest_response.status_code == 201
    manifest = manifest_response.json()["manifest"]
    telemetry.reset()
    shard_response = CLIENT.post(
        "/v1/market-data/shards",
        json={"manifest_id": manifest["manifest_id"], "shard_id": manifest["shards"][0]["shard_id"]},
        headers=idem("freshness-telemetry-shard"),
    )
    assert shard_response.status_code == 200
    snapshot = telemetry.snapshot()
    assert snapshot["counters"]["stale_data_denial"] == 1
    assert all("ip" not in event and "url" not in event for event in snapshot["events"])


def test_telemetry_allowlist_does_not_retain_private_content_or_identifiers() -> None:
    telemetry.record(
        "provider_failure", provider="https://secret.example/key", endpoint="/v1/challenges",
        ip="203.0.113.4", **{"token": "redacted-token"}, content="private thesis text",
    )
    snapshot = repr(telemetry.snapshot())
    assert "secret.example" not in snapshot
    assert "203.0.113.4" not in snapshot
    assert "bearer-secret" not in snapshot
    assert "private thesis text" not in snapshot
    assert telemetry.snapshot()["counters"]["provider_failure"] == 1
