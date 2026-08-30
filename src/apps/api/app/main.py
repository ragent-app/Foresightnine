from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import os
from typing import Annotated, Any
from uuid import uuid4

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

from .contract_models import (
    CONTRACT_SCHEMA,
    OAuthAuthorizationResponse,
    OAuthProviderName,
    OAuthProvidersResponse,
    OAuthProviderReadiness,
    OAuthStartRequest,
)
from .abuse import AbuseBackend
from .redis_abuse import build_abuse_control
from .models import (
    AssetSearchResponse,
    CancelOperationRequest,
    ChallengeMessageRequest,
    ChallengeOperationResponse,
    ChallengeRecord,
    ClientAttestationOperationResponse,
    ClientComputationAttestRequest,
    ConfirmChallengeRequest,
    CreateChallengeRequest,
    CreateSessionRequest,
    ErrorDetail,
    ErrorResponse,
    EvidenceItem,
    EvidenceSubmissionRequest,
    FactorLibraryManifest,
    FactorLibraryShard,
    HealthResponse,
    MarketDataManifest,
    MarketDataManifestOperationResponse,
    MarketDataManifestRequest,
    MarketDataShardOperationResponse,
    MarketDataShardRequest,
    MethodologyResponse,
    OperationResponse,
    ProbabilityAssessment,
    ProbabilityOperationResponse,
    ProbabilityRequest,
    ResearchSession,
    ResumeOperationRequest,
    SearchCandidate,
    SearchCandidatesResponse,
    SessionOperationResponse,
    SourceRef,
    ThesisDocument,
    Worldline,
    WorldlineBundle,
    WorldlineBundleOperationResponse,
    WorldlineRequest,
)
from .operations import ApiProblem, OperationManager, format_sse
from .retention import RetentionPolicy, cleanup_research_state
from .security import SecurityBoundaryMiddleware, TrustedProxyResolver, build_captcha_verifier, build_security_config
from .telemetry import record_provider_failure, record_stale_data_denial, telemetry
from .phase3 import ChallengeDraft, ChallengeInput, ProviderFailure, SearchEvidenceBundle, build_llm_provider, build_search_evidence_provider
from .phase4 import DeterministicMockProbabilityAssessor, DeterministicMockWorldlineGenerator, ProbabilityAssessmentInput, WorldlineGenerationInput
from .providers import FixedClock, MockDataProvider, MockFactorProvider, MockResearchProvider, RuntimeClock, build_attestation
from .registry import ASSET_INDEX, get_asset as registry_get_asset, search_assets as registry_search_assets
from .services import (
    ContractViolation,
    content_hash,
    deterministic_id,
    validate_branch_probability_consistency,
    validate_payload,
    validate_probability_semantics,
    validate_worldline_bundle_semantics,
)
from .storage import JsonDocumentRepository, StorageConfig, StorageMode, build_repository
from .operation_store import build_operation_store
from .oauth_foundation import OAuthConfigurationError, OAuthFoundation, OAuthTransactionError, SUPPORTED_PROVIDERS
from .market_provider import MarketSnapshotRequest, build_snapshot_response
from .local_auth import (
    LocalAccount,
    LocalAuthError,
    LocalRegisterRequest,
    LocalSession,
    authenticate as authenticate_local,
    delete_account as delete_local_account,
    login as login_local,
    logout as logout_local,
    purge_expired_sessions,
    register as register_local,
)

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]

ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Malformed request or replay cursor"},
    403: {"model": ErrorResponse, "description": "Invalid operation control token"},
    404: {"model": ErrorResponse, "description": "Entity not found"},
    409: {"model": ErrorResponse, "description": "Idempotency or lifecycle conflict"},
    413: {"model": ErrorResponse, "description": "Request body exceeds the configured limit"},
    415: {"model": ErrorResponse, "description": "Unsupported request content type"},
    422: {"model": ErrorResponse, "description": "Strict contract validation failure"},
    429: {"model": ErrorResponse, "description": "Rate or model-call quota exceeded"},
    500: {"model": ErrorResponse, "description": "Validated internal failure"},
}

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Verify external state before accepting traffic in a persistent runtime."""
    _assert_runtime_ready()
    yield


app = FastAPI(
    title="ForesightNine API",
    version="1.0.0",
    description="Contract-first research scenario API. Deterministic mock providers only in Phase 1.",
    lifespan=lifespan,
)
clock = FixedClock()
lifecycle_clock = RuntimeClock()
research_provider = MockResearchProvider(clock)
probability_assessor = DeterministicMockProbabilityAssessor(clock)
worldline_generator = DeterministicMockWorldlineGenerator(probability_assessor, clock)
challenge_provider = build_llm_provider()
evidence_provider = build_search_evidence_provider()
data_provider = MockDataProvider(clock)
factor_provider = MockFactorProvider(clock)
security_config = build_security_config()
try:
    _model_call_quota = int(os.getenv("FORESIGHTNINE_MODEL_CALL_QUOTA", "20"))
except ValueError:
    _model_call_quota = 20
abuse_control: AbuseBackend = build_abuse_control(model_quota=max(1, _model_call_quota))
captcha_verifier = build_captcha_verifier(security_config)
trusted_proxy_resolver = TrustedProxyResolver(security_config.trusted_proxies)
retention_policy = RetentionPolicy()
try:
    oauth_foundation: OAuthFoundation | None = OAuthFoundation.from_environment()
except OAuthConfigurationError:
    # OAuth is optional.  Invalid or absent deployment configuration must make
    # every provider unavailable without preventing the research API from booting.
    oauth_foundation = None

def _storage_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Normalize deployment URL names to the storage foundation's safe alias.

    The production preflight intentionally names the server-side URL as either
    ``FORESIGHTNINE_POSTGRES_URL`` or ``DATABASE_URL``.  ``storage.py`` keeps
    its narrower ``FORESIGHTNINE_POSTGRES_DSN`` input, so the API adapts the
    names without changing the storage foundation or ever logging the value.
    The existing DSN name remains the highest-priority compatibility alias.
    """
    normalized = dict(environ if environ is not None else os.environ)
    if not normalized.get("FORESIGHTNINE_POSTGRES_DSN", "").strip():
        for name in ("FORESIGHTNINE_POSTGRES_URL", "DATABASE_URL"):
            configured_url = normalized.get(name)
            if configured_url and configured_url.strip():
                normalized["FORESIGHTNINE_POSTGRES_DSN"] = configured_url
                break
    return normalized


def _storage_config(environ: Mapping[str, str] | None = None) -> StorageConfig:
    return StorageConfig.from_env(_storage_environment(environ))


def _build_entity_repositories(
    config: StorageConfig,
) -> tuple[
    JsonDocumentRepository[ResearchSession],
    JsonDocumentRepository[ThesisDocument],
    JsonDocumentRepository[ChallengeRecord],
    JsonDocumentRepository[int],
    JsonDocumentRepository[EvidenceItem],
    JsonDocumentRepository[Worldline],
    JsonDocumentRepository[MarketDataManifest],
    JsonDocumentRepository[str],
    JsonDocumentRepository[LocalAccount],
    JsonDocumentRepository[LocalSession],
]:
    """Build every API entity map with its canonical typed document loader."""
    return (
        build_repository(config, "sessions", value_loader=ResearchSession.model_validate),
        build_repository(config, "theses", value_loader=ThesisDocument.model_validate),
        build_repository(config, "challenges", value_loader=ChallengeRecord.model_validate),
        build_repository(config, "challenge_turns", value_loader=int),
        build_repository(config, "evidence", value_loader=EvidenceItem.model_validate),
        build_repository(config, "worldlines", value_loader=Worldline.model_validate),
        build_repository(config, "market_manifests", value_loader=MarketDataManifest.model_validate),
        build_repository(config, "worldline_challenges", value_loader=str),
        build_repository(config, "accounts", value_loader=LocalAccount.model_validate),
        build_repository(config, "auth_sessions", value_loader=LocalSession.model_validate),
    )


