from __future__ import annotations

from contextlib import asynccontextmanager
from copy import copy, deepcopy
from collections.abc import Callable, Mapping
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
import inspect
import logging
import os
import secrets
from time import monotonic
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ValidationError
from starlette.middleware.cors import CORSMiddleware

from .contract_models import (
    CONTRACT_SCHEMA,
    OAuthAuthorizationResponse,
    OAuthProviderName,
    OAuthProvidersResponse,
    OAuthProviderReadiness,
    OAuthStartRequest,
    OAuthTicketExchangeRequest,
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
from .phase4_live import LivePhase4Adapter
from .phase3 import ChallengeDraft, ChallengeInput, ClientSearchEvidenceProvider, LlmProvider, ProviderFailure, SearchEvidenceBundle, build_llm_provider, build_search_evidence_provider
from .openai_compatible import OpenAICompatibleLlmProvider
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
from .storage import JsonDocumentRepository, StorageConfig, StorageConflictError, StorageMode, build_repository
from .research_topics import AttachSourcesInput, ResearchFullExport, ResearchInput, ResearchMetadataExport, ResearchTopic, ResearchTopics, TopicNotFound, TopicVersionConflict, VersionInput
from .research_calculations import CalculationError, CalculationRequest, NumericFact, RecalculateRequest, calculate, calculation_input_status, claim_calculation_differences
from .research_sources import ResearchSource, ResearchSources, SourceInput, SourceNotFound, SourcePermissionError, locate_user_quotation
from .research_analysis import SYSTEM_PROMPT as RESEARCH_SYSTEM_PROMPT, SELECTION_METHOD as RESEARCH_SELECTION_METHOD, AnalysisInterrupted, InvalidAnalysis, analyze
from .research_evidence import claim_amount_differences, evidence_differences, flag_publication_date_mismatches, suggest_percentage_evidence
from .research_verification import (REVIEW_METHOD, REVIEW_SYSTEM_PROMPT, InvalidReview,
                                    apply_support_review, prepare_support_review)
from .research_runs import ResearchRunRequest, ResearchRunResult
from .research_topics import ResearchContent
from .research_fetch import HTML_EXTRACTION_DISCLOSURE, FetchFailure, FetchSourceRequest, fetch_official_text
from .research_sec import SecConceptRequest, SecIdentityRequest, SecFailure, fetch_sec_concept, search_sec_identities, reserve_sec_request, validate_user_agent
from .research_uploads import PdfFailure, PdfUploadRequest, extract_pdf


oauth_trace_logger = logging.getLogger("foresightnine.oauth")
from .operation_store import build_operation_store
from .oauth_foundation import OAuthConfigurationError, OAuthExchangeTicketError, OAuthFoundation, OAuthTransactionError, SUPPORTED_PROVIDERS
from .oauth_identity import OAuthIdentity, OAuthSessionResponse
from .oauth_provider import OAuthProviderClient, OAuthProviderError
from .market_provider import MarketSnapshotRequest, build_snapshot_response
from .market_credentials import (
    AccountMarketDataSettings,
    MarketCredentialCipher,
    MarketCredentialError,
    MarketDataConfigResponse,
    MarketDataConfigUpdate,
    MarketProviderTestRequest,
    MarketProviderTestResponse,
    StoredMarketDataConfig,
)
from .model_credentials import (
    AccountModelSettings,
    ModelConfigResponse,
    ModelConfigTestResponse,
    ModelConfigUpdate,
    ModelCredentialCipher,
    ModelCredentialError,
    StoredModelConfig,
)
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
    issue_session as issue_local_session,
)
from .billing import (
    BillingError,
    BillingLedger,
    GooglePlaySubscriptionVerifier,
    GooglePlayVerifyRequest,
    PurchaseRecord,
    PurchaseTokenCipher,
    BillingState,
    UsageLedgerEntry,
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


def _oauth_client_secret(provider: str) -> str:
    foundation = oauth_foundation
    if foundation is None:
        raise OAuthProviderError("OAuth client secret is unavailable")
    return foundation.configuration.provider_config(provider).client_secret


oauth_provider_client = OAuthProviderClient(client_secret_for=_oauth_client_secret)

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
    JsonDocumentRepository[OAuthIdentity],
    JsonDocumentRepository[BillingState],
    JsonDocumentRepository[PurchaseRecord],
    JsonDocumentRepository[UsageLedgerEntry],
    JsonDocumentRepository[StoredMarketDataConfig],
    JsonDocumentRepository[StoredModelConfig],
    JsonDocumentRepository[ChallengeRecord],
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
        build_repository(config, "oauth_identities", value_loader=OAuthIdentity.model_validate),
        build_repository(config, "billing_states", value_loader=BillingState.model_validate),
        build_repository(config, "billing_purchases", value_loader=PurchaseRecord.model_validate),
        build_repository(config, "usage_ledger", value_loader=UsageLedgerEntry.model_validate),
        build_repository(config, "market_data_configs", value_loader=StoredMarketDataConfig.model_validate),
        build_repository(config, "model_configs", value_loader=StoredModelConfig.model_validate),
        build_repository(config, "challenge_operation_results", value_loader=ChallengeRecord.model_validate),
    )


storage_config = _storage_config()
RESEARCH_TOPICS = build_repository(storage_config, "research_topics", value_loader=ResearchTopic.model_validate)
research_topics = ResearchTopics(RESEARCH_TOPICS)
RESEARCH_SOURCES = build_repository(storage_config, "research_sources", value_loader=ResearchSource.model_validate)
research_sources = ResearchSources(RESEARCH_SOURCES)
RESEARCH_RUN_INPUTS = build_repository(storage_config, "research_run_inputs")
SOURCE_RATE_LIMITS = build_repository(storage_config, "source_rate_limits")
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
    OAUTH_IDENTITIES,
    BILLING_STATES,
    BILLING_PURCHASES,
    USAGE_LEDGER,
    MARKET_DATA_CONFIGS,
    MODEL_CONFIGS,
    CHALLENGE_OPERATION_RESULTS,
) = _build_entity_repositories(storage_config)
operations = OperationManager(lifecycle_clock, store=build_operation_store(storage_config))
OPERATION_SESSIONS: dict[str, str] = {}
CHALLENGE_OPERATION_IDS: dict[str, str] = {}
billing_ledger = BillingLedger(
    BILLING_STATES,
    BILLING_PURCHASES,
    USAGE_LEDGER,
    cipher=PurchaseTokenCipher.from_environment(),
)


def _release_terminal_reservation(operation_id: str, status: str) -> None:
    if status not in {"failed", "cancelled"}:
        return
    try:
        operation = operations.store.get_operation(operation_id)
        if operation and str(operation.get("endpoint", "")).startswith("/v1/research/topics/"):
            # A research call may already have reached the provider; leave
            # ambiguous reservations for reconciliation instead of reversing
            # a charge whose outcome is unknown.
            return
        billing_ledger.release(f"model:{operation_id}", now=lifecycle_clock.now(), reverse_settled=True)
    except Exception:
        # The durable reconciliation pass below retries terminal reservations;
        # an already committed operation transition must not be rolled back by
        # a transient billing-store failure.
        return


operations.set_terminal_observer(_release_terminal_reservation)
market_data_settings = AccountMarketDataSettings(
    MARKET_DATA_CONFIGS,
    MarketCredentialCipher.from_environment(),
)
model_settings = AccountModelSettings(
    MODEL_CONFIGS,
    ModelCredentialCipher.from_environment(),
)


def _store_challenge_operation_result(operation_id: str, challenge: ChallengeRecord) -> None:
    """Persist the immutable challenge version named by one terminal receipt."""
    CHALLENGE_OPERATION_RESULTS[operation_id] = challenge


def _commit_challenge_mutation(
    challenge_id: str,
    *,
    expected: ChallengeRecord,
    expected_turn: int,
    operation: Callable[[], None],
) -> None:
    """Commit one aggregate mutation only if its source snapshot is current.

    Provider work intentionally runs outside this lock. The short terminal
    section is serialized per challenge in memory and by a PostgreSQL
    transaction advisory lock in deployed runtimes. A loser fails closed
    rather than overwriting a sibling message, evidence, or confirmation.
    """
    def guarded() -> None:
        current = CHALLENGES.get(challenge_id)
        current_turn = CHALLENGE_TURNS.get(challenge_id)
        if current is None or current.content_hash != expected.content_hash or current_turn != expected_turn:
            # StorageConflictError crosses the repository callback boundary
            # unchanged; translate it to the public API contract outside that
            # boundary so PostgreSQL does not wrap it as a storage outage.
            raise StorageConflictError("Challenge aggregate version conflict.")
        operation()

    try:
        CHALLENGES.with_advisory_lock(f"challenge-mutation:{challenge_id}", guarded)
    except StorageConflictError as error:
        raise ApiProblem(
            409,
            "lifecycle_conflict",
            "Challenge changed while this operation was running; reload the current challenge and retry.",
        ) from error


def _remove_challenge_operation_result(operation_id: str, challenge: ChallengeRecord) -> None:
    stored = CHALLENGE_OPERATION_RESULTS.get(operation_id)
    if stored is not None and stored.content_hash == challenge.content_hash:
        CHALLENGE_OPERATION_RESULTS.delete(operation_id)


def _assert_runtime_ready() -> None:
    """Probe every external dependency required by the selected runtime."""
    if storage_config.mode is StorageMode.POSTGRES:
        try:
            SESSIONS.check_ready()
            ACCOUNTS.check_ready()
            AUTH_SESSIONS.check_ready()
            OAUTH_IDENTITIES.check_ready()
            MARKET_DATA_CONFIGS.check_ready()
            MODEL_CONFIGS.check_ready()
            CHALLENGE_OPERATION_RESULTS.check_ready()
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
    RESEARCH_TOPICS.clear()
    RESEARCH_SOURCES.clear()
    RESEARCH_RUN_INPUTS.clear()
    SOURCE_RATE_LIMITS.clear()
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
    OAUTH_IDENTITIES.clear()
    BILLING_STATES.clear()
    BILLING_PURCHASES.clear()
    USAGE_LEDGER.clear()
    MARKET_DATA_CONFIGS.clear()
    MODEL_CONFIGS.clear()
    CHALLENGE_OPERATION_RESULTS.clear()
    OPERATION_SESSIONS.clear()
    CHALLENGE_OPERATION_IDS.clear()


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
    # Reconcile both freshly recovered leases and terminal operations left by a
    # prior process. Reservation IDs are deterministic and release is idempotent.
    for reservation_id, entry in tuple(USAGE_LEDGER.items_for_retention()):
        if not reservation_id.startswith("model:") or entry.status not in {"reserved", "settled"}:
            continue
        operation_id = reservation_id.removeprefix("model:")
        operation_document = operations.store.get_operation(operation_id)
        if operation_document is not None and operation_document.get("status") in {"failed", "cancelled"}:
            if str(operation_document.get("endpoint", "")).startswith("/v1/research/topics/"):
                continue
            billing_ledger.release(reservation_id, now=lifecycle_clock.now(), reverse_settled=True)
    active = operations.active_operation_ids()
    for operation_id in tuple(OPERATION_SESSIONS):
        if operation_id not in active:
            OPERATION_SESSIONS.pop(operation_id, None)
    for operation_id, _challenge in tuple(CHALLENGE_OPERATION_RESULTS.items_for_retention()):
        if operations.store.get_operation(operation_id) is None:
            CHALLENGE_OPERATION_RESULTS.delete(operation_id)
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


def _model_provider_for_request(request: Request) -> tuple[LlmProvider, bool]:
    """Resolve an account-owned provider once, without mutating the global one."""
    authorization = request.headers.get("Authorization")
    if authorization is None:
        request.state.model_account = None
        request.state.personal_model = False
        return challenge_provider, False
    try:
        account, _ = authenticate_local(ACCOUNTS, AUTH_SESSIONS, authorization, now=lifecycle_clock.now())
    except LocalAuthError as error:
        # An explicitly supplied but invalid/expired credential must never turn
        # into an anonymous platform-model request.
        raise _local_auth_problem(error) from error
    request.state.model_account = account
    try:
        settings = model_settings.runtime_settings(account.account_id)
    except ModelCredentialError as error:
        raise ApiProblem(error.status_code, error.code, error.message, retryable=error.status_code == 503, retry_after=30) from error
    if settings is None:
        request.state.personal_model = False
        return challenge_provider, False
    request.state.personal_model = True
    # This object and its decrypted key remain scoped to this request closure.
    return OpenAICompatibleLlmProvider(settings), True


def _reserve_model_call(request: Request, operation_id: str, *, session_id: str | None = None, token_budget: int = 4_500) -> None:
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
    # An authenticated account receives the same server-side token gate for
    # every provider call.  Anonymous sessions retain the legacy abuse-control
    # call quota and never create billing records.
    account = getattr(request.state, "model_account", None)
    if account is None:
        authorization = request.headers.get("Authorization")
        try:
            account, _ = authenticate_local(ACCOUNTS, AUTH_SESSIONS, authorization, now=lifecycle_clock.now())
        except LocalAuthError:
            account = None
    if account is not None and not getattr(request.state, "personal_model", False):
        reservation_id = f"model:{operation_id}"
        try:
            billing_ledger.reserve(account.account_id, reservation_id, token_budget, now=lifecycle_clock.now())
        except BillingError as error:
            raise ApiProblem(error.status_code, error.code, error.message, retryable=error.status_code in {502, 503}, retry_after=error.retry_after) from error
        request.state.billing_account_id = account.account_id
        request.state.billing_reservation_id = reservation_id
        request.state.billing_token_budget = token_budget


def _settle_model_call(request: Request, operation_id: str, total_tokens: int | None) -> None:
    account_id = getattr(request.state, "billing_account_id", None)
    reservation_id = getattr(request.state, "billing_reservation_id", f"model:{operation_id}")
    if not account_id:
        return
    try:
        actual_tokens = getattr(request.state, "billing_token_budget", 4_500) if total_tokens is None else max(0, int(total_tokens))
        billing_ledger.settle(reservation_id, actual_tokens, now=lifecycle_clock.now())
    except BillingError as error:
        raise ApiProblem(error.status_code, error.code, error.message, retryable=error.status_code in {502, 503}, retry_after=error.retry_after) from error


def _release_model_call(request: Request) -> None:
    reservation_id = getattr(request.state, "billing_reservation_id", None)
    if reservation_id:
        billing_ledger.release(reservation_id, now=lifecycle_clock.now(), reverse_settled=True)


def _release_model_on_failure(
    request: Request,
    build_result: Callable[[str], tuple[BaseModel, str]],
) -> Callable[[str], tuple[BaseModel, str]]:
    """Release an accepted async reservation when its worker fails."""
    def wrapped(operation_id: str) -> tuple[BaseModel, str]:
        try:
            return build_result(operation_id)
        except BaseException as error:
            # Record only bounded internal categories, never provider text,
            # account identifiers, prompt content, or credentials.
            logging.getLogger(__name__).warning(
                "model_operation_failed kind=%s code=%s",
                type(error).__name__, error.code if isinstance(error, ProviderFailure) else "internal",
            )
            _release_model_call(request)
            raise
    return wrapped


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
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
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