storage_config = _storage_config()
(
    SESSIONS,
    THESES,
    CHALLENGES,
    CHALLENGE_TURNS,
    EVIDENCE,
    WORLDLINES,
    MARKET_MANIFESTS,
    WORLDLINE_CHALLENGES,
    ACCOUNTS,
    AUTH_SESSIONS,
) = _build_entity_repositories(storage_config)
operations = OperationManager(lifecycle_clock, store=build_operation_store(storage_config))
OPERATION_SESSIONS: dict[str, str] = {}


def _assert_runtime_ready() -> None:
    """Probe every external dependency required by the selected runtime."""
    if storage_config.mode is StorageMode.POSTGRES:
        try:
            SESSIONS.check_ready()
            ACCOUNTS.check_ready()
            AUTH_SESSIONS.check_ready()
            operations.store.check_ready()
        except Exception as error:
            raise RuntimeError("Persistent PostgreSQL state is not ready.") from error
    try:
        abuse_control.check_ready()
    except Exception as error:
        raise RuntimeError("Abuse-control state is not ready.") from error

NORMAL_CONFIRM_MINIMUM_TOTAL = 30
NORMAL_CONFIRM_MINIMUM_CRITICAL_SCORE = 3
NORMAL_CONFIRM_CRITICAL_CRITERIA = (
    "premise_clarity",
    "time_boundary",
    "causal_chain",
    "asset_transmission",
    "falsifiability",
)


def reset_state() -> None:
    operations.reset()
    abuse_control.reset()
    telemetry.reset()
    SESSIONS.clear()
    THESES.clear()
    CHALLENGES.clear()
    CHALLENGE_TURNS.clear()
    EVIDENCE.clear()
    WORLDLINES.clear()
    MARKET_MANIFESTS.clear()
    WORLDLINE_CHALLENGES.clear()
    ACCOUNTS.clear()
    AUTH_SESSIONS.clear()
    OPERATION_SESSIONS.clear()


def cleanup_expired_state() -> object:
    purge_expired_sessions(AUTH_SESSIONS, now=lifecycle_clock.now())
    durable_operation_sessions = operations.session_ids()
    active_session_ids = {
        durable_operation_sessions[operation_id]
        for operation_id in operations.active_operation_ids()
        if operation_id in durable_operation_sessions
    }
    report = cleanup_research_state(
        sessions=SESSIONS, theses=THESES, challenges=CHALLENGES,
        challenge_turns=CHALLENGE_TURNS, evidence=EVIDENCE, worldlines=WORLDLINES,
        market_manifests=MARKET_MANIFESTS,
        worldline_challenges=WORLDLINE_CHALLENGES, active_session_ids=active_session_ids,
        operations=operations, now=lifecycle_clock.now(), policy=retention_policy,
    )
    active = operations.active_operation_ids()
    for operation_id in tuple(OPERATION_SESSIONS):
        if operation_id not in active:
            OPERATION_SESSIONS.pop(operation_id, None)
    return report


def _model_subjects(request: Request, session_id: str | None = None) -> tuple[str, ...]:
    client_ip = getattr(request.state, "client_ip", "0.0.0.0")
    # Keep a shared rolling 24-hour bucket in addition to per-IP/session
    # limits.  The provider call reservation is still idempotency keyed, so a
    # replay cannot consume the global daily allowance twice.
    subjects = ["global:daily", f"ip:{client_ip}"]
    retained_session = session_id or getattr(request.state, "session_id", None)
    if retained_session:
        subjects.append(f"session:{retained_session}")
    return tuple(subjects)


def _reserve_model_call(request: Request, operation_id: str, *, session_id: str | None = None) -> None:
    decision = abuse_control.model.reserve(operation_id, _model_subjects(request, session_id))
    if not decision.available:
        raise ApiProblem(
            503, "abuse_backend_unavailable", "Abuse controls are temporarily unavailable.",
            retryable=True, retry_after=decision.retry_after or 1,
        )
    if not decision.allowed:
        raise ApiProblem(
            429, "quota_exceeded", "Model-call quota exceeded; retry later.",
            retryable=True, retry_after=decision.retry_after,
        )


def _reserve_search_call(request: Request) -> None:
    """Reserve one provider-search call before crossing the external boundary.

    The request middleware already enforces the per-IP/session sliding window.
    Reuse the provider-call quota as the daily budget so MCP search cannot be
    used to bypass the same public cost bound as challenge generation.
    """
    decision = abuse_control.model.reserve(f"search:{uuid4().hex}", _model_subjects(request))
    if not decision.available:
        raise ApiProblem(
            503, "abuse_backend_unavailable", "Abuse controls are temporarily unavailable.",
            retryable=True, retry_after=decision.retry_after or 1,
        )
    if not decision.allowed:
        raise ApiProblem(
            429, "quota_exceeded", "Search-provider quota exceeded; retry later.",
            retryable=True, retry_after=decision.retry_after,
        )


def _bind_operation_session(operation_id: str, session_id: str | None) -> None:
    if session_id:
        OPERATION_SESSIONS[operation_id] = session_id


def _session_for_challenge(challenge_id: str) -> str | None:
    challenge = CHALLENGES.get(challenge_id)
    if challenge is None:
        return None
    thesis = THESES.get(challenge.thesis_id)
    return thesis.session_id if thesis is not None else None


app.add_middleware(
    CORSMiddleware,
    allow_origins=list(security_config.allowed_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "Last-Event-ID", "X-Captcha-Token", "X-Session-ID"],
    max_age=600,
)
app.add_middleware(
    SecurityBoundaryMiddleware,
    config=security_config,
    abuse=abuse_control,
    captcha=lambda: captcha_verifier,
    resolver=trusted_proxy_resolver,
    cleanup=cleanup_expired_state,
)


def _metadata(payload: dict[str, Any], ref_id: str, label: str, source_type: str = "model_inference") -> dict[str, Any]:
    ref = SourceRef(ref_id=ref_id, source_type=source_type, label=label)
    refs = (ref,)
    hash_input = {"schema_version": "v1", "source_refs": [ref.model_dump(mode="json", exclude_none=True)], **payload}
    return {
        "schema_version": "v1",
        "created_at": clock.now(),
        "source_refs": refs,
        "content_hash": content_hash(hash_input),
    }


def _request_payload(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


def _validate_request(model: BaseModel, contract: str) -> None:
    try:
        validate_payload(model, contract)
    except ContractViolation as error:
        raise ApiProblem(422, "validation_error", str(error)) from error


def _provider_name(provider: object, fallback: str) -> str:
    """Expose a stable capability label without serialising configuration or secrets."""
    name = getattr(provider, "name", None)
    return name if isinstance(name, str) and name else fallback


def _candidate_response(raw: object, provider_name: str) -> SearchCandidate:
    """Convert a provider's unverified candidate into canonical display-only data.

    A candidate cannot become evidence through this route.  The client must still
    show it to the user and submit a separately hashed EvidenceSubmissionRequest.
    """
    title = getattr(raw, "title", None)
    url = getattr(raw, "url", None)
    excerpt = getattr(raw, "excerpt", None)
    publisher = getattr(raw, "publisher", None)
    raw_published_at = getattr(raw, "published_at", None)
    independently_reviewable = getattr(raw, "independently_reviewable", None)
    if not all(isinstance(value, str) and value for value in (title, url, excerpt)):
        raise ProviderFailure("search_response_invalid", "Search provider returned an invalid candidate.", retryable=False)
    if publisher is not None and (not isinstance(publisher, str) or not publisher):
        raise ProviderFailure("search_response_invalid", "Search provider returned an invalid candidate.", retryable=False)
    if raw_published_at is not None and not isinstance(raw_published_at, date):
        raise ProviderFailure("search_response_invalid", "Search provider returned an invalid candidate.", retryable=False)
    if type(independently_reviewable) is not bool:
        raise ProviderFailure("search_response_invalid", "Search provider returned an invalid candidate.", retryable=False)
    published_at = (
        datetime.combine(raw_published_at, time.min, tzinfo=timezone.utc)
        if raw_published_at is not None
        else None
    )
    retrieved_at = clock.now()
    hash_payload = {
        "search_provider": provider_name,
        "title": title,
        "url": url,
        "excerpt": excerpt,
        "publisher": publisher,
        "published_at": published_at.isoformat() if published_at else None,
        "retrieved_at": retrieved_at.isoformat(),
        "independently_reviewable": independently_reviewable,
    }
    return SearchCandidate(
        search_result_hash=content_hash(hash_payload),
        search_provider=provider_name,
        title=title,
        url=url,
        excerpt=excerpt,
        publisher=publisher,
        published_at=published_at,
        retrieved_at=retrieved_at,
        independently_reviewable=independently_reviewable,
    )


def _search_candidates(query: str, limit: int, request: Request) -> SearchCandidatesResponse:
    """Discover safe source candidates or return the explicit client fallback.

    Provider failures are intentionally surfaced as capability unavailability,
    not as fabricated citations and not as raw provider diagnostics.  Discovery
    is stateless and never fetches, stores, or promotes a selected URL.
    """
    provider_name = _provider_name(evidence_provider, "client-evidence-bundle")
    search = getattr(evidence_provider, "search", None)
    if not getattr(evidence_provider, "supports_project_search", False) or not callable(search):
        return SearchCandidatesResponse(
            schema_version="v1", provider=provider_name, project_search_available=False,
            fallback="client_bundle", candidates=[],
        )
    _reserve_search_call(request)
    try:
        candidates = [_candidate_response(item, provider_name) for item in search(query, limit=limit)]
    except ProviderFailure:
        record_provider_failure(provider=provider_name, endpoint="/v1/search/candidates")
        return SearchCandidatesResponse(
            schema_version="v1", provider=provider_name, project_search_available=False,
            fallback="client_bundle", candidates=[],
        )
    return SearchCandidatesResponse(
        schema_version="v1", provider=provider_name, project_search_available=True,
        fallback="client_bundle", candidates=candidates,
    )


def _validate_provider_worldline_bundle(
    bundle: WorldlineBundle,
    *,
    challenge_id: str,
    expected_premise: str,
    expected_baseline_date: date,
    expected_horizon_end: date,
) -> None:
    """Fail closed before a typed provider result can cross into API state.

    Pydantic's ``model_copy`` deliberately permits unvalidated updates, so an
    ``isinstance`` check alone is not a provider boundary.  Re-run the
    canonical schema and every Phase 4 semantic gate before persisting any
    nested worldline, impact, reveal schedule, or probability assessment.
    """
    try:
        validate_payload(bundle, "WorldlineBundle")
        if bundle.challenge_id != challenge_id:
            raise ContractViolation("WorldlineBundle", ["bundle challenge_id must match the requested challenge"])
        for worldline in bundle.worldlines:
            validate_payload(worldline, "Worldline")
            if worldline.bundle_id != bundle.bundle_id:
                raise ContractViolation("WorldlineBundle", ["every worldline bundle_id must match the bundle_id"])
            if worldline.premise != expected_premise:
                raise ContractViolation("WorldlineBundle", ["every worldline premise must match the retained thesis"])
            if worldline.baseline_date != expected_baseline_date:
                raise ContractViolation("WorldlineBundle", ["every worldline baseline_date must match the fixed API baseline"])
            if worldline.horizon_end != expected_horizon_end:
                raise ContractViolation("WorldlineBundle", ["every worldline horizon_end must match the requested horizon"])
        validate_worldline_bundle_semantics(bundle)
        validate_branch_probability_consistency(bundle.probability_assessments, bundle.worldlines)
    except Exception as error:
        # Do not let a typed-but-invalid result bypass the operation terminal
        # path or disclose provider/schema diagnostics to a client.
        raise ProviderFailure(
            "invalid_provider_output",
            "Worldline provider returned a result outside the accepted boundary.",
            retryable=False,
        ) from error


def _validate_provider_probability_assessment(
    assessment: ProbabilityAssessment,
    *,
    expected_worldline_id: str,
) -> None:
    """Re-validate typed probability output at the route boundary."""
    try:
        validate_payload(assessment, "ProbabilityAssessment")
        if assessment.worldline_id != expected_worldline_id:
            raise ContractViolation("ProbabilityAssessment", ["assessment worldline_id must match the requested worldline"])
        validate_probability_semantics(assessment)
    except Exception as error:
        raise ProviderFailure(
            "invalid_provider_output",
            "Probability provider returned a result outside the accepted boundary.",
            retryable=False,
        ) from error


def _mark_replay(response: Response, replayed: bool) -> None:
    response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
    response.headers["Cache-Control"] = "no-store"


def _challenge_from_draft(
    draft: ChallengeDraft,
    *,
    operation_id: str,
    challenge_id: str,
    thesis_id: str,
    turn: int,
    status: str,
    private_evidence_declared: bool,
) -> ChallengeRecord:
    payload = {
        "turn_id": deterministic_id("turn", f"{challenge_id}:{turn}"),
        "challenge_id": challenge_id,
        "thesis_id": thesis_id,
        "claims": list(draft.claims),
        "objections": list(draft.objections),
        "missing_evidence": list(draft.missing_evidence),
        "questions": list(draft.questions),
        "rubric_scores": [score.model_dump(mode="json", exclude_none=True) for score in draft.rubric_scores],
        "unresolved_issues": list(draft.unresolved_issues),
        "citation_ids": list(draft.citation_ids),
        "public_fact_citation_ids": list(draft.public_fact_citation_ids),
        "native_search_used": draft.native_search_used,
        "provenance": draft.provenance,
        "user_confirmation": "pending",
        "private_evidence_declared": private_evidence_declared,
        "status": status,
    }
    result = ChallengeRecord(
        **_metadata(payload, f"provider:{challenge_provider.name}", "Validated adversarial challenge draft"),
        **payload,
    )
    validate_payload(result, "ChallengeRecord")
    return result


def _raise_if_operation_cancelled(operation_id: str) -> None:
    if operations.cancel_event(operation_id).is_set():
        raise ProviderFailure("provider_cancelled", "Provider request was cancelled before state was committed.", retryable=True)


def _untrusted_evidence_text(items: tuple[EvidenceItem, ...]) -> tuple[str, ...]:
    """Every user/model-controlled evidence string is prompt-control checked."""
    return tuple(text for item in items for text in (item.title, item.excerpt, item.user_note, item.publisher or ""))


def _challenge_status(draft: ChallengeDraft) -> str:
    return "EVIDENCE_REQUIRED" if draft.missing_evidence or draft.unresolved_issues else "CHALLENGING"


def _error(status_code: int, error_code: str, message: str, request: Request, *, retryable: bool = False, details: list[ErrorDetail] | None = None, retry_after: int | None = None) -> JSONResponse:
    request_id = deterministic_id("request", f"{request.method}:{request.url.path}:{error_code}")
    payload = ErrorResponse(
        schema_version="v1",
        error_code=error_code,
        message=message,
        request_id=request_id,
        retryable=retryable,
        details=details or [],
    )
    validate_payload(payload, "ErrorResponse")
    response = JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))
    if status_code in {403, 429, 503}:
        response.headers["Retry-After"] = str(max(1, retry_after or 60))
    return response