@app.middleware("http")
async def canonical_web_host(request: Request, call_next: Any) -> Response:
    """Keep the public apex as a path-preserving entry to the canonical app host."""
    host = request.headers.get("host", "").partition(":")[0].lower()
    if host in {"foresightnine.com", "www.foresightnine.com"}:
        target = urlunsplit(("https", "app.foresightnine.com", request.url.path, request.url.query, ""))
        return RedirectResponse(target, status_code=308, headers={"Cache-Control": "public, max-age=3600"})
    return await call_next(request)


@app.middleware("http")
async def model_config_test_content_type(request: Request, call_next: Any) -> Response:
    """Keep the no-body model probe compatible with the JSON-only boundary."""
    if (
        request.method == "POST"
        and request.url.path == "/v1/account/model-config/test"
        and "content-type" not in request.headers
    ):
        request.scope["headers"] = [
            *[(key, value) for key, value in request.scope["headers"] if key.lower() != b"content-type"],
            (b"content-type", b"application/json"),
        ]
    return await call_next(request)


def _metadata(payload: dict[str, Any], ref_id: str, label: str, source_type: str = "model_inference") -> dict[str, Any]:
    ref = SourceRef(ref_id=ref_id, source_type=source_type, label=label)
    refs = (ref,)
    hash_input = {"schema_version": "v1", "source_refs": [ref.model_dump(mode="json", exclude_none=True)], **payload}
    return {
        "schema_version": "v1",
        "created_at": lifecycle_clock.now(),
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
    retrieved_at = lifecycle_clock.now()
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
                raise ContractViolation("WorldlineBundle", ["every worldline baseline_date must match the requested API baseline"])
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
    provider: LlmProvider | None = None,
    personal_provider: bool = False,
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
    selected_provider = provider or challenge_provider
    source_id = "provider:account-openai-compatible" if personal_provider else f"provider:{selected_provider.name}"
    source_label = "Validated account-scoped OpenAI-compatible challenge draft" if personal_provider else "Validated adversarial challenge draft"
    result = ChallengeRecord(
        **_metadata(payload, source_id, source_label),
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
    # Provider reservations are created before the operation layer converts a
    # provider failure into ApiProblem. Releasing here is idempotent and avoids
    # failed requests holding account quota indefinitely.
    _release_model_call(request)
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
    _release_model_call(request)
    details = [ErrorDetail(location=error.contract, message=item) for item in error.errors]
    return _error(500, "contract_violation", "Generated response violated the canonical contract.", request, details=details)


@app.exception_handler(ProviderFailure)
async def provider_failure_handler(request: Request, error: ProviderFailure) -> JSONResponse:
    # Raw CLI/model diagnostics are intentionally not exposed or stored.
    _release_model_call(request)
    return _error(
        503 if error.retryable else 400,
        "internal_error" if error.retryable else "bad_request",
        "Provider request could not produce an accepted result.",
        request,
        retryable=error.retryable,
        retry_after=15 if error.retryable else None,
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, error: Exception) -> JSONResponse:
    _release_model_call(request)
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


@app.get("/metrics", include_in_schema=False)
def metrics(request: Request) -> Response:
    """Expose fixed redacted counters only when local scraping is enabled."""
    if os.getenv("FORESIGHTNINE_METRICS_ENABLED", "false").strip().lower() not in {"1", "true", "yes"}:
        return _error(404, "not_found", "Not found.", request)
    return PlainTextResponse(
        telemetry.prometheus_text(),
        media_type="text/plain; version=0.0.4",
        headers={"Cache-Control": "no-store"},
    )


ANDROID_CERT_FINGERPRINT_PATTERN = __import__("re").compile(r"^(?:[0-9a-f]{2}:){31}[0-9a-f]{2}$", __import__("re").IGNORECASE)


@app.get("/.well-known/assetlinks.json", include_in_schema=False, response_model=None)
def android_asset_links() -> Response:
    """Serve the Android App Link declaration from deployment-owned values.

    An empty list is served when no release fingerprint is configured so a
    SPA fallback cannot masquerade as an App Link declaration; production
    values are injected only through the process environment.
    """
    import re as _re

    package = os.getenv("FORESIGHTNINE_ANDROID_APP_LINK_PACKAGE", "").strip()
    fingerprints = [
        value.strip().upper()
        for value in os.getenv("FORESIGHTNINE_ANDROID_APP_LINK_SHA256_CERT_FINGERPRINTS", "").split(",")
        if value.strip() and _re.fullmatch(r"(?:[0-9a-fA-F]{2}:){31}[0-9a-fA-F]{2}", value.strip())
    ]
    statements = (
        [
            {
                "relation": ["delegate_permission/common.handle_all_urls"],
                "target": {
                    "namespace": "android_app",
                    "package_name": package,
                    "sha256_cert_fingerprints": fingerprints,
                },
            }
        ]
        if package and fingerprints
        else []
    )
    return Response(
        content=json.dumps(statements, indent=2) + "\n",
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.post("/snapshots", include_in_schema=False)
def market_snapshots(
    request: MarketSnapshotRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> JSONResponse:
    """Serve the bounded client market runtime without exposing a vendor API."""
    provider_order = None
    provider_credentials: Mapping[str, str] = {}
    if authorization:
        account = _authenticated_account(authorization)
        try:
            provider_order, provider_credentials = market_data_settings.runtime(account.account_id)
        except MarketCredentialError as error:
            raise ApiProblem(error.status_code, error.code, error.message, retryable=error.status_code == 503, retry_after=30) from error
    return JSONResponse(build_snapshot_response(
        request,
        provider_order=provider_order,
        provider_credentials=provider_credentials,
    ))


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


@app.post("/v1/auth/logout", include_in_schema=False)
def logout_authenticated_session(authorization: str | None = Header(default=None, alias="Authorization")) -> JSONResponse:
    """Revoke any server-issued session, including an OAuth session."""

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
        research_topics.delete_owner(account.account_id)
        research_sources.delete_owner(account.account_id)
        _delete_research_run_inputs(account.account_id)
        delete_local_account(ACCOUNTS, AUTH_SESSIONS, account, now=lifecycle_clock.now(), identities=OAUTH_IDENTITIES)
        market_data_settings.delete(account.account_id)
        model_settings.delete(account.account_id)
    except LocalAuthError as error:
        raise _local_auth_problem(error) from error
    return JSONResponse({"deleted": True}, headers={"Cache-Control": "no-store"})


@app.delete("/v1/auth/account", include_in_schema=False)
def delete_authenticated_account(authorization: str | None = Header(default=None, alias="Authorization")) -> JSONResponse:
    """Delete a local or OAuth account and revoke every related session."""

    try:
        account, _session = authenticate_local(ACCOUNTS, AUTH_SESSIONS, authorization, now=lifecycle_clock.now())
        research_topics.delete_owner(account.account_id)
        research_sources.delete_owner(account.account_id)
        _delete_research_run_inputs(account.account_id)
        delete_local_account(ACCOUNTS, AUTH_SESSIONS, account, now=lifecycle_clock.now(), identities=OAUTH_IDENTITIES)
        market_data_settings.delete(account.account_id)
        model_settings.delete(account.account_id)
    except LocalAuthError as error:
        raise _local_auth_problem(error) from error
    return JSONResponse({"deleted": True}, headers={"Cache-Control": "no-store"})


def _delete_research_run_inputs(owner_id: str) -> None:
    for key, record in list(RESEARCH_RUN_INPUTS.items()):
        if record.get("owner_id") == owner_id:
            RESEARCH_RUN_INPUTS.delete(key)


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


def _oauth_identity_key(provider: str, subject: str) -> str:
    return f"oauth_identity_{sha256(f'{provider}\0{subject}'.encode('utf-8')).hexdigest()}"


def _oauth_account(identity: Any) -> LocalAccount:
    """Find or create an account for a provider-verified identity."""

    identity_key = _oauth_identity_key(identity.provider, identity.subject)
    existing_identity = OAUTH_IDENTITIES.get(identity_key)
    now = lifecycle_clock.now()
    if existing_identity is not None:
        account = ACCOUNTS.get(existing_identity.account_id)
        if account is None or account.deleted_at is not None:
            raise OAuthProviderError("OAuth identity account is unavailable")
        if existing_identity.email != identity.email:
            OAUTH_IDENTITIES.put(
                identity_key,
                existing_identity.model_copy(update={"email": identity.email, "updated_at": now}),
            )
        return account

    password_account = next(
        (
            account
            for _, account in ACCOUNTS.items()
            if account.email == identity.email and account.deleted_at is None
        ),
        None
    )
    if password_account is not None:
        # The provider has already verified control of this email address, so
        # the OAuth identity joins the existing password account instead of
        # creating a duplicate.  The link is recorded as its own identity row
        # and both credentials continue to authenticate the same account.
        record = OAuthIdentity(
            identity_id=identity_key,
            provider=identity.provider,
            subject=identity.subject,
            account_id=password_account.account_id,
            email=identity.email,
            created_at=now,
            updated_at=now,
        )
        OAUTH_IDENTITIES.put(identity_key, record)
        return password_account

    account = LocalAccount(
        account_id=f"account_{uuid4().hex}",
        email=identity.email,
        # This is an unusable sentinel, not a password hash. OAuth accounts
        # can only authenticate through the bound provider identity.
        password_digest=f"oauth_identity${secrets.token_urlsafe(48)}",
        created_at=now,
    )
    record = OAuthIdentity(
        identity_id=identity_key,
        provider=identity.provider,
        subject=identity.subject,
        account_id=account.account_id,
        email=identity.email,
        created_at=now,
        updated_at=now,
    )
    try:
        ACCOUNTS.put(account.account_id, account)
        OAUTH_IDENTITIES.put(identity_key, record)
    except Exception:
        ACCOUNTS.delete(account.account_id)
        OAUTH_IDENTITIES.delete(identity_key)
        raise
    return account


@app.get("/oauth/{provider}/start", include_in_schema=False)
def browser_oauth_start(
    provider: OAuthProviderName,
    return_uri: Annotated[str, Query(min_length=1, max_length=2048)],
) -> RedirectResponse:
    """Start a browser OAuth redirect using the same server-held PKCE state."""

    foundation = oauth_foundation
    if foundation is None:
        raise ApiProblem(400, "bad_request", "OAuth authorization is not available.")
    try:
        authorization = foundation.create_transaction(provider, return_uri)
        transaction = foundation.transaction_for_state(authorization.state)
    except (OAuthConfigurationError, OAuthTransactionError) as error:
        raise ApiProblem(400, "bad_request", "OAuth authorization request is unavailable.") from error
    return RedirectResponse(
        OAuthProviderClient.authorization_url(transaction),
        status_code=307,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/oauth/callback", include_in_schema=False, response_model=None)
@app.get("/api/auth/callback/google", include_in_schema=False, response_model=None)
@app.get("/api/auth/callback/github", include_in_schema=False, response_model=None)
def browser_oauth_callback(
    code: Annotated[str | None, Query(max_length=4096)] = None,
    state: Annotated[str | None, Query(max_length=512)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> RedirectResponse | JSONResponse:
    """Complete a provider callback and redirect with a one-time opaque ticket."""

    def callback_redirect(return_uri: str, *, ticket: str | None = None) -> RedirectResponse:
        # The return URI came from the server-held transaction and was checked
        # against the allowlist before the provider redirect was constructed.
        # Remove reserved values if a deployment's allowlisted URI already has
        # them, then append only the fixed ticket/error result.
        parsed = urlsplit(return_uri)
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key not in {"ticket", "error"}]
        query.append(("ticket", ticket) if ticket is not None else ("error", "oauth_callback_failed"))
        location = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
        return RedirectResponse(
            location,
            status_code=303,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"},
        )

    foundation = oauth_foundation
    if foundation is None:
        raise ApiProblem(400, "bad_request", "OAuth callback is not available.")
    if state is None:
        raise ApiProblem(400, "bad_request", "OAuth callback is unavailable.")
    transaction = None
    stage = "consume_state"
    try:
        transaction = foundation.consume_state(state)
        if error is not None or code is None:
            oauth_trace_logger.info(
                "oauth_callback_provider_result provider=%s has_error=%s has_code=%s",
                transaction.provider,
                error is not None,
                code is not None,
            )
            return callback_redirect(transaction.return_uri)
        stage = "token_exchange"
        access_token = oauth_provider_client.exchange_code(transaction, code)
        stage = "identity"
        provider_identity = oauth_provider_client.identity(transaction.provider, access_token)
        stage = "provider_match"
        if provider_identity.provider != transaction.provider:
            raise OAuthProviderError("OAuth provider identity mismatch")
        stage = "account"
        account = _oauth_account(provider_identity)
        stage = "ticket"
        ticket = foundation.issue_exchange_ticket(
            provider=provider_identity.provider,
            account_id=account.account_id,
            return_uri=transaction.return_uri,
        )
    except (OAuthTransactionError, OAuthProviderError, OAuthExchangeTicketError) as exception:
        oauth_trace_logger.warning(
            "oauth_callback_failed provider=%s stage=%s error_type=%s",
            transaction.provider if transaction is not None else "unknown",
            stage,
            type(exception).__name__,
        )
        if transaction is not None:
            return callback_redirect(transaction.return_uri)
        raise ApiProblem(400, "bad_request", "OAuth callback is unavailable.") from exception
    oauth_trace_logger.info("oauth_callback_ticket_issued provider=%s", provider_identity.provider)
    return callback_redirect(transaction.return_uri, ticket=ticket)


@app.post(
    "/v1/auth/oauth/exchange",
    response_model=OAuthSessionResponse,
    response_model_exclude_none=True,
    responses=ERROR_RESPONSES,
    operation_id="exchangeOAuthTicket",
)
def exchange_oauth_ticket(request: OAuthTicketExchangeRequest, response: Response) -> OAuthSessionResponse:
    """Consume a callback ticket before issuing a local bearer session."""

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    foundation = oauth_foundation
    if foundation is None:
        raise ApiProblem(400, "bad_request", "OAuth exchange is unavailable.")
    try:
        ticket = foundation.consume_exchange_ticket(request.ticket)
        account = ACCOUNTS.get(ticket.account_id)
        if account is None or account.deleted_at is not None:
            raise OAuthExchangeTicketError("OAuth exchange ticket is unavailable")
        if not any(
            identity.account_id == ticket.account_id and identity.provider == ticket.provider
            for _, identity in OAUTH_IDENTITIES.items()
        ):
            raise OAuthExchangeTicketError("OAuth exchange ticket is unavailable")
        session = issue_local_session(
            AUTH_SESSIONS,
            account,
            now=lifecycle_clock.now(),
            auth_mode="oauth",
        )
        return OAuthSessionResponse(**{**session, "provider": ticket.provider})
    except OAuthExchangeTicketError as exception:
        raise ApiProblem(400, "bad_request", "OAuth exchange is unavailable.") from exception


def _authenticated_account(authorization: str | None) -> LocalAccount:
    try:
        account, _session = authenticate_local(
            ACCOUNTS,
            AUTH_SESSIONS,
            authorization,
            now=lifecycle_clock.now(),
        )
        return account
    except LocalAuthError as error:
        raise _local_auth_problem(error) from error


@app.post("/v2/research/topics", response_model=ResearchTopic, status_code=201, include_in_schema=False)
def create_research_topic(request: ResearchInput, authorization: str | None = Header(default=None)) -> ResearchTopic:
    account = _authenticated_account(authorization)
    try:
        for source_id in request.source_ids:
            research_sources.get(account.account_id, source_id)
    except SourceNotFound as error:
        raise ApiProblem(404, "not_found", "Research source not found.") from error
    return research_topics.create(account.account_id, request)


@app.get("/v2/research/topics", response_model=list[ResearchTopic], include_in_schema=False)
def list_research_topics(authorization: str | None = Header(default=None)) -> list[ResearchTopic]:
    account = _authenticated_account(authorization)
    return research_topics.list(account.account_id)


@app.get("/v2/research/topics/{topic_id}", response_model=ResearchTopic, include_in_schema=False)
def get_research_topic(topic_id: str, authorization: str | None = Header(default=None)) -> ResearchTopic:
    account = _authenticated_account(authorization)
    try:
        return research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error


@app.get("/v2/research/topics/{topic_id}/versions/{version_id}", include_in_schema=False)
def get_research_version(topic_id: str, version_id: str, authorization: str | None = Header(default=None)) -> dict:
    account = _authenticated_account(authorization)
    try:
        topic = research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error
    version = next((v for v in topic.versions if v.version_id == version_id), None)
    if version is None:
        raise ApiProblem(404, "not_found", "Research version not found.")
    return version.model_dump(mode="json")


@app.get("/v2/research/topics/{topic_id}/versions/{version_id}/sources", include_in_schema=False)
def list_research_version_sources(topic_id: str, version_id: str, authorization: str | None = Header(default=None)) -> list[dict]:
    account = _authenticated_account(authorization)
    try:
        topic = research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error
    version = next((item for item in topic.versions if item.version_id == version_id), None)
    if version is None:
        raise ApiProblem(404, "not_found", "Research version not found.")
    result = []
    for source_id in version.source_ids:
        try:
            source = research_sources.get(account.account_id, source_id)
        except SourceNotFound:
            result.append({"source_id": source_id, "status": "unavailable"})
            continue
        metadata = source.model_dump(mode="json", exclude={"text", "owner_id", "url", "original_pdf_base64"})
        if source.url:
            parts = urlsplit(str(source.url))
            metadata["url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        metadata["status"] = "available"
        metadata["verification"] = "user_declared_not_verified"
        if source.retrieval_method == "official_html":
            metadata["extraction_disclosure"] = HTML_EXTRACTION_DISCLOSURE
        result.append(metadata)
    return result


@app.get("/v2/research/topics/{topic_id}/versions/{version_id}/export", response_model=ResearchFullExport, include_in_schema=False)
def export_research_version(topic_id: str, version_id: str, authorization: str | None = Header(default=None)) -> ResearchFullExport:
    account = _authenticated_account(authorization)
    try:
        topic = research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error
    version = next((v for v in topic.versions if v.version_id == version_id), None)
    if version is None:
        raise ApiProblem(404, "not_found", "Research version not found.")
    source_ids = set(topic.request.source_ids) | set(version.source_ids)
    # All source references in stored content must be authorized. A reference
    # to a missing or another account's source fails closed.
    for entry in [*version.content.sources, *version.content.facts, *version.content.claims]:
        source_id = entry.get("source_id")
        if isinstance(source_id, str):
            source_ids.add(source_id)
        citations = entry.get("citations", [])
        if not isinstance(citations, list):
            raise ApiProblem(422, "invalid_request", "Research citations are invalid.")
        for citation in citations:
            if not isinstance(citation, dict) or not isinstance(citation.get("source_id"), str):
                raise ApiProblem(422, "invalid_request", "Research citations are invalid.")
            source_ids.add(citation["source_id"])
    sources = []
    try:
        for source_id in sorted(source_ids):
            source = research_sources.get(account.account_id, source_id)
            if not source.permissions.export:
                raise ApiProblem(403, "invalid_request", "A research source is not permitted for export.")
            metadata = source.model_dump(mode="json", exclude={"text", "owner_id", "url", "original_pdf_base64"})
            if source.url:
                parts = urlsplit(str(source.url))
                metadata["url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            metadata["verification"] = "user_declared_not_verified"
            if source.retrieval_method == "official_html":
                metadata["extraction_disclosure"] = HTML_EXTRACTION_DISCLOSURE
            sources.append(metadata)
    except SourceNotFound as error:
        raise ApiProblem(403, "invalid_request", "A research source is unavailable for export.") from error
    return ResearchFullExport(topic_id=topic_id, generated_at=lifecycle_clock.now(), request=topic.request,
                              version=version, source_provenance=sources)


@app.get("/v2/research/topics/{topic_id}/versions/{version_id}/export/metadata",
         response_model=ResearchMetadataExport, include_in_schema=False)
def export_research_metadata(topic_id: str, version_id: str,
                             authorization: str | None = Header(default=None)) -> ResearchMetadataExport:
    account = _authenticated_account(authorization)
    try:
        topic = research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research version not found.") from error
    version = next((item for item in topic.versions if item.version_id == version_id), None)
    if version is None:
        raise ApiProblem(404, "not_found", "Research version not found.")
    return ResearchMetadataExport(topic_id=topic.topic_id, version_id=version.version_id,
        parent_version_id=version.parent_version_id, version_created_at=version.created_at,
        generated_at=lifecycle_clock.now(), as_of=topic.request.as_of, review=version.review,
        created_by=version.created_by, tracked_source_count=len(set(topic.request.source_ids) | set(version.source_ids)),
        fact_count=len(version.content.facts), claim_count=len(version.content.claims),
        calculation_count=len(version.content.calculations), limitation_count=len(version.content.limitations))


@app.post("/v2/research/topics/{topic_id}/versions", response_model=ResearchTopic, status_code=201, include_in_schema=False)
def create_research_version(topic_id: str, request: VersionInput, authorization: str | None = Header(default=None)) -> ResearchTopic:
    account = _authenticated_account(authorization)
    try:
        topic = research_topics.get(account.account_id, topic_id)
        previous = topic.versions[-1]
        if previous.version_id != request.parent_version_id:
            raise TopicVersionConflict()
        # A user may change findings, facts, and calculations, but an edited
        # item must not masquerade as an untouched model extraction. The run
        # receipt and source inventory are server-produced historical records.
        content = request.content.model_copy(deep=True)
        existing_citations = {json.dumps(citation, sort_keys=True, ensure_ascii=False) for claim in previous.content.claims
                              for citation in (claim.get("citations") if isinstance(claim.get("citations"), list) else [])
                              if isinstance(citation, dict)}
        allowed_sources = set(previous.source_ids)
        checked_citations: dict[tuple[str, str], dict] = {}
        citation_sources: dict[str, ResearchSource] = {}
        for claim in content.claims:
            if claim in previous.content.claims:
                continue
            text = claim.get("text")
            citations = claim.get("citations", [])
            role = claim.get("role", "support")
            if (not isinstance(text, str) or not text.strip() or len(text) > 3000 or
                    not isinstance(role, str) or role not in {"support", "counterargument"} or
                    not isinstance(citations, list) or len(citations) > 10 or
                    any(not isinstance(citation, dict) or not isinstance(citation.get("source_id"), str) or
                        not citation["source_id"].strip() or len(citation["source_id"]) > 100 or
                        ("quotation" in citation and (not isinstance(citation["quotation"], str) or len(citation["quotation"]) > 1000))
                        for citation in citations)):
                raise ApiProblem(422, "invalid_request", "Research findings or their citations are invalid.")
            normalized = []
            for citation in citations:
                source_id = citation["source_id"]
                if source_id not in allowed_sources:
                    raise ApiProblem(422, "invalid_request", "Citation must match an attached, displayable, time-eligible source.")
                try:
                    if source_id not in citation_sources:
                        citation_sources[source_id] = research_sources.get(account.account_id, source_id)
                    source = citation_sources[source_id]
                    if (source.is_simulated or not source.permissions.display or topic.request.as_of is not None and
                            (source.published_at is None or source.published_at > topic.request.as_of or
                             (source.revised_at is not None and source.revised_at > topic.request.as_of))):
                        raise ValueError("Source availability at the research cutoff is not established.")
                except (SourceNotFound, ValueError) as error:
                    raise ApiProblem(422, "invalid_request", "Citation must match an attached, displayable, time-eligible source.") from error
                if json.dumps(citation, sort_keys=True, ensure_ascii=False) in existing_citations:
                    # Preserve existing citation locations after rechecking access and cutoff.
                    normalized.append(citation)
                    continue
                quotation = citation.get("quotation")
                if (not isinstance(quotation, str) or not quotation.strip()
                        or len(checked_citations) >= 200 and (source_id, quotation) not in checked_citations):
                    raise ApiProblem(422, "invalid_request", "Citation must match an attached, displayable, time-eligible source.")
                key = (source_id, quotation)
                if key not in checked_citations:
                    try:
                        checked_citations[key] = locate_user_quotation(source, quotation)
                    except ValueError as error:
                        raise ApiProblem(422, "invalid_request", "Citation must match an attached, displayable, time-eligible source.") from error
                normalized.append(deepcopy(checked_citations[key]))
            claim["citations"] = normalized
        fact_id_counts = Counter(item["fact_id"].strip() for item in content.facts if isinstance(item.get("fact_id"), str))
        for fact in content.facts:
            if fact in previous.content.facts:
                continue
            try:
                numeric = NumericFact.model_validate({key: fact[key] for key in
                    ("fact_id", "value", "unit", "currency", "period") if key in fact})
            except ValidationError as error:
                raise ApiProblem(422, "invalid_request", "Research fact value, unit, currency, or period is invalid.") from error
            if fact_id_counts[numeric.fact_id] != 1:
                raise ApiProblem(422, "invalid_request", "Research fact identifiers must be unique.")
            if ("label" in fact and (not isinstance(fact["label"], str) or not fact["label"].strip() or len(fact["label"]) > 300) or
                    "source_id" in fact and fact["source_id"] is not None and (not isinstance(fact["source_id"], str) or not fact["source_id"].strip() or len(fact["source_id"]) > 100) or
                    "quotation" in fact and fact["quotation"] is not None and (not isinstance(fact["quotation"], str) or len(fact["quotation"]) > 1000)):
                raise ApiProblem(422, "invalid_request", "Research fact provenance is invalid.")
            if fact.get("source_id") is not None:
                source_id = fact["source_id"]
                if source_id not in allowed_sources:
                    raise ApiProblem(422, "invalid_request", "Research fact must use an attached real source.")
                try:
                    if source_id not in citation_sources:
                        citation_sources[source_id] = research_sources.get(account.account_id, source_id)
                    if citation_sources[source_id].is_simulated:
                        raise ValueError("Simulated source cannot be used as real evidence.")
                except (SourceNotFound, ValueError) as error:
                    raise ApiProblem(422, "invalid_request", "Research fact must use an attached real source.") from error
            fact.update(numeric.model_dump(mode="json"))
        for field in ("claims", "facts", "calculations"):
            originals = getattr(previous.content, field)
            entries = getattr(content, field)
            for entry in entries:
                if entry not in originals:
                    entry["authorship"] = "user"
                    entry["verification"] = "user_edited_not_independently_verified"
                    if field == "claims":
                        entry.pop("support_review", None)
                        entry["support_status"] = "not_independently_verified" if entry.get("citations") else "unsupported"
                        if isinstance(entry.get("citations"), list):
                            for citation in entry["citations"]:
                                if isinstance(citation, dict):
                                    citation["support_status"] = "not_independently_verified"
                    if field == "facts":
                        entry.pop("numeric_normalization", None)
                        entry.pop("range_parent_id", None)
        for entry in content.calculations:
            entry["input_status"] = calculation_input_status(entry, content.facts)
        content.claims = suggest_percentage_evidence(content.claims, content.facts)
        for claim in content.claims:
            citations = claim.get("citations")
            if not isinstance(citations, list):
                continue
            for citation in citations:
                source_id = citation.get("source_id") if isinstance(citation, dict) else None
                if isinstance(source_id, str) and source_id in allowed_sources and source_id not in citation_sources:
                    try:
                        citation_sources[source_id] = research_sources.get(account.account_id, source_id)
                    except SourceNotFound:
                        pass
        content.claims = flag_publication_date_mismatches(content.claims, list(citation_sources.values()))
        content.evidence_differences = evidence_differences(content.facts)
        content.calculation_differences = claim_calculation_differences(content.claims, content.calculations, content.facts)
        content.amount_differences = claim_amount_differences(content.claims, content.facts)
        retained = previous.content.model_copy(deep=True)
        content.run = retained.run
        content.sources = retained.sources
        content.source_candidates = retained.source_candidates
        return research_topics.add_version(account.account_id, topic_id, request.model_copy(update={"content": content}))
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error
    except TopicVersionConflict as error:
        raise ApiProblem(409, "lifecycle_conflict", "Research topic has changed; reload before saving.") from error


@app.post("/v2/research/topics/{topic_id}/sources", response_model=ResearchTopic, status_code=201, include_in_schema=False)
def attach_research_sources(topic_id: str, request: AttachSourcesInput, authorization: str | None = Header(default=None)) -> ResearchTopic:
    account = _authenticated_account(authorization)
    try:
        topic = research_topics.get(account.account_id, topic_id)
        for source_id in request.source_ids:
            research_sources.get(account.account_id, source_id)
        previous = topic.versions[-1]
        return research_topics.add_version(account.account_id, topic_id, VersionInput(
            parent_version_id=request.parent_version_id, content=previous.content,
            review="draft", note=previous.note), source_ids=request.source_ids)
    except (TopicNotFound, SourceNotFound) as error:
        raise ApiProblem(404, "not_found", "Research topic or source not found.") from error
    except TopicVersionConflict as error:
        raise ApiProblem(409, "lifecycle_conflict", "Research topic has changed; reload before saving.") from error
    except ValueError as error:
        raise ApiProblem(422, "invalid_request", str(error)) from error


@app.post("/v2/research/topics/{topic_id}/calculations/recalculate", response_model=ResearchTopic,
          status_code=201, include_in_schema=False)
def recalculate_research(topic_id: str, request: RecalculateRequest,
                         authorization: str | None = Header(default=None)) -> ResearchTopic:
    account = _authenticated_account(authorization)
    try:
        topic = research_topics.get(account.account_id, topic_id)
        previous = topic.versions[-1]
        if previous.version_id != request.parent_version_id:
            raise TopicVersionConflict()
        if request.calculation_index >= len(previous.content.calculations):
            raise ApiProblem(422, "invalid_request", "Calculation does not exist in this version.")
        old = previous.content.calculations[request.calculation_index]
        inputs = old.get("inputs")
        if (old.get("formula_version") not in {"1", "2"} or not isinstance(inputs, list) or len(inputs) != 2 or
                any(not isinstance(item, dict) or not isinstance(item.get("fact_id"), str) for item in inputs)):
            raise ApiProblem(422, "invalid_request", "Calculation inputs or formula version are unavailable.")
        facts = []
        for item in inputs:
            candidates = [fact for fact in previous.content.facts if fact.get("fact_id") == item["fact_id"]]
            if len(candidates) != 1:
                raise ApiProblem(422, "invalid_request", "Calculation input facts are missing or ambiguous.")
            facts.append({key: candidates[0][key] for key in ("fact_id", "value", "unit", "currency", "period")
                          if key in candidates[0]})
        spec = CalculationRequest.model_validate({"formula": old.get("formula_id"), "first": facts[0],
            "second": facts[1], "decimal_places": old.get("decimal_places", 2)})
        result = calculate(spec)
        result.update({"input_status": "matched", "authorship": "calculation_engine"})
        content = previous.content.model_copy(deep=True)
        content.calculations[request.calculation_index] = result
        content.calculation_differences = claim_calculation_differences(content.claims, content.calculations, content.facts)
        return research_topics.add_version(account.account_id, topic_id, VersionInput(
            parent_version_id=previous.version_id, content=content, note=previous.note))
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error
    except TopicVersionConflict as error:
        raise ApiProblem(409, "lifecycle_conflict", "Research topic has changed; reload before calculating.") from error
    except (ValidationError, CalculationError) as error:
        raise ApiProblem(422, "invalid_request", "Calculation cannot be performed with the current facts.") from error


@app.post("/v2/research/calculations", include_in_schema=False)
def calculate_research(request: CalculationRequest, authorization: str | None = Header(default=None)) -> dict:
    _authenticated_account(authorization)
    try:
        return calculate(request)
    except CalculationError as error:
        raise ApiProblem(422, "invalid_request", str(error)) from error


def _visible_research_source(source: ResearchSource) -> dict:
    result = source.model_dump(mode="json", exclude={"original_pdf_base64"})
    if not source.permissions.display:
        result.pop("text", None)
    # User-supplied provenance and permissions are declarations, not proof.
    result["verification"] = "user_declared_not_verified"
    if source.retrieval_method == "official_html":
        result["extraction_disclosure"] = HTML_EXTRACTION_DISCLOSURE
    return result


@app.post("/v2/research/sources", status_code=201, include_in_schema=False)
def create_research_source(request: SourceInput, authorization: str | None = Header(default=None)) -> dict:
    account = _authenticated_account(authorization)
    try:
        return _visible_research_source(research_sources.create(account.account_id, request))
    except SourcePermissionError as error:
        raise ApiProblem(422, "invalid_request", str(error)) from error


@app.post("/v2/research/sources/pdf", status_code=201, include_in_schema=False)
def upload_research_pdf(request: PdfUploadRequest, authorization: str | None = Header(default=None)) -> dict:
    account = _authenticated_account(authorization)
    try:
        document = extract_pdf(request)
        source = research_sources.create(account.account_id, SourceInput(title=request.title, text=document.text,
            published_at=request.published_at, event_at=request.event_at, revised_at=request.revised_at,
            is_simulated=request.is_simulated,
            permissions=request.permissions), retrieval_method="user_pdf",
            response_sha256=document.original_sha256, response_content_type="application/pdf",
            original_pdf_base64=document.original_base64, pdf_text_order=request.text_order)
        metadata = _visible_research_source(source)
        metadata.pop("text", None)
        return metadata
    except PdfFailure as error:
        if error.code in {"pdf_extractor_unavailable", "pdf_timeout", "pdf_busy"}:
            raise ApiProblem(503, "internal_error", "PDF extraction is unavailable or timed out.", retryable=True) from error
        raise ApiProblem(422, "invalid_request", "PDF is invalid, too large, encrypted, or has no extractable text.") from error


@app.get("/v2/research/sources/{source_id}/original", include_in_schema=False)
def get_research_source_original(source_id: str, authorization: str | None = Header(default=None)) -> Response:
    account = _authenticated_account(authorization)
    try:
        source = research_sources.get(account.account_id, source_id)
    except SourceNotFound as error:
        raise ApiProblem(404, "not_found", "Research source not found.") from error
    if not source.original_pdf_base64 or not source.permissions.display or not source.permissions.export:
        raise ApiProblem(403, "invalid_request", "The original document is not available for download.")
    import base64
    return Response(content=base64.b64decode(source.original_pdf_base64), media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="source.pdf"', "Cache-Control": "no-store"})


@app.get("/v2/research/sources/{source_id}/context", include_in_schema=False)
def get_research_source_context(source_id: str, start: Annotated[int, Query(ge=0, le=2_000_000)],
                                end: Annotated[int, Query(ge=1, le=2_000_000)],
                                authorization: str | None = Header(default=None)) -> dict:
    account = _authenticated_account(authorization)
    try:
        source = research_sources.get(account.account_id, source_id)
    except SourceNotFound as error:
        raise ApiProblem(404, "not_found", "Research source not found.") from error
    if not source.permissions.display:
        raise ApiProblem(403, "invalid_request", "Source text is not permitted for display.")
    if end <= start or end > len(source.text) or end - start > 1000:
        raise ApiProblem(422, "invalid_request", "Source text location is invalid.")
    left, right = max(0, start - 300), min(len(source.text), end + 300)
    if source.retrieval_method == "user_pdf":
        # Context stays on the relevant page(s), while retaining an exact
        # quote that itself crosses a page boundary.
        left = max(left, source.text.rfind("\f", 0, start) + 1)
        next_page = source.text.find("\f", end)
        if next_page >= 0:
            right = min(right, next_page)
    return {"source_id": source.source_id, "title": source.title, "content_sha256": source.content_sha256,
            "start": start, "end": end, "context_start": left, "context_end": right,
            "before": source.text[left:start], "selected": source.text[start:end], "after": source.text[end:right],
            "page_start": source.text[:start].count("\f") + 1 if source.retrieval_method == "user_pdf" else None,
            "page_end": source.text[:end].count("\f") + 1 if source.retrieval_method == "user_pdf" else None}


@app.post("/v2/research/sources/fetch", status_code=201, include_in_schema=False)
def fetch_research_source(request: FetchSourceRequest, http_request: Request) -> dict:
    account = _authenticated_account(http_request.headers.get("Authorization"))
    if not request.permissions.store:
        raise ApiProblem(422, "invalid_request", "Source storage permission is required.")
    _reserve_search_call(http_request)
    try:
        fetched = fetch_official_text(request)
        source = research_sources.create(account.account_id, SourceInput(title=request.title,
            text=fetched.text, url=request.url, kind="official", permissions=request.permissions,
            published_at=request.published_at, event_at=request.event_at, revised_at=request.revised_at,
            is_simulated=request.is_simulated),
            retrieval_method="official_html", response_sha256=fetched.response_sha256,
            response_content_type=fetched.content_type)
        return _visible_research_source(source)
    except FetchFailure as error:
        if error.code == "source_rate_limited":
            raise ApiProblem(429, "rate_limited", "Source temporarily rate limited.", retryable=True) from error
        if error.code in {"source_timeout", "source_unavailable"}:
            raise ApiProblem(503, "internal_error", "Source unavailable or timed out.", retryable=True) from error
        raise ApiProblem(422, "invalid_request", "Source could not be used in the requested format.") from error


@app.post("/v2/research/identities/sec-search", include_in_schema=False)
def search_research_sec_identities(request: SecIdentityRequest, http_request: Request) -> dict:
    _authenticated_account(http_request.headers.get("Authorization"))
    try:
        agent = validate_user_agent(os.environ.get("FORESIGHTNINE_SEC_USER_AGENT") or os.environ.get("SEC_USER_AGENT"))
        _reserve_search_call(http_request)
        if not reserve_sec_request(SOURCE_RATE_LIMITS):
            raise ApiProblem(429, "rate_limited", "SEC request limit reached; retry later.", retryable=True, retry_after=1)
        return search_sec_identities(request, agent)
    except SecFailure as error:
        if error.code == "sec_contact_unconfigured":
            raise ApiProblem(503, "internal_error", "SEC source contact is not configured.", retryable=False) from error
        if error.code == "source_rate_limited":
            raise ApiProblem(429, "rate_limited", "SEC source rate limited.", retryable=True, retry_after=1) from error
        raise ApiProblem(503, "internal_error", "SEC identity search is unavailable.", retryable=True) from error


@app.post("/v2/research/sources/sec-concept", status_code=201, include_in_schema=False)
def fetch_research_sec_concept(request: SecConceptRequest, http_request: Request) -> dict:
    account = _authenticated_account(http_request.headers.get("Authorization"))
    try:
        agent = validate_user_agent(os.environ.get("FORESIGHTNINE_SEC_USER_AGENT") or os.environ.get("SEC_USER_AGENT"))
        if not request.permissions.store:
            raise SecFailure("source_permission_unknown")
        _reserve_search_call(http_request)
        if not reserve_sec_request(SOURCE_RATE_LIMITS):
            raise ApiProblem(429, "rate_limited", "SEC request limit reached; retry later.", retryable=True, retry_after=1)
        data = fetch_sec_concept(request, agent)
        source = research_sources.create(account.account_id, SourceInput(title=data.title, text=data.text,
            url=data.url, kind="official", permissions=request.permissions), retrieval_method="sec_companyconcept",
            response_sha256=data.response_sha256, response_content_type="application/json")
        return _visible_research_source(source)
    except SecFailure as error:
        if error.code == "sec_contact_unconfigured":
            raise ApiProblem(503, "internal_error", "SEC source contact is not configured.", retryable=False) from error
        if error.code == "source_rate_limited":
            raise ApiProblem(429, "rate_limited", "SEC source rate limited.", retryable=True, retry_after=1) from error
        if error.code in {"source_timeout", "source_unavailable"}:
            raise ApiProblem(503, "internal_error", "SEC source unavailable or timed out.", retryable=True) from error
        raise ApiProblem(422, "invalid_request", "SEC source data could not be used.") from error


@app.get("/v2/research/sources/{source_id}", include_in_schema=False)
def get_research_source(source_id: str, authorization: str | None = Header(default=None)) -> dict:
    account = _authenticated_account(authorization)
    try:
        return _visible_research_source(research_sources.get(account.account_id, source_id))
    except SourceNotFound as error:
        raise ApiProblem(404, "not_found", "Research source not found.") from error


@app.post("/v1/research/topics/{topic_id}/runs", response_model=OperationResponse, response_model_exclude_none=True, status_code=202, include_in_schema=False)
@app.post("/v2/research/topics/{topic_id}/runs", response_model=OperationResponse, response_model_exclude_none=True, status_code=202, include_in_schema=False)
def start_research_run(topic_id: str, request: ResearchRunRequest, response: Response,
                       idempotency_key: IdempotencyKey, http_request: Request) -> OperationResponse:
    account = _authenticated_account(http_request.headers.get("Authorization"))
    try:
        topic = research_topics.get(account.account_id, topic_id)
        source_ids = list(dict.fromkeys([*topic.request.source_ids, *topic.versions[-1].source_ids, *request.source_ids]))
        if len(source_ids) > 100:
            raise ApiProblem(422, "invalid_request", "A research version can include at most 100 sources.")
        sources = [research_sources.get(account.account_id, source_id) for source_id in source_ids]
    except (TopicNotFound, SourceNotFound) as error:
        raise ApiProblem(404, "not_found", "Research topic or source not found.") from error
    provider, _personal = _model_provider_for_request(http_request)
    generate_structured = getattr(provider, "generate_structured", None)
    checkpoint_parent: str | None = None

    def source_summaries(analyzed_ids: list[str]) -> list[dict[str, Any]]:
        return [{"source_id": s.source_id, "title": s.title,
                 "url": urlunsplit((urlsplit(str(s.url)).scheme, urlsplit(str(s.url)).netloc,
                     urlsplit(str(s.url)).path, "", "")) if s.url else None,
                 "event_at": s.event_at.isoformat() if s.event_at else None,
                 "published_at": s.published_at.isoformat() if s.published_at else None,
                 "obtained_at": s.obtained_at.isoformat(),
                 "revised_at": s.revised_at.isoformat() if s.revised_at else None,
                 "content_sha256": s.content_sha256, "is_simulated": s.is_simulated,
                 "pdf_text_order": s.pdf_text_order,
                 "verification": "user_declared_not_verified"}
                for s in sources if s.source_id in analyzed_ids]

    def build(operation_id: str) -> tuple[ResearchRunResult, str]:
        nonlocal checkpoint_parent
        started = monotonic()
        deadline = started + request.max_elapsed_seconds
        calls = 0
        search_calls = 0
        discovered: list[dict[str, Any]] = []
        discovery_notes: list[str] = []
        search_status = "not_requested"
        usage: int | None = None
        unavailable_reason: str | None = None
        _raise_if_operation_cancelled(operation_id)
        if research_topics.get(account.account_id, topic_id).versions[-1].version_id != request.parent_version_id:
            raise ApiProblem(409, "lifecycle_conflict", "Research topic has changed; reload before running.")

        # Search results are leads only: they never become evidence or model
        # input until the user separately attaches appropriately licensed text.
        if not sources and topic.request.question and request.max_search_calls:
            search = getattr(evidence_provider, "search", None)
            if request.max_cost_usd is not None:
                search_status = "cost_unknown"
                discovery_notes.append("Source discovery was skipped because search pricing is unknown under the requested cost cap.")
            elif not getattr(evidence_provider, "supports_project_search", False) or not callable(search):
                search_status = "unavailable"
                discovery_notes.append("Source discovery is unavailable; attach source material manually.")
            else:
                try:
                    try:
                        parameters = inspect.signature(search).parameters
                    except (TypeError, ValueError) as error:
                        raise ProviderFailure("search_budget_unsupported", "Search provider cannot enforce the time budget.", retryable=False) from error
                    if "deadline" not in parameters or "cancel_event" not in parameters:
                        raise ProviderFailure("search_budget_unsupported", "Search provider cannot enforce the time budget.", retryable=False)
                    if monotonic() >= deadline:
                        raise ProviderFailure("search_timeout", "Search time budget is exhausted.", retryable=False)
                    _reserve_search_call(http_request)
                    search_calls = 1
                    raw_candidates = search(topic.request.question.strip()[:70], limit=5,
                                            deadline=deadline, cancel_event=operations.cancel_event(operation_id))
                    if not isinstance(raw_candidates, (list, tuple)):
                        raise ProviderFailure("search_response_invalid", "Search provider returned invalid results.", retryable=False)
                    provider_name = _provider_name(evidence_provider, "search")
                    invalid_candidates = 0
                    for raw in raw_candidates[:5]:
                        try:
                            candidate = _candidate_response(raw, provider_name)
                            ClientSearchEvidenceProvider().validate_url(str(candidate.url))
                        except (ProviderFailure, ValidationError, ValueError):
                            # Search is an untrusted list: reject just the bad
                            # lead without discarding independently valid ones.
                            invalid_candidates += 1
                            continue
                        # A date is not a publication timestamp. In historical
                        # mode omit unknown or same-day/future leads.
                        if topic.request.as_of is not None and (candidate.published_at is None or
                                candidate.published_at.date() >= topic.request.as_of.astimezone(timezone.utc).date()):
                            continue
                        parts = urlsplit(str(candidate.url))
                        discovered.append({"title": candidate.title,
                            "url": urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
                            "published_at": candidate.published_at.isoformat() if candidate.published_at else None,
                            "retrieved_at": candidate.retrieved_at.isoformat(),
                            "search_result_hash": candidate.search_result_hash,
                            "search_provider": provider_name, "status": "unverified"})
                    search_status = ("partial" if discovered else "failed") if invalid_candidates else "completed"
                    if invalid_candidates:
                        discovery_notes.append("Some search candidates could not be validated and were omitted." if discovered
                                               else "No eligible search candidates remained after validation.")
                    discovery_notes.append("Search candidates are unverified leads, not evidence. Attach and authorize material before analysis.")
                    if topic.request.as_of is not None:
                        discovery_notes.append("Historical search publication dates are not independently verified.")
                except (ProviderFailure, ApiProblem):
                    search_status = "failed"
                    discovery_notes.append("Source discovery failed or was rate limited; attach source material manually.")
        _raise_if_operation_cancelled(operation_id)

        attempted_prompt: dict[str, Any] = {}
        analysis_error_detail: str | None = None

        def generate(prompt: str) -> tuple[dict[str, Any], int | None]:
            nonlocal calls, usage
            if request.max_model_calls == 0:
                raise ProviderFailure("provider_budget_exhausted", "Model call budget is zero.", retryable=False)
            # A currency cap cannot be guaranteed without a provider quote and
            # usage pricing. Fail closed rather than silently overspending it.
            if request.max_cost_usd is not None:
                raise ProviderFailure("provider_budget_unknown", "Model pricing is not configured for the requested cost cap.", retryable=False)
            if not callable(generate_structured):
                raise ProviderFailure("provider_unavailable", "A structured research model is not configured.", retryable=False)
            policy = getattr(provider, "policy", None)
            input_limit = getattr(policy, "max_input_tokens", None)
            output_limit = getattr(policy, "max_output_tokens", None)
            timeout = getattr(policy, "timeout_seconds", None)
            try:
                parameters = inspect.signature(generate_structured).parameters
            except (TypeError, ValueError) as error:
                raise ProviderFailure("provider_budget_unknown", "Provider limits cannot be enforced.", retryable=False) from error
            if (not isinstance(input_limit, int) or not isinstance(output_limit, int) or
                    input_limit <= 0 or output_limit <= 0 or not isinstance(timeout, (int, float)) or
                    not 0 < timeout <= 180 or "timeout_seconds" not in parameters):
                raise ProviderFailure("provider_budget_unknown", "Provider limits cannot be enforced.", retryable=False)
            token_reservation = input_limit + output_limit
            if token_reservation > request.max_total_tokens:
                raise ProviderFailure("provider_budget_exhausted", "Model token budget is insufficient.", retryable=False)
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ProviderFailure("provider_budget_exhausted", "Research time budget is exhausted.", retryable=False)
            _reserve_model_call(http_request, operation_id, token_budget=token_reservation)
            calls = 1
            # Retain non-sensitive prompt provenance even if the provider
            # returns invalid output or fails after accepting the request.
            prompt_data = json.loads(prompt)
            attempted_prompt.update({"prompt_sha256": sha256(prompt.encode("utf-8")).hexdigest(),
                "prompt_characters": len(prompt), "prompt_bytes": len(prompt.encode("utf-8")),
                "segments": [{"source_id": item["source_id"], "character_start": item["range"][0],
                              "character_end": item["range"][1]}
                             for item in prompt_data["sources"]]})
            try:
                payload, usage = generate_structured(prompt, cancel_event=operations.cancel_event(operation_id),
                    system=RESEARCH_SYSTEM_PROMPT, timeout_seconds=min(float(timeout), remaining))
                return payload, usage
            finally:
                # An attempted remote request can incur charges even if the
                # response is lost. Unknown token usage settles the reserved
                # token budget; provider currency cost remains unknown.
                _settle_model_call(http_request, operation_id, usage)

        try:
            max_input_tokens = getattr(getattr(provider, "policy", None), "max_input_tokens", 3000)
            # Reserve room for the system instruction and transport framing;
            # byte length is a conservative approximation, not a provider tokenizer.
            max_prompt_chars = max(1, min(48000, int(max_input_tokens) - len(RESEARCH_SYSTEM_PROMPT.encode("utf-8")) - 512))
            analysis = analyze(topic.request.question, sources, topic.request.as_of, generate,
                               max_prompt_chars=max_prompt_chars, should_stop=lambda: monotonic() >= deadline)
        except AnalysisInterrupted:
            unavailable_reason = "provider_budget_exhausted"
            from .research_analysis import AnalysisResult
            analysis = AnalysisResult([], [], ["Research analysis could not be completed: provider_budget_exhausted"], [], usage)
        except (ProviderFailure, InvalidAnalysis) as error:
            unavailable_reason = error.code if isinstance(error, ProviderFailure) else "invalid_model_output"
            if isinstance(error, InvalidAnalysis):
                # InvalidAnalysis messages are fixed internal diagnostics and
                # contain neither source text nor provider output.
                analysis_error_detail = str(error)
            from .research_analysis import AnalysisResult
            analysis = AnalysisResult([], [], ["Research analysis could not be completed: " + unavailable_reason], [], usage,
                                      **attempted_prompt)
        _raise_if_operation_cancelled(operation_id)
        claims = [{**item, "role": "support"} for item in analysis.claims]
        claims.extend({**item, "role": "counterargument"} for item in analysis.counterarguments)
        claims = suggest_percentage_evidence(claims, analysis.facts)
        claims = flag_publication_date_mismatches(claims, sources)
        generation_usage = usage
        review_usage: int | None = None
        review_status = "not_requested"
        review_error: str | None = None
        review_hash: str | None = None
        review_segments: list[dict[str, int | str]] = []
        review_indexes: list[int] = []
        if request.support_review:
            review_status = "not_reviewable"
            policy = getattr(provider, "policy", None)
            input_limit = getattr(policy, "max_input_tokens", None)
            output_limit = getattr(policy, "max_output_tokens", None)
            timeout = getattr(policy, "timeout_seconds", None)
            if calls and claims and request.max_model_calls < 2:
                review_status = "budget_exhausted"
            elif calls and claims and isinstance(input_limit, int) and isinstance(output_limit, int) and isinstance(timeout, (int, float)):
                per_call = input_limit + output_limit
                first_charge = max(per_call, generation_usage if generation_usage is not None else per_call)
                if request.max_total_tokens < first_charge + per_call or monotonic() >= deadline:
                    review_status = "budget_exhausted"
                else:
                    # Only sources already admitted by the first analysis can
                    # be reviewed; no extra retrieval or permission escalation.
                    review_budget = max(1, min(48000, input_limit - len(REVIEW_SYSTEM_PROMPT.encode("utf-8")) - 512))
                    batch = prepare_support_review(claims, [s for s in sources if s.source_id in analysis.source_ids], review_budget)
                    if batch.indexes:
                        def save_generation_checkpoint() -> None:
                            nonlocal checkpoint_parent
                            try:
                                current = research_topics.get(account.account_id, topic_id).versions[-1]
                                if current.version_id != request.parent_version_id:
                                    raise TopicVersionConflict()
                                partial = ResearchContent(summary=current.content.summary, source_candidates=discovered,
                                    sources=source_summaries(analysis.source_ids), facts=analysis.facts,
                                    evidence_differences=evidence_differences(analysis.facts),
                                    calculations=analysis.calculations,
                                    calculation_differences=claim_calculation_differences(claims, analysis.calculations, analysis.facts),
                                    amount_differences=claim_amount_differences(claims, analysis.facts),
                                    claims=claims, limitations=[*discovery_notes, *analysis.limitations,
                                        "Generation is saved; support review is pending or was interrupted."],
                                    run={"operation_id": operation_id, "status": "partial", "checkpoint": "generation",
                                         "model_calls": calls, "total_tokens": generation_usage, "cost_usd": None,
                                         "cost_status": "unknown" if calls or search_calls else "no_external_call",
                                         "generation_tokens": generation_usage, "support_review_requested": True,
                                         "support_review_status": "pending", "search_calls": search_calls,
                                         "model": getattr(getattr(provider, "settings", None), "model", None),
                                         "provider": getattr(provider, "name", None),
                                         "prompt_sha256": analysis.prompt_sha256,
                                         "prompt_characters": analysis.prompt_characters, "prompt_bytes": analysis.prompt_bytes,
                                         "system_prompt_sha256": sha256(RESEARCH_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
                                         "selection_method": RESEARCH_SELECTION_METHOD, "selected_segments": analysis.segments,
                                         "source_hashes": {s.source_id: s.content_sha256 for s in sources},
                                         "max_model_calls": request.max_model_calls, "max_search_calls": request.max_search_calls,
                                         "max_elapsed_seconds": request.max_elapsed_seconds, "max_total_tokens": request.max_total_tokens,
                                         "max_cost_usd": str(request.max_cost_usd) if request.max_cost_usd is not None else None})
                                saved = research_topics.add_version(account.account_id, topic_id, VersionInput(
                                    parent_version_id=request.parent_version_id, content=partial, note=current.note),
                                    source_ids=source_ids, created_by="agent")
                                checkpoint_parent = saved.versions[-1].version_id
                            except (TopicNotFound, TopicVersionConflict) as error:
                                raise ApiProblem(409, "lifecycle_conflict", "Research changed before the checkpoint was saved.") from error
                        if not operations.store.checkpoint_if_running(operation_id, save_generation_checkpoint):
                            _raise_if_operation_cancelled(operation_id)
                            raise ApiProblem(409, "lifecycle_conflict", "Research run ended before the checkpoint was saved.")
                        review_hash, review_segments, review_indexes = batch.prompt_sha256, batch.segments, batch.indexes
                        review_call_id = f"{operation_id}:support-review"
                        try:
                            _raise_if_operation_cancelled(operation_id)
                            remaining = deadline - monotonic()
                            if remaining <= 0:
                                raise ProviderFailure("provider_budget_exhausted", "Research time budget is exhausted.", retryable=False)
                            _reserve_model_call(http_request, review_call_id, token_budget=per_call)
                            calls += 1
                            try:
                                raw_review, review_usage = generate_structured(batch.prompt,
                                    cancel_event=operations.cancel_event(operation_id), system=REVIEW_SYSTEM_PROMPT,
                                    timeout_seconds=min(float(timeout), remaining))
                            finally:
                                _settle_model_call(http_request, review_call_id, review_usage)
                            claims = apply_support_review(claims, batch, raw_review)
                            review_status = "partial" if batch.omitted else "complete"
                        except (ProviderFailure, InvalidReview, ApiProblem) as error:
                            review_status = "failed"
                            review_error = error.code if isinstance(error, ProviderFailure) else "review_unavailable"
                        if calls == 2:
                            usage = generation_usage + review_usage if generation_usage is not None and review_usage is not None else None
        _raise_if_operation_cancelled(operation_id)
        differences = evidence_differences(analysis.facts)
        calculation_differences = claim_calculation_differences(claims, analysis.calculations, analysis.facts)
        amount_differences = claim_amount_differences(claims, analysis.facts)
        processing_incomplete = any(marker in note for note in analysis.limitations for marker in (
            "not processed", "remaining text was not examined", "was omitted", "were omitted",
            "permission is not granted", "not established", "not authorized", "quotation location is ambiguous",
            "simulated material is excluded"))
        review_supports_all = (review_status == "complete" and all(
            c.get("support_review", {}).get("verdict") == "supports" for c in claims))
        complete = bool(analysis.claims and analysis.counterarguments and all(c["citations"] for c in claims)
                        and not processing_incomplete and not differences and not calculation_differences and not amount_differences and
                        not any(c.get("publication_date_warnings") for c in claims) and
                        (not request.support_review or review_supports_all))
        limitations = [*discovery_notes, *analysis.limitations]
        if request.support_review:
            limitations.append("A second model assessment is not independent proof that evidence supports a claim.")
            if review_status != "complete":
                limitations.append("The requested model support review did not cover all claims.")
        if differences:
            limitations.append("Cross-source numeric differences require entity and accounting-basis review; they are not confirmed conflicts.")
        if calculation_differences:
            limitations.append("A claim percentage may differ from a deterministic calculation; review the wording and calculation.")
        if amount_differences:
            limitations.append("A claim amount may not match the cited numeric facts after unit conversion; review the source and wording.")
            if len(amount_differences) == 50:
                limitations.append("At most 50 amount review candidates are shown; additional candidates may exist.")
        if any(c.get("publication_date_warnings") for c in claims):
            limitations.append("A claimed publication date may differ from the source's declared date; review both.")
        if len(differences) == 50:
            limitations.append("At most 50 candidate differences are shown.")
        if monotonic() > deadline:
            complete = False
            limitations.append("Research exceeded its time budget; the retained result is partial.")
        if usage is not None and usage > request.max_total_tokens:
            complete = False
            limitations.append("Provider-reported token use exceeded the requested budget; the result is partial.")
        if not complete:
            limitations.append("The research is partial: supported claims or counterarguments are incomplete.")
        result = ResearchRunResult(operation_id=operation_id, topic_id=topic_id,
            parent_version_id=request.parent_version_id, status="completed" if complete else "partial",
            content=ResearchContent(source_candidates=discovered, sources=source_summaries(analysis.source_ids), facts=analysis.facts,
                evidence_differences=differences,
                calculations=analysis.calculations, calculation_differences=calculation_differences, amount_differences=amount_differences,
                claims=claims, limitations=limitations,
                run={"operation_id": operation_id, "status": "completed" if complete else "partial",
                     "model_calls": calls, "total_tokens": usage, "cost_usd": None,
                     "generation_tokens": generation_usage, "support_review_tokens": review_usage,
                     "support_review_requested": request.support_review, "support_review_status": review_status,
                     "support_review_error": review_error, "support_review_method": REVIEW_METHOD if request.support_review else None,
                     "analysis_error_detail": analysis_error_detail,
                     "support_review_prompt_sha256": review_hash, "support_review_indexes": review_indexes,
                     "support_review_segments": review_segments,
                     "support_review_system_sha256": sha256(REVIEW_SYSTEM_PROMPT.encode("utf-8")).hexdigest() if request.support_review else None,
                     "search_calls": search_calls, "search_status": search_status,
                     "elapsed_seconds": round(monotonic() - started, 3),
                     "cost_status": "unknown" if calls or search_calls else "no_external_call",
                     "model": getattr(getattr(provider, "settings", None), "model", None),
                     "provider": getattr(provider, "name", None),
                     "prompt_sha256": analysis.prompt_sha256, "prompt_characters": analysis.prompt_characters,
                     "prompt_bytes": analysis.prompt_bytes,
                     "selection_method": RESEARCH_SELECTION_METHOD,
                     "system_prompt_sha256": sha256(RESEARCH_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
                     "selected_segments": analysis.segments,
                     "provider_timeout_seconds": getattr(getattr(provider, "policy", None), "timeout_seconds", None),
                     "provider_input_token_limit": getattr(getattr(provider, "policy", None), "max_input_tokens", None),
                     "provider_output_token_limit": getattr(getattr(provider, "policy", None), "max_output_tokens", None),
                     "source_hashes": {s.source_id: s.content_sha256 for s in sources},
                     "max_model_calls": request.max_model_calls,
                     "max_search_calls": request.max_search_calls,
                     "max_elapsed_seconds": request.max_elapsed_seconds,
                     "max_total_tokens": request.max_total_tokens,
                     "max_cost_usd": str(request.max_cost_usd) if request.max_cost_usd is not None else None,
                     "error": unavailable_reason}))
        return result, topic_id

    def commit(_operation_id: str, result: BaseModel) -> None:
        assert isinstance(result, ResearchRunResult)
        # Refuse to save a stale result over a user's intervening edit.
        try:
            parent = research_topics.get(account.account_id, topic_id).versions[-1]
            expected_parent = checkpoint_parent or result.parent_version_id
            if parent.version_id != expected_parent:
                raise TopicVersionConflict()
            content = result.content.model_copy(update={"summary": parent.content.summary})
            research_topics.add_version(account.account_id, topic_id, VersionInput(
                parent_version_id=expected_parent, content=content, note=parent.note),
                source_ids=source_ids, created_by="agent")
        except (TopicNotFound, TopicVersionConflict) as error:
            raise ApiProblem(409, "lifecycle_conflict", "Research changed before the result was saved.") from error

    def record_inputs(operation_id: str) -> None:
        # Write before any external call, within operation admission. Replays do
        # not replace the original snapshot or send the provider another request.
        policy = getattr(provider, "policy", None)
        RESEARCH_RUN_INPUTS.put(operation_id, {
            "schema_version": "1", "operation_id": operation_id, "owner_id": account.account_id,
            "topic_id": topic_id, "parent_version_id": request.parent_version_id,
            "captured_at": lifecycle_clock.now().isoformat(),
            "question_sha256": sha256(topic.request.question.encode("utf-8")).hexdigest(),
            "as_of": topic.request.as_of.isoformat() if topic.request.as_of else None,
            "sources": [{"source_id": item.source_id, "content_sha256": item.content_sha256,
                         "pdf_text_order": item.pdf_text_order,
                         "event_at": item.event_at.isoformat() if item.event_at else None,
                         "published_at": item.published_at.isoformat() if item.published_at else None,
                         "obtained_at": item.obtained_at.isoformat(),
                         "revised_at": item.revised_at.isoformat() if item.revised_at else None,
                         "is_simulated": item.is_simulated, "permissions": item.permissions.model_dump(mode="json")}
                        for item in sources],
            "budget": _request_payload(request),
            "model": getattr(getattr(provider, "settings", None), "model", None),
            "provider": getattr(provider, "name", None),
            "provider_limits": {key: getattr(policy, key, None) for key in
                                ("max_input_tokens", "max_output_tokens", "timeout_seconds")},
            "system_prompt_sha256": sha256(RESEARCH_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
            "selection_method": RESEARCH_SELECTION_METHOD,
        })

    # Existing operation receipts have a v1 endpoint constraint. The v1 alias
    # above is a real route and keeps old receipt readers compatible.
    result, replayed = operations.submit_async(endpoint=f"/v1/research/topics/{topic_id}/runs",
        idempotency_key=idempotency_key, owner_subject=f"account:{account.account_id}",
        request_payload=_request_payload(request), result_field="research", build_result=build,
        session_id=topic_id, before_accept=record_inputs, commit_result=commit)
    _mark_replay(response, replayed)
    return result


@app.get("/v2/research/topics/{topic_id}/runs", response_model=list[OperationResponse], response_model_exclude_none=True, include_in_schema=False)
def list_research_runs(topic_id: str, http_request: Request) -> list[OperationResponse]:
    account = _authenticated_account(http_request.headers.get("Authorization"))
    try:
        research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error
    endpoint = f"/v1/research/topics/{topic_id}/runs"
    result: list[OperationResponse] = []
    for operation_id, session_id in operations.store.session_ids().items():
        if session_id != topic_id:
            continue
        record = operations.store.get_operation(operation_id)
        if not record or record.get("endpoint") != endpoint or record.get("owner_subject") != f"account:{account.account_id}":
            continue
        result.append(OperationResponse(operation=operations._cache_operation(operation_id)))
    return sorted(result, key=lambda item: item.operation.created_at, reverse=True)


@app.get("/v2/research/topics/{topic_id}/runs/{operation_id}", response_model=OperationResponse, response_model_exclude_none=True, include_in_schema=False)
def get_research_run(topic_id: str, operation_id: str, http_request: Request) -> OperationResponse:
    account = _authenticated_account(http_request.headers.get("Authorization"))
    try:
        research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error
    operations.require_owner(operation_id, f"account:{account.account_id}")
    operation = operations._cache_operation(operation_id)
    if operation.endpoint != f"/v1/research/topics/{topic_id}/runs":
        raise ApiProblem(404, "not_found", "Research run not found.")
    return OperationResponse(operation=operation)


@app.get("/v2/research/topics/{topic_id}/runs/{operation_id}/inputs", include_in_schema=False)
def get_research_run_inputs(topic_id: str, operation_id: str, http_request: Request) -> dict:
    account = _authenticated_account(http_request.headers.get("Authorization"))
    try:
        research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research run not found.") from error
    operations.require_owner(operation_id, f"account:{account.account_id}")
    record = RESEARCH_RUN_INPUTS.get(operation_id)
    if not record or record.get("owner_id") != account.account_id or record.get("topic_id") != topic_id:
        raise ApiProblem(404, "not_found", "Research run inputs not found.")
    return {key: value for key, value in record.items() if key != "owner_id"}


def _operation_owner_subject(request: Request) -> str:
    """Return the non-secret subject used to namespace operation receipts."""
    account = getattr(request.state, "model_account", None)
    if account is None:
        authorization = request.headers.get("Authorization")
        if authorization:
            account = _authenticated_account(authorization)
    return f"account:{account.account_id}" if account is not None else "anonymous"


def _require_operation_owner(request: Request, operation_id: str) -> str:
    subject = _operation_owner_subject(request)
    operations.require_owner(operation_id, subject)
    return subject


def _require_session_owner(request: Request, session_id: str) -> str:
    subject = _operation_owner_subject(request)
    if operations.owner_for_session(session_id) != subject:
        raise ApiProblem(404, "not_found", "Session not found.")
    return subject


def _market_credential_problem(error: MarketCredentialError) -> ApiProblem:
    return ApiProblem(
        error.status_code,
        error.code,
        error.message,
        retryable=error.status_code == 503,
        retry_after=30 if error.status_code == 503 else None,
    )


@app.get("/v1/account/market-data-config", response_model=MarketDataConfigResponse, include_in_schema=False)
def get_market_data_config(
    response: Response,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> MarketDataConfigResponse:
    """Return account provider order and masked credential metadata only."""
    account = _authenticated_account(authorization)
    response.headers["Cache-Control"] = "no-store"
    return market_data_settings.snapshot(account.account_id)


@app.put("/v1/account/market-data-config", response_model=MarketDataConfigResponse, include_in_schema=False)
def update_market_data_config(
    request: MarketDataConfigUpdate,
    response: Response,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> MarketDataConfigResponse:
    """Encrypt account-owned provider keys and update the bounded fallback order."""
    account = _authenticated_account(authorization)
    response.headers["Cache-Control"] = "no-store"
    try:
        return market_data_settings.update(account.account_id, request, now=lifecycle_clock.now())
    except MarketCredentialError as error:
        raise _market_credential_problem(error) from error


@app.post("/v1/account/market-data-config/test", response_model=MarketProviderTestResponse, include_in_schema=False)
def test_market_data_provider(
    request: MarketProviderTestRequest,
    response: Response,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> MarketProviderTestResponse:
    """Perform one bounded read through a saved provider without exposing diagnostics."""
    account = _authenticated_account(authorization)
    response.headers["Cache-Control"] = "no-store"
    try:
        _order, credentials = market_data_settings.runtime(account.account_id)
    except MarketCredentialError as error:
        raise _market_credential_problem(error) from error
    cutoff = lifecycle_clock.now().date() - timedelta(days=1)
    month_start = cutoff.replace(day=1)
    year_start = cutoff.replace(month=1, day=1)
    probe = MarketSnapshotRequest(
        assetIds=["GRID"],
        anchors={
            "year_start": year_start.isoformat(),
            "latest_month_start": month_start.isoformat(),
            "latest_completed_session": cutoff.isoformat(),
        },
        cutoff=cutoff.isoformat(),
        timezone="UTC",
        requestedAt=lifecycle_clock.now().isoformat(),
    )
    payload = build_snapshot_response(
        probe,
        provider_order=(request.provider_id,),
        provider_credentials={request.provider_id: credentials[request.provider_id]} if request.provider_id in credentials else {},
    )
    ready = any(item.get("status") in {"ok", "stale"} for item in payload.get("snapshots", []))
    return MarketProviderTestResponse(provider_id=request.provider_id, status="ready" if ready else "unavailable")


def _model_credential_problem(error: ModelCredentialError) -> ApiProblem:
    return ApiProblem(
        error.status_code,
        error.code,
        error.message,
        retryable=error.status_code == 503,
        retry_after=30 if error.status_code == 503 else None,
    )


@app.get("/v1/account/model-config", response_model=ModelConfigResponse, response_model_exclude_none=True, include_in_schema=False)
def get_model_config(
    response: Response,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> ModelConfigResponse:
    """Return account model mode and masked personal configuration only."""
    account = _authenticated_account(authorization)
    response.headers["Cache-Control"] = "no-store"
    return model_settings.snapshot(account.account_id)


@app.put("/v1/account/model-config", response_model=ModelConfigResponse, response_model_exclude_none=True, include_in_schema=False)
def update_model_config(
    request: ModelConfigUpdate,
    response: Response,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> ModelConfigResponse:
    """Encrypt and validate account-owned OpenAI-compatible credentials."""
    account = _authenticated_account(authorization)
    response.headers["Cache-Control"] = "no-store"
    try:
        return model_settings.update(account.account_id, request, now=lifecycle_clock.now())
    except ModelCredentialError as error:
        raise _model_credential_problem(error) from error


@app.delete("/v1/account/model-config", response_model=ModelConfigResponse, response_model_exclude_none=True, include_in_schema=False)
def delete_model_config(
    response: Response,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> ModelConfigResponse:
    """Delete every account-owned model configuration field and ciphertext."""
    account = _authenticated_account(authorization)
    response.headers["Cache-Control"] = "no-store"
    model_settings.delete(account.account_id)
    return model_settings.snapshot(account.account_id)


@app.post("/v1/account/model-config/test", response_model=ModelConfigTestResponse, include_in_schema=False)
def test_model_config(
    response: Response,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> ModelConfigTestResponse:
    """Run one bounded, redirect-free capability probe against a saved personal endpoint."""
    account = _authenticated_account(authorization)
    response.headers["Cache-Control"] = "no-store"
    try:
        settings = model_settings.runtime_settings(account.account_id)
    except ModelCredentialError:
        # A stale DNS answer, unavailable key file, or corrupted ciphertext is
        # deliberately indistinguishable from an unavailable provider probe.
        return ModelConfigTestResponse(status="unavailable")
    if settings is None:
        return ModelConfigTestResponse(status="unavailable")
    return ModelConfigTestResponse(status="ready" if OpenAICompatibleLlmProvider(settings).connection_ready() else "unavailable")


@app.get("/v1/account", include_in_schema=False)
def account_summary(authorization: str | None = Header(default=None, alias="Authorization")) -> JSONResponse:
    """Return server-authoritative plan and token quota state for the session."""

    account = _authenticated_account(authorization)
    return JSONResponse(
        {"account": {"account_id": account.account_id, "email": account.email}, **billing_ledger.snapshot(account.account_id, now=lifecycle_clock.now())},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/v1/billing/google-play/verify", include_in_schema=False)
def verify_google_play_subscription(
    request: GooglePlayVerifyRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> JSONResponse:
    """Verify a Play purchase server-side before granting subscription access."""

    account = _authenticated_account(authorization)
    credentials_path = os.getenv(
        "FORESIGHTNINE_GOOGLE_PLAY_CREDENTIALS",
        "/home/lxx/.config/google-play/service-account.json",
    ).strip()
    verifier = GooglePlaySubscriptionVerifier(credentials_path)
    try:
        expected_obfuscated_account_id = sha256(account.account_id.encode("utf-8")).hexdigest()
        verified = verifier.fetch(
            request.purchase_token,
            expected_product_id=request.product_id,
            expected_base_plan_id=request.base_plan_id,
            expected_obfuscated_account_id=expected_obfuscated_account_id,
        )
        snapshot = billing_ledger.apply_google_purchase(
            account.account_id,
            request.purchase_token,
            verified,
            now=lifecycle_clock.now(),
        )
        # Grant the verified entitlement before acknowledgement. A retry is
        # idempotent if the acknowledgement call transiently fails after the
        # durable entitlement write.
        if (
            verified.subscription_state not in {"pending", "on_hold", "canceled", "expired"}
            and verified.acknowledgement_state == "pending"
        ):
            verifier.acknowledge(request.purchase_token, verified.product_id)
    except BillingError as error:
        response = JSONResponse(
            {"schema_version": "v1", "error_code": error.code, "message": error.message, "retryable": error.status_code in {502, 503}},
            status_code=error.status_code,
            headers={"Cache-Control": "no-store"},
        )
        if error.retry_after is not None:
            response.headers["Retry-After"] = str(error.retry_after)
        return response
    return JSONResponse(
        jsonable_encoder({"verified": True, "purchase_state": verified.subscription_state, **snapshot}),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/v1/billing/google-play/restore", include_in_schema=False)
def restore_google_play_subscription(
    request: GooglePlayVerifyRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> JSONResponse:
    """Use the same idempotent server verification path when restoring access."""

    return verify_google_play_subscription(request, authorization)


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
def create_session(request: CreateSessionRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> SessionOperationResponse:
    _validate_request(request, "CreateSessionRequest")
    prepared: dict[str, ResearchSession] = {}

    def build(operation_id: str) -> tuple[ResearchSession, str]:
        session_id = deterministic_id("session", operation_id)
        payload = {"session_id": session_id, "status": "DRAFT", "expires_at": lifecycle_clock.now() + timedelta(hours=24)}
        metadata = _metadata(payload, "mock:session", "Deterministic anonymous session")
        metadata["created_at"] = lifecycle_clock.now()
        session = ResearchSession(**metadata, **payload)
        validate_payload(session, "ResearchSession")
        prepared[operation_id] = session
        return session, session_id

    def commit(operation_id: str, _result: BaseModel) -> None:
        session = prepared[operation_id]
        SESSIONS[session.session_id] = session
        operations.associate_session(operation_id, session.session_id)

    def rollback(operation_id: str, _result: BaseModel) -> None:
        session = prepared.get(operation_id)
        if session is not None:
            SESSIONS.pop(session.session_id, None)

    result, replayed = operations.execute(
        endpoint="/v1/sessions",
        idempotency_key=idempotency_key,
        owner_subject=_operation_owner_subject(http_request),
        request_payload=_request_payload(request),
        response_model=SessionOperationResponse,
        result_field="session",
        build_result=build,
        commit_result=commit,
        rollback_result=rollback,
    )
    _mark_replay(response, replayed)
    return result


@app.post("/v1/challenges", response_model=ChallengeOperationResponse, response_model_exclude_none=True, status_code=201, responses=ERROR_RESPONSES, operation_id="createChallenge")
def create_challenge(request: CreateChallengeRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> ChallengeOperationResponse:
    _validate_request(request, "CreateChallengeRequest")
    provider, personal_provider = _model_provider_for_request(http_request)
    if request.session_id not in SESSIONS:
        raise ApiProblem(404, "not_found", "Session not found.")
    _require_session_owner(http_request, request.session_id)
    prepared: dict[str, tuple[ThesisDocument, ChallengeRecord, int | None]] = {}

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
        draft = provider.generate_challenge(
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
            provider=provider, personal_provider=personal_provider,
        )
        prepared[operation_id] = (thesis, challenge, draft.total_tokens)
        return challenge, challenge_id

    def commit(operation_id: str, _result: BaseModel) -> None:
        thesis, challenge, total_tokens = prepared[operation_id]
        THESES[thesis.thesis_id] = thesis
        CHALLENGES[challenge.challenge_id] = challenge
        CHALLENGE_TURNS[challenge.challenge_id] = 1
        CHALLENGE_OPERATION_IDS[challenge.challenge_id] = operation_id
        _store_challenge_operation_result(operation_id, challenge)
        _settle_model_call(http_request, operation_id, total_tokens)

    def rollback(operation_id: str, _result: BaseModel) -> None:
        staged = prepared.get(operation_id)
        if staged is not None:
            thesis, challenge, _total_tokens = staged
            _remove_challenge_operation_result(operation_id, challenge)
            if CHALLENGE_OPERATION_IDS.get(challenge.challenge_id) == operation_id:
                CHALLENGE_OPERATION_IDS.pop(challenge.challenge_id, None)
                CHALLENGE_TURNS.pop(challenge.challenge_id, None)
                CHALLENGES.pop(challenge.challenge_id, None)
                THESES.pop(thesis.thesis_id, None)
        _release_model_call(http_request)

    result, replayed = operations.execute(
        endpoint="/v1/challenges",
        idempotency_key=idempotency_key,
        owner_subject=_operation_owner_subject(http_request),
        request_payload=_request_payload(request),
        response_model=ChallengeOperationResponse,
        result_field="challenge",
        build_result=build,
        session_id=request.session_id,
        commit_result=commit,
        rollback_result=rollback,
    )
    _mark_replay(response, replayed)
    return result


@app.post("/v1/challenges/async", response_model=OperationResponse, response_model_exclude_none=True, status_code=202, responses=ERROR_RESPONSES, operation_id="startChallengeStream", include_in_schema=False)
def start_challenge_stream(request: CreateChallengeRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> OperationResponse:
    """Accept an initial challenge as a cancellable operation before provider work starts."""
    _validate_request(request, "CreateChallengeRequest")
    provider, personal_provider = _model_provider_for_request(http_request)
    if request.session_id not in SESSIONS:
        raise ApiProblem(404, "not_found", "Session not found.")
    _require_session_owner(http_request, request.session_id)
    prepared: dict[str, tuple[ThesisDocument, ChallengeRecord, int | None]] = {}

    def before_accept(operation_id: str) -> None:
        _bind_operation_session(operation_id, request.session_id)
        _reserve_model_call(http_request, operation_id, session_id=request.session_id)

    def build(operation_id: str) -> tuple[ChallengeRecord, str]:
        _raise_if_operation_cancelled(operation_id)
        thesis = research_provider.thesis(
            operation_id=operation_id,
            session_id=request.session_id,
            raw_text=request.thesis,
            capital=request.capital,
            base_currency=request.base_currency,
            private_evidence=request.user_private_evidence_claims,
        )
        _raise_if_operation_cancelled(operation_id)
        challenge_id = deterministic_id("challenge", operation_id)
        draft = provider.generate_challenge(
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
            provider=provider, personal_provider=personal_provider,
        )
        prepared[operation_id] = (thesis, challenge, draft.total_tokens)
        return challenge, challenge_id

    def commit(operation_id: str, _result: BaseModel) -> None:
        thesis, challenge, total_tokens = prepared[operation_id]
        THESES[thesis.thesis_id] = thesis
        CHALLENGES[challenge.challenge_id] = challenge
        CHALLENGE_TURNS[challenge.challenge_id] = 1
        CHALLENGE_OPERATION_IDS[challenge.challenge_id] = operation_id
        _store_challenge_operation_result(operation_id, challenge)
        _settle_model_call(http_request, operation_id, total_tokens)

    def rollback(operation_id: str, _result: BaseModel) -> None:
        staged = prepared.get(operation_id)
        if staged is not None:
            thesis, challenge, _total_tokens = staged
            _remove_challenge_operation_result(operation_id, challenge)
            if CHALLENGE_OPERATION_IDS.get(challenge.challenge_id) == operation_id:
                CHALLENGE_OPERATION_IDS.pop(challenge.challenge_id, None)
                CHALLENGE_TURNS.pop(challenge.challenge_id, None)
                CHALLENGES.pop(challenge.challenge_id, None)
                THESES.pop(thesis.thesis_id, None)
        _release_model_call(http_request)

    result, replayed = operations.submit_async(
        endpoint="/v1/challenges/async", idempotency_key=idempotency_key,
        owner_subject=_operation_owner_subject(http_request),
        request_payload=_request_payload(request), result_field="challenge", build_result=_release_model_on_failure(http_request, build),
        before_accept=before_accept, session_id=request.session_id,
        commit_result=commit, rollback_result=rollback,
    )
    _mark_replay(response, replayed)
    return result


@app.get("/v1/challenges/{challenge_id}", response_model=ChallengeOperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="getChallenge", include_in_schema=False)
def get_challenge(
    http_request: Request,
    challenge_id: str,
    operation_id: Annotated[str | None, Query(min_length=8, max_length=160)] = None,
) -> ChallengeOperationResponse:
    latest_challenge = CHALLENGES.get(challenge_id)
    if not latest_challenge:
        raise ApiProblem(404, "not_found", "Challenge not found.")
    challenge = latest_challenge
    if operation_id is not None:
        # The explicit receipt route survives process restart: both the
        # terminal status and the result reference are read from durable
        # operation state, not the compatibility in-memory association.
        _require_operation_owner(http_request, operation_id)
        try:
            operation = operations._cache_operation(operation_id)
        except ApiProblem as error:
            raise ApiProblem(409, "lifecycle_conflict", "Challenge result has no matching completed operation receipt.") from error
        complete_events = [
            event for event in operations.store.get_events(operation_id)
            if event.get("event") == "operation.complete" and event.get("result_ref") == challenge_id
        ]
        if operation.status != "completed" or len(complete_events) != 1:
            raise ApiProblem(409, "lifecycle_conflict", "Challenge result has no matching completed operation receipt.")
        result_hash = complete_events[0].get("result_hash")
        immutable_result = CHALLENGE_OPERATION_RESULTS.get(operation_id)
        if immutable_result is None:
            # Compatibility for receipts created before operation-bound result
            # storage: only the still-current version may be returned.
            immutable_result = latest_challenge if latest_challenge.content_hash == result_hash else None
        if immutable_result is None or immutable_result.challenge_id != challenge_id or immutable_result.content_hash != result_hash:
            raise ApiProblem(409, "lifecycle_conflict", "Challenge result version does not match its completed operation receipt.")
        challenge = immutable_result
    else:
        # Preserve existing callers while new callers supply operation_id for
        # restart-durable receipt lookup.
        remembered_operation_id = CHALLENGE_OPERATION_IDS.get(challenge_id)
        if remembered_operation_id:
            _require_operation_owner(http_request, remembered_operation_id)
        operation = operations.operations.get(remembered_operation_id) if remembered_operation_id else None
    if operation is None:
        raise ApiProblem(409, "lifecycle_conflict", "Challenge result has no retained operation receipt.")
    return ChallengeOperationResponse(operation=operation, challenge=challenge)


@app.post("/v1/challenges/{challenge_id}/messages", response_model=ChallengeOperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="addChallengeMessage")
def add_challenge_message(challenge_id: str, request: ChallengeMessageRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> ChallengeOperationResponse:
    _validate_request(request, "ChallengeMessageRequest")
    provider, personal_provider = _model_provider_for_request(http_request)
    session_id = _session_for_challenge(challenge_id)
    if session_id is not None:
        _require_session_owner(http_request, session_id)
    prepared: dict[str, tuple[ChallengeRecord, ChallengeRecord, int, str | None, int | None]] = {}

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
        draft = provider.generate_challenge(
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
            provider=provider, personal_provider=personal_provider,
        )
        prepared[operation_id] = (current, updated, CHALLENGE_TURNS[challenge_id], CHALLENGE_OPERATION_IDS.get(challenge_id), draft.total_tokens)
        return updated, challenge_id

    def commit(operation_id: str, _result: BaseModel) -> None:
        previous, updated, previous_turn, _previous_operation_id, total_tokens = prepared[operation_id]

        def apply() -> None:
            CHALLENGES[challenge_id] = updated
            CHALLENGE_TURNS[challenge_id] = previous_turn + 1
            CHALLENGE_OPERATION_IDS[challenge_id] = operation_id
            _store_challenge_operation_result(operation_id, updated)
            _settle_model_call(http_request, operation_id, total_tokens)

        _commit_challenge_mutation(challenge_id, expected=previous, expected_turn=previous_turn, operation=apply)

    def rollback(operation_id: str, _result: BaseModel) -> None:
        staged = prepared.get(operation_id)
        if staged is not None and CHALLENGE_OPERATION_IDS.get(challenge_id) == operation_id:
            previous, _updated, previous_turn, previous_operation_id, _total_tokens = staged
            _remove_challenge_operation_result(operation_id, _updated)
            CHALLENGES[challenge_id] = previous
            CHALLENGE_TURNS[challenge_id] = previous_turn
            if previous_operation_id is None:
                CHALLENGE_OPERATION_IDS.pop(challenge_id, None)
            else:
                CHALLENGE_OPERATION_IDS[challenge_id] = previous_operation_id
        _release_model_call(http_request)

    result, replayed = operations.execute(
        endpoint=f"/v1/challenges/{challenge_id}/messages",
        idempotency_key=idempotency_key,
        owner_subject=_operation_owner_subject(http_request),
        request_payload=_request_payload(request),
        response_model=ChallengeOperationResponse,
        result_field="challenge",
        build_result=build,
        session_id=session_id,
        commit_result=commit,
        rollback_result=rollback,
    )
    _mark_replay(response, replayed)
    return result


@app.post("/v1/challenges/{challenge_id}/messages/async", response_model=OperationResponse, response_model_exclude_none=True, status_code=202, responses=ERROR_RESPONSES, operation_id="startChallengeMessageStream")
def start_challenge_message_stream(challenge_id: str, request: ChallengeMessageRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> OperationResponse:
    """Start a Phase 3 message without waiting for its provider runner."""
    _validate_request(request, "ChallengeMessageRequest")
    provider, personal_provider = _model_provider_for_request(http_request)
    session_id = _session_for_challenge(challenge_id)
    if session_id is not None:
        _require_session_owner(http_request, session_id)
    prepared: dict[str, tuple[ChallengeRecord, ChallengeRecord, int, str | None, int | None]] = {}

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
        draft = provider.generate_challenge(ChallengeInput(
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
            provider=provider, personal_provider=personal_provider,
        )
        prepared[operation_id] = (current, updated, CHALLENGE_TURNS[challenge_id], CHALLENGE_OPERATION_IDS.get(challenge_id), draft.total_tokens)
        return updated, challenge_id

    def commit(operation_id: str, _result: BaseModel) -> None:
        previous, updated, previous_turn, _previous_operation_id, total_tokens = prepared[operation_id]

        def apply() -> None:
            CHALLENGES[challenge_id] = updated
            CHALLENGE_TURNS[challenge_id] = previous_turn + 1
            CHALLENGE_OPERATION_IDS[challenge_id] = operation_id
            _store_challenge_operation_result(operation_id, updated)
            _settle_model_call(http_request, operation_id, total_tokens)

        _commit_challenge_mutation(challenge_id, expected=previous, expected_turn=previous_turn, operation=apply)

    def rollback(operation_id: str, _result: BaseModel) -> None:
        staged = prepared.get(operation_id)
        if staged is not None and CHALLENGE_OPERATION_IDS.get(challenge_id) == operation_id:
            previous, _updated, previous_turn, previous_operation_id, _total_tokens = staged
            _remove_challenge_operation_result(operation_id, _updated)
            CHALLENGES[challenge_id] = previous
            CHALLENGE_TURNS[challenge_id] = previous_turn
            if previous_operation_id is None:
                CHALLENGE_OPERATION_IDS.pop(challenge_id, None)
            else:
                CHALLENGE_OPERATION_IDS[challenge_id] = previous_operation_id
        _release_model_call(http_request)

    result, replayed = operations.submit_async(
        endpoint=f"/v1/challenges/{challenge_id}/messages/async", idempotency_key=idempotency_key,
        owner_subject=_operation_owner_subject(http_request),
        request_payload=_request_payload(request), result_field="challenge", build_result=_release_model_on_failure(http_request, build),
        before_accept=before_accept,
        session_id=session_id,
        commit_result=commit, rollback_result=rollback,
    )
    _mark_replay(response, replayed)
    return result


@app.post("/v1/challenges/{challenge_id}/evidence", response_model=ChallengeOperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="addChallengeEvidence")
def add_challenge_evidence(challenge_id: str, request: EvidenceSubmissionRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> ChallengeOperationResponse:
    _validate_request(request, "EvidenceSubmissionRequest")
    provider, personal_provider = _model_provider_for_request(http_request)
    session_id = _session_for_challenge(challenge_id)
    if session_id is not None:
        _require_session_owner(http_request, session_id)
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
    prepared: dict[str, tuple[ChallengeRecord, ChallengeRecord, EvidenceItem, EvidenceItem | None, int, str | None, int | None]] = {}

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
        draft = provider.generate_challenge(
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
            provider=provider, personal_provider=personal_provider,
        )
        prepared[operation_id] = (
            current, updated, evidence, EVIDENCE.get(evidence_id), CHALLENGE_TURNS[challenge_id],
            CHALLENGE_OPERATION_IDS.get(challenge_id), draft.total_tokens,
        )
        return updated, challenge_id

    def commit(operation_id: str, _result: BaseModel) -> None:
        previous, updated, evidence, _previous_evidence, previous_turn, _previous_operation_id, total_tokens = prepared[operation_id]

        def apply() -> None:
            EVIDENCE[evidence.evidence_id] = evidence
            CHALLENGES[challenge_id] = updated
            CHALLENGE_TURNS[challenge_id] = previous_turn + 1
            CHALLENGE_OPERATION_IDS[challenge_id] = operation_id
            _store_challenge_operation_result(operation_id, updated)
            _settle_model_call(http_request, operation_id, total_tokens)

        _commit_challenge_mutation(challenge_id, expected=previous, expected_turn=previous_turn, operation=apply)

    def rollback(operation_id: str, _result: BaseModel) -> None:
        staged = prepared.get(operation_id)
        if staged is not None and CHALLENGE_OPERATION_IDS.get(challenge_id) == operation_id:
            previous, _updated, evidence, previous_evidence, previous_turn, previous_operation_id, _total_tokens = staged
            _remove_challenge_operation_result(operation_id, _updated)
            CHALLENGES[challenge_id] = previous
            CHALLENGE_TURNS[challenge_id] = previous_turn
            if previous_evidence is None:
                EVIDENCE.pop(evidence.evidence_id, None)
            else:
                EVIDENCE[evidence.evidence_id] = previous_evidence
            if previous_operation_id is None:
                CHALLENGE_OPERATION_IDS.pop(challenge_id, None)
            else:
                CHALLENGE_OPERATION_IDS[challenge_id] = previous_operation_id
        _release_model_call(http_request)

    result, replayed = operations.execute(
        endpoint=f"/v1/challenges/{challenge_id}/evidence",
        idempotency_key=idempotency_key,
        owner_subject=_operation_owner_subject(http_request),
        request_payload=_request_payload(request),
        response_model=ChallengeOperationResponse,
        result_field="challenge",
        build_result=build,
        session_id=session_id,
        commit_result=commit,
        rollback_result=rollback,
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
        created_at=lifecycle_clock.now(),
        source_refs=refs,
        content_hash=content_hash(hash_input),
        **payload,
    )
    validate_payload(updated, "ChallengeRecord")
    return updated


@app.post("/v1/challenges/{challenge_id}/confirm", response_model=ChallengeOperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="confirmChallenge")
def confirm_challenge(challenge_id: str, request: ConfirmChallengeRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> ChallengeOperationResponse:
    _validate_request(request, "ConfirmChallengeRequest")
    session_id = _session_for_challenge(challenge_id)
    if session_id is not None:
        _require_session_owner(http_request, session_id)
    reservation = request.command == "CONFIRM_WITH_RESERVATIONS"
    prepared: dict[str, tuple[ChallengeRecord, ChallengeRecord, int, str | None]] = {}

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
        prepared[operation_id] = (current, updated, CHALLENGE_TURNS[challenge_id], CHALLENGE_OPERATION_IDS.get(challenge_id))
        return updated, challenge_id

    def commit(operation_id: str, _result: BaseModel) -> None:
        previous, updated, previous_turn, _previous_operation_id = prepared[operation_id]

        def apply() -> None:
            CHALLENGES[challenge_id] = updated
            CHALLENGE_TURNS[challenge_id] = previous_turn + 1
            CHALLENGE_OPERATION_IDS[challenge_id] = operation_id
            _store_challenge_operation_result(operation_id, updated)

        _commit_challenge_mutation(challenge_id, expected=previous, expected_turn=previous_turn, operation=apply)

    def rollback(operation_id: str, _result: BaseModel) -> None:
        staged = prepared.get(operation_id)
        if staged is not None:
            previous, updated, previous_turn, previous_operation_id = staged
            _remove_challenge_operation_result(operation_id, updated)
            current = CHALLENGES.get(challenge_id)
            if current is not None and current.content_hash == updated.content_hash:
                CHALLENGES[challenge_id] = previous
                CHALLENGE_TURNS[challenge_id] = previous_turn
                if previous_operation_id is None:
                    CHALLENGE_OPERATION_IDS.pop(challenge_id, None)
                else:
                    CHALLENGE_OPERATION_IDS[challenge_id] = previous_operation_id

    result, replayed = operations.execute(
        endpoint=f"/v1/challenges/{challenge_id}/confirm",
        idempotency_key=idempotency_key,
        owner_subject=_operation_owner_subject(http_request),
        request_payload=_request_payload(request),
        response_model=ChallengeOperationResponse,
        result_field="challenge",
        build_result=build,
        session_id=session_id,
        commit_result=commit,
        rollback_result=rollback,
    )
    _mark_replay(response, replayed)
    return result


@app.post("/v1/worldlines", response_model=WorldlineBundleOperationResponse, response_model_exclude_none=True, status_code=201, responses=ERROR_RESPONSES, operation_id="createWorldlines")
def create_worldlines(request: WorldlineRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> WorldlineBundleOperationResponse:
    _validate_request(request, "WorldlineRequest")
    challenge = CHALLENGES.get(request.challenge_id)
    if not challenge:
        raise ApiProblem(404, "not_found", "Challenge not found.")
    if challenge.status != "CONFIRMED":
        raise ApiProblem(409, "invalid_state_transition", "Worldlines require a confirmed challenge.")
    selected_provider, _personal = _model_provider_for_request(http_request)
    live = None
    if isinstance(selected_provider, OpenAICompatibleLlmProvider):
        live = LivePhase4Adapter(selected_provider, lifecycle_clock)
    elif selected_provider.name != "deterministic_mock":
        raise ApiProblem(503, "internal_error", "The selected model does not support structured scenarios.", retryable=False)
    baseline = live.clock.today() if live else clock.today()
    if request.horizon_end < baseline:
        raise ApiProblem(422, "validation_error", "Worldline horizon must not precede the baseline date.")
    session_id = _session_for_challenge(request.challenge_id)
    if session_id is not None:
        _require_session_owner(http_request, session_id)
    prepared: dict[str, tuple[WorldlineBundle, dict[str, tuple[Worldline | None, str | None]]]] = {}

    def build(operation_id: str) -> tuple[BaseModel, str]:
        thesis = THESES.get(challenge.thesis_id)
        if not thesis:
            raise ApiProblem(409, "lifecycle_conflict", "Confirmed challenge has no retained thesis input.")
        if live:
            live.cancel_event = operations.cancel_event(operation_id)
            _reserve_model_call(http_request, operation_id, session_id=session_id, token_budget=live.token_budget)
        bundle = (live or worldline_generator).generate(WorldlineGenerationInput(
            operation_id=operation_id, challenge=challenge, thesis=thesis, horizon_end=request.horizon_end,
        ))
        if not isinstance(bundle, WorldlineBundle):
            raise ProviderFailure("invalid_provider_output", "Worldline provider returned an unknown structured result.", retryable=False)
        _validate_provider_worldline_bundle(
            bundle,
            challenge_id=request.challenge_id,
            expected_premise=thesis.normalized_premise,
            expected_baseline_date=baseline,
            expected_horizon_end=request.horizon_end,
        )
        before = {worldline.worldline_id: (WORLDLINES.get(worldline.worldline_id), WORLDLINE_CHALLENGES.get(worldline.worldline_id)) for worldline in bundle.worldlines}
        prepared[operation_id] = (bundle, before)
        return bundle, bundle.bundle_id

    def commit(operation_id: str, _result: BaseModel) -> None:
        bundle, _before = prepared[operation_id]
        if live:
            _settle_model_call(http_request, operation_id, live.total_tokens)
        for worldline in bundle.worldlines:
            WORLDLINES[worldline.worldline_id] = worldline
            WORLDLINE_CHALLENGES[worldline.worldline_id] = request.challenge_id

    def rollback(operation_id: str, _result: BaseModel) -> None:
        _release_model_call(http_request)
        staged = prepared.get(operation_id)
        if staged is None:
            return
        bundle, before = staged
        for worldline in bundle.worldlines:
            previous, previous_challenge = before[worldline.worldline_id]
            current = WORLDLINES.get(worldline.worldline_id)
            if current is None or current.content_hash != worldline.content_hash:
                continue
            if previous is None:
                WORLDLINES.pop(worldline.worldline_id, None)
            else:
                WORLDLINES[worldline.worldline_id] = previous
            if previous_challenge is None:
                WORLDLINE_CHALLENGES.pop(worldline.worldline_id, None)
            else:
                WORLDLINE_CHALLENGES[worldline.worldline_id] = previous_challenge

    result, replayed = operations.execute(
        endpoint="/v1/worldlines",
        idempotency_key=idempotency_key,
        owner_subject=_operation_owner_subject(http_request),
        request_payload=_request_payload(request),
        response_model=WorldlineBundleOperationResponse,
        result_field="bundle",
        build_result=_release_model_on_failure(http_request, build),
        session_id=session_id,
        commit_result=commit,
        rollback_result=rollback,
    )
    _mark_replay(response, replayed)
    return result


@app.get("/v1/worldlines/{worldline_id}", response_model=Worldline, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="getWorldline")
def get_worldline(worldline_id: str, http_request: Request) -> Worldline:
    worldline = WORLDLINES.get(worldline_id)
    if not worldline:
        raise ApiProblem(404, "not_found", "Worldline not found.")
    challenge_id = WORLDLINE_CHALLENGES.get(worldline_id)
    session_id = _session_for_challenge(challenge_id) if challenge_id is not None else None
    if session_id is not None:
        _require_session_owner(http_request, session_id)
    return worldline


@app.post("/v1/probabilities/assess", response_model=ProbabilityOperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="assessProbability")
def assess_probability(request: ProbabilityRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> ProbabilityOperationResponse:
    _validate_request(request, "ProbabilityRequest")
    if request.worldline_id not in WORLDLINES:
        raise ApiProblem(404, "not_found", "Worldline not found.")
    session_id = None
    challenge_id = WORLDLINE_CHALLENGES.get(request.worldline_id)
    if challenge_id is not None:
        session_id = _session_for_challenge(challenge_id)
    if session_id is not None:
        _require_session_owner(http_request, session_id)

    selected_provider, _personal = _model_provider_for_request(http_request)
    live = None
    if isinstance(selected_provider, OpenAICompatibleLlmProvider):
        live = LivePhase4Adapter(selected_provider, lifecycle_clock)
    elif selected_provider.name != "deterministic_mock":
        raise ApiProblem(503, "internal_error", "The selected model does not support structured probabilities.", retryable=False)

    def build(operation_id: str) -> tuple[BaseModel, str]:
        if live:
            live.cancel_event = operations.cancel_event(operation_id)
            _reserve_model_call(http_request, operation_id, session_id=session_id, token_budget=live.token_budget)
        result = (live or probability_assessor).assess(ProbabilityAssessmentInput(
            operation_id=operation_id, worldline=WORLDLINES[request.worldline_id],
        ))
        if not isinstance(result, ProbabilityAssessment):
            raise ProviderFailure("invalid_provider_output", "Probability provider returned an unknown structured result.", retryable=False)
        _validate_provider_probability_assessment(result, expected_worldline_id=request.worldline_id)
        return result, result.assessment_id

    def commit(operation_id: str, _result: BaseModel) -> None:
        if live:
            _settle_model_call(http_request, operation_id, live.total_tokens)

    def rollback(_operation_id: str, _result: BaseModel) -> None:
        _release_model_call(http_request)

    result, replayed = operations.execute(
        endpoint="/v1/probabilities/assess",
        idempotency_key=idempotency_key,
        owner_subject=_operation_owner_subject(http_request),
        request_payload=_request_payload(request),
        response_model=ProbabilityOperationResponse,
        result_field="probability",
        build_result=_release_model_on_failure(http_request, build),
        commit_result=commit,
        rollback_result=rollback,
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
def create_market_manifest(request: MarketDataManifestRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> MarketDataManifestOperationResponse:
    _validate_request(request, "MarketDataManifestRequest")
    unknown = [asset_id for asset_id in request.asset_ids if asset_id not in ASSET_INDEX]
    if unknown:
        raise ApiProblem(422, "validation_error", f"Unknown registry asset IDs: {', '.join(unknown)}")
    from_at = getattr(request, "from")
    if from_at > request.to:
        raise ApiProblem(422, "validation_error", "Market-data 'from' must not be after 'to'.")
    prepared: dict[str, tuple[MarketDataManifest, MarketDataManifest | None]] = {}

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
        prepared[operation_id] = (manifest, MARKET_MANIFESTS.get(manifest.manifest_id))
        return manifest, manifest.manifest_id

    def commit(operation_id: str, _result: BaseModel) -> None:
        manifest, _previous = prepared[operation_id]
        MARKET_MANIFESTS[manifest.manifest_id] = manifest

    def rollback(operation_id: str, _result: BaseModel) -> None:
        staged = prepared.get(operation_id)
        if staged is None:
            return
        manifest, previous = staged
        current = MARKET_MANIFESTS.get(manifest.manifest_id)
        if current is None or current.content_hash != manifest.content_hash:
            return
        if previous is None:
            MARKET_MANIFESTS.pop(manifest.manifest_id, None)
        else:
            MARKET_MANIFESTS[manifest.manifest_id] = previous

    result, replayed = operations.execute(
        endpoint="/v1/market-data/manifests",
        idempotency_key=idempotency_key,
        owner_subject=_operation_owner_subject(http_request),
        request_payload=_request_payload(request),
        response_model=MarketDataManifestOperationResponse,
        result_field="manifest",
        build_result=build,
        commit_result=commit,
        rollback_result=rollback,
    )
    _mark_replay(response, replayed)
    return result


@app.post("/v1/market-data/shards", response_model=MarketDataShardOperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="getMarketDataShard")
def get_market_shard(request: MarketDataShardRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> MarketDataShardOperationResponse:
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
        owner_subject=_operation_owner_subject(http_request),
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
def attest_client_computation(computation_id: str, request: ClientComputationAttestRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> ClientAttestationOperationResponse:
    _validate_request(request, "ClientComputationAttestRequest")
    if request.operation_id not in operations.operations:
        if operations.store.get_operation(request.operation_id) is None:
            raise ApiProblem(404, "not_found", "Referenced operation not found.")
    _require_operation_owner(http_request, request.operation_id)
    session_id = operations.session_ids().get(request.operation_id)

    def build(operation_id: str) -> tuple[BaseModel, str]:
        result = build_attestation(operation_id=operation_id, computation_id=computation_id, request=request, clock=clock)
        return result, computation_id

    result, replayed = operations.execute(
        endpoint=f"/v1/client-computations/{computation_id}/attest",
        idempotency_key=idempotency_key,
        owner_subject=_operation_owner_subject(http_request),
        request_payload=_request_payload(request),
        response_model=ClientAttestationOperationResponse,
        result_field="attestation",
        build_result=build,
        session_id=session_id,
    )
    _mark_replay(response, replayed)
    return result


@app.post("/v1/operations/{operation_id}/cancel", response_model=OperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="cancelOperation")
def cancel_operation(operation_id: str, request: CancelOperationRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> OperationResponse:
    _validate_request(request, "CancelOperationRequest")
    result, replayed = operations.cancel(operation_id, request.cancel_token, idempotency_key, owner_subject=_operation_owner_subject(http_request))
    if result.operation.status == "cancelled":
        if not result.operation.endpoint.startswith("/v1/research/topics/"):
            billing_ledger.release(f"model:{operation_id}", now=lifecycle_clock.now(), reverse_settled=True)
    _mark_replay(response, replayed)
    return result


@app.post("/v1/operations/{operation_id}/resume", response_model=OperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="resumeOperation")
def resume_operation(operation_id: str, request: ResumeOperationRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> OperationResponse:
    _validate_request(request, "ResumeOperationRequest")
    result, replayed = operations.resume(operation_id, request.resume_token, idempotency_key, owner_subject=_operation_owner_subject(http_request))
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
def stream_operation_events(http_request: Request, operation_id: str, last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None) -> StreamingResponse:
    # Validate the replay cursor before response headers are committed; exceptions
    # raised only from a streaming iterator would otherwise surface as HTTP 200.
    _require_operation_owner(http_request, operation_id)
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


@app.get("/v2/research/openapi.json", include_in_schema=False)
def research_openapi() -> dict[str, Any]:
    """Expose v2 schemas without changing the pinned v1 OpenAPI snapshot."""
    routes = []
    for route in app.routes:
        if getattr(route, "path", "").startswith("/v2/research/") and getattr(route, "path", "") != "/v2/research/openapi.json":
            route_copy = copy(route)
            route_copy.include_in_schema = True
            routes.append(route_copy)
    return get_openapi(title="ForesightNine Research API", version="2", routes=routes)


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
    for name in ("OAuthAuthorizationResponse", "OAuthProviderReadiness", "OAuthProvidersResponse", "OAuthSessionResponse", "OAuthStartRequest", "OAuthTicketExchangeRequest"):
        route_schema = document.get("components", {}).get("schemas", {}).get(name)
        if route_schema is not None:
            component_schemas[name] = route_schema
    document.setdefault("components", {})["schemas"] = component_schemas
    app.openapi_schema = document
    return document


app.openapi = canonical_openapi