@app.exception_handler(ApiProblem)
async def api_problem_handler(request: Request, error: ApiProblem) -> JSONResponse:
    return _error(
        error.status_code,
        error.error_code,
        error.message,
        request,
        retryable=error.retryable,
        details=error.details,
        retry_after=error.retry_after,
    )


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, error: RequestValidationError) -> JSONResponse:
    if request.url.path.startswith("/v1/auth/") and any(item["loc"] == ("path", "provider") for item in error.errors()):
        return _error(400, "bad_request", "OAuth authorization request is unavailable.", request)
    details = [
        ErrorDetail(location="/".join(str(part) for part in item["loc"]), message=item["msg"])
        for item in error.errors()
    ]
    return _error(422, "validation_error", "Request failed strict contract validation.", request, details=details)


@app.exception_handler(ContractViolation)
async def contract_violation_handler(request: Request, error: ContractViolation) -> JSONResponse:
    details = [ErrorDetail(location=error.contract, message=item) for item in error.errors]
    return _error(500, "contract_violation", "Generated response violated the canonical contract.", request, details=details)


@app.exception_handler(ProviderFailure)
async def provider_failure_handler(request: Request, error: ProviderFailure) -> JSONResponse:
    # Raw CLI/model diagnostics are intentionally not exposed or stored.
    return _error(
        500 if error.retryable else 400,
        "internal_error" if error.retryable else "bad_request",
        "Provider request could not produce an accepted result.",
        request,
        retryable=error.retryable,
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, error: Exception) -> JSONResponse:
    return _error(500, "internal_error", "Internal operation failed without exposing sensitive detail.", request, retryable=True)


@app.get("/health", response_model=HealthResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="health")
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        provider=_provider_name(challenge_provider, "deterministic_mock"),
        search_provider=_provider_name(evidence_provider, "client-evidence-bundle"),
        project_search_available=bool(getattr(evidence_provider, "supports_project_search", False)),
        schema_version="v1",
        clock=clock.now(),
    )


@app.get("/readyz", include_in_schema=False)
def readyz(request: Request) -> JSONResponse:
    """Dependency readiness probe for the supervisor and deployment monitor."""
    try:
        _assert_runtime_ready()
    except Exception:
        return _error(503, "internal_error", "Required runtime dependencies are not ready.", request, retryable=True)
    return JSONResponse({"status": "ready"}, headers={"Cache-Control": "no-store"})


@app.post("/snapshots", include_in_schema=False)
def market_snapshots(request: MarketSnapshotRequest) -> JSONResponse:
    """Serve the bounded client market runtime without exposing a vendor API."""
    return JSONResponse(build_snapshot_response(request))


def _local_auth_available() -> bool:
    return (
        os.getenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "disabled").strip().lower() == "mock"
        and os.getenv("FORESIGHTNINE_ENV", "development").strip().lower() != "production"
    )


def _local_auth_problem(error: LocalAuthError) -> ApiProblem:
    return ApiProblem(error.status_code, error.error_code, error.message)


def _local_auth_unavailable() -> None:
    if not _local_auth_available():
        raise ApiProblem(503, "internal_error", "Local staging authentication is not enabled.", retryable=False)


@app.post("/v1/auth/local/register", include_in_schema=False, status_code=201)
def local_register(request: LocalRegisterRequest) -> JSONResponse:
    _local_auth_unavailable()
    try:
        result = register_local(
            ACCOUNTS,
            AUTH_SESSIONS,
            email=request.email,
            password=request.password,
            now=lifecycle_clock.now(),
        )
    except LocalAuthError as error:
        raise _local_auth_problem(error) from error
    return JSONResponse(result, status_code=201, headers={"Cache-Control": "no-store"})


@app.post("/v1/auth/local/login", include_in_schema=False)
def local_login(request: LocalRegisterRequest) -> JSONResponse:
    _local_auth_unavailable()
    try:
        result = login_local(
            ACCOUNTS,
            AUTH_SESSIONS,
            email=request.email,
            password=request.password,
            now=lifecycle_clock.now(),
        )
    except LocalAuthError as error:
        raise _local_auth_problem(error) from error
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.post("/v1/auth/local/logout", include_in_schema=False)
def local_logout(authorization: str | None = Header(default=None, alias="Authorization")) -> JSONResponse:
    _local_auth_unavailable()
    try:
        _account, session = authenticate_local(ACCOUNTS, AUTH_SESSIONS, authorization, now=lifecycle_clock.now())
        logout_local(AUTH_SESSIONS, session)
    except LocalAuthError as error:
        raise _local_auth_problem(error) from error
    return JSONResponse({"logged_out": True}, headers={"Cache-Control": "no-store"})


@app.delete("/v1/auth/local/account", include_in_schema=False)
def local_delete_account(authorization: str | None = Header(default=None, alias="Authorization")) -> JSONResponse:
    _local_auth_unavailable()
    try:
        account, _session = authenticate_local(ACCOUNTS, AUTH_SESSIONS, authorization, now=lifecycle_clock.now())
        delete_local_account(ACCOUNTS, AUTH_SESSIONS, account, now=lifecycle_clock.now())
    except LocalAuthError as error:
        raise _local_auth_problem(error) from error
    return JSONResponse({"deleted": True}, headers={"Cache-Control": "no-store"})


@app.get(
    "/v1/auth/providers",
    response_model=OAuthProvidersResponse,
    response_model_exclude_none=True,
    responses=ERROR_RESPONSES,
    operation_id="getOAuthProviders",
)
def oauth_providers() -> OAuthProvidersResponse:
    """Return only server-validated OAuth readiness metadata."""

    configured = oauth_foundation
    enabled = set(configured.supported_providers) if configured is not None else set()
    return OAuthProvidersResponse(
        providers=tuple(
            OAuthProviderReadiness(provider=provider, enabled=provider in enabled)
            for provider in SUPPORTED_PROVIDERS
        )
    )


@app.post(
    "/v1/auth/{provider}/start",
    response_model=OAuthAuthorizationResponse,
    response_model_exclude_none=True,
    responses=ERROR_RESPONSES,
    operation_id="startOAuthAuthorization",
)
def start_oauth(provider: OAuthProviderName, request: OAuthStartRequest) -> OAuthAuthorizationResponse:
    """Create a short-lived PKCE authorization request without contacting a provider."""

    foundation = oauth_foundation
    if foundation is None:
        raise ApiProblem(400, "bad_request", "OAuth authorization is not available.")
    try:
        authorization = foundation.create_transaction(provider, request.return_uri)
    except (OAuthConfigurationError, OAuthTransactionError) as error:
        # The foundation messages are intentionally generic, but the route also
        # uses one fixed public message so configuration details never cross the API boundary.
        raise ApiProblem(400, "bad_request", "OAuth authorization request is unavailable.") from error
    return OAuthAuthorizationResponse(
        provider=authorization.provider,
        client_id=authorization.client_id,
        redirect_uri=authorization.redirect_uri,
        return_uri=authorization.return_uri,
        state=authorization.state,
        code_challenge=authorization.code_challenge,
        code_challenge_method="S256",
    )


@app.get("/v1/search/candidates", response_model=SearchCandidatesResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="searchEvidenceCandidates")
def search_evidence_candidates(
    http_request: Request,
    query: Annotated[str, Query(min_length=1, max_length=70)],
    limit: Annotated[int, Query(ge=1, le=10)] = 5,
) -> SearchCandidatesResponse:
    """Return unverified project-search candidates or an explicit client fallback.

    The endpoint never fetches a candidate URL, does not send candidate text to
    the challenge model, and does not create an EvidenceItem.  A user selection
    must take the normal canonical evidence-submission path.
    """
    return _search_candidates(query, limit, http_request)


@app.post("/v1/sessions", response_model=SessionOperationResponse, response_model_exclude_none=True, status_code=201, responses=ERROR_RESPONSES, operation_id="createSession")
def create_session(request: CreateSessionRequest, response: Response, idempotency_key: IdempotencyKey) -> SessionOperationResponse:
    _validate_request(request, "CreateSessionRequest")

    def build(operation_id: str) -> tuple[ResearchSession, str]:
        session_id = deterministic_id("session", operation_id)
        payload = {"session_id": session_id, "status": "DRAFT", "expires_at": lifecycle_clock.now() + timedelta(hours=24)}
        metadata = _metadata(payload, "mock:session", "Deterministic anonymous session")
        metadata["created_at"] = lifecycle_clock.now()
        session = ResearchSession(**metadata, **payload)
        validate_payload(session, "ResearchSession")
        SESSIONS[session_id] = session
        return session, session_id

    result, replayed = operations.execute(
        endpoint="/v1/sessions",
        idempotency_key=idempotency_key,
        request_payload=_request_payload(request),
        response_model=SessionOperationResponse,
        result_field="session",
        build_result=build,
    )
    _mark_replay(response, replayed)
    return result


@app.post("/v1/challenges", response_model=ChallengeOperationResponse, response_model_exclude_none=True, status_code=201, responses=ERROR_RESPONSES, operation_id="createChallenge")
def create_challenge(request: CreateChallengeRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> ChallengeOperationResponse:
    _validate_request(request, "CreateChallengeRequest")
    if request.session_id not in SESSIONS:
        raise ApiProblem(404, "not_found", "Session not found.")

    def build(operation_id: str) -> tuple[ChallengeRecord, str]:
        _bind_operation_session(operation_id, request.session_id)
        _reserve_model_call(http_request, operation_id, session_id=request.session_id)
        thesis = research_provider.thesis(
            operation_id=operation_id,
            session_id=request.session_id,
            raw_text=request.thesis,
            capital=request.capital,
            base_currency=request.base_currency,
            private_evidence=request.user_private_evidence_claims,
        )
        challenge_id = deterministic_id("challenge", operation_id)
        draft = challenge_provider.generate_challenge(
            ChallengeInput(
                operation_id=operation_id, challenge_id=challenge_id, thesis_id=thesis.thesis_id,
                claim=request.thesis, turn=1, status="CHALLENGING",
                private_evidence_declared=request.user_private_evidence_claims,
                cancel_event=operations.cancel_event(operation_id),
            )
        )
        _raise_if_operation_cancelled(operation_id)
        challenge = _challenge_from_draft(
            draft, operation_id=operation_id, thesis_id=thesis.thesis_id, challenge_id=challenge_id,
            turn=1, status="CHALLENGING", private_evidence_declared=request.user_private_evidence_claims,
        )
        THESES[thesis.thesis_id] = thesis
        CHALLENGES[challenge_id] = challenge
        CHALLENGE_TURNS[challenge_id] = 1
        return challenge, challenge_id

    result, replayed = operations.execute(
        endpoint="/v1/challenges",
        idempotency_key=idempotency_key,
        request_payload=_request_payload(request),
        response_model=ChallengeOperationResponse,
        result_field="challenge",
        build_result=build,
        session_id=request.session_id,
    )
    _mark_replay(response, replayed)
    return result


@app.post("/v1/challenges/{challenge_id}/messages", response_model=ChallengeOperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="addChallengeMessage")
def add_challenge_message(challenge_id: str, request: ChallengeMessageRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> ChallengeOperationResponse:
    _validate_request(request, "ChallengeMessageRequest")
    session_id = _session_for_challenge(challenge_id)

    def build(operation_id: str) -> tuple[ChallengeRecord, str]:
        current = CHALLENGES.get(challenge_id)
        if not current:
            raise ApiProblem(404, "not_found", "Challenge not found.")
        if current.status not in {"CHALLENGING", "EVIDENCE_REQUIRED"}:
            raise ApiProblem(409, "invalid_state_transition", f"Message command cannot transition from {current.status}.")
        session_id = THESES[current.thesis_id].session_id
        _bind_operation_session(operation_id, session_id)
        _reserve_model_call(http_request, operation_id, session_id=session_id)
        turn = CHALLENGE_TURNS[challenge_id] + 1
        attached = tuple(item for item in EVIDENCE.values() if item.challenge_id == challenge_id)
        draft = challenge_provider.generate_challenge(
            ChallengeInput(
                operation_id=operation_id, challenge_id=challenge_id, thesis_id=current.thesis_id,
                claim=request.message, turn=turn, status="EVIDENCE_REQUIRED",
                private_evidence_declared=current.private_evidence_declared,
                evidence_ids=tuple(item.evidence_id for item in attached if item.independently_reviewable),
                evidence_grades=tuple((item.evidence_id, item.grade) for item in attached),
                untrusted_text=_untrusted_evidence_text(attached),
                cancel_event=operations.cancel_event(operation_id),
            )
        )
        _raise_if_operation_cancelled(operation_id)
        updated = _challenge_from_draft(
            draft, operation_id=operation_id, thesis_id=current.thesis_id, challenge_id=challenge_id,
            turn=turn, status=_challenge_status(draft), private_evidence_declared=current.private_evidence_declared,
        )
        CHALLENGES[challenge_id] = updated
        CHALLENGE_TURNS[challenge_id] = turn
        return updated, challenge_id

    result, replayed = operations.execute(
        endpoint=f"/v1/challenges/{challenge_id}/messages",
        idempotency_key=idempotency_key,
        request_payload=_request_payload(request),
        response_model=ChallengeOperationResponse,
        result_field="challenge",
        build_result=build,
        session_id=session_id,
    )
    _mark_replay(response, replayed)
    return result


@app.post("/v1/challenges/{challenge_id}/messages/async", response_model=OperationResponse, response_model_exclude_none=True, status_code=202, responses=ERROR_RESPONSES, operation_id="startChallengeMessageStream")
def start_challenge_message_stream(challenge_id: str, request: ChallengeMessageRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> OperationResponse:
    """Start a Phase 3 message without waiting for its provider runner."""
    _validate_request(request, "ChallengeMessageRequest")
    session_id = _session_for_challenge(challenge_id)

    def before_accept(operation_id: str) -> None:
        current = CHALLENGES.get(challenge_id)
        if not current:
            raise ApiProblem(404, "not_found", "Challenge not found.")
        if current.status not in {"CHALLENGING", "EVIDENCE_REQUIRED"}:
            raise ApiProblem(409, "invalid_state_transition", f"Message command cannot transition from {current.status}.")
        session_id = THESES[current.thesis_id].session_id
        _bind_operation_session(operation_id, session_id)
        _reserve_model_call(http_request, operation_id, session_id=session_id)

    def build(operation_id: str) -> tuple[ChallengeRecord, str]:
        current = CHALLENGES.get(challenge_id)
        if not current:
            raise ApiProblem(404, "not_found", "Challenge not found.")
        if current.status not in {"CHALLENGING", "EVIDENCE_REQUIRED"}:
            raise ApiProblem(409, "invalid_state_transition", f"Message command cannot transition from {current.status}.")
        turn = CHALLENGE_TURNS[challenge_id] + 1
        attached = tuple(item for item in EVIDENCE.values() if item.challenge_id == challenge_id)
        draft = challenge_provider.generate_challenge(ChallengeInput(
            operation_id=operation_id, challenge_id=challenge_id, thesis_id=current.thesis_id,
            claim=request.message, turn=turn, status=current.status,
            private_evidence_declared=current.private_evidence_declared,
            evidence_ids=tuple(item.evidence_id for item in attached if item.independently_reviewable),
            evidence_grades=tuple((item.evidence_id, item.grade) for item in attached),
            untrusted_text=_untrusted_evidence_text(attached), cancel_event=operations.cancel_event(operation_id),
        ))
        _raise_if_operation_cancelled(operation_id)
        updated = _challenge_from_draft(
            draft, operation_id=operation_id, thesis_id=current.thesis_id, challenge_id=challenge_id,
            turn=turn, status=_challenge_status(draft), private_evidence_declared=current.private_evidence_declared,
        )
        CHALLENGES[challenge_id] = updated
        CHALLENGE_TURNS[challenge_id] = turn
        return updated, challenge_id

    result, replayed = operations.submit_async(
        endpoint=f"/v1/challenges/{challenge_id}/messages/async", idempotency_key=idempotency_key,
        request_payload=_request_payload(request), result_field="challenge", build_result=build,
        before_accept=before_accept,
        session_id=session_id,
    )
    _mark_replay(response, replayed)
    return result


@app.post("/v1/challenges/{challenge_id}/evidence", response_model=ChallengeOperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="addChallengeEvidence")
def add_challenge_evidence(challenge_id: str, request: EvidenceSubmissionRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> ChallengeOperationResponse:
    _validate_request(request, "EvidenceSubmissionRequest")
    session_id = _session_for_challenge(challenge_id)
    has_search_provider = request.search_provider is not None
    has_search_result_hash = request.search_result_hash is not None
    if has_search_provider != has_search_result_hash:
        raise ApiProblem(422, "validation_error", "Search evidence provenance requires both a provider and result hash.")
    independently_reviewable = evidence_provider.validate_client_bundle(SearchEvidenceBundle(
        url=str(request.url) if request.url is not None else None,
        title=request.title,
        published_at=request.published_at,
        retrieved_at=request.retrieved_at,
        excerpt=request.excerpt,
        user_note=request.user_note,
        supplied_content_hash=request.content_hash,
        search_provider=request.search_provider,
        search_result_hash=request.search_result_hash,
    ))

    def build(operation_id: str) -> tuple[ChallengeRecord, str]:
        current = CHALLENGES.get(challenge_id)
        if not current:
            raise ApiProblem(404, "not_found", "Challenge not found.")
        if current.status != "EVIDENCE_REQUIRED":
            raise ApiProblem(409, "invalid_state_transition", f"Evidence command cannot transition from {current.status}.")
        session_id = THESES[current.thesis_id].session_id
        _bind_operation_session(operation_id, session_id)
        _reserve_model_call(http_request, operation_id, session_id=session_id)
        evidence_id = deterministic_id("evidence", operation_id)
        evidence_payload: dict[str, Any] = {
            "evidence_id": evidence_id,
            "challenge_id": challenge_id,
            "source_type": "commentary" if independently_reviewable else "user_private",
            "title": request.title,
            "retrieved_at": request.retrieved_at,
            "excerpt": request.excerpt,
            "user_note": request.user_note,
            # A client URL is independently reviewable, not independently
            # verified by this API. Keep third-party fallback evidence at C.
            "grade": "C" if independently_reviewable else "D",
            "supports_or_challenges": "context",
            "independently_reviewable": independently_reviewable,
        }
        if request.url is not None:
            evidence_payload["url"] = str(request.url)
        if request.publisher is not None:
            evidence_payload["publisher"] = request.publisher
        if request.published_at is not None:
            evidence_payload["published_at"] = request.published_at
        if request.search_provider is not None:
            evidence_payload["search_provider"] = request.search_provider
            evidence_payload["search_result_hash"] = request.search_result_hash
        evidence = EvidenceItem(**_metadata(evidence_payload, "client:evidence", "Client submitted evidence", "user_submitted"), **evidence_payload)
        validate_payload(evidence, "EvidenceItem")
        turn = CHALLENGE_TURNS[challenge_id] + 1
        attached = tuple(item for item in EVIDENCE.values() if item.challenge_id == challenge_id)
        attached = (*attached, evidence)
        draft = challenge_provider.generate_challenge(
            ChallengeInput(
                operation_id=operation_id, challenge_id=challenge_id, thesis_id=current.thesis_id,
                claim=current.claims[0], turn=turn, status="CHALLENGING",
                private_evidence_declared=current.private_evidence_declared or not independently_reviewable,
                evidence_ids=tuple(item.evidence_id for item in attached if item.independently_reviewable),
                evidence_grades=tuple((item.evidence_id, item.grade) for item in attached),
                untrusted_text=_untrusted_evidence_text(attached),
                cancel_event=operations.cancel_event(operation_id),
            )
        )
        _raise_if_operation_cancelled(operation_id)
        updated = _challenge_from_draft(
            draft, operation_id=operation_id, thesis_id=current.thesis_id, challenge_id=challenge_id,
            turn=turn, status=_challenge_status(draft),
            private_evidence_declared=current.private_evidence_declared or not independently_reviewable,
        )
        EVIDENCE[evidence_id] = evidence
        CHALLENGES[challenge_id] = updated
        CHALLENGE_TURNS[challenge_id] = turn
        return updated, challenge_id

    result, replayed = operations.execute(
        endpoint=f"/v1/challenges/{challenge_id}/evidence",
        idempotency_key=idempotency_key,
        request_payload=_request_payload(request),
        response_model=ChallengeOperationResponse,
        result_field="challenge",
        build_result=build,
        session_id=session_id,
    )
    _mark_replay(response, replayed)
    return result


def _normal_confirmation_failures(challenge: ChallengeRecord) -> list[ErrorDetail]:
    failures: list[ErrorDetail] = []
    total = sum(score.score for score in challenge.rubric_scores)
    if total < NORMAL_CONFIRM_MINIMUM_TOTAL:
        failures.append(ErrorDetail(
            location="challenge/rubric_scores/total",
            message=f"Rubric total {total}/40 is below the required {NORMAL_CONFIRM_MINIMUM_TOTAL}/40.",
        ))

    for criterion in NORMAL_CONFIRM_CRITICAL_CRITERIA:
        criterion_scores = [score.score for score in challenge.rubric_scores if score.criterion == criterion]
        if len(criterion_scores) != 1:
            failures.append(ErrorDetail(
                location=f"challenge/rubric_scores/{criterion}",
                message="Exactly one score is required for this critical rubric criterion.",
            ))
        elif criterion_scores[0] < NORMAL_CONFIRM_MINIMUM_CRITICAL_SCORE:
            failures.append(ErrorDetail(
                location=f"challenge/rubric_scores/{criterion}",
                message=(
                    f"Critical rubric score {criterion_scores[0]}/4 is below the required "
                    f"{NORMAL_CONFIRM_MINIMUM_CRITICAL_SCORE}/4."
                ),
            ))

    if challenge.missing_evidence:
        failures.append(ErrorDetail(
            location="challenge/missing_evidence",
            message=f"{len(challenge.missing_evidence)} major evidence gap(s) remain.",
        ))
    if challenge.unresolved_issues:
        failures.append(ErrorDetail(
            location="challenge/unresolved_issues",
            message=f"{len(challenge.unresolved_issues)} major unresolved issue(s) remain.",
        ))
    if not any(item.challenge_id == challenge.challenge_id and item.independently_reviewable for item in EVIDENCE.values()):
        failures.append(ErrorDetail(
            location="challenge/evidence",
            message="Normal confirmation requires at least one independently reviewable evidence item.",
        ))
    return failures


def _confirmed_challenge(
    current: ChallengeRecord,
    *,
    operation_id: str,
    turn: int,
    confirmation: str,
    reservation_note: str | None,
) -> ChallengeRecord:
    metadata_fields = {"schema_version", "created_at", "source_refs", "content_hash"}
    payload = current.model_dump(mode="json", exclude=metadata_fields, exclude_none=True)
    payload.update({
        "turn_id": deterministic_id("turn", f"{current.challenge_id}:{turn}"),
        "user_confirmation": confirmation,
        "status": "CONFIRMED",
    })
    if reservation_note is None:
        payload.pop("reservation_note", None)
    else:
        payload["reservation_note"] = reservation_note

    confirmation_ref = SourceRef(
        ref_id=deterministic_id("confirmation", operation_id),
        source_type="user_submitted",
        label="Explicit user challenge confirmation",
    )
    refs = (*current.source_refs, confirmation_ref)
    hash_input = {
        "schema_version": "v1",
        "source_refs": [ref.model_dump(mode="json", exclude_none=True) for ref in refs],
        **payload,
    }
    updated = ChallengeRecord(
        schema_version="v1",
        created_at=clock.now(),
        source_refs=refs,
        content_hash=content_hash(hash_input),
        **payload,
    )
    validate_payload(updated, "ChallengeRecord")
    return updated


@app.post("/v1/challenges/{challenge_id}/confirm", response_model=ChallengeOperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="confirmChallenge")
def confirm_challenge(challenge_id: str, request: ConfirmChallengeRequest, response: Response, idempotency_key: IdempotencyKey) -> ChallengeOperationResponse:
    _validate_request(request, "ConfirmChallengeRequest")
    session_id = _session_for_challenge(challenge_id)
    reservation = request.command == "CONFIRM_WITH_RESERVATIONS"

    def build(operation_id: str) -> tuple[ChallengeRecord, str]:
        current = CHALLENGES.get(challenge_id)
        if not current:
            raise ApiProblem(404, "not_found", "Challenge not found.")
        if reservation and current.status not in {"CHALLENGING", "EVIDENCE_REQUIRED"}:
            raise ApiProblem(409, "invalid_state_transition", "Reservation command is only legal before confirmation.")
        if not reservation and current.status != "CHALLENGING":
            raise ApiProblem(409, "invalid_state_transition", "Normal confirmation is only legal from CHALLENGING.")
        if not reservation:
            failures = _normal_confirmation_failures(current)
            if failures:
                raise ApiProblem(
                    409,
                    "confirmation_requirements_not_met",
                    "Normal confirmation requires a passing rubric and no major gaps; use CONFIRM_WITH_RESERVATIONS to confirm early.",
                    details=failures,
                )
        turn = CHALLENGE_TURNS[challenge_id] + 1
        updated = _confirmed_challenge(
            current,
            operation_id=operation_id,
            turn=turn,
            confirmation="confirmed_with_reservations" if reservation else "confirmed",
            reservation_note=request.reservation_note if reservation else None,
        )
        CHALLENGES[challenge_id] = updated
        CHALLENGE_TURNS[challenge_id] = turn
        return updated, challenge_id

    result, replayed = operations.execute(
        endpoint=f"/v1/challenges/{challenge_id}/confirm",
        idempotency_key=idempotency_key,
        request_payload=_request_payload(request),
        response_model=ChallengeOperationResponse,
        result_field="challenge",
        build_result=build,
        session_id=session_id,
    )
    _mark_replay(response, replayed)
    return result


@app.post("/v1/worldlines", response_model=WorldlineBundleOperationResponse, response_model_exclude_none=True, status_code=201, responses=ERROR_RESPONSES, operation_id="createWorldlines")
def create_worldlines(request: WorldlineRequest, response: Response, idempotency_key: IdempotencyKey) -> WorldlineBundleOperationResponse:
    _validate_request(request, "WorldlineRequest")
    challenge = CHALLENGES.get(request.challenge_id)
    if not challenge:
        raise ApiProblem(404, "not_found", "Challenge not found.")
    if challenge.status != "CONFIRMED":
        raise ApiProblem(409, "invalid_state_transition", "Worldlines require a confirmed challenge.")
    if request.horizon_end < clock.today():
        raise ApiProblem(422, "validation_error", "Worldline horizon must not precede the baseline date.")
    session_id = _session_for_challenge(request.challenge_id)

    def build(operation_id: str) -> tuple[BaseModel, str]:
        thesis = THESES.get(challenge.thesis_id)
        if not thesis:
            raise ApiProblem(409, "lifecycle_conflict", "Confirmed challenge has no retained thesis input.")
        bundle = worldline_generator.generate(WorldlineGenerationInput(
            operation_id=operation_id, challenge=challenge, thesis=thesis, horizon_end=request.horizon_end,
        ))
        if not isinstance(bundle, WorldlineBundle):
            raise ProviderFailure("invalid_provider_output", "Worldline provider returned an unknown structured result.", retryable=False)
        _validate_provider_worldline_bundle(
            bundle,
            challenge_id=request.challenge_id,
            expected_premise=thesis.normalized_premise,
            expected_baseline_date=clock.today(),
            expected_horizon_end=request.horizon_end,
        )
        for worldline in bundle.worldlines:
            WORLDLINES[worldline.worldline_id] = worldline
            WORLDLINE_CHALLENGES[worldline.worldline_id] = request.challenge_id
        return bundle, bundle.bundle_id

    result, replayed = operations.execute(
        endpoint="/v1/worldlines",
        idempotency_key=idempotency_key,
        request_payload=_request_payload(request),
        response_model=WorldlineBundleOperationResponse,
        result_field="bundle",
        build_result=build,
        session_id=session_id,
    )
    _mark_replay(response, replayed)
    return result


@app.get("/v1/worldlines/{worldline_id}", response_model=Worldline, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="getWorldline")
def get_worldline(worldline_id: str) -> Worldline:
    worldline = WORLDLINES.get(worldline_id)
    if not worldline:
        raise ApiProblem(404, "not_found", "Worldline not found.")
    return worldline


@app.post("/v1/probabilities/assess", response_model=ProbabilityOperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="assessProbability")
def assess_probability(request: ProbabilityRequest, response: Response, idempotency_key: IdempotencyKey) -> ProbabilityOperationResponse:
    _validate_request(request, "ProbabilityRequest")
    if request.worldline_id not in WORLDLINES:
        raise ApiProblem(404, "not_found", "Worldline not found.")
    session_id = None
    challenge_id = WORLDLINE_CHALLENGES.get(request.worldline_id)
    if challenge_id is not None:
        session_id = _session_for_challenge(challenge_id)

    def build(operation_id: str) -> tuple[BaseModel, str]:
        result = probability_assessor.assess(ProbabilityAssessmentInput(
            operation_id=operation_id, worldline=WORLDLINES[request.worldline_id],
        ))
        if not isinstance(result, ProbabilityAssessment):
            raise ProviderFailure("invalid_provider_output", "Probability provider returned an unknown structured result.", retryable=False)
        _validate_provider_probability_assessment(result, expected_worldline_id=request.worldline_id)
        return result, result.assessment_id

    result, replayed = operations.execute(
        endpoint="/v1/probabilities/assess",
        idempotency_key=idempotency_key,
        request_payload=_request_payload(request),
        response_model=ProbabilityOperationResponse,
        result_field="probability",
        build_result=build,
        session_id=session_id,
    )
    _mark_replay(response, replayed)
    return result


@app.get("/v1/assets/search", response_model=AssetSearchResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="searchAssets")
def search_assets(query: Annotated[str, Query(max_length=200)] = "") -> AssetSearchResponse:
    items = registry_search_assets(query)
    return AssetSearchResponse(schema_version="v1", items=items, total=len(items))


@app.get("/v1/assets/{asset_id}", response_model=ASSET_INDEX["ETF:SPY"].__class__, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="getAsset")
def get_asset(asset_id: str) -> BaseModel:
    asset = registry_get_asset(asset_id)
    if not asset:
        raise ApiProblem(404, "not_found", "Asset not found.")
    return asset


@app.post("/v1/market-data/manifests", response_model=MarketDataManifestOperationResponse, response_model_exclude_none=True, status_code=201, responses=ERROR_RESPONSES, operation_id="createMarketDataManifest")
def create_market_manifest(request: MarketDataManifestRequest, response: Response, idempotency_key: IdempotencyKey) -> MarketDataManifestOperationResponse:
    _validate_request(request, "MarketDataManifestRequest")
    unknown = [asset_id for asset_id in request.asset_ids if asset_id not in ASSET_INDEX]
    if unknown:
        raise ApiProblem(422, "validation_error", f"Unknown registry asset IDs: {', '.join(unknown)}")
    from_at = getattr(request, "from")
    if from_at > request.to:
        raise ApiProblem(422, "validation_error", "Market-data 'from' must not be after 'to'.")

    def build(operation_id: str) -> tuple[MarketDataManifest, str]:
        manifest = data_provider.manifest(
            operation_id=operation_id,
            asset_ids=tuple(request.asset_ids),
            field_set=tuple(request.field_set),
            from_at=from_at,
            to_at=request.to,
            assets=ASSET_INDEX,
        )
        # ``as_of`` remains the deterministic mock data timestamp, while
        # ``created_at`` tracks storage age for retention.
        manifest = manifest.model_copy(update={"created_at": lifecycle_clock.now()})
        validate_payload(manifest, "MarketDataManifest")
        MARKET_MANIFESTS[manifest.manifest_id] = manifest
        return manifest, manifest.manifest_id

    result, replayed = operations.execute(
        endpoint="/v1/market-data/manifests",
        idempotency_key=idempotency_key,
        request_payload=_request_payload(request),
        response_model=MarketDataManifestOperationResponse,
        result_field="manifest",
        build_result=build,
    )
    _mark_replay(response, replayed)
    return result


@app.post("/v1/market-data/shards", response_model=MarketDataShardOperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="getMarketDataShard")
def get_market_shard(request: MarketDataShardRequest, response: Response, idempotency_key: IdempotencyKey) -> MarketDataShardOperationResponse:
    _validate_request(request, "MarketDataShardRequest")
    manifest = MARKET_MANIFESTS.get(request.manifest_id)
    if not manifest:
        raise ApiProblem(404, "not_found", "Market-data manifest not found.")
    if request.shard_id not in {item.shard_id for item in manifest.shards}:
        raise ApiProblem(404, "not_found", "Market-data shard not found.")
    if manifest.freshness != "current":
        # The public MVP may serve historical data for display, but it must be
        # observable whenever a current-data decision is impossible.
        record_stale_data_denial(endpoint="/v1/market-data/shards")

    def build(operation_id: str) -> tuple[BaseModel, str]:
        shard = data_provider.shard(manifest, ASSET_INDEX)
        return shard, shard.shard_id

    result, replayed = operations.execute(
        endpoint="/v1/market-data/shards",
        idempotency_key=idempotency_key,
        request_payload=_request_payload(request),
        response_model=MarketDataShardOperationResponse,
        result_field="shard",
        build_result=build,
    )
    _mark_replay(response, replayed)
    return result


@app.get("/v1/factor-libraries/{version}/manifest", response_model=FactorLibraryManifest, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="getFactorLibraryManifest")
def get_factor_manifest(version: str) -> FactorLibraryManifest:
    try:
        return factor_provider.manifest(version)
    except KeyError as error:
        raise ApiProblem(404, "not_found", "Factor library version not found.") from error


@app.get("/v1/factor-libraries/{version}/shards", response_model=FactorLibraryShard, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="getFactorLibraryShard")
def get_factor_shard(version: str, shard_id: Annotated[str, Query(min_length=1)]) -> FactorLibraryShard:
    try:
        return factor_provider.shard(version, shard_id)
    except KeyError as error:
        raise ApiProblem(404, "not_found", "Factor library shard not found.") from error


@app.post("/v1/client-computations/{computation_id}/attest", response_model=ClientAttestationOperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="attestClientComputation")
def attest_client_computation(computation_id: str, request: ClientComputationAttestRequest, response: Response, idempotency_key: IdempotencyKey) -> ClientAttestationOperationResponse:
    _validate_request(request, "ClientComputationAttestRequest")
    if request.operation_id not in operations.operations:
        if operations.store.get_operation(request.operation_id) is None:
            raise ApiProblem(404, "not_found", "Referenced operation not found.")
    session_id = operations.session_ids().get(request.operation_id)

    def build(operation_id: str) -> tuple[BaseModel, str]:
        result = build_attestation(operation_id=operation_id, computation_id=computation_id, request=request, clock=clock)
        return result, computation_id

    result, replayed = operations.execute(
        endpoint=f"/v1/client-computations/{computation_id}/attest",
        idempotency_key=idempotency_key,
        request_payload=_request_payload(request),
        response_model=ClientAttestationOperationResponse,
        result_field="attestation",
        build_result=build,
        session_id=session_id,
    )
    _mark_replay(response, replayed)
    return result


@app.post("/v1/operations/{operation_id}/cancel", response_model=OperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="cancelOperation")
def cancel_operation(operation_id: str, request: CancelOperationRequest, response: Response, idempotency_key: IdempotencyKey) -> OperationResponse:
    _validate_request(request, "CancelOperationRequest")
    result, replayed = operations.cancel(operation_id, request.cancel_token, idempotency_key)
    _mark_replay(response, replayed)
    return result


@app.post("/v1/operations/{operation_id}/resume", response_model=OperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="resumeOperation")
def resume_operation(operation_id: str, request: ResumeOperationRequest, response: Response, idempotency_key: IdempotencyKey) -> OperationResponse:
    _validate_request(request, "ResumeOperationRequest")
    result, replayed = operations.resume(operation_id, request.resume_token, idempotency_key)
    _mark_replay(response, replayed)
    return result


@app.get(
    "/v1/operations/{operation_id}/events",
    response_class=StreamingResponse,
    responses={
        **ERROR_RESPONSES,
        200: {
            "description": "Replayable Server-Sent Events. IDs are operation_id:sequence.",
            "content": {"text/event-stream": {"schema": {"$ref": "#/components/schemas/SseEvent"}}},
        },
    },
    operation_id="streamOperationEvents",
)
def stream_operation_events(operation_id: str, last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None) -> StreamingResponse:
    # Validate the replay cursor before response headers are committed; exceptions
    # raised only from a streaming iterator would otherwise surface as HTTP 200.
    operations.events_after(operation_id, last_event_id)
    return StreamingResponse(
        operations.sse_events(operation_id, last_event_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/v1/methodology", response_model=MethodologyResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="getMethodology")
def methodology() -> MethodologyResponse:
    return MethodologyResponse(
        schema_version="v1",
        methodology_version="v1",
        disclosure="Research scenario and portfolio simulation only; not investment advice and no broker execution.",
        probability_display="interval_first",
        client_computation=True,
    )


def canonical_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    document = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
    document["openapi"] = "3.1.0"
    document["info"]["x-contract-sha256"] = sha256(
        __import__("json").dumps(CONTRACT_SCHEMA, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    component_schemas = deepcopy(CONTRACT_SCHEMA["$defs"])

    def rewrite_refs(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "$ref" and isinstance(child, str) and child.startswith("#/$defs/"):
                    value[key] = child.replace("#/$defs/", "#/components/schemas/", 1)
                else:
                    rewrite_refs(child)
        elif isinstance(value, list):
            for child in value:
                rewrite_refs(child)

    rewrite_refs(component_schemas)
    # OAuth is deliberately a narrow API-only contract and is not part of the
    # research payload schema authority.  Preserve its strict route models in
    # generated OpenAPI while keeping the canonical research schemas unchanged.
    for name in ("OAuthAuthorizationResponse", "OAuthProviderReadiness", "OAuthProvidersResponse", "OAuthStartRequest"):
        route_schema = document.get("components", {}).get("schemas", {}).get(name)
        if route_schema is not None:
            component_schemas[name] = route_schema
    document.setdefault("components", {})["schemas"] = component_schemas
    app.openapi_schema = document
    return document


app.openapi = canonical_openapi
