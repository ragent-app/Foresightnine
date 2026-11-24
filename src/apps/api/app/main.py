from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import copy, deepcopy
from dataclasses import asdict, replace as dataclass_replace
from collections.abc import Callable, Mapping
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import logging
import math
import os
import random
import re
import secrets
import tempfile
from pathlib import Path
from threading import Event, RLock, Thread
from time import monotonic, sleep
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from fastapi import Body, FastAPI, Header, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError
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
from .security import BodyTooLarge, SecurityBoundaryMiddleware, TrustedProxyResolver, build_captcha_verifier, build_security_config
from .telemetry import record_provider_failure, record_stale_data_denial, telemetry
from .phase4_live import LivePhase4Adapter
from .phase3 import (
    ChallengeDraft,
    ChallengeInput,
    LlmProvider,
    ProviderFailure,
    ProviderPolicy,
    SearchEvidenceBundle,
    build_llm_provider,
    build_search_evidence_provider,
)
from .openai_compatible import OpenAICompatibleLlmProvider, OpenAICompatibleSettings
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
from .storage import JsonDocumentRepository, StorageConfig, StorageConflictError, StorageMode, _drop_legacy_null_fields, build_repository, owner_items
from .research_topics import (AttachSourcesInput, ResearchCalculation, ResearchFact, ResearchFullExport, ResearchInput, ResearchMetadataExport,
    ResearchTopic, ResearchTopics, ResearchTurn, TopicDeletion, TopicForkInput, TopicMeta, TopicNotFound, TopicUpdate, TopicVersionConflict,
    VersionInput, VersionRestoreInput)
from .research_clock import build_research_clock
from .research_progress import build_progress
from .research_live_stream import (
    ResearchLiveBroker,
    format_org_graph_sse_frame,
    format_sse_frame,
    normalize_org_graph_event,
    parse_last_event_id,
    parse_org_last_event_id,
)
from .research_answer_evidence import AnswerEvidence, invalidate_edited_answers, part_references, visible_answer_content
from .research_turns import merge_turn, prior_research_input
from .research_turn_persister import ResearchTurnPersister, record_failed_checkpoint_version
from .research_calculations import BINARY_FORMULAS, CalculationError, CalculationRequest, PublicCalculationRequest, ConfirmDirectionRequest, CreateResearchCalculationRequest, MULTI_FACT_FORMULAS, NumericFact, RecalculateRequest, calculate, calculation_input_status, claim_calculation_differences, direction_confirmation_fingerprint, direction_user_confirmed, verified_direction_proof, table_user_confirmed, xbrl_source_consistent, suggest_percentage_calculations, is_percent_unit, monetary_currency_not_in_quotation, monetary_currency_unspecified, monetary_scale_not_in_quotation, table_currency_quote_missing, currency_value_conflict, quotation_is_inside_table_row, quotation_is_table_row, quotation_is_pdf_layout_row, table_period_conflict, legacy_percentage_difference_unit
from .research_sources import ResearchSource, ResearchSources, SourceInput, SourceNotFound, SourcePermissionError, locate_user_quotation, partial_ascii_token, pdf_text_spacing_suspect, table_context_window
from .research_table_provenance import bounded_table_trace
from .research_analysis import SELECTION_METHOD as RESEARCH_SELECTION_METHOD, _directional_negative_amount, _directional_negative_percent, _quoted_numbers
from .research_runs import DOCUMENT_OVERVIEW_QUESTION, ResearchRunRequest, ResearchRunResult, resolve_automatic_research_request
from .research_source_policy import prepare_sources, summarize_sources
from .research_budget import ResearchExecutionBudget
from .research_coordinator import ResearchExecutionContext
from .research_lifecycle import ResearchVersionChain
from .research_official_retrieval import execute_official_retrieval, official_source_id
from .research_news_tool import default_news_providers
from .research_run_insights import method_pack_records
from .research_ocr import OcrUsageHooks, build_ocr_pages_callback, vision_engine_configured
from .research_workflow import ResearchWorkflow
from .research_recovery import (RecoveryJournal, RUNTIME_CHECKPOINT_SCHEMA_VERSION,
    runtime_poison_pill_blocked, _POISON_PILL_MESSAGE,
    runtime_truncation_loop_blocked, _REPEATED_TRUNCATION_MESSAGE,
    runtime_timeout_loop_blocked, _REPEATED_TIMEOUT_MESSAGE,
    runtime_interruption_no_progress_blocked, _REPEATED_INTERRUPTION_MESSAGE)
from .research_topics import ResearchContent, ResearchInstruction, SavedResearchInstruction, ResearchVersion
from .research_run_instructions import (
    InstructionConflict, InstructionInvalid, InstructionNotFound, ResearchRunInstructions,
)
from .research_fetch import (
    HTML_EXTRACTION_DISCLOSURE,
    MAX_FETCH_BYTES,
    PUBLIC_DOMAIN_OFFICIAL_HOSTS,
    FetchFailure,
    FetchSourceRequest,
    fetch_allowed_hosts,
    fetch_official_text,
    fetch_source_document,
    is_sec_host,
    validate_public_https_url,
)
from .research_sec import SecConceptRequest, SecIdentityRequest, SecFailure, fetch_sec_concept, fetch_sec_filing, pace_sec_request, search_sec_identities, reserve_sec_request, validate_user_agent
from .research_source_catalog import canonical_source_url, source_capabilities
from .research_source_terms import user_initiated_basis, user_initiated_scope
from .issuer_documents import IssuerDocuments
from .research_uploads import HtmlUploadRequest, PdfFailure, PdfReextractRequest, PdfUploadRequest, extract_pdf, extract_uploaded_html
from .earnings_external_data import ExternalEarningsService
from .fin_data.adapters.fmp import FmpEarningsAdapter
from .fin_data.adapters.sec_deep import SecDeepAdapter
from .fin_data.adapters.sec_edgar import SecEdgarAdapter
from .research_earnings_diagnostics import GuidanceMetricInput, NonGaapBridgeInput, PeriodFinancials
from .research_earnings_export import ExportUnavailableError, MarkdownReportMode, export_earnings_dossier
from .research_earnings_routing import (
    EarningsSquadRouteDecision,
    derive_sources_mode,
    detect_us_earnings_squad_route,
)
from .research_earnings_squad import (
    EarningsDossier,
    EarningsSquadOrchestrator,
    FinToolboxToolRunner,
    InMemorySquadCheckpointStore,
    OrgGraphEvent,
    PrefetchDependencies,
    SlotBinding,
    SlotModelRequest,
    SlotModelResponse,
    SquadCancelledError,
    SquadCheckpoint,
    SquadCheckpointStore,
    SquadConfigurationError,
    SquadExecutionError,
    SquadModelRunner,
    SquadPrefetchRequest,
    SquadToolViolationError,
    UserSteeringInstruction,
    render_deep_report,
    resolve_squad_slots,
)
from .research_model_slots import (
    ModelSlotConfig,
    ModelSlotError,
    resolve_model_slots,
    slot_routing_summary,
)

_DEFAULT_FETCH_OFFICIAL_TEXT = fetch_official_text
_DEFAULT_RESERVE_SEC_REQUEST = reserve_sec_request
_HTML_RETRIEVAL_METHODS = frozenset({"official_html", "user_html", "sec_filing_html", "web_html"})
_PDF_RETRIEVAL_METHODS = frozenset({"user_pdf", "web_pdf"})


def _configured_sec_user_agent() -> tuple[str | None, bool]:
    raw = os.environ.get("FORESIGHTNINE_SEC_USER_AGENT") or os.environ.get("SEC_USER_AGENT")
    try:
        return validate_user_agent(raw), True
    except SecFailure:
        return None, False


def _resolve_market_key(account_id: str, policy=None) -> tuple[str | None, str | None]:
    """(api_key, credential_owner) for Twelve Data market data, or (None, None).

    The account's own key always wins ('user_byok'). The platform environment key is used
    ('platform') only when the fin_data policy explicitly allows twelve_data for credential_owner
    'platform' for identity or bars; with the default policy it is never read.
    """
    try:
        _order, credentials = market_data_settings.runtime(account_id)
    except MarketCredentialError:
        credentials = {}
    own = credentials.get("twelve_data") or None
    if own:
        return own, "user_byok"
    policy = policy or load_fin_data_policy()
    allowed = any(not isinstance(policy.decide("twelve_data", category, credential_owner="platform"), FinDataDenied)
                  for category in ("identity", "bars_daily", "bars_intraday"))
    if allowed:
        platform = platform_secret("FORESIGHTNINE_TWELVEDATA_API_KEY") or ""
        if platform:
            return platform, "platform"
    return None, None


def _resolve_tushare_key(account_id: str, policy=None) -> tuple[str | None, str | None]:
    """(token, credential_owner) for Tushare A-share data, or (None, None).

    Same rule as Twelve Data: the account's own token wins ('user_byok'); the platform token
    (FORESIGHTNINE_TUSHARE_TOKEN) is used ('platform') only when the fin_data policy explicitly allows tushare for
    credential_owner 'platform'. The default policy denies it.
    """
    try:
        _order, credentials = market_data_settings.runtime(account_id)
    except MarketCredentialError:
        credentials = {}
    own = credentials.get("tushare") or None
    if own:
        return own, "user_byok"
    policy = policy or load_fin_data_policy()
    allowed = any(not isinstance(policy.decide("tushare", category, credential_owner="platform"), FinDataDenied)
                  for category in ("identity", "bars_daily", "financials"))
    if allowed:
        platform = platform_secret("FORESIGHTNINE_TUSHARE_TOKEN") or ""
        if platform:
            return platform, "platform"
    return None, None


def _resolve_fmp_key(account_id: str, policy=None) -> tuple[str | None, str | None]:
    """(api_key, credential_owner) for FMP earnings consensus & transcripts, or (None, None).

    The account's own FMP BYOK key wins ('user_byok'). The platform environment key
    (FORESIGHTNINE_FMP_API_KEY) is used ('platform') only when the fin_data policy allows it
    (requires FORESIGHTNINE_FMP_ALLOW_PLATFORM_KEY=true or explicit policy opt-in).
    """
    return resolve_fmp_key(
        account_id,
        market_data_settings,
        policy=policy,
        platform_secret_lookup=platform_secret,
    )


def _cn_data_possible(tushare_key: str | None, policy=None) -> bool:
    """A-share/HK price data may be possible: a Tushare token, BaoStock, or Yahoo allowed by policy. Reachability is
    not probed here (admission must stay fast); the tools report cn_provider_unreachable per call."""
    from .fin_data.adapters.baostock import baostock_installed
    if tushare_key is not None or baostock_installed():
        return True
    policy = policy or load_fin_data_policy()
    return not isinstance(policy.decide("yahoo", "bars_daily", "SSE", credential_owner="platform"), FinDataDenied) or \
        not isinstance(policy.decide("yahoo", "bars_daily", "HKEX", credential_owner="platform"), FinDataDenied)


oauth_trace_logger = logging.getLogger("foresightnine.oauth")
from .operation_store import build_operation_store
from .oauth_foundation import OAuthConfigurationError, OAuthExchangeTicketError, OAuthFoundation, OAuthTransactionError, SUPPORTED_PROVIDERS
from .oauth_identity import OAuthIdentity, OAuthSessionResponse
from .oauth_provider import OAuthProviderClient, OAuthProviderError
from .market_provider import MarketSnapshotRequest, build_snapshot_response
from .fin_data.policy import Denied as FinDataDenied, load_policy as load_fin_data_policy
from .admin_auth import ELEVATION_HEADER, AdminAuthError, AdminGate
from .admin_router import create_admin_router
from .platform_secrets import PlatformSecretError, PlatformSecretStore, configure as configure_platform_secrets, default_cipher as platform_secret_cipher, managed_secret, managed_secrets, platform_secret, provider_registry, search_secret_name
from .market_credentials import (
    AccountMarketDataSettings,
    MarketCredentialCipher,
    MarketCredentialError,
    MarketDataConfigResponse,
    MarketDataConfigUpdate,
    MarketProviderTestRequest,
    MarketProviderTestResponse,
    StoredMarketDataConfig,
    resolve_fmp_key,
)
from .model_discovery import ModelCatalog, ModelDiscoveryRequest, discover_models
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
    configured_research_model,
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
    try:
        operations.recover_expired_leases()
        _reconcile_orphaned_earnings_squads()
    except Exception:
        logging.getLogger(__name__).exception("Startup reconciliation failed")
    stopped = asyncio.Event()
    async def supervise() -> None:
        while not stopped.is_set():
            try:
                await asyncio.to_thread(_reconcile_research_work)
            except Exception:
                logging.getLogger(__name__).exception("Research job reconciliation failed")
            try:
                await asyncio.wait_for(stopped.wait(), timeout=5)
            except TimeoutError:
                pass
    supervisor = asyncio.create_task(supervise())
    try:
        yield
    finally:
        stopped.set()
        await supervisor


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
_model_call_quota_raw = os.getenv("FORESIGHTNINE_MODEL_CALL_QUOTA", "").strip()
try:
    _model_call_quota = int(_model_call_quota_raw) if _model_call_quota_raw else None
    if _model_call_quota is not None and _model_call_quota < 0:
        raise ValueError("model quota must be non-negative")
except ValueError as error:
    raise RuntimeError("Invalid model call quota configuration") from error
abuse_control: AbuseBackend = build_abuse_control(model_quota=_model_call_quota)
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
RESEARCH_TOPIC_META = build_repository(storage_config, "research_topic_meta", value_loader=TopicMeta.model_validate)
research_topics = ResearchTopics(RESEARCH_TOPICS, RESEARCH_TOPIC_META)
RESEARCH_SOURCES = build_repository(storage_config, "research_sources", value_loader=ResearchSource.model_validate)
research_sources = ResearchSources(RESEARCH_SOURCES)
RESEARCH_RUN_INPUTS = build_repository(storage_config, "research_run_inputs")
TURN_RECORDS = build_repository(storage_config, "research_turn_instructions")
TURN_STATUS = build_repository(storage_config, "research_turn_status")
RESEARCH_WORK = build_repository(storage_config, "research_turn_work")
EARNINGS_SQUAD_STATES = build_repository(storage_config, "earnings_squad_states")
EARNINGS_SQUAD_CHECKPOINTS = build_repository(storage_config, "earnings_squad_checkpoints")
turn_instructions = ResearchRunInstructions(TURN_RECORDS)
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
research_live_broker = ResearchLiveBroker()
_ACTIVE_SQUAD_LOCK = RLock()
_ACTIVE_SQUAD_RUNS: dict[tuple[str, str], dict[str, Any]] = {}
OPERATION_SESSIONS: dict[str, str] = {}
CHALLENGE_OPERATION_IDS: dict[str, str] = {}
billing_ledger = BillingLedger(
    BILLING_STATES,
    BILLING_PURCHASES,
    USAGE_LEDGER,
    cipher=PurchaseTokenCipher.from_environment(),
)


def _publish_research_terminal(operation_id: str, status: str) -> None:
    inputs = RESEARCH_RUN_INPUTS.get(operation_id)
    if not inputs:
        return
    owner_id = inputs.get("owner_id")
    topic_id = inputs.get("topic_id")
    if not isinstance(owner_id, str) or not isinstance(topic_id, str):
        return
    if inputs.get("run_mode") == "earnings_squad":
        row = EARNINGS_SQUAD_STATES.get(_squad_state_key(owner_id, topic_id)) or {}
        dossier_raw = row.get("dossier") if isinstance(row, Mapping) else None
        ver_id = dossier_raw.get("dossier_id") if isinstance(dossier_raw, Mapping) else None
        org_events = list(row.get("org_events") or []) if isinstance(row, Mapping) else []
        research_live_broker.publish_terminal(
            operation_id,
            operation_status=status,
            version_id=ver_id,
            progress={
                "org_events": org_events,
                "logical_status": "stopped" if status == "cancelled" else status,
                "current_operation_id": operation_id,
            },
        )
        return
    version = None
    if status == "completed":
        try:
            topic = research_topics.get(owner_id, topic_id)
            version = next(
                (
                    v
                    for v in reversed(topic.versions)
                    if v.created_by == "agent" and v.content.turns and operation_id in v.content.turns[-1].operation_ids
                ),
                None,
            )
        except TopicNotFound:
            version = None
    progress = build_progress(
        operation_id=operation_id,
        operation_status=status,
        inputs=inputs,
        checkpoint=inputs.get("runtime_checkpoint") or None,
        saved_turn=version.content.turns[-1] if version else None,
        version_id=version.version_id if version else None,
        after=0,
    )
    turn_id = (inputs.get('turn') or {}).get('turn_id')
    doc = TURN_RECORDS.get(turn_id) if turn_id else None
    if doc and doc['account_id'] == owner_id:
        progress.update(_turn_progress_fields(doc, status, version))
    research_live_broker.publish_terminal(
        operation_id,
        operation_status=status,
        version_id=version.version_id if version else None,
        progress=progress,
    )


def _release_terminal_reservation(operation_id: str, status: str) -> None:
    # A terminal operation alone does not establish whether submission occurred
    # or whether a provider charged it. Known-unsent paths release explicitly.
    _publish_research_terminal(operation_id, status)


operations.set_terminal_observer(_release_terminal_reservation)
market_data_settings = AccountMarketDataSettings(
    MARKET_DATA_CONFIGS,
    MarketCredentialCipher.from_environment(),
)
model_settings = AccountModelSettings(
    MODEL_CONFIGS,
    ModelCredentialCipher.from_environment(),
)
PLATFORM_SECRETS = build_repository(storage_config, "platform_secrets", value_loader=None)
PLATFORM_SECRET_AUDIT = build_repository(storage_config, "platform_secret_audit")
ADMIN_ELEVATIONS = build_repository(storage_config, "admin_elevations")
ADMIN_ATTEMPTS = build_repository(storage_config, "admin_attempts")
platform_secret_store = PlatformSecretStore(PLATFORM_SECRETS, PLATFORM_SECRET_AUDIT, platform_secret_cipher(), now=lifecycle_clock.now)
configure_platform_secrets(platform_secret_store)
admin_gate = AdminGate(OAUTH_IDENTITIES, ADMIN_ELEVATIONS, ADMIN_ATTEMPTS, lifecycle_clock.now)


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


def _warn_if_research_timeout_mismatch(
    max_output_tokens: int | None,
    timeout_seconds: float | None,
    *,
    source: str,
) -> None:
    """Emit a non-blocking warning when high research output caps pair with short LLM timeouts."""
    import math
    if (
        type(max_output_tokens) is int
        and max_output_tokens >= 8192
        and isinstance(timeout_seconds, (int, float))
        and not isinstance(timeout_seconds, bool)
        and math.isfinite(timeout_seconds)
        and timeout_seconds < 240
    ):
        logging.getLogger(__name__).warning(
            "research_timeout_output_mismatch source=%s max_output_tokens=%s timeout_seconds=%s recommended_min_timeout=240",
            source,
            max_output_tokens,
            timeout_seconds,
        )


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
    raw_max_output = os.environ.get("FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS", "").strip()
    if raw_max_output:
        try:
            configured_max_output = int(raw_max_output)
        except ValueError:
            configured_max_output = None
        if configured_max_output is not None and configured_max_output >= 8192:
            raw_timeout = os.environ.get("FORESIGHTNINE_RESEARCH_LLM_TIMEOUT_SECONDS", "").strip()
            if not raw_timeout and os.environ.get("FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL", "").strip() != "anthropic_messages":
                raw_timeout = os.environ.get("FORESIGHTNINE_LLM_TIMEOUT_SECONDS", "").strip()
            try:
                configured_timeout = float(raw_timeout or "20")
            except ValueError:
                configured_timeout = None
            _warn_if_research_timeout_mismatch(
                configured_max_output,
                configured_timeout,
                source="startup_config",
            )

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
    research_live_broker.reset()
    RESEARCH_TOPICS.clear()
    RESEARCH_TOPIC_META.clear()
    RESEARCH_SOURCES.clear()
    RESEARCH_RUN_INPUTS.clear()
    RESEARCH_WORK.clear()
    TURN_STATUS.clear()
    TURN_RECORDS.clear()
    with _ACTIVE_SQUAD_LOCK:
        for entry in _ACTIVE_SQUAD_RUNS.values():
            orch = entry.get("orchestrator")
            if orch is not None and callable(getattr(orch, "cancel", None)):
                orch.cancel()
        _ACTIVE_SQUAD_RUNS.clear()
    EARNINGS_SQUAD_STATES.clear()
    EARNINGS_SQUAD_CHECKPOINTS.clear()
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
    PLATFORM_SECRETS.clear()
    ADMIN_ELEVATIONS.clear()
    ADMIN_ATTEMPTS.clear()
    platform_secret_store.invalidate()
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
    # Terminal operation status alone cannot establish that a provider request
    # was not submitted. Explicit known-unsent failure paths release their own
    # reservation; unknown or settled usage remains for reconciliation.
    active = operations.active_operation_ids()
    for operation_id in tuple(OPERATION_SESSIONS):
        if operation_id not in active:
            OPERATION_SESSIONS.pop(operation_id, None)
    for operation_id, _challenge in tuple(CHALLENGE_OPERATION_RESULTS.items_for_retention()):
        if operations.store.get_operation(operation_id) is None:
            CHALLENGE_OPERATION_RESULTS.delete(operation_id)
    research_live_broker.prune_expired()
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


def _model_provider_for_request(request: Request, *, research: bool = False) -> tuple[LlmProvider, bool]:
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
    return _model_provider_for_account(account, request, research=research)


def _model_provider_for_account(account: LocalAccount, request: Request, *, research: bool) -> tuple[LlmProvider, bool]:
    request.state.model_account = account
    try:
        settings, resolved_config = model_settings.runtime_configuration(account.account_id)
        request.state.resolved_model_config = resolved_config
    except ModelCredentialError as error:
        raise ApiProblem(error.status_code, error.code, error.message, retryable=error.status_code == 503, retry_after=30) from error
    if settings is None:
        request.state.personal_model = False
        if research:
            protocol = os.environ.get('FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL', 'openai_chat_completions')
            if protocol == 'anthropic_messages':
                if not os.environ.get('FORESIGHTNINE_RESEARCH_LLM_CREDENTIAL_REVISION', '').strip():
                    raise ApiProblem(503, 'internal_error', 'Research credential revision is required for safe recovery.')
                try:
                    settings = OpenAICompatibleSettings.from_values(
                        base_url=os.environ.get('FORESIGHTNINE_RESEARCH_LLM_BASE_URL'),
                        model=os.environ.get('FORESIGHTNINE_RESEARCH_LLM_MODEL'),
                        api_key=os.environ.get('FORESIGHTNINE_RESEARCH_LLM_API_KEY'),
                        allowlist=os.environ.get('FORESIGHTNINE_RESEARCH_LLM_ALLOWED_HOSTS'))
                except ProviderFailure:
                    raise ApiProblem(503, 'internal_error', 'Research model configuration is unavailable.') from None
                return _messages_research_provider(settings), False
            if protocol != 'openai_chat_completions':
                raise ApiProblem(503, 'internal_error', 'Research model protocol is unsupported.')
        return challenge_provider, False
    request.state.personal_model = True
    if resolved_config.protocol in ('anthropic_messages', 'openai_responses') and not research:
        raise ApiProblem(422, 'invalid_request', 'This model protocol is available for research only.')
    return _personal_model_provider(settings, resolved_config, research=research), True


def _personal_model_provider(settings: OpenAICompatibleSettings, config: StoredModelConfig, *, research: bool):
    if config.protocol == 'anthropic_messages':
        allowed = {url.strip().rstrip('/') for url in os.environ.get('FORESIGHTNINE_MESSAGES_PERSONAL_BASE_URL', '').split(',') if url.strip()}
        if allowed and settings.base_url not in allowed:
            raise ApiProblem(422, 'invalid_request', 'This Messages endpoint is outside the configured personal endpoint permissions.')
        if config.messages_options is None:
            raise ApiProblem(422, 'invalid_request', 'Configure the Messages version, authentication scheme and output limit in model settings.')
    if research and not settings.tools_verified:
        raise ApiProblem(422, 'invalid_request', 'Verify research tools for this model in model settings.')
    if config.protocol == 'openai_responses':
        from .openai_responses import OpenAIResponsesProvider
        return OpenAIResponsesProvider(settings)
    if config.protocol == 'anthropic_messages':
        from .anthropic_messages import AnthropicMessagesProvider
        from .openai_compatible import _llm_timeout_from_environment
        from .phase3 import ProviderPolicy
        options = config.messages_options
        policy = ProviderPolicy(max_output_tokens=options.max_tokens, timeout_seconds=_llm_timeout_from_environment())
        return AnthropicMessagesProvider(settings, policy, anthropic_version=options.version,
                                        auth_scheme=options.auth_scheme, max_tokens=options.max_tokens, effort=options.effort)
    return OpenAICompatibleLlmProvider(settings)


def _messages_research_provider(settings: OpenAICompatibleSettings) -> Any:
    from .anthropic_messages import AnthropicMessagesProvider
    from .phase3 import ProviderPolicy
    try:
        maximum = int(os.environ['FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS'])
        version = os.environ['FORESIGHTNINE_MESSAGES_VERSION'].strip()
        auth = os.environ['FORESIGHTNINE_MESSAGES_AUTH_SCHEME'].strip()
        timeout = float(os.environ.get('FORESIGHTNINE_RESEARCH_LLM_TIMEOUT_SECONDS', '20'))
        import math
        if maximum <= 0 or not version or not auth or not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise ValueError()
        if os.environ.get('FORESIGHTNINE_LLM_CAPABILITY_PROFILES', '').strip():
            from .anthropic_messages import _endpoint
            from .provider_capabilities import configured_profile
            profile = configured_profile(_endpoint(settings.base_url), settings.model, protocol='anthropic_messages')
            if not profile or profile.version != version or not (profile.native_tools and profile.continuation):
                raise ValueError()
            stream_enabled = profile.stream is True
        elif (os.environ.get('FORESIGHTNINE_MESSAGES_NATIVE_TOOLS') != 'true'
                or os.environ.get('FORESIGHTNINE_MESSAGES_CONTINUATION') != 'true'
                or os.environ.get('FORESIGHTNINE_MESSAGES_CAPABILITY_BASE_URL', '').rstrip('/') != settings.base_url
                or os.environ.get('FORESIGHTNINE_MESSAGES_CAPABILITY_MODEL') != settings.model
                or os.environ.get('FORESIGHTNINE_MESSAGES_CAPABILITY_VERSION') != version):
            raise ValueError()
        else:
            stream_enabled = os.environ.get('FORESIGHTNINE_MESSAGES_STREAM') == 'true'
    except (KeyError, ValueError, ProviderFailure):
        raise ApiProblem(503, 'internal_error', 'Messages research requires an explicit output limit, version, authentication scheme and native tools capability.') from None
    _warn_if_research_timeout_mismatch(maximum, timeout, source="messages_provider")
    try:
        return AnthropicMessagesProvider(settings, ProviderPolicy(max_output_tokens=maximum, timeout_seconds=timeout),
                                         anthropic_version=version, auth_scheme=auth, max_tokens=maximum,
                                         stream_enabled=stream_enabled)
    except ProviderFailure:
        raise ApiProblem(503, 'internal_error', 'Messages research configuration is invalid.') from None


def _reserve_model_call(request: Request, operation_id: str, *, session_id: str | None = None, token_budget: int | None,
                        provider_model: str | None = None) -> None:
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
    if token_budget == 0:
        # A deterministic local mock has no model usage and needs no ledger reservation.
        return
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
        if token_budget is None:
            raise ApiProblem(503, "billing_reservation_unknown", "Unable to bound the account token reservation.", retryable=False)
        try:
            billing_ledger.reserve(account.account_id, reservation_id, token_budget, now=lifecycle_clock.now(), provider_model=provider_model)
        except BillingError as error:
            raise ApiProblem(error.status_code, error.code, error.message, retryable=error.status_code in {502, 503}, retry_after=error.retry_after) from error
        request.state.billing_account_id = account.account_id
        request.state.billing_reservation_id = reservation_id
        request.state.billing_token_budget = token_budget


def _settle_model_call(request: Request, operation_id: str, total_tokens: int | None) -> None:
    account_id = getattr(request.state, "billing_account_id", None)
    reservation_id = getattr(request.state, "billing_reservation_id", f"model:{operation_id}")
    if not account_id:
        if total_tokens is not None:
            request.state.model_submission_started = False
        return
    if total_tokens is None:
        # Keep the reservation unresolved; never invent actual consumption.
        return
    try:
        actual_tokens = max(0, int(total_tokens))
        billing_ledger.settle(reservation_id, actual_tokens, now=lifecycle_clock.now())
        request.state.model_submission_started = False
    except BillingError as error:
        raise ApiProblem(error.status_code, error.code, error.message, retryable=error.status_code in {502, 503}, retry_after=error.retry_after) from error


def _release_model_call(request: Request) -> None:
    reservation_id = getattr(request.state, "billing_reservation_id", None)
    if reservation_id and not getattr(request.state, 'model_submission_started', False):
        billing_ledger.release(reservation_id, now=lifecycle_clock.now(), reverse_settled=True)


def _prepare_scenario_model_call(request: Request, operation_id: str, session_id: str | None,
                                 provider: Any, prompt: str, system: str) -> int | None:
    def remaining() -> int | None:
        account = getattr(request.state, 'model_account', None)
        if account is None or getattr(request.state, 'personal_model', False):
            return None
        return billing_ledger.snapshot(account.account_id, now=lifecycle_clock.now())['quota_remaining']
    ctx = ResearchExecutionContext(
        budget=ResearchExecutionBudget(None, None, None, float('inf')), cost_limited=False,
        ensure_running=lambda: None, cancel_event=lambda: None,
        reserve=lambda call_id, tokens: _reserve_model_call(request, call_id, session_id=session_id, token_budget=tokens),
        settle=lambda *_: None, reserve_search=lambda: None, entitlement_remaining=remaining)
    reservation, output_cap = ctx.prepare_model_call(provider, prompt, system)
    ctx.start_model(operation_id, reservation)
    return output_cap


def _generate_challenge_with_budget(provider: Any, request: Request, operation_id: str,
                                    session_id: str | None, challenge_input: ChallengeInput):
    if isinstance(provider, OpenAICompatibleLlmProvider):
        return provider.generate_challenge(challenge_input,
            prepare_call=lambda prompt, system: _prepare_scenario_model_call(request, operation_id, session_id, provider, prompt, system),
            on_submission=lambda: setattr(request.state, 'model_submission_started', True))
    if getattr(provider, 'name', None) == 'deterministic_mock':
        _reserve_model_call(request, operation_id, session_id=session_id, token_budget=0)
        return provider.generate_challenge(challenge_input)
    account = getattr(request.state, 'model_account', None)
    if account is not None and not getattr(request.state, 'personal_model', False):
        raise ProviderFailure('provider_budget_unknown', 'Provider output usage cannot be bounded.', retryable=False)
    _reserve_model_call(request, operation_id, session_id=session_id, token_budget=None)
    request.state.model_submission_started = True
    return provider.generate_challenge(challenge_input)


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
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "Last-Event-ID", "X-Captcha-Token", "X-Session-ID", ELEVATION_HEADER],
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
    from .research_discovery import RedirectLead, normalize_search_lead
    try:
        lead = normalize_search_lead(raw)
    except RedirectLead:
        raise
    except (ProviderFailure, ValueError):
        raise ProviderFailure("search_response_invalid", "Search provider returned an invalid candidate.", retryable=False) from None
    title, url, excerpt = lead.title, lead.url, lead.excerpt
    publisher, published_at, independently_reviewable = lead.publisher, lead.published_at, lead.independently_reviewable
    if excerpt is None or independently_reviewable is None:
        raise ProviderFailure("search_response_invalid", "Search provider returned an invalid candidate.", retryable=False)
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
        **({"publisher": publisher} if publisher is not None else {}),
        **({"published_at": published_at} if published_at is not None else {}),
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
    from .research_discovery import RedirectLead
    _reserve_search_call(request)
    try:
        candidates = []
        for item in search(query, limit=limit):
            try:
                candidates.append(_candidate_response(item, provider_name))
            except RedirectLead:
                continue
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
    if error.code == "provider_request_rejected":
        from .research_diagnostics import log_provider_rejection

        log_provider_rejection("http_endpoint", error)
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
    for key, _ in owner_items(TURN_STATUS, owner_id):
        TURN_STATUS.delete(key)
    for repo in (RESEARCH_WORK, TURN_RECORDS):
        for key, record in list(repo.items()):
            if record.get("account_id") == owner_id:
                repo.delete(key)
    for key, record in list(RESEARCH_RUN_INPUTS.items()):
        if record.get("owner_id") == owner_id:
            RESEARCH_RUN_INPUTS.delete(key)
    for repo in (EARNINGS_SQUAD_STATES, EARNINGS_SQUAD_CHECKPOINTS):
        for key, record in list(repo.items()):
            if isinstance(record, Mapping) and record.get("account_id") == owner_id:
                repo.delete(key)
    with _ACTIVE_SQUAD_LOCK:
        for key in [k for k in _ACTIVE_SQUAD_RUNS if k[0] == owner_id]:
            entry = _ACTIVE_SQUAD_RUNS.pop(key, None)
            if entry and entry.get("orchestrator") is not None:
                entry["orchestrator"].cancel()


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
            auth_provider=ticket.provider,
        )
        return OAuthSessionResponse(**{**session, "provider": ticket.provider})
    except OAuthExchangeTicketError as exception:
        raise ApiProblem(400, "bad_request", "OAuth exchange is unavailable.") from exception


def _authenticated_account_session(authorization: str | None) -> tuple[LocalAccount, LocalSession]:
    try:
        return authenticate_local(
            ACCOUNTS,
            AUTH_SESSIONS,
            authorization,
            now=lifecycle_clock.now(),
        )
    except LocalAuthError as error:
        raise _local_auth_problem(error) from error


def _authenticated_account(authorization: str | None) -> LocalAccount:
    account, _session = _authenticated_account_session(authorization)
    return account


app.include_router(
    create_admin_router(
        admin_gate,
        platform_secret_store,
        _authenticated_account_session,
        ApiProblem,
    ),
    include_in_schema=False,
)


@app.post("/v2/research/topics", response_model=ResearchTopic, status_code=201, include_in_schema=False)
def create_research_topic(request: ResearchInput, authorization: str | None = Header(default=None)) -> ResearchTopic:
    account = _authenticated_account(authorization)
    try:
        for source_id in request.source_ids:
            research_sources.get(account.account_id, source_id)
    except SourceNotFound as error:
        raise ApiProblem(404, "not_found", "Research source not found.") from error
    return research_topics.create(account.account_id, request)


def _fact_review_flags(fact: ResearchFact) -> dict[str, bool]:
    """Narrow lexical cues about a monetary fact's currency and scale; not source verification."""
    data = fact.model_dump(mode="json")
    flags: dict[str, bool] = {}
    if monetary_currency_unspecified(data):
        flags["currency_unspecified"] = True
    if monetary_currency_not_in_quotation(data):
        flags["currency_quote_missing"] = True
    if monetary_scale_not_in_quotation(data):
        flags["scale_quote_missing"] = True
    return flags


def _legacy_calculation_diagnostics(version):
    """Annotate a copy on read; never rewrite an immutable saved version."""
    flagged = {fact.fact_id: flags for fact in version.content.facts if (flags := _fact_review_flags(fact))}
    legacy = [legacy_percentage_difference_unit(item.model_dump(mode="json")) for item in version.content.calculations]
    if not flagged and not any(legacy):
        return version
    version = version.model_copy(deep=True)
    for fact in version.content.facts:
        for name, value in flagged.get(fact.fact_id, {}).items():
            setattr(fact, name, value)
    for item, is_legacy in zip(version.content.calculations, legacy):
        if is_legacy:
            item.legacy_unit_warning = "percentage_difference_requires_recalculation"
        if any(input_fact.fact_id in flagged for input_fact in item.inputs):
            item.input_status = "unverifiable"
    return version


def _version_source_hashes(version) -> dict[str, str]:
    content = version.content
    # Attached, unread sources have no historical citation to resolve. For used
    # sources a missing or ambiguous version binding cannot authorize old offsets.
    used = {f.source_id for f in content.facts}
    used.update(c.source_id for claim in content.claims for c in claim.citations)
    used.update(r.source_id for turn in content.turns for r in turn.read_ranges)
    used.update(sid for turn in content.turns for analysis in turn.market_analyses for sid in analysis.result["source_ids"])
    used.update(sid for artifact in [*content.charts, *content.tables] for sid in artifact.source_ids)
    hashes: dict[str, str] = {}
    for row in content.sources:
        sid, digest = row.get("source_id"), row.get("content_sha256")
        if isinstance(sid, str):
            hashes[sid] = digest if isinstance(digest, str) and hashes.get(sid, digest) == digest else ""
    for turn in content.turns:
        for review in turn.semantic_reviews:
            for item in review.evidence:
                if item.kind != 'source':
                    continue
                used.add(item.source_id)
                if hashes.get(item.source_id) != item.content_sha256:
                    hashes[item.source_id] = ""
    return {**dict.fromkeys(used, ""), **hashes}


def _can_read_version_source(source, cutoff, expected_hash: str | None) -> bool:
    return bool(source and source.permissions.display and not source.is_simulated
                and (expected_hash is None or expected_hash == source.content_sha256)
                and (cutoff is None or source.published_at is not None and source.published_at <= cutoff
                     and (source.revised_at is None or source.revised_at <= cutoff)))


def _version_reader(topic: ResearchTopic):
    # Different versions can bind different hashes of the same source.
    current = {}

    def read(version):
        hashes = _version_source_hashes(version)
        def can_display(source_id: str) -> bool:
            if source_id not in current:
                try:
                    current[source_id] = research_sources.get(topic.owner_id, source_id)
                except SourceNotFound:
                    current[source_id] = None
            return _can_read_version_source(current[source_id], topic.request.as_of, hashes.get(source_id))

        content = visible_answer_content(version.content, can_display)
        if content is not version.content:
            version = version.model_copy(update={"content": content})
        return _legacy_calculation_diagnostics(version)

    return read


def _topic_squad_metadata(owner_id: str, topic_id: str) -> dict[str, Any]:
    row = EARNINGS_SQUAD_STATES.get(_squad_state_key(owner_id, topic_id))
    if not isinstance(row, Mapping):
        return {}
    dossier_raw = row.get("dossier") if isinstance(row.get("dossier"), Mapping) else {}
    return {
        "run_mode": row.get("run_mode") or "earnings_squad",
        "squad_run_id": row.get("run_id"),
        "ticker": row.get("ticker") or dossier_raw.get("symbol"),
        "cik": row.get("cik") or dossier_raw.get("cik"),
    }


def _visible_topic(topic: ResearchTopic) -> ResearchTopic:
    read = _version_reader(topic)
    updates: dict[str, Any] = {"versions": [read(version) for version in topic.versions]}
    squad_meta = _topic_squad_metadata(topic.owner_id, topic.topic_id)
    if squad_meta:
        updates.update(squad_meta)
    return topic.model_copy(update=updates)


@app.get("/v2/research/topics", response_model=list[ResearchTopic], include_in_schema=False)
def list_research_topics(authorization: str | None = Header(default=None)) -> list[ResearchTopic]:
    account = _authenticated_account(authorization)
    return [_visible_topic(topic) for topic in research_topics.list(account.account_id)]


@app.get("/v2/research/topics/{topic_id}", response_model=ResearchTopic, include_in_schema=False)
def get_research_topic(topic_id: str, authorization: str | None = Header(default=None)) -> ResearchTopic:
    account = _authenticated_account(authorization)
    try:
        return _visible_topic(research_topics.get(account.account_id, topic_id))
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
    return _version_reader(topic)(version).model_dump(mode="json")


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
    hashes = _version_source_hashes(version)
    result = []
    for source_id in version.source_ids:
        try:
            source = research_sources.get(account.account_id, source_id)
        except SourceNotFound:
            result.append({"source_id": source_id, "status": "unavailable"})
            continue
        if not _can_read_version_source(source, topic.request.as_of, hashes.get(source_id)):
            result.append({"source_id": source_id, "status": "unavailable"})
            continue
        metadata = source.model_dump(mode="json", exclude={"text", "owner_id", "url", "original_pdf_base64", "original_html_base64"})
        is_html = source.retrieval_method in _HTML_RETRIEVAL_METHODS
        if source.retrieval_method in _PDF_RETRIEVAL_METHODS:
            metadata["original_pdf_available"] = bool(source.original_pdf_base64)
        if is_html:
            metadata["original_html_available"] = bool(source.original_html_base64)
        if pdf_text_spacing_suspect(source):
            metadata["pdf_text_spacing_warning"] = True
        if source.url:
            parts = urlsplit(str(source.url))
            metadata["url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        metadata["status"] = "available"
        metadata["verification"] = "user_declared_not_verified"
        if is_html:
            metadata["extraction_disclosure"] = HTML_EXTRACTION_DISCLOSURE
        result.append(metadata)
    return result


@app.get("/v2/research/topics/{topic_id}/versions/{version_id}/sources/{source_id}/text", include_in_schema=False)
def get_research_version_source_text(topic_id: str, version_id: str, source_id: str,
                                     authorization: str | None = Header(default=None)) -> dict:
    """Read attached source text only when display and research cutoff permit it."""
    account = _authenticated_account(authorization)
    try:
        topic = research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error
    version = next((item for item in topic.versions if item.version_id == version_id), None)
    if version is None:
        raise ApiProblem(404, "not_found", "Research version not found.")
    if source_id not in version.source_ids:
        raise ApiProblem(404, "not_found", "Research source not found.")
    try:
        source = research_sources.get(account.account_id, source_id)
    except SourceNotFound as error:
        raise ApiProblem(404, "not_found", "Research source not found.") from error
    if not _can_read_version_source(source, topic.request.as_of, _version_source_hashes(version).get(source_id)):
        raise ApiProblem(403, "invalid_request", "Source text is not available for this research version.")
    return {"source_id": source.source_id, "text": source.text, "content_sha256": source.content_sha256}


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
    for row in version.content.sources:
        if isinstance(row, dict) and isinstance(row.get("source_id"), str):
            source_ids.add(row["source_id"])
    source_ids.update(fact.source_id for fact in version.content.facts)
    source_ids.update(citation.source_id for claim in version.content.claims for citation in claim.citations)
    source_ids.update(item.source_id for turn in version.content.turns for item in turn.read_ranges)
    evidence = AnswerEvidence(version.content)
    answer_sources = {sid for analysis in evidence.analyses.values() for sid in analysis.result["source_ids"]}
    try:
        for turn in version.content.turns:
            for part in turn.question_parts:
                for ref in part_references(part):
                    answer_sources.update(evidence.source_ids(ref))
    except KeyError as error:
        raise ApiProblem(403, "invalid_request", "A research answer references unavailable evidence.") from error
    answer_sources.update(item.source_id for turn in version.content.turns
                          for review in turn.semantic_reviews for item in review.evidence if item.kind == 'source')
    source_ids.update(answer_sources)
    chart_source_ids: set[str] = set()
    for chart in version.content.charts:
        chart_source_ids.update(chart.source_ids)
        for chart_series in chart.series:
            for point in chart_series.points:
                chart_source_ids.add(point.provenance.source_id)
    source_ids.update(chart_source_ids)
    table_source_ids = {source_id for table in version.content.tables for source_id in table.source_ids}
    table_source_ids.update(cell.provenance.source_id for table in version.content.tables
                            for row in table.rows for cell in row.cells.values() if cell.provenance)
    source_ids.update(table_source_ids)
    sources = []
    hashes = _version_source_hashes(version)
    try:
        for source_id in sorted(source_ids):
            source = research_sources.get(account.account_id, source_id)
            if not source.permissions.export:
                raise ApiProblem(403, "invalid_request", "A research source is not permitted for export.")
            if not _can_read_version_source(source, topic.request.as_of, hashes.get(source_id)):
                if source_id in hashes or source_id in answer_sources | table_source_ids | chart_source_ids:
                    raise ApiProblem(403, "invalid_request", "A research source is unavailable for display in this version.")
                # An attached but unused source contributes no text or findings.
                sources.append({"source_id": source_id, "status": "unavailable"})
                continue
            metadata = source.model_dump(mode="json", exclude={"text", "owner_id", "url", "original_pdf_base64", "original_html_base64"})
            if pdf_text_spacing_suspect(source):
                metadata["pdf_text_spacing_warning"] = True
            if source.url:
                parts = urlsplit(str(source.url))
                metadata["url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            metadata["verification"] = "user_declared_not_verified"
            if source.retrieval_method in _HTML_RETRIEVAL_METHODS:
                metadata["extraction_disclosure"] = HTML_EXTRACTION_DISCLOSURE
            sources.append(metadata)
    except SourceNotFound as error:
        raise ApiProblem(403, "invalid_request", "A research source is unavailable for export.") from error
    return ResearchFullExport(topic_id=topic_id, generated_at=lifecycle_clock.now(), request=topic.request,
                              version=_legacy_calculation_diagnostics(version), source_provenance=sources)


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
        calculation_count=len(version.content.calculations), gap_count=len(version.content.gaps),
        turn_count=len(version.content.turns))


_CALCULATION_COMPUTED_FIELDS = {"input_status", "legacy_unit_warning", "result_status"}
_FACT_COMPUTED_FIELDS = {"currency_quote_missing", "scale_quote_missing", "currency_unspecified"}


def _fact_identity(fact: ResearchFact) -> dict:
    return fact.model_dump(mode="json", exclude=_FACT_COMPUTED_FIELDS)


def _calculation_identity(calculation: ResearchCalculation) -> dict:
    return calculation.model_dump(mode="json", exclude=_CALCULATION_COMPUTED_FIELDS)


@app.post("/v2/research/topics/{topic_id}/versions/{version_id}/restore", response_model=ResearchTopic, status_code=201, include_in_schema=False)
def restore_research_version(topic_id: str, version_id: str, request: VersionRestoreInput,
                             authorization: str | None = Header(default=None)) -> ResearchTopic:
    """Append a copy of an earlier version (for example a replaced answer) as the newest version.

    The server copies the stored content, so verification records survive; history only grows.
    """
    account = _authenticated_account(authorization)
    try:
        topic = research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error
    source = next((item for item in topic.versions if item.version_id == version_id), None)
    if source is None:
        raise ApiProblem(404, "not_found", "Research version not found.")
    if _topic_has_active_run(account.account_id, topic_id):
        raise ApiProblem(409, "lifecycle_conflict", "Stop the running research before restoring a version.")
    head = topic.versions[-1]
    if head.version_id != request.parent_version_id:
        raise ApiProblem(409, "lifecycle_conflict", "Research changed; reload before restoring.")
    number = topic.versions.index(source) + 1
    try:
        updated = research_topics.add_version(account.account_id, topic_id, VersionInput(
            parent_version_id=head.version_id, content=source.content, review="draft", note=f"恢复为 v{number} 的内容"),
            created_by="user")
    except TopicVersionConflict as error:
        raise ApiProblem(409, "lifecycle_conflict", "Research changed; reload before restoring.") from error
    return _visible_topic(updated)


@app.post("/v2/research/topics/{topic_id}/versions", response_model=ResearchTopic, status_code=201, include_in_schema=False)
def create_research_version(topic_id: str, request: VersionInput, authorization: str | None = Header(default=None)) -> ResearchTopic:
    account = _authenticated_account(authorization)
    try:
        topic = research_topics.get(account.account_id, topic_id)
        previous = topic.versions[-1]
        if previous.version_id != request.parent_version_id:
            raise TopicVersionConflict()
        # A user may change findings, facts, and calculations, but an edited
        # item must not masquerade as an untouched model extraction. Turns,
        # source inventory, leads and gaps are server-produced records.
        content = request.content.model_copy(deep=True)
        before = previous.content
        if "sources" not in request.content.model_fields_set:
            content.sources = deepcopy(before.sources)
        if (content.turns != before.turns or content.sources != before.sources or
                content.source_candidates != before.source_candidates or content.gaps != before.gaps or
                content.charts != before.charts or content.tables != before.tables):
            raise ApiProblem(422, "invalid_request", "Server-produced research records cannot be edited.")
        allowed_sources = set(previous.source_ids)
        loaded: dict[str, ResearchSource] = {}

        def usable_source(source_id: str, message: str) -> ResearchSource:
            if source_id not in allowed_sources:
                raise ApiProblem(422, "invalid_request", message)
            try:
                if source_id not in loaded:
                    loaded[source_id] = research_sources.get(account.account_id, source_id)
                source = loaded[source_id]
                if (source.is_simulated or not source.permissions.display or topic.request.as_of is not None and
                        (source.published_at is None or source.published_at > topic.request.as_of or
                         (source.revised_at is not None and source.revised_at > topic.request.as_of))):
                    raise ValueError("Source availability at the research cutoff is not established.")
            except (SourceNotFound, ValueError) as error:
                raise ApiProblem(422, "invalid_request", message) from error
            return source

        def check_span(source_id: str, quotation: str, start: int, end: int, message: str) -> None:
            source = usable_source(source_id, message)
            if not quotation.strip() or end > len(source.text) or source.text[start:end] != quotation:
                raise ApiProblem(422, "invalid_request", message)

        # Findings: unchanged claims pass untouched; changed ones are rechecked and marked.
        claim_ids = [claim.claim_id for claim in content.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ApiProblem(422, "invalid_request", "Research findings or their citations are invalid.")
        parent_claims = {claim.claim_id: claim for claim in before.claims}
        id_map: dict[str, str] = {}
        claims = []
        changed_claims = []
        for claim in content.claims:
            original = parent_claims.get(claim.claim_id)
            if original is not None and claim == original:
                claims.append(original.model_copy(deep=True))
                continue
            for citation in claim.citations:
                check_span(citation.source_id, citation.quotation, citation.location.character_start,
                           citation.location.character_end, "Citation must match an attached, displayable, time-eligible source.")
            if original is None:
                id_map[claim.claim_id] = "claim-" + uuid4().hex
            saved = claim.model_copy(update={"claim_id": id_map.get(claim.claim_id, claim.claim_id),
                                             "turn_id": original.turn_id if original is not None else None,
                                             "edited_by_user": True, "scenario_calculation": None})
            claims.append(saved)
            changed_claims.append(saved)
        final_ids = {claim.claim_id for claim in claims}
        for claim in changed_claims:
            claim.challenges_claim_ids = [id_map.get(item, item) for item in claim.challenges_claim_ids]
            if any(item not in final_ids or item == claim.claim_id for item in claim.challenges_claim_ids):
                raise ApiProblem(422, "invalid_request", "A challenged finding must be another existing finding.")
        content.claims = claims

        fact_ids = [fact.fact_id.strip() for fact in content.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ApiProblem(422, "invalid_request", "Research fact identifiers must be unique.")
        parent_facts = {fact.fact_id: fact for fact in before.facts}
        facts = []
        for fact in content.facts:
            original = parent_facts.get(fact.fact_id)
            if original is not None and _fact_identity(fact) == _fact_identity(original):
                facts.append(original.model_copy(deep=True))
                continue
            try:
                numeric = NumericFact.model_validate({"fact_id": fact.fact_id, "value": fact.value, "unit": fact.unit,
                                                      "currency": fact.currency, "period": fact.period})
            except ValidationError as error:
                raise ApiProblem(422, "invalid_request", "Research fact value, unit, currency, or period is invalid.") from error
            if len(fact.label) > 300 or len(fact.quotation) > 1000 or len(fact.source_id) > 100:
                raise ApiProblem(422, "invalid_request", "Research fact provenance is invalid.")
            if (fact.location.character_start != fact.character_start or fact.location.character_end != fact.character_end):
                raise ApiProblem(422, "invalid_request", "Research fact location is inconsistent.")
            check_span(fact.source_id, fact.quotation, fact.character_start, fact.character_end,
                       "Research fact must use an attached, displayable, time-eligible real source.")
            quote_has_value = (numeric.value in _quoted_numbers(fact.quotation) or
                               _directional_negative_percent(numeric.value, numeric.unit, fact.quotation) or
                               _directional_negative_amount(numeric.value, numeric.unit, fact.quotation, numeric.currency))
            saved = fact.model_copy(update={
                "turn_id": original.turn_id if original is not None else None,
                # Consistency records are server-derived and must never survive
                # an edit or be accepted from a newly supplied fact.
                "quotation_match": original.quotation_match if original is not None else None,
                "table_alignment_unverified": original.table_alignment_unverified if original is not None else None,
                "table_structure_alignment": None,
                "xbrl_source_consistency": None,
                "verification": ("user_edited_not_independently_verified" if quote_has_value
                                 else "user_edited_value_not_in_quotation")})
            data = saved.model_dump(mode="json")
            saved.currency_unspecified = True if monetary_currency_unspecified(data) else None
            saved.currency_quote_missing = True if (
                (not is_percent_unit(saved.unit) and table_currency_quote_missing(saved.currency, saved.quotation)) or
                monetary_currency_not_in_quotation(data)) else None
            saved.scale_quote_missing = True if monetary_scale_not_in_quotation(data) else None
            facts.append(saved)
        content.facts = facts

        calculation_ids = [item.calculation_id for item in content.calculations]
        if len(calculation_ids) != len(set(calculation_ids)):
            raise ApiProblem(422, "invalid_request", "Research calculation identifiers must be unique.")
        parent_calculations = {item.calculation_id: item for item in before.calculations}
        calculations = []
        for calculation in content.calculations:
            original = parent_calculations.get(calculation.calculation_id)
            if original is not None and _calculation_identity(calculation) == _calculation_identity(original):
                calculations.append(original.model_copy(deep=True))
                continue
            calculations.append(calculation.model_copy(update={
                "turn_id": original.turn_id if original is not None else None, "legacy_unit_warning": None,
                "authorship": "user", "verification": "user_edited_not_independently_verified"}))
        fact_rows = [fact.model_dump(mode="json") for fact in content.facts]
        for entry in calculations:
            entry.input_status = calculation_input_status(entry.model_dump(mode="json"), fact_rows)
        content.calculations = calculations
        invalidate_edited_answers(before, content)
        existing = {row.get("source_id") for row in content.sources}
        for sid, source in loaded.items():
            if sid not in existing:
                content.sources.append({"source_id": sid, "content_sha256": source.content_sha256})
        return _visible_topic(research_topics.add_version(account.account_id, topic_id, request.model_copy(update={"content": content})))
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
        return _visible_topic(research_topics.add_version(account.account_id, topic_id, VersionInput(
            parent_version_id=request.parent_version_id, content=previous.content,
            review="draft", note=previous.note), source_ids=request.source_ids))
    except (TopicNotFound, SourceNotFound) as error:
        raise ApiProblem(404, "not_found", "Research topic or source not found.") from error
    except TopicVersionConflict as error:
        raise ApiProblem(409, "lifecycle_conflict", "Research topic has changed; reload before saving.") from error
    except ValueError as error:
        raise ApiProblem(422, "invalid_request", str(error)) from error


def _reject_unconfirmable_fact(topic_id: str, request: ConfirmDirectionRequest, authorization: str | None) -> ResearchTopic:
    """Direction and table confirmation need legacy table metadata that native facts never carry."""
    account = _authenticated_account(authorization)
    try:
        topic = research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error
    if topic.versions[-1].version_id != request.parent_version_id:
        raise ApiProblem(409, "lifecycle_conflict", "Research topic has changed; reload before saving.")
    raise ApiProblem(422, "invalid_request", "This figure has no table metadata to confirm.")


@app.post("/v2/research/topics/{topic_id}/facts/confirm-direction", response_model=ResearchTopic,
          status_code=201, include_in_schema=False)
def confirm_research_direction(topic_id: str, request: ConfirmDirectionRequest,
                               authorization: str | None = Header(default=None)) -> ResearchTopic:
    return _reject_unconfirmable_fact(topic_id, request, authorization)


@app.post("/v2/research/topics/{topic_id}/facts/confirm-table", response_model=ResearchTopic,
          status_code=201, include_in_schema=False)
def confirm_research_table(topic_id: str, request: ConfirmDirectionRequest,
                           authorization: str | None = Header(default=None)) -> ResearchTopic:
    return _reject_unconfirmable_fact(topic_id, request, authorization)


def _research_calculation_inputs(content: ResearchContent, fact_ids: list[str]) -> list[dict]:
    """Resolve version-owned inputs and apply the same checks for both actions."""
    facts = []
    for fact_id in fact_ids:
        candidates = [fact for fact in content.facts if fact.fact_id == fact_id]
        if len(candidates) != 1:
            raise ApiProblem(422, "invalid_request", "Calculation input facts are missing or ambiguous.")
        fact = candidates[0].model_dump(mode="json")
        if fact.get("verification") == "user_edited_value_not_in_quotation":
            raise ApiProblem(422, "invalid_request", "The fact's source quotation requires review before calculation.")
        if currency_value_conflict(fact.get("currency"), fact.get("value"), fact.get("quotation")):
            raise ApiProblem(422, "invalid_request", "Currency and quoted value require review before calculation.")
        if monetary_currency_unspecified(fact):
            raise ApiProblem(422, "invalid_request", "The monetary fact's currency is unspecified; review it before calculation.")
        if monetary_currency_not_in_quotation(fact):
            raise ApiProblem(422, "invalid_request", "The monetary fact's currency is not identified in its quotation; review it before calculation.")
        if monetary_scale_not_in_quotation(fact):
            raise ApiProblem(422, "invalid_request", "The monetary fact's unit scale is not identified in its quotation; review it before calculation.")
        if is_percent_unit(str(fact.get("unit", ""))) and fact.get("currency") is not None:
            raise ApiProblem(422, "invalid_request", "Percentage currency context requires review before calculation.")
        facts.append({key: fact[key] for key in ("fact_id", "value", "unit", "currency", "period")})
    return facts


@app.post("/v2/research/topics/{topic_id}/calculations", response_model=ResearchTopic,
          status_code=201, include_in_schema=False)
def create_research_calculation(topic_id: str, request: CreateResearchCalculationRequest,
                                authorization: str | None = Header(default=None)) -> ResearchTopic:
    account = _authenticated_account(authorization)
    try:
        topic = research_topics.get(account.account_id, topic_id)
        previous = topic.versions[-1]
        if previous.version_id != request.parent_version_id:
            raise TopicVersionConflict()
        if len(previous.content.calculations) >= 2000:
            raise ApiProblem(422, "invalid_request", "The research calculation limit has been reached.")
        facts = _research_calculation_inputs(previous.content, [request.first_fact_id, request.second_fact_id])
        spec = CalculationRequest.model_validate({"formula": request.formula, "first": facts[0], "second": facts[1],
                                                  "decimal_places": request.decimal_places})
        result = calculate(spec)
        if calculation_input_status(result, [fact.model_dump(mode="json") for fact in previous.content.facts]) != "matched":
            raise ApiProblem(422, "invalid_request", "Calculation input evidence requires review.")
        result.update({"calculation_id": "calc-" + uuid4().hex, "turn_id": None,
                       "input_status": "matched", "authorship": "calculation_engine"})
        content = previous.content.model_copy(deep=True)
        content.calculations.append(ResearchCalculation.model_validate(result))
        return _visible_topic(research_topics.add_version(account.account_id, topic_id, VersionInput(
            parent_version_id=previous.version_id, content=content, note=previous.note)))
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error
    except TopicVersionConflict as error:
        raise ApiProblem(409, "lifecycle_conflict", "Research topic has changed; reload before calculating.") from error
    except (ValidationError, CalculationError) as error:
        raise ApiProblem(422, "invalid_request", "Calculation cannot be performed with the current facts.") from error


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
        if old.formula_id == 'contribution_to_change_percent':
            raise ApiProblem(422, 'invalid_request', 'This calculation must be repeated through the research evidence tool.')
        if old.formula_version not in {"1", "2"}:
            raise ApiProblem(422, "invalid_request", "Calculation inputs or formula version are unavailable.")
        if old.formula_id in BINARY_FORMULAS:
            if len(old.inputs) != 2:
                raise ApiProblem(422, "invalid_request", "Calculation inputs or formula version are unavailable.")
        elif old.formula_id in MULTI_FACT_FORMULAS:
            if not (2 <= len(old.inputs) <= 50):
                raise ApiProblem(422, "invalid_request", "Calculation inputs or formula version are unavailable.")
        else:
            raise ApiProblem(422, "invalid_request", "Calculation inputs or formula version are unavailable.")
        facts = _research_calculation_inputs(previous.content, [item.fact_id for item in old.inputs])
        spec_payload: dict[str, Any] = {
            "formula": old.formula_id,
            "decimal_places": old.decimal_places,
        }
        if old.formula_id in BINARY_FORMULAS:
            spec_payload["first"] = facts[0]
            spec_payload["second"] = facts[1]
        else:
            spec_payload["facts"] = facts
        if old.periods is not None:
            spec_payload["periods"] = old.periods
        if old.weights is not None:
            spec_payload["weights"] = old.weights
        spec = CalculationRequest.model_validate(spec_payload)
        result = calculate(spec)
        if calculation_input_status(result, [fact.model_dump(mode="json") for fact in previous.content.facts]) != "matched":
            raise ApiProblem(422, "invalid_request", "Calculation input evidence requires review.")
        result.update({"calculation_id": old.calculation_id, "turn_id": old.turn_id,
                       "input_status": "matched", "authorship": "calculation_engine"})
        content = previous.content.model_copy(deep=True)
        content.calculations[request.calculation_index] = ResearchCalculation.model_validate(result)
        return _visible_topic(research_topics.add_version(account.account_id, topic_id, VersionInput(
            parent_version_id=previous.version_id, content=content, note=previous.note)))
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error
    except TopicVersionConflict as error:
        raise ApiProblem(409, "lifecycle_conflict", "Research topic has changed; reload before calculating.") from error
    except (ValidationError, CalculationError) as error:
        raise ApiProblem(422, "invalid_request", "Calculation cannot be performed with the current facts.") from error


@app.post("/v2/research/calculations", include_in_schema=False)
def calculate_research(request: PublicCalculationRequest, authorization: str | None = Header(default=None)) -> dict:
    _authenticated_account(authorization)
    if request.formula == 'contribution_to_change_percent':
        raise ApiProblem(422, 'invalid_request', 'This calculation requires the research evidence tool.')
    try:
        return calculate(request)
    except CalculationError as error:
        raise ApiProblem(422, "invalid_request", str(error)) from error


def _visible_research_source(source: ResearchSource) -> dict:
    result = source.model_dump(mode="json", exclude={"original_pdf_base64", "original_html_base64"})
    is_html = source.retrieval_method in _HTML_RETRIEVAL_METHODS
    if source.retrieval_method in _PDF_RETRIEVAL_METHODS:
        result["original_pdf_available"] = bool(source.original_pdf_base64)
    if is_html:
        result["original_html_available"] = bool(source.original_html_base64)
    if pdf_text_spacing_suspect(source):
        result["pdf_text_spacing_warning"] = True
    if not source.permissions.display:
        result.pop("text", None)
    # User-supplied provenance and permissions are declarations, not proof.
    result["verification"] = "user_declared_not_verified"
    if is_html:
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


@app.post("/v2/research/sources/html", status_code=201, include_in_schema=False)
def upload_research_html(request: HtmlUploadRequest, authorization: str | None = Header(default=None)) -> dict:
    account = _authenticated_account(authorization)
    try:
        text, raw_hash = extract_uploaded_html(request)
        source = research_sources.create(account.account_id, SourceInput(title=request.title, text=text,
            published_at=request.published_at, event_at=request.event_at, revised_at=request.revised_at,
            is_simulated=request.is_simulated, permissions=request.permissions), retrieval_method="user_html",
            response_sha256=raw_hash, response_content_type="text/html", original_html_base64=request.html_base64)
        metadata = _visible_research_source(source)
        metadata.pop("text", None)
        return metadata
    except PdfFailure as error:
        if error.code == "html_busy":
            raise ApiProblem(503, "internal_error", "HTML extraction is busy.", retryable=True) from error
        raise ApiProblem(422, "invalid_request", "HTML is invalid, too large, not UTF-8, or has no extractable text.") from error
    except SourcePermissionError as error:
        raise ApiProblem(422, "invalid_request", "HTML is invalid, too large, not UTF-8, or has no extractable text.") from error


@app.post("/v2/research/sources/upload", status_code=201, include_in_schema=False)
async def upload_research_document_stream(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """Stream a large PDF, HTML or text document (multipart) to disk and ingest it with the paged pipeline."""
    from starlette.concurrency import run_in_threadpool
    from .research_fetch import large_document_max_bytes
    from .research_upload_stream import (
        MAX_FIELD_BYTES, MAX_FORM_FIELDS, UPLOAD_WORKERS, HashingFileSink, UploadFormError, check_form_items,
        ingest_uploaded_file, parse_stream_fields, sniff_upload_kind,
    )
    account = _authenticated_account(authorization)  # before reading any body bytes
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise ApiProblem(415, "unsupported_media_type", "Multipart form data is required.")
    cap = large_document_max_bytes()
    # Starlette spools file parts to disk but does not cap their size, so count the receive stream: an oversized
    # chunked upload stops here without being buffered.
    original_receive = request._receive
    received = 0

    async def limited_receive() -> Any:
        nonlocal received
        message = await original_receive()
        if message.get("type") == "http.request":
            received += len(message.get("body", b""))
            if received > cap + 1_048_576:
                raise BodyTooLarge
        return message

    request._receive = limited_receive
    try:
        form = await request.form(max_files=1, max_fields=MAX_FORM_FIELDS, max_part_size=MAX_FIELD_BYTES)
    except BodyTooLarge as error:
        raise ApiProblem(413, "request_too_large", "The document exceeds the upload size limit.") from error
    except StarletteHTTPException as error:
        raise ApiProblem(422, "invalid_request", "The upload form is invalid or incomplete.") from error
    except Exception as error:
        if type(error).__name__ in {"MultiPartException", "MalformedMultipartError"}:
            raise ApiProblem(422, "invalid_request", "The upload form is invalid or incomplete.") from error
        raise
    try:
        try:
            raw_fields, upload = check_form_items(list(form.multi_items()))
            fields = parse_stream_fields(raw_fields)
        except UploadFormError as error:
            raise ApiProblem(422, "invalid_request", "The upload form is invalid or incomplete.") from error
        filename = getattr(upload, "filename", None)
        with tempfile.TemporaryDirectory(prefix="foresight-upload-") as directory:
            def spool_to_disk() -> HashingFileSink:
                sink = HashingFileSink(Path(directory) / "upload.bin")
                try:
                    upload.file.seek(0)
                    while True:
                        block = upload.file.read(1 << 20)
                        if not block:
                            break
                        sink.write(block)
                finally:
                    sink.close()
                return sink

            sink = await run_in_threadpool(spool_to_disk)
            if sink.size > cap:
                raise ApiProblem(413, "request_too_large", "The document exceeds the upload size limit.")
            path, digest, size = sink.path, sink.hexdigest(), sink.size

            def ingest() -> dict:
                kind = sniff_upload_kind(path, filename)
                # Vision OCR needs a research run to reserve and settle usage; an upload outside a run has
                # no billing hooks to bind, so only local OCR engines run here.
                ingested = ingest_uploaded_file(path, kind=kind, title=fields.title, permissions=fields.permissions,
                                                sha256_hex=digest, size=size, ocr_usage=None)
                doc = ingested.document
                provenance = {"document_pages": doc.pages} if doc.pages else {}
                if kind == "pdf":
                    created = research_sources.create(account.account_id, SourceInput(title=fields.title, text=doc.text,
                        published_at=fields.published_at, event_at=fields.event_at, revised_at=fields.revised_at,
                        is_simulated=fields.is_simulated, permissions=fields.permissions), retrieval_method="user_pdf",
                        response_sha256=digest, response_content_type="application/pdf",
                        original_pdf_base64=ingested.original_base64, pdf_text_order=fields.text_order,
                        provenance=provenance)
                else:
                    created = research_sources.create(account.account_id, SourceInput(title=fields.title, text=doc.text,
                        published_at=fields.published_at, event_at=fields.event_at, revised_at=fields.revised_at,
                        is_simulated=fields.is_simulated, permissions=fields.permissions), retrieval_method="user_html",
                        response_sha256=digest, response_content_type=doc.content_type,
                        original_html_base64=ingested.original_base64, provenance=provenance)
                metadata = _visible_research_source(created)
                metadata.pop("text", None)
                return metadata

            if not UPLOAD_WORKERS.acquire(blocking=False):
                raise ApiProblem(503, "internal_error", "Document extraction is busy.", retryable=True, retry_after=5)
            try:
                return await run_in_threadpool(ingest)
            except FetchFailure as error:
                if error.code == "source_unavailable":
                    raise ApiProblem(503, "internal_error", "Document extraction is unavailable.", retryable=True) from error
                if error.code == "ocr_required":
                    raise ApiProblem(422, "invalid_request", "The PDF has no usable text layer and no OCR engine produced text for it.") from error
                if error.code == "source_too_large":
                    raise ApiProblem(422, "invalid_request", "The document text exceeds the supported size.") from error
                if error.code == "source_format_unsupported":
                    raise ApiProblem(422, "invalid_request", "Only PDF, HTML or plain text files are supported.") from error
                raise ApiProblem(422, "invalid_request", "The document is invalid, encrypted, or has no extractable text.") from error
            except SourcePermissionError as error:
                raise ApiProblem(422, "invalid_request", "Source storage permission is required.") from error
            finally:
                UPLOAD_WORKERS.release()
    finally:
        await form.close()


@app.post("/v2/research/sources/{source_id}/reextract", status_code=201, include_in_schema=False)
def reextract_research_pdf(source_id: str, request: PdfReextractRequest, response: Response,
                           authorization: str | None = Header(default=None),
                           idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=128)] = None) -> dict:
    """Create a separate source from the owner's retained PDF and its original permissions."""
    account = _authenticated_account(authorization)
    try:
        original = research_sources.get(account.account_id, source_id)
    except SourceNotFound as error:
        raise ApiProblem(404, "not_found", "Research source not found.") from error
    if original.retrieval_method != "user_pdf" or not original.original_pdf_base64:
        raise ApiProblem(422, "invalid_request", "The retained original PDF is unavailable.")
    title = original.title[:470] + (" — layout" if request.text_order == "layout" else " — reading")
    stable_id = ("source_" + sha256(f"pdf-reextract-v1\0{account.account_id}\0{idempotency_key}".encode()).hexdigest()[:32]
                 if idempotency_key is not None else None)

    def create_or_replay() -> tuple[str, Any]:
        if stable_id:
            existing = research_sources.repository.get(stable_id)
            if existing is not None:
                if (existing.owner_id == account.account_id and existing.derived_from_source_id == original.source_id and
                        existing.pdf_text_order == request.text_order and
                        (original.response_sha256 is None or existing.response_sha256 == original.response_sha256)):
                    return "replayed", existing
                return "conflict", None
        try:
            document = extract_pdf(PdfUploadRequest(title=title, pdf_base64=original.original_pdf_base64,
                text_order=request.text_order, permissions=original.permissions))
            if original.response_sha256 and original.response_sha256 != document.original_sha256:
                return "invalid_hash", None
            source = research_sources.create(account.account_id, SourceInput(title=title, text=document.text,
                url=original.url, kind=original.kind, is_simulated=original.is_simulated,
                published_at=original.published_at, event_at=original.event_at, revised_at=original.revised_at,
                permissions=original.permissions), retrieval_method="user_pdf", response_sha256=document.original_sha256,
                response_content_type="application/pdf", original_pdf_base64=document.original_base64,
                pdf_text_order=request.text_order, derived_from_source_id=original.source_id, source_id=stable_id)
            return "created", source
        except (PdfFailure, SourcePermissionError, ValidationError) as error:
            # Return expected failures through the storage lock so PostgreSQL
            # does not wrap a user-facing error as a storage failure.
            return "error", error

    outcome, result = (research_sources.repository.with_advisory_lock("pdf-reextract:" + stable_id, create_or_replay)
                       if stable_id else create_or_replay())
    if outcome == "conflict":
        raise ApiProblem(409, "idempotency_conflict", "This key was used for a different PDF extraction.")
    if outcome == "invalid_hash":
        raise ApiProblem(422, "invalid_request", "The retained original PDF does not match its recorded hash.")
    if outcome == "error":
        if isinstance(result, PdfFailure) and result.code in {"pdf_extractor_unavailable", "pdf_timeout", "pdf_busy"}:
            raise ApiProblem(503, "internal_error", "PDF extraction is unavailable or timed out.", retryable=True) from result
        raise ApiProblem(422, "invalid_request", "The retained PDF could not be extracted.") from result
    _mark_replay(response, outcome == "replayed")
    metadata = _visible_research_source(result)
    metadata.pop("text", None)
    return metadata


@app.get("/v2/research/sources/{source_id}/original", include_in_schema=False)
def get_research_source_original(source_id: str, authorization: str | None = Header(default=None)) -> Response:
    account = _authenticated_account(authorization)
    try:
        source = research_sources.get(account.account_id, source_id)
    except SourceNotFound as error:
        raise ApiProblem(404, "not_found", "Research source not found.") from error
    raw = source.original_pdf_base64 or source.original_html_base64
    is_html = source.retrieval_method in _HTML_RETRIEVAL_METHODS
    is_pdf = source.retrieval_method in _PDF_RETRIEVAL_METHODS
    if not raw or not source.permissions.display or not source.permissions.export or (not is_html and not is_pdf):
        raise ApiProblem(403, "invalid_request", "The original document is not available for download.")
    import base64
    return Response(content=base64.b64decode(raw), media_type="application/octet-stream" if is_html else "application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="source.html"' if is_html else 'attachment; filename="source.pdf"',
                             "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


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
    is_pdf = source.retrieval_method in _PDF_RETRIEVAL_METHODS
    if is_pdf:
        # Context stays on the relevant page(s), while retaining an exact
        # quote that itself crosses a page boundary.
        left = max(left, source.text.rfind("\f", 0, start) + 1)
        next_page = source.text.find("\f", end)
        if next_page >= 0:
            right = min(right, next_page)
    table_context = table_context_window(source.text, start, end) if source.retrieval_method in _HTML_RETRIEVAL_METHODS else None
    return {"source_id": source.source_id, "title": source.title, "content_sha256": source.content_sha256,
            "start": start, "end": end, "context_start": left, "context_end": right,
            "before": source.text[left:start], "selected": source.text[start:end], "after": source.text[end:right],
            **({"table_context": table_context} if table_context else {}),
            "page_start": source.text[:start].count("\f") + 1 if is_pdf else None,
            "page_end": source.text[:end].count("\f") + 1 if is_pdf else None}


@app.get("/v2/research/sources/{source_id}/table-structure", include_in_schema=False)
def get_research_table_structure(source_id: str, start: Annotated[int, Query(ge=0, le=2_000_000)],
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
    return bounded_table_trace(source, start, end)


@app.post("/v2/research/sources/fetch", status_code=201, include_in_schema=False)
def fetch_research_source(request: FetchSourceRequest, http_request: Request) -> dict:
    account = _authenticated_account(http_request.headers.get("Authorization"))
    if not request.permissions.store:
        raise ApiProblem(422, "invalid_request", "Source storage permission is required.")
    try:
        fetch_host = validate_public_https_url(str(request.url), allow_fragment=False, check_sec_path=False)
    except ValueError as error:
        raise ApiProblem(422, "invalid_request", "Only supported public HTTPS sources are available.") from error
    if is_sec_host(fetch_host):
        if fetch_host != "www.sec.gov":
            raise ApiProblem(422, "invalid_request", "Only direct www.sec.gov SEC filing URLs are supported by fetch.")
        parts = urlsplit(str(request.url))
        sec_url = urlunsplit(("https", "www.sec.gov", parts.path, parts.query, ""))
        try:
            validate_public_https_url(sec_url, allow_fragment=False, check_sec_path=True)
        except ValueError as error:
            raise ApiProblem(422, "invalid_request", "Only direct www.sec.gov SEC filing URLs are supported by fetch.") from error
        sec_request = request if str(request.url) == sec_url else request.model_copy(update={"url": HttpUrl(sec_url)})
        try:
            agent = validate_user_agent(os.environ.get("FORESIGHTNINE_SEC_USER_AGENT") or os.environ.get("SEC_USER_AGENT"))
            _reserve_search_call(http_request)
            if not reserve_sec_request(SOURCE_RATE_LIMITS):
                raise ApiProblem(429, "rate_limited", "SEC request limit reached; retry later.", retryable=True, retry_after=1)
            data = fetch_sec_filing(sec_request, agent)
            source = research_sources.create(account.account_id, SourceInput(title=sec_request.title, text=data.text,
                url=sec_request.url, kind="official", permissions=sec_request.permissions, published_at=sec_request.published_at,
                event_at=sec_request.event_at, revised_at=sec_request.revised_at, is_simulated=sec_request.is_simulated),
                retrieval_method="sec_filing_html", response_sha256=data.response_sha256, response_content_type="text/html",
                original_html_base64=data.original_base64)
            result = _visible_research_source(source)
            result.pop("text", None)
            return result
        except SecFailure as error:
            if error.code == "sec_contact_unconfigured":
                raise ApiProblem(503, "internal_error", "SEC source contact is not configured.", retryable=False) from error
            if error.code == "source_rate_limited":
                raise ApiProblem(429, "rate_limited", "SEC source rate limited.", retryable=True, retry_after=1) from error
            if error.code in {"source_timeout", "source_unavailable"}:
                raise ApiProblem(503, "internal_error", "SEC source unavailable or timed out.", retryable=True) from error
            raise ApiProblem(422, "invalid_request", "SEC filing data could not be used.") from error
    _reserve_search_call(http_request)
    try:
        requested_url = canonical_source_url(str(request.url))
        requested_host = (urlsplit(requested_url).hostname or "").lower().rstrip(".")
        # The user pasted this URL: a personal-mode host (terms 'user_initiated_only') may be fetched as this single document.
        with user_initiated_scope(requested_url):
            if fetch_official_text is not _DEFAULT_FETCH_OFFICIAL_TEXT:
                fetched = fetch_official_text(request)
                final_url = requested_url
                redirect_count = 0
            else:
                fetched = fetch_source_document(request)
                final_url = canonical_source_url(fetched.final_url)
                redirect_count = fetched.redirect_count
        final_host = (urlsplit(final_url).hostname or "").lower().rstrip(".")
        is_official = requested_host in PUBLIC_DOMAIN_OFFICIAL_HOSTS and final_host in PUBLIC_DOMAIN_OFFICIAL_HOSTS
        content_type = fetched.content_type
        if content_type == "application/pdf":
            retrieval_kwargs: dict[str, Any] = {
                "retrieval_method": "user_pdf" if is_official else "web_pdf",
                "original_pdf_base64": getattr(fetched, "original_pdf_base64", None),
                "pdf_text_order": "layout",
            }
        else:
            retrieval_kwargs = {
                "retrieval_method": "official_html" if is_official else "web_html",
                "original_html_base64": getattr(fetched, "original_html_base64", None),
            }
        provenance: dict[str, Any] = {}
        if not is_official:
            provenance = {
                "final_url": final_url,
                "web_retrieval": {
                    "requested_url": requested_url,
                    "final_url": final_url,
                    "redirect_count": redirect_count,
                    "content_type": content_type,
                },
            }
        elif final_url != requested_url:
            provenance = {"final_url": final_url}
        initiated = user_initiated_basis(requested_url, "user")
        if initiated is not None:
            provenance = {**provenance, **initiated}
        source = research_sources.create(
            account.account_id,
            SourceInput(
                title=request.title,
                text=fetched.text,
                url=final_url if getattr(fetched, "final_url", None) else request.url,
                kind="official" if is_official else "user_supplied",
                permissions=request.permissions,
                published_at=request.published_at,
                event_at=request.event_at,
                revised_at=request.revised_at,
                is_simulated=request.is_simulated,
            ),
            **retrieval_kwargs,
            **({"provenance": provenance} if provenance else {}),
            response_sha256=fetched.response_sha256,
            response_content_type=content_type,
        )
        return _visible_research_source(source)
    except FetchFailure as error:
        if error.code == "source_rate_limited":
            raise ApiProblem(429, "rate_limited", "Source temporarily rate limited.", retryable=True) from error
        if error.code in {"source_timeout", "source_unavailable"}:
            raise ApiProblem(503, "internal_error", "Source unavailable or timed out.", retryable=True) from error
        if error.code == "source_blocked_terms":
            raise ApiProblem(422, "invalid_request", "This site's terms do not permit automated access; upload the document instead.") from error
        if error.code == "source_blocked_policy":
            raise ApiProblem(422, "invalid_request", "Source host or target is blocked by fetch policy.") from error
        if error.code == "http_forbidden_or_challenge":
            raise ApiProblem(422, "invalid_request", "Upstream site rejected the request (401/403 or challenge).") from error
        if error.code == "not_found":
            raise ApiProblem(422, "invalid_request", "Source URL was not found (404/410).") from error
        if error.code == "source_too_large":
            raise ApiProblem(422, "invalid_request", "Source exceeded the 5 MB download or text size limit.") from error
        raise ApiProblem(422, "invalid_request", "Source could not be used in the requested format.") from error


@app.get("/v2/research/capabilities", include_in_schema=False)
def get_research_capabilities(authorization: str | None = Header(default=None)) -> dict:
    """Which source capabilities this deployment has configured; no configuration values or secrets."""
    from .research_fin_tools import FinDataToolbox
    account = _authenticated_account(authorization)
    _, sec_available = _configured_sec_user_agent()
    search_available = bool(getattr(evidence_provider, "supports_project_search", False)) or bool(default_news_providers(None))
    allowed_hosts = fetch_allowed_hosts()
    _market = _resolve_market_key(account.account_id)
    _tushare = _resolve_tushare_key(account.account_id)
    _fmp = _resolve_fmp_key(account.account_id)
    _fin_box = FinDataToolbox(
        account_id=account.account_id, market_key=_market[0], credential_owner=_market[1] or 'user_byok',
        sec_user_agent=os.environ.get("FORESIGHTNINE_SEC_USER_AGENT") or os.environ.get("SEC_USER_AGENT"),
        tushare_key=_tushare[0], tushare_owner=_tushare[1] or 'user_byok',
        fmp_key=_fmp[0], fmp_owner=_fmp[1] or 'user_byok',
        persist=lambda *_a, **_k: None,
    )
    return {
        "schema_version": "v1",
        "search_available": search_available,
        "web_fetch_enabled": not bool(allowed_hosts),
        "sec_available": sec_available,
        # Structured data tools for this account: market_data uses the account's own Twelve Data key, or the
        # platform key only when the fin_data policy allows it; SEC tools follow the configured SEC contact.
        **_fin_box.capabilities(),
        **_fin_box.earnings_capabilities(),
        "sources": [asdict(item) for item in source_capabilities(search_available=search_available, sec_available=sec_available)],
    }


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


def _stored_form(value: Any) -> Any:
    """Normalize a value the way the PostgreSQL repository returns it (null fields dropped).

    Saved run inputs come back without null-valued keys, so comparing them with freshly built
    values must normalize both sides; otherwise every continuation looks changed (363/364).
    """
    return _drop_legacy_null_fields(value)


def _research_source_fingerprints(sources: list[ResearchSource]) -> list[dict[str, Any]]:
    return [{"source_id": item.source_id, "content_sha256": item.content_sha256,
             "pdf_text_order": item.pdf_text_order, "derived_from_source_id": item.derived_from_source_id,
             "event_at": item.event_at.isoformat() if item.event_at else None,
             "published_at": item.published_at.isoformat() if item.published_at else None,
             "obtained_at": item.obtained_at.isoformat(),
             "revised_at": item.revised_at.isoformat() if item.revised_at else None,
             "is_simulated": item.is_simulated, "permissions": item.permissions.model_dump(mode="json")}
            for item in sources]


def _runtime_provider_binding(provider: Any, request: Request) -> str:
    from .research_runtime import SYSTEM, TOOLS
    from dataclasses import asdict
    settings = getattr(provider, 'settings', None)
    profile = getattr(provider, 'research_capacity', lambda: None)()
    account = getattr(request.state, 'model_account', None)
    config = getattr(request.state, 'resolved_model_config', None) if account else None
    # A platform-mode record may retain an inactive personal configuration; only
    # an active personal configuration describes the provider actually bound.
    personal = config if getattr(config, 'mode', None) == 'personal' else None
    parameters = os.environ.get('FORESIGHTNINE_LLM_GENERATION_PARAMETERS')
    if personal:
        parameters = getattr(provider, 'generation_parameters_json', lambda: None)() or None
    elif os.environ.get('FORESIGHTNINE_LLM_CAPABILITY_PROFILES', '').strip():
        parameters = getattr(provider, 'generation_parameters_json', lambda: None)()
    payload = {'provider': getattr(provider, 'name', None), 'protocol': getattr(personal, 'protocol', None) or os.environ.get('FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL', 'openai_chat_completions'), 'endpoint': getattr(settings, 'base_url', None),
               'model': getattr(settings, 'model', None), 'policy': repr(getattr(provider, 'policy', None)),
               'profile': repr(profile), 'adapter_binding': getattr(provider, 'research_capacity_key', lambda: None)(), 'protocol_version': getattr(provider, 'anthropic_version', None), 'auth_scheme': getattr(provider, 'auth_scheme', None), 'output_limit': getattr(provider, 'max_tokens', None), 'parameters': parameters,
               'account_model_mode': getattr(config, 'mode', None),
               'credential_revision': str(getattr(config, 'updated_at', None)),
               'credential_hint': getattr(personal, 'key_hint', None),
               'platform_credential_revision': None if personal else os.environ.get('FORESIGHTNINE_RESEARCH_LLM_CREDENTIAL_REVISION') if getattr(provider, 'name', None) == 'anthropic_messages' else os.environ.get('FORESIGHTNINE_LLM_CREDENTIAL_REVISION'),
               'system': SYSTEM, 'tools': [asdict(tool) for tool in TOOLS]}
    return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


_OFFICIAL_FETCH_STATUS_PRIORITY = {
    "not_requested": 0,
    "no_eligible_source": 1,
    "empty": 1,
    "question_missing": 2,
    "empty_selection": 2,
    "historical_cutoff": 2,
    "cost_unknown": 2,
    "source_capacity": 2,
    "time_budget": 2,
    "restricted": 3,
    "unavailable": 3,
    "unconfigured": 3,
    "sec_contact_unconfigured": 3,
    "web_retrieved": 4,
    "unknown": 5,
    "failed": 6,
    "partial": 7,
    "retrieved": 8,
    "completed": 8,
}


def _merge_official_fetch_status(current: str, next_status: str | None, *, auto_fetch_official: bool) -> str:
    if not isinstance(next_status, str) or not next_status:
        return current
    if not auto_fetch_official and current == "not_requested" and next_status not in {"completed", "retrieved", "partial"}:
        return "not_requested"
    if next_status == "partial" and current in {"completed", "retrieved"}:
        return "partial"
    if _OFFICIAL_FETCH_STATUS_PRIORITY.get(next_status, 0) >= _OFFICIAL_FETCH_STATUS_PRIORITY.get(current, 0):
        return next_status
    return current


class ResearchStartResponse(OperationResponse):
    turn_id: str
    run_mode: Literal["earnings_squad", "legacy_single_loop"] = "legacy_single_loop"
    squad_run_id: str | None = None
    ticker: str | None = None
    cik: str | None = None
    entity_id: str | None = None
    company_name: str | None = None
    routing_reason: str | None = None


class ResearchRunListEntry(OperationResponse):
    turn_id: str | None = None
    current_operation_id: str | None = None
    logical_status: Literal["running", "awaiting_user", "completed", "stopped", "failed"] | None = None
    run_mode: Literal["earnings_squad", "legacy_single_loop"] | None = None
    squad_run_id: str | None = None
    ticker: str | None = None
    cik: str | None = None
    entity_id: str | None = None
    company_name: str | None = None


class ResearchTurnSummary(BaseModel):
    topic_id: str
    turn_id: str
    current_operation_id: str
    logical_status: Literal["running", "awaiting_user", "completed", "stopped", "failed"]
    version_id: str | None = None
    run_mode: Literal["earnings_squad", "legacy_single_loop"] | None = None
    squad_run_id: str | None = None
    ticker: str | None = None
    cik: str | None = None


class ResearchTurnStopped(BaseModel):
    turn_id: str
    status: Literal["stopped"]


class ResearchInstructionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)


def _saved_instructions(doc: dict, state: Mapping, content: ResearchContent) -> list[SavedResearchInstruction]:
    reviews = {row['sequence']: row for row in (state.get('submission') or {}).get('instruction_reviews', [])}
    saved = []
    ids = {'claim_id': {item.claim_id for item in content.claims}, 'fact_id': {item.fact_id for item in content.facts},
           'calculation_id': {item.calculation_id for item in content.calculations}}
    instructions = _instruction_view(doc)
    instruction_text = {row['sequence']: row['text'] for row in instructions}
    turn = content.turns[-1]
    for item in instructions:
        review = reviews.get(item['sequence'], {})
        refs = [ref for ref in review.get('references', [])
                if ref and all(k in ids and value in ids[k] for k, value in ref.items())]
        status = review.get('status', 'unresolved')
        locations = []
        for ref in review.get('references', []):
            field = ref.get('field')
            text = (getattr(turn, field) if field in {'report', 'partial_notes'}
                    else instruction_text.get(ref.get('instruction_sequence')))
            start, end = ref.get('start'), ref.get('end')
            if (text and type(start) is int and type(end) is int and 0 <= start < end <= len(text)
                    and text[start:end] == ref.get('excerpt')):
                locations.append(ref)
        if status in {'linked_for_review', 'supersession_needs_review'} and not (refs or locations):
            status = 'unresolved'
        saved.append(SavedResearchInstruction(**item, delivery_status=status, text_locations=locations,
                                              reason=review.get('reason'), references=refs))
    return saved


def _update_turn_status(topic_id: str, operation_id: str, status: str, version_id: str | None = None) -> None:
    def update() -> None:
        summary = TURN_STATUS.get(topic_id)
        if summary and summary['current_operation_id'] == operation_id:
            TURN_STATUS.put(topic_id, {**summary, 'logical_status': status, 'version_id': version_id})
    TURN_STATUS.with_advisory_lock('turn-status:' + topic_id, update)


@app.get('/v2/research/turn-statuses', response_model=list[ResearchTurnSummary], response_model_exclude_none=True, include_in_schema=False)
def get_latest_turn_statuses(http_request: Request) -> list[dict]:
    account = _authenticated_account(http_request.headers.get('Authorization'))
    keys = ('topic_id', 'turn_id', 'current_operation_id', 'logical_status', 'version_id',
            'run_mode', 'squad_run_id', 'ticker', 'cik')
    return [{key: row.get(key) for key in keys}
            for _, row in owner_items(TURN_STATUS, account.account_id) if row['owner_id'] == account.account_id]


def _instruction_view(doc: dict) -> list[dict]:
    if not doc['sequence']:
        return []
    if 'instructions' not in doc:
        doc = turn_instructions.read(doc['account_id'], doc['topic_id'], doc['turn_id'])
    return [
        ResearchInstruction(**{k: row[k] for k in ("instruction_id", "turn_id", "operation_id", "sequence", "text", "received_at")},
                            status=("included" if row["sequence"] <= doc["acknowledged_sequence"] else
                                    "awaiting_receipt" if row["sequence"] <= doc.get("awaiting_sequence", 0) else
                                    "received")).model_dump(mode="json")
        for row in doc["instructions"]
    ]


@app.post("/v1/research/topics/{topic_id}/runs", response_model=OperationResponse, response_model_exclude_none=True, status_code=202, include_in_schema=False)
@app.post("/v2/research/topics/{topic_id}/runs", response_model=ResearchStartResponse, response_model_exclude_none=True, status_code=202, include_in_schema=False)
def start_research_run(topic_id: str, request: ResearchRunRequest, response: Response,
                       idempotency_key: IdempotencyKey, http_request: Request) -> OperationResponse:
    account = _authenticated_account(http_request.headers.get("Authorization"))
    return _start_research_for_account(topic_id, request, response, idempotency_key, http_request, account)


def _start_response_from_inputs(operation: Operation, stored: Mapping[str, Any]) -> ResearchStartResponse:
    turn_id = str((stored.get("turn") or {}).get("turn_id") or "")
    if stored.get("run_mode") == "earnings_squad":
        return ResearchStartResponse(
            operation=operation,
            turn_id=turn_id,
            run_mode="earnings_squad",
            squad_run_id=stored.get("squad_run_id"),
            ticker=stored.get("ticker"),
            cik=stored.get("cik"),
            entity_id=stored.get("entity_id"),
            company_name=stored.get("company_name"),
            routing_reason=stored.get("routing_reason"),
        )
    return ResearchStartResponse(operation=operation, turn_id=turn_id)


def _start_research_for_account(topic_id: str, request: ResearchRunRequest, response: Response,
                                idempotency_key: str, http_request: Request, account: LocalAccount,
                                *, background: bool = False) -> OperationResponse:
    continuation_input = getattr(http_request.state, 'research_continuation', None)
    resuming_checkpoint = continuation_input is not None and not continuation_input.get('unstarted')
    try:
        topic = research_topics.get(account.account_id, topic_id)
        available_ids = list(dict.fromkeys([*topic.request.source_ids, *topic.versions[-1].source_ids, *request.source_ids]))
        if request.selected_source_ids is not None and not set(request.selected_source_ids).issubset(available_ids):
            raise ApiProblem(422, "invalid_request", "Selected sources must belong to this research request.")
        source_ids = (list(continuation_input['source_ids']) if continuation_input is not None else
                      available_ids if request.selected_source_ids is None else list(request.selected_source_ids))
        # Additional IDs have always required account ownership, even if a
        # selection excludes them from this particular model input.
        for source_id in request.source_ids:
            research_sources.get(account.account_id, source_id)
        sources = [research_sources.get(account.account_id, source_id) for source_id in source_ids]
    except (TopicNotFound, SourceNotFound) as error:
        raise ApiProblem(404, "not_found", "Research topic or source not found.") from error
    user_question = request.question_override if request.question_override is not None else topic.request.question
    question_origin = 'user' if user_question else 'document_overview_default'
    effective_question = user_question or DOCUMENT_OVERVIEW_QUESTION
    from .research_runtime import SYSTEM as RUNTIME_SYSTEM
    # Presence is part of authorization: omitted controls may be resolved
    # automatically, while explicit false, zero, null, or [] are caller input.
    # Historical keys with a different payload fail closed rather than being
    # replayed under a potentially different permission policy.
    run_request_payload = request.model_dump(mode='json', exclude_unset=True)
    existing = operations.replay_async(endpoint=f"/v1/research/topics/{topic_id}/runs", idempotency_key=idempotency_key,
        owner_subject=f"account:{account.account_id}", request_payload=run_request_payload)
    if existing is not None:
        if continuation_input is not None:
            replay_inputs = RESEARCH_RUN_INPUTS.get(existing.operation.operation_id) or {}
            if (replay_inputs.get('owner_id') != account.account_id or replay_inputs.get('topic_id') != topic_id
                    or replay_inputs.get('runtime_previous_operation_id') != continuation_input['operation_id']):
                raise ApiProblem(409, 'idempotency_conflict', 'The idempotency key belongs to another research run.')
        _mark_replay(response, True)
        stored = RESEARCH_RUN_INPUTS[existing.operation.operation_id]
        return _start_response_from_inputs(existing.operation, stored)
    parent_version = next((item for item in topic.versions if item.version_id == request.parent_version_id), None)
    if parent_version is None:
        raise ApiProblem(409, "lifecycle_conflict", "Research changed before the run was admitted.")
    # Regenerate / edit-and-resend: the run reads the version before the replaced turn, while the
    # newest version stays the concurrency anchor and receives the result as a new version.
    rebase_base: ResearchVersion | None = None
    if request.replace_turn_id is not None and (continuation_input is None or background):
        head = topic.versions[-1]
        if head.version_id != request.parent_version_id:
            raise ApiProblem(409, "lifecycle_conflict", "Research changed before the run was admitted.")
        replaced = head.content.turns[-1] if head.content.turns else None
        if replaced is None or replaced.turn_id != request.replace_turn_id:
            raise ApiProblem(409, "lifecycle_conflict", "Only the latest answer can be regenerated.")
        base_index = next((index for index, item in enumerate(topic.versions) if item.version_id == replaced.parent_version_id), None)
        if base_index is None:
            raise ApiProblem(409, "lifecycle_conflict", "The version before this answer is unavailable.")
        if not request.discard_review_edits and _review_edits_after(topic.versions, base_index):
            raise ApiProblem(409, "lifecycle_conflict", "Review edits made after this answer would not carry over; confirm to discard them.")
        rebase_base = topic.versions[base_index]
        parent_version = rebase_base
        if request.question_override is None:
            user_question = replaced.question
            question_origin = replaced.question_origin
            effective_question = replaced.question
    # A turn is one question plus its run. A continuation keeps the original turn;
    # a new turn needs a new question once the topic already has an answer.
    if continuation_input is not None:
        turn_meta = deepcopy((RESEARCH_RUN_INPUTS.get(continuation_input['operation_id']) or {}).get('turn') or {})
        if not turn_meta:
            raise ApiProblem(409, 'lifecycle_conflict', 'The saved runtime inputs are unavailable.')
    else:
        if request.question_override is None and parent_version.content.turns and rebase_base is None:
            raise ApiProblem(422, "invalid_request", "A follow-up question is required.")
        turn_meta = {'turn_id': 'turn-' + uuid4().hex, 'index': len(parent_version.content.turns) + 1,
                     'replaces_turn_id': request.replace_turn_id if rebase_base is not None else None,
                     'question': effective_question, 'question_origin': question_origin,
                     'focus_citation': request.focus_citation.model_dump() if request.focus_citation else None,
                     'started_at': lifecycle_clock.now().isoformat(), 'parent_version_id': request.parent_version_id,
                     'operation_ids': []}
    # Default-entry deterministic auto-routing to Scheme 2 US Earnings Buy-Side Squad.
    should_check_auto_squad = (
        continuation_input is None
        and request.replace_turn_id is None
        and request.focus_citation is None
        and request.max_model_calls != 0
        and request.max_total_tokens != 0
        and request.max_elapsed_seconds != 0
        and request.allow_auto_squad is not False
    )
    if should_check_auto_squad:
        sec_agent_raw, sec_ok = _configured_sec_user_agent()
        routing_sec = SecEdgarAdapter(user_agent=sec_agent_raw) if sec_ok and sec_agent_raw else None
        supplied_fields = request.model_fields_set
        sources_mode = derive_sources_mode(request, has_attached_sources=bool(source_ids))
        route_decision = detect_us_earnings_squad_route(
            effective_question,
            sources_mode=sources_mode,
            allow_auto_squad=request.allow_auto_squad is not False,
            sec_adapter=routing_sec,
            discover_sources=request.discover_sources if "discover_sources" in supplied_fields else None,
            auto_fetch_official=request.auto_fetch_official if "auto_fetch_official" in supplied_fields else None,
            selected_source_ids=request.selected_source_ids,
        )
        if route_decision.should_route:
            return _start_auto_routed_earnings_squad(
                topic_id=topic_id,
                topic=topic,
                request=request,
                run_request_payload=run_request_payload,
                response=response,
                idempotency_key=idempotency_key,
                http_request=http_request,
                account=account,
                effective_question=effective_question,
                question_origin=question_origin,
                turn_meta=turn_meta,
                route_decision=route_decision,
            )
    # Idempotency uses the caller's original request, while a newly admitted
    # operation persists the resolved policy. A continuation always retains
    # its original server-owned policy rather than applying today's defaults.
    # Structured data tools: the account's own market-data key (BYOK) and/or the SEC contact. The platform
    # price key is used only when the fin_data policy explicitly allows it (default policy denies it).
    market_key, market_owner = _resolve_market_key(account.account_id)
    tushare_key, tushare_owner = _resolve_tushare_key(account.account_id)
    fmp_key, fmp_owner = _resolve_fmp_key(account.account_id)
    historical_available = (market_key is not None or _configured_sec_user_agent()[1]
                            or _cn_data_possible(tushare_key)) and topic.request.as_of is None
    has_news_search = bool(getattr(evidence_provider, 'supports_project_search', False)) or bool(default_news_providers(None))
    if continuation_input is None:
        request = resolve_automatic_research_request(request,
            search_available=has_news_search,
            historical_prices_available=historical_available)
    provider, _personal = (_model_provider_for_account(account, http_request, research=True)
                           if background else _model_provider_for_request(http_request, research=True))
    identity = (account.account_id, topic_id, turn_meta['turn_id'])
    if isinstance(provider, OpenAICompatibleLlmProvider):
        try:
            provider = provider.for_research(request.max_input_tokens_per_call)
        except ValueError as error:
            raise ApiProblem(422, "invalid_request", "The input limit exceeds the configured research limit.") from error
    elif request.max_input_tokens_per_call is not None:
        if callable(getattr(provider, 'complete_turn', None)):
            from dataclasses import replace
            provider.policy = replace(provider.policy, max_input_tokens=request.max_input_tokens_per_call)
        else:
            raise ApiProblem(422, "invalid_request", "The selected provider does not support a per-run input limit.")
    generate_structured = getattr(provider, "generate_structured", None)
    version_chain = ResearchVersionChain(research_topics, account.account_id, topic_id, request.parent_version_id, source_ids)
    # A continuation restores the focus from its checkpoint; only a fresh turn resolves it.
    focus: dict[str, Any] | None = None
    if request.focus_citation is not None and not resuming_checkpoint:
        try:
            claim = next(c for c in parent_version.content.claims if c.claim_id == request.focus_citation.claim_id)
            citation = claim.citations[request.focus_citation.citation_index]
            source = next(s for s in sources if s.source_id == citation.source_id and
                          s.source_id in {*topic.request.source_ids, *parent_version.source_ids})
            eligible, _ = prepare_sources(sources, topic.request.as_of)
            start, end = citation.location.character_start, citation.location.character_end
            if source.source_id not in {s.source_id for s in eligible} or source.text[start:end] != citation.quotation:
                raise ValueError()
            focus = {'claim_id': claim.claim_id, 'source_id': source.source_id, 'quotation': citation.quotation,
                     'character_start': start, 'character_end': end}
        except (StopIteration, IndexError, KeyError, TypeError, ValueError) as error:
            raise ApiProblem(422, "invalid_request", "The selected quotation is unavailable for focused research.") from error

    # Bind this run's mutable source list to the shared, network-free policy.
    def source_summaries(analyzed_ids: list[str], segments: object) -> list[dict[str, Any]]:
        return summarize_sources(sources, analyzed_ids, segments)

    admitted_continuation: dict[str, Any] = {}
    last_runtime_state: dict[str, Any] = {}
    persister = ResearchTurnPersister(
        topic_id=topic_id,
        parent_version_id=request.parent_version_id,
        as_of=topic.request.as_of,
        sources=sources,
        version_chain=version_chain,
        turn_meta=turn_meta,
        merge_turn_fn=lambda *args, **kwargs: merge_turn(*args, **kwargs),
        source_summaries_fn=source_summaries,
        record_failure_fn=lambda op_id, state: _record_failed_checkpoint_version(op_id, state),
    )

    def build(operation_id: str) -> tuple[ResearchRunResult, str]:
        started = monotonic()
        deadline = started + request.max_elapsed_seconds if request.max_elapsed_seconds is not None else float('inf')
        budget = ResearchExecutionBudget(request.max_model_calls, request.max_search_calls,
                                         request.max_total_tokens, deadline)
        calls = 0
        search_calls = 0
        official_fetch_calls = 0
        official_fetch_status = "not_requested" if not request.auto_fetch_official else "no_eligible_source"
        if continuation_input is not None and parent_version.content.turns and parent_version.content.turns[-1].turn_id == turn_meta.get('turn_id'):
            official_fetch_status = _merge_official_fetch_status(
                official_fetch_status,
                parent_version.content.turns[-1].official_fetch_status,
                auto_fetch_official=request.auto_fetch_official,
            )
        discovered: list[dict[str, Any]] = []
        search_receipts: list[dict[str, Any]] = []
        discovery_notes: list[str] = []
        search_status = "not_requested"
        if (request.discover_sources is not False and request.max_search_calls != 0
                and not has_news_search):
            search_status = 'unavailable'
            discovery_notes.append('Source discovery is unavailable because no search provider is configured.')
        usage: int | None = None
        unavailable_reason: str | None = None
        _raise_if_operation_cancelled(operation_id)
        if research_topics.get(account.account_id, topic_id).versions[-1].version_id != request.parent_version_id:
            raise ApiProblem(409, "lifecycle_conflict", "Research topic has changed; reload before running.")
        def remaining_entitlement() -> int | None:
            model_account = getattr(http_request.state, 'model_account', None)
            if model_account is None or getattr(http_request.state, 'personal_model', False):
                return None
            return billing_ledger.snapshot(model_account.account_id, now=lifecycle_clock.now())['quota_remaining']

        def settle_research_call(call_id: str, tokens: int | None) -> None:
            if not getattr(http_request.state, 'personal_model', False):
                http_request.state.billing_account_id = account.account_id
                http_request.state.billing_reservation_id = f'model:{call_id}'
            _settle_model_call(http_request, call_id, tokens)
        context = ResearchExecutionContext(
            budget=budget, cost_limited=request.max_cost_usd is not None,
            ensure_running=lambda: _raise_if_operation_cancelled(operation_id),
            cancel_event=lambda: operations.cancel_event(operation_id),
            reserve=lambda call_id, tokens: _reserve_model_call(http_request, call_id, token_budget=tokens,
                provider_model=getattr(getattr(provider, 'settings', None), 'model', None)),
            settle=settle_research_call,
            reserve_search=lambda: _reserve_search_call(http_request), entitlement_remaining=remaining_entitlement)
        if continuation_input is not None:
            saved = admitted_continuation.get('runtime') or {}
            accounting = saved.get('_accounting') or {}
            context.calls = accounting.get('calls', 0)
            context.search_calls = accounting.get('search_calls', 0)
            context.usage = accounting.get('usage')
            context.accounted_tokens = accounting.get('accounted_tokens')
            context.model_call_receipts = dict(accounting.get('model_call_receipts') or {})
            if request.max_elapsed_seconds is not None:
                elapsed = (lifecycle_clock.now() - datetime.fromisoformat(continuation_input['captured_at'])).total_seconds()
                context.budget = ResearchExecutionBudget(request.max_model_calls, request.max_search_calls,
                    request.max_total_tokens, started + max(0, request.max_elapsed_seconds - elapsed))
        context.diagnostic_context = {'operation_id': operation_id, 'request_id': operation_id,
            'model': getattr(getattr(provider, 'settings', None), 'model', None),
            'provider': getattr(provider, 'name', 'unknown')}
        workflow = ResearchWorkflow(context)
        journal = RecoveryJournal(RESEARCH_RUN_INPUTS, operation_id, account.account_id)
        context.persist_private_diagnostics = journal.record_private_diagnostics

        # The native tool loop is the default research execution path. Its
        # private checkpoint is persisted before each external request.
        from .research_fin_tools import FinDataToolbox
        from .research_runtime import run_research_agent
        search = getattr(evidence_provider, 'search', None)
        discover = None
        if request.discover_sources and callable(search) and getattr(evidence_provider, 'supports_project_search', False):
            discover = lambda query: search(query, limit=10, deadline=context.budget.deadline,
                                             cancel_event=context.cancel_event())
        run_news_providers = default_news_providers(discover) if request.discover_sources else []
        sec_agent_raw = os.environ.get('FORESIGHTNINE_SEC_USER_AGENT') or os.environ.get('SEC_USER_AGENT')
        _, sec_configured = _configured_sec_user_agent()
        retrieval_permitted = bool(request.auto_fetch_official and request.selected_source_ids != [])
        def reserve_sec_for_run() -> bool:
            if reserve_sec_request is not _DEFAULT_RESERVE_SEC_REQUEST:
                return reserve_sec_request(SOURCE_RATE_LIMITS)
            return pace_sec_request(
                SOURCE_RATE_LIMITS,
                cancel_event=context.cancel_event(),
                deadline=context.budget.deadline,
            )
        # Vision-model OCR is billed like any research model call: each page call is reserved and settled
        # through this run's context (budget, abuse control, account ledger or personal model), keyed by a
        # deterministic call id. Without a configured vision engine only local OCR runs.
        ocr_vision_model = os.environ.get('FORESIGHTNINE_OCR_VISION_MODEL', '').strip() or None
        ocr_before: dict[str, Any] = {}
        def ocr_reserve(call_id: str, max_tokens: int) -> None:
            saved_reserve = context.reserve
            context.reserve = lambda cid, tokens: _reserve_model_call(
                http_request, cid, token_budget=tokens, provider_model=ocr_vision_model)
            try:
                ocr_before[call_id] = context.start_model(call_id, max_tokens)
            finally:
                context.reserve = saved_reserve
        def ocr_settle(call_id: str, tokens: int | None) -> None:
            receipt = context.model_call_receipts.get(call_id)
            if receipt is not None:
                receipt['provider'] = 'returned' if tokens is not None else 'unknown'
            context.finish_model(call_id, tokens, ocr_before.pop(call_id))
        ocr_usage = OcrUsageHooks(ocr_reserve, ocr_settle) if vision_engine_configured() else None
        def acquire(url: str, title: str):
            nonlocal official_fetch_calls, official_fetch_status
            acquire_ocr = dataclass_replace(ocr_usage, events=[]) if ocr_usage is not None else None
            result = execute_official_retrieval(request=request, question=user_question, as_of=topic.request.as_of,
                available_count=len(sources), candidates=[{'url': url, 'title': title}], sources=sources,
                owner_id=account.account_id, operation_id=operation_id, store=research_sources,
                sec_agent_config=sec_agent_raw,
                reserve_sec=reserve_sec_for_run, context=context,
                batch_id=uuid4().hex, **({'ocr_usage': acquire_ocr} if acquire_ocr is not None else {}))
            official_fetch_calls += result.calls
            has_official = any(getattr(item, 'kind', None) == 'official' for item, _ in result.retrieved)
            merged_status: str | None = result.status
            if result.retrieved and not has_official:
                merged_status = 'web_retrieved'
            elif not result.retrieved and result.status == 'failed':
                target_host = (urlsplit(url).hostname or '').lower().rstrip('.')
                if target_host not in PUBLIC_DOMAIN_OFFICIAL_HOSTS and not is_sec_host(target_host):
                    merged_status = None
            official_fetch_status = _merge_official_fetch_status(
                official_fetch_status,
                merged_status,
                auto_fetch_official=request.auto_fetch_official,
            )
            return result.source, {'status': result.status, 'notes': result.notes, 'outcomes': result.outcomes,
                                   'ocr_events': list(acquire_ocr.events) if acquire_ocr is not None else []}
        def persist_ocr_version(old: ResearchSource, new_source_id: str, text: str, provenance: dict) -> ResearchSource:
            return research_sources.create(
                account.account_id,
                SourceInput(title=old.title, text=text, url=str(old.url) if old.url is not None else None, kind=old.kind,
                            is_simulated=old.is_simulated, published_at=old.published_at, event_at=old.event_at,
                            revised_at=old.revised_at, permissions=old.permissions),
                retrieval_method=old.retrieval_method, response_sha256=old.response_sha256,
                response_content_type=old.response_content_type, original_pdf_base64=old.original_pdf_base64,
                pdf_text_order=old.pdf_text_order, derived_from_source_id=old.source_id,
                source_id=new_source_id, provenance=provenance)
        ocr_pages_callback = build_ocr_pages_callback(
            operation_id=operation_id, persist_version=persist_ocr_version, usage=ocr_usage)
        allowed_hosts = fetch_allowed_hosts()
        retrieval_active = retrieval_permitted and topic.request.as_of is None and request.max_cost_usd is None
        capabilities: dict[str, Any] = {
            'search_available': bool(run_news_providers),
            'news_search_available': bool(run_news_providers),
            'search_library_available': True,
            'acquire_available': retrieval_active,
            'web_fetch_enabled': retrieval_active and allowed_hosts is None,
            'sec_available': sec_configured and retrieval_active,
            'max_fetch_bytes': int(os.environ.get('FORESIGHTNINE_LARGE_DOCUMENT_MAX_BYTES', '200000000') or 200_000_000),
            'pdf_paged_reading': True,
            'supported_fetch_content_types': ['text/html', 'text/plain', 'application/pdf'],
        }
        if allowed_hosts is not None:
            capabilities['fetch_allowed_hosts'] = sorted(allowed_hosts)
        fin_disabled_reason = ('historical_cutoff' if topic.request.as_of is not None
                               else 'cost_unknown' if request.max_cost_usd is not None
                               else None if retrieval_permitted else 'permission_denied')
        fin_data = FinDataToolbox(
            account_id=account.account_id, market_key=market_key, credential_owner=market_owner or 'user_byok',
            tushare_key=tushare_key, tushare_owner=tushare_owner or 'user_byok',
            fmp_key=fmp_key, fmp_owner=fmp_owner or 'user_byok',
            sec_user_agent=sec_agent_raw,
            persist=lambda item, **kwargs: research_sources.create(account.account_id, item, **kwargs),
            reserve_call=context.reserve_search, reserve_sec=reserve_sec_for_run,
            remaining_seconds=lambda: context.budget.deadline - context.now(), ensure_running=context.ensure_running,
            now=lifecycle_clock.now, disabled_reason=fin_disabled_reason, cancel_event=context.cancel_event())
        capabilities.update(fin_data.capabilities())
        capabilities.update(fin_data.earnings_capabilities())
        def note_fin_result(name: str, result: dict[str, Any]) -> None:
            # SEC based tools count as official retrieval for the turn's official_fetch_status, like the old SEC tool.
            nonlocal official_fetch_calls, official_fetch_status
            if name not in (
                'company_financials', 'filings', 'sec_earnings_release',
                'sec_filing_tables', 'sec_filing_sections', 'sec_redline_diff', 'sec_fulltext_search',
            ):
                return
            # A-share structured financials come from third-party aggregators (Tushare/BaoStock), not the issuer's
            # filed report: they are structured data, so they neither count as an official fetch nor change its status.
            if result.get('provider') in ('tushare', 'baostock'):
                return
            cost = result.get('cost') if isinstance(result.get('cost'), dict) else {}
            official_fetch_calls += int(cost.get('provider_calls') or 0)
            raw = result.get('status')
            mapped = {'ok': 'completed', 'partial': 'completed', 'empty': 'unknown', 'failed': 'failed',
                      'rate_limited': 'failed', 'unknown': 'unknown', 'denied': 'restricted'}.get(raw, raw)
            official_fetch_status = _merge_official_fetch_status(
                official_fetch_status, mapped, auto_fetch_official=request.auto_fetch_official)
        fin_data.on_result = note_fin_result
        last_runtime_state.clear()
        def persist_snapshot(state: dict) -> None:
            snapshot = deepcopy(state)
            if snapshot.get('checkpoint_schema_version') is None:
                snapshot['checkpoint_schema_version'] = RUNTIME_CHECKPOINT_SCHEMA_VERSION
            snapshot['_accounting'] = {'calls': context.calls, 'search_calls': context.search_calls,
                'usage': context.usage, 'accounted_tokens': context.accounted_tokens,
                'model_call_receipts': deepcopy(context.model_call_receipts)}
            snapshot['_sources'] = _research_source_fingerprints(sources)
            snapshot['_saved_at'] = lifecycle_clock.now().isoformat()
            journal.record_runtime_checkpoint(snapshot)
            last_runtime_state.clear()
            last_runtime_state.update(snapshot)
            op_record = operations.store.get_operation(operation_id) or {}
            if str(op_record.get('status') or 'running') == 'running':
                inputs_now = RESEARCH_RUN_INPUTS.get(operation_id) or {
                    'turn': turn_meta,
                    'question': effective_question,
                    'captured_at': turn_meta.get('started_at'),
                }
                live_progress = build_progress(
                    operation_id=operation_id,
                    operation_status='running',
                    inputs=inputs_now,
                    checkpoint=snapshot,
                    saved_turn=None,
                    version_id=None,
                    after=0,
                )
                doc = TURN_RECORDS.get(turn_meta['turn_id'])
                if doc:
                    live_progress.update(_turn_progress_fields(doc, 'running', None))
                research_live_broker.publish_progress(operation_id, live_progress)
        def save_runtime(state: dict) -> None:
            # Old workers must not update a checkpoint after another attempt has taken over.
            def save() -> None:
                constructed = state.get('_context_input_records') or []
                if constructed and constructed[-1].get('instruction_sequence'):
                    attempt = constructed[-1]
                    if attempt.get('submission_state') == 'not_started':
                        turn_instructions.awaiting_receipt(*identity, operation_id, 0)
                    elif (state.get('pending') or {}).get('kind') == 'model':
                        turn_instructions.awaiting_receipt(*identity, operation_id, attempt['instruction_sequence'])
                seen = state.get("acknowledged_instruction_sequence", 0)
                if seen and seen > last_runtime_state.get("acknowledged_instruction_sequence", 0):
                    turn_instructions.ack(*identity, operation_id, seen)
                persist_snapshot(state)
            turn_instructions.fenced(*identity, operation_id, save, allow_sealed=True)

        def sync_instructions(state: dict, boundary: str) -> bool:
            doc = TURN_RECORDS[identity[-1]]
            if doc['status'] == 'stopped' or doc['operation_id'] != operation_id:
                raise ProviderFailure('provider_cancelled', 'Research stopped or superseded.', retryable=False)
            previous = state.get('user_instructions') or []
            if not previous or previous[-1]['sequence'] != doc['sequence']:
                state['user_instructions'] = _instruction_view(doc)
            expected = state.get('model_instruction_sequence', 0)
            if boundary == 'submit':
                return not turn_instructions.seal(*identity, expected, operation_id=operation_id,
                                                   persist=lambda: persist_snapshot(state))
            if boundary == 'tool':
                pending = state.get('pending') or {}
                action = f"{pending.get('call_id')}:{pending.get('cursor', 0)}"
                cursor = pending.get('cursor', 0)
                calls = pending.get('calls', [])
                tool_name = calls[cursor].get('name') if 0 <= cursor < len(calls) else None
                if tool_name == 'submit_research' and state.get('_semantic_replay_action') == action:
                    if doc['sequence'] != expected:
                        state['user_instructions'] = _instruction_view(turn_instructions.read(*identity))
                        return True
                    return False
                local_read = tool_name in {'read_sources', 'outline_source', 'search_source', 'read_pages'}
                if turn_instructions.claim_action(*identity, operation_id, action, expected, local_read=local_read):
                    return False
                disposition = turn_instructions.action_status(*identity, operation_id, action, expected)
                if disposition == 'claimed_unknown':
                    raise ApiProblem(409, 'lifecycle_conflict', 'A tool start was recorded without a result.')
                if disposition != 'instruction_changed':
                    raise ProviderFailure('provider_cancelled', 'Research stopped or superseded.', retryable=False)
                state['user_instructions'] = _instruction_view(turn_instructions.read(*identity))
                return True
            return doc['sequence'] > expected

        def publish_stream_event(event: dict[str, Any]) -> None:
            kind = event.get('type')
            if kind == 'delta':
                research_live_broker.publish_delta(
                    operation_id,
                    call_seq=int(event['call_seq']),
                    channel=event['channel'],
                    delta=str(event['delta']),
                    seq=int(event['seq']),
                )
            elif kind == 'reset':
                research_live_broker.publish_reset(
                    operation_id,
                    call_seq=int(event['call_seq']),
                    reason=str(event['reason']),
                )
        def _prior_with_failed_sources(version_obj: Any) -> dict | None:
            prior_data = prior_research_input(version_obj)
            if prior_data is None:
                return None
            failed_records: list[dict[str, Any]] = []
            seen_hosts: set[str] = set()
            for prior_turn in version_obj.content.turns:
                for op_id in getattr(prior_turn, 'operation_ids', ()) or ():
                    op_inputs = RESEARCH_RUN_INPUTS.get(op_id) or {}
                    cp = op_inputs.get('runtime_checkpoint') if isinstance(op_inputs, Mapping) else None
                    for row in (cp.get('failed_source_accesses') or []) if isinstance(cp, Mapping) else []:
                        if isinstance(row, Mapping) and row not in failed_records:
                            failed_records.append(dict(row))
                            if isinstance(row.get('host'), str) and row['host']:
                                seen_hosts.add(row['host'])
                for ev in getattr(prior_turn, 'events', ()) or ():
                    if not isinstance(ev, Mapping) or ev.get('kind') != 'tool' or ev.get('name') != 'acquire_sources':
                        continue
                    summary = ev.get('summary')
                    for item in (summary.get('items') or []) if isinstance(summary, Mapping) else []:
                        if not isinstance(item, Mapping) or item.get('status') in ('acquired', 'completed'):
                            continue
                        host = item.get('host')
                        if isinstance(host, str) and host in seen_hosts:
                            continue
                        rec = {k: item.get(k) for k in ('host', 'status', 'reason') if item.get(k)}
                        if rec and rec not in failed_records:
                            failed_records.append(rec)
            if failed_records:
                prior_data['failed_source_accesses'] = failed_records[-50:]
            return prior_data

        if admitted_continuation.get('runtime') is not None:
            resumed = admitted_continuation['runtime']
            resumed.pop('clarification', None)
            if (resumed.get('stopped') == 'no_progress'
                    and TURN_RECORDS[identity[-1]]['sequence'] > resumed.get('model_instruction_sequence', 0)):
                resumed.pop('stopped', None)
            save_runtime(admitted_continuation['runtime'])
        try:
            latest_topic = research_topics.get(account.account_id, topic_id)
            parent_now = latest_topic.versions[-1]
            if rebase_base is not None:
                # Build on the version before the replaced turn; the head check above still guards the append.
                parent_now = next(item for item in latest_topic.versions if item.version_id == rebase_base.version_id)
            research_clock = build_research_clock(lifecycle_clock.now())
            # The first question names the topic unless the user already did; an edited first
            # question renames a topic whose title was summarised.
            title_question = None
            renaming = bool(turn_meta.get('replaces_turn_id'))
            if turn_meta.get('index') == 1 and turn_meta.get('question_origin') == 'user':
                current_title, generated = research_topics.title_state(account.account_id, topic_id)
                if not current_title or (generated and renaming):
                    title_question = str(turn_meta.get('question') or '') or None
            outcome = run_research_agent(question=effective_question, sources=sources, model=provider,
                title_question=title_question,
                on_title=lambda title: research_topics.set_generated_title(
                    account.account_id, topic_id, title, replace_generated=renaming),
                context=context, discover=discover, acquire=acquire if retrieval_permitted else None,
                fin_data=fin_data, capabilities=capabilities,
                news_providers=run_news_providers,
                issuer_docs=(IssuerDocuments(remaining_seconds=lambda: context.budget.deadline - context.now(),
                                             ensure_running=context.ensure_running, reserve_call=context.reserve_search,
                                             now=lifecycle_clock.now) if retrieval_active else None),
                checkpoint=save_runtime, sync_instructions=sync_instructions, initial_state=admitted_continuation.get('runtime'),
                on_submission=lambda: setattr(http_request.state, 'model_submission_started', True),
                on_stream_event=publish_stream_event,
                as_of=topic.request.as_of,
                clock=None if resuming_checkpoint else research_clock,
                now=lifecycle_clock.now, ocr_pages=ocr_pages_callback,
                prior=None if resuming_checkpoint else _prior_with_failed_sources(parent_now), focus=focus)
            _raise_if_operation_cancelled(operation_id)
            job = RESEARCH_WORK[operation_id]
            pending = last_runtime_state.get('pending') or {}
            last_failure = next((event for event in reversed(last_runtime_state.get('events', []))
                                 if event.get('kind') == 'model_failure'), {})
            retryable_outcome = outcome.reason in {
                'incomplete_model_response', 'provider_timeout', 'provider_rate_limited', 'provider_output_truncated',
                'provider_transport_error', 'provider_context_limit'}
            settled = (not pending and last_failure.get('external_state') != 'unknown'
                       and not (RESEARCH_RUN_INPUTS.get(operation_id) or {}).get('recovery_pending'))
            new_guidance = (outcome.reason in {'no_submission', 'no_progress'}
                            and TURN_RECORDS[identity[-1]]['sequence'] > last_runtime_state.get('model_instruction_sequence', 0))
            if (retryable_outcome or new_guidance) and settled:
                signature = content_hash({'reason': outcome.reason, 'reads': last_runtime_state.get('read_ranges'),
                                          'facts': last_runtime_state.get('facts'),
                                          'sequence': last_runtime_state.get('model_instruction_sequence', 0)})
                if signature != job.get('retry_signature') and not any(guard(last_runtime_state) for guard in (
                        runtime_timeout_loop_blocked, runtime_interruption_no_progress_blocked, runtime_truncation_loop_blocked)):
                    RESEARCH_WORK.put(operation_id, {**job, 'retry_signature': signature,
                                      'resume_after': (lifecycle_clock.now() + timedelta(seconds=0 if new_guidance else 5)).isoformat()})
                    raise ApiProblem(503, 'internal_error', 'Research will resume from its settled checkpoint.',
                                     retryable=True)
            result = persister.finalize_turn(
                operation_id=operation_id,
                parent_version=parent_now,
                operation_started_at=(operations.store.get_operation(operation_id) or {}).get('created_at'),
                outcome=outcome,
                runtime_state=last_runtime_state,
                context=context,
                finished_at=lifecycle_clock.now(),
                default_search_status=search_status,
                official_fetch_status=official_fetch_status,
            )
            doc = turn_instructions.read(*identity)
            result.content.turns[-1].lifecycle_status = 'completed'
            result.content.turns[-1].user_instructions = _saved_instructions(doc, last_runtime_state, result.content)
            if rebase_base is not None and result.content.turns[-1].reason != 'submitted':
                # A regeneration only replaces the answer when the model submitted research; a
                # provider failure, timeout or exhausted budget leaves the previous answer current.
                raise ApiProblem(409, 'lifecycle_conflict', 'The regenerated answer did not finish; the previous answer is unchanged.')
            job = RESEARCH_WORK[operation_id]
            # The validated candidate permits local delivery after a failed version write.
            def stage_delivery() -> None:
                RESEARCH_WORK.put(operation_id, {**job, 'result': result.model_dump(mode='json')})
            if outcome.reason == 'awaiting_user' and isinstance(last_runtime_state.get('clarification'), dict):
                if not turn_instructions.wait_for_user(*identity, operation_id,
                        last_runtime_state.get('model_instruction_sequence', 0), last_runtime_state['clarification'],
                        persist=stage_delivery):
                    raise ApiProblem(503, 'internal_error', 'Research changed while requesting clarification.', retryable=True)
            elif doc['status'] == 'sealed':
                turn_instructions.fenced(*identity, operation_id, stage_delivery, allow_sealed=True)
            else:
                # Budget and terminal provider failures may prevent another model call.
                # Finalize the instruction receipts under the same fence as the candidate.
                def finish_with_limits() -> None:
                    latest = turn_instructions.read(*identity)
                    result.content.turns[-1].user_instructions = _saved_instructions(latest, last_runtime_state, result.content)
                    stage_delivery()
                closing_sequence = (last_runtime_state.get('model_instruction_sequence', 0)
                                    if outcome.reason in {'no_submission', 'no_progress'} else doc['sequence'])
                if not turn_instructions.seal(*identity, closing_sequence, operation_id=operation_id,
                                              persist=finish_with_limits):
                    raise ApiProblem(409, 'lifecycle_conflict', 'The research task changed during finalization.',
                                     retryable=True)
            return result, topic_id
        except (ProviderFailure, ApiProblem):
            raise
        except Exception:
            persister.record_failed_checkpoint(operation_id, last_runtime_state)
            raise

    def commit(_operation_id: str, result: BaseModel) -> None:
        assert isinstance(result, ResearchRunResult)
        saved = turn_instructions.fenced(*identity, _operation_id, lambda: persister.commit_result(
            operation_id=_operation_id, result=result, runtime_state=last_runtime_state,
        ), allow_sealed=True, allow_waiting=True)

        turn_doc = turn_instructions.read(*identity)
        _update_turn_status(topic_id, _operation_id,
                            'awaiting_user' if turn_doc['status'] == 'awaiting_user' else 'completed', saved.version_id)
        RESEARCH_WORK.delete(_operation_id)

    def record_inputs(operation_id: str) -> None:
        current_status = TURN_STATUS.get(topic_id)
        if continuation_input is None and current_status and current_status.get('owner_id') == account.account_id and current_status.get('logical_status') == 'awaiting_user':
            raise ApiProblem(409, 'lifecycle_conflict', 'Answer or stop the waiting research turn first.')
        for job_id, job in RESEARCH_WORK.items():
            if (job['account_id'] == account.account_id and job['topic_id'] == topic_id
                    and job_id != (continuation_input or {}).get('operation_id')):
                raise ApiProblem(409, 'lifecycle_conflict', 'The existing research turn is still active.')
        # Write before any external call, within operation admission. Replays do
        # not replace the original snapshot or send the provider another request.
        # The shared topic lock remains held through PostgreSQL admission; the
        # memory operation store serializes admissions with its condition lock.
        # Thus different keys cannot start concurrent runs for the same topic.
        def has_running_research() -> bool:
            endpoint = f"/v1/research/topics/{topic_id}/runs"
            owner = f"account:{account.account_id}"
            for other_id, session in operations.store.session_ids().items():
                if session != topic_id or other_id == operation_id:
                    continue
                record = operations.store.get_operation(other_id)
                if (record and record.get('owner_subject') == owner and record.get('endpoint') == endpoint and
                        record.get('status') in {'pending', 'running'}):
                    return True
            return False
        if research_topics.get(account.account_id, topic_id).versions[-1].version_id != request.parent_version_id:
            raise ApiProblem(409, 'lifecycle_conflict', 'Research changed before the run was admitted.')
        if has_running_research():
            raise ApiProblem(409, 'lifecycle_conflict', 'Another research run is already active for this topic.')
        if continuation_input is not None:
            prior_id = continuation_input['operation_id']
            original = RESEARCH_RUN_INPUTS.get(prior_id) or {}
            prior_operation = operations.store.get_operation(prior_id) or {}
            snapshot = original.get('runtime_checkpoint') or {}
            unstarted = bool(background and continuation_input.get('unstarted') and not snapshot
                             and not original.get('recovery_pending'))
            if prior_operation.get('status') in {'pending', 'running'}:
                raise ApiProblem(409, 'lifecycle_conflict', 'The original run is still active.')
            if (original.get('owner_id') != account.account_id or original.get('topic_id') != topic_id
                    or continuation_input.get('parent_version_id') != request.parent_version_id
                    or original.get('runtime_binding') != _runtime_provider_binding(provider, http_request)
                    or original.get('question_sha256') != sha256(effective_question.encode('utf-8')).hexdigest()
                    or original.get('as_of') != (topic.request.as_of.isoformat() if topic.request.as_of else None)
                    or original.get('scope_sha256') != (sha256(topic.request.scope.encode('utf-8')).hexdigest() if topic.request.scope else None)
                    or _stored_form(original.get('sources') if unstarted else snapshot.get('_sources')) != _stored_form(_research_source_fingerprints(sources))
                    or _stored_form({k: v for k, v in (original.get('runtime_request') or {}).items() if k != 'parent_version_id'}) !=
                       _stored_form({k: v for k, v in request.model_dump(mode='json').items() if k != 'parent_version_id'})):
                raise ApiProblem(409, 'lifecycle_conflict', 'Research inputs or provider configuration changed.')
            max_history_chars: int | None = None
            policy_input_limit = getattr(getattr(provider, 'policy', None), 'max_input_tokens', None)
            if type(policy_input_limit) is int and policy_input_limit > 0:
                max_history_chars = max(2_000, policy_input_limit * 2)
            if not unstarted:
                admitted_continuation['runtime'] = RecoveryJournal(
                    RESEARCH_RUN_INPUTS, prior_id, account.account_id,
                ).claim_runtime_continuation(operation_id, max_history_chars=max_history_chars,
                                             saved_turn=_agent_turn_for(parent_version, prior_id))
        previous = continuation_input['operation_id'] if continuation_input else None
        old_turn = TURN_RECORDS.get(turn_meta['turn_id'])
        TURN_RECORDS.put(operation_id, {'account_id': account.account_id, 'topic_id': topic_id,
                                        'turn_id': turn_meta['turn_id']})
        try:
            turn_instructions.begin(*identity, operation_id, previous_operation_id=previous if old_turn else None,
                                    allow_reopen=not background and bool(previous),
                                    require_waiting=bool(continuation_input and not background and snapshot.get('clarification')))
        except (InstructionNotFound, InstructionConflict, InstructionInvalid) as error:
            raise _turn_http_error(error) from error
        TURN_STATUS.put(topic_id, {'owner_id': account.account_id, 'topic_id': topic_id,
                                   'turn_id': turn_meta['turn_id'], 'current_operation_id': operation_id,
                                   'logical_status': 'running'})
        RESEARCH_WORK.put(operation_id, {
            'account_id': account.account_id, 'topic_id': topic_id, 'turn_id': turn_meta['turn_id'],
            'client_ip': getattr(http_request.state, 'client_ip', '0.0.0.0'),
            'session_id': getattr(http_request.state, 'session_id', None),
            'retry_signature': (RESEARCH_WORK.get(previous) or {}).get('retry_signature') if previous else None,
        })
        if previous:
            RESEARCH_WORK.delete(previous)
        policy = getattr(provider, "policy", None)
        RESEARCH_RUN_INPUTS.put(operation_id, {
            "schema_version": "1", "operation_id": operation_id, "owner_id": account.account_id,
            "runtime_binding": _runtime_provider_binding(provider, http_request),
            "runtime_request": request.model_dump(mode="json"),
            "runtime_origin_captured_at": continuation_input["captured_at"] if continuation_input else lifecycle_clock.now().isoformat(),
            "runtime_previous_operation_id": continuation_input['operation_id'] if continuation_input else None,
            "topic_id": topic_id, "parent_version_id": request.parent_version_id,
            "captured_at": lifecycle_clock.now().isoformat(),
            "question_origin": question_origin,
            "question": effective_question,
            "turn": {**turn_meta, "operation_ids": [*turn_meta["operation_ids"], operation_id]},
            **({"default_task": DOCUMENT_OVERVIEW_QUESTION} if question_origin == "document_overview_default" else {}),
            "question_sha256": sha256(effective_question.encode("utf-8")).hexdigest(),
            **({"scope_sha256": sha256(topic.request.scope.encode("utf-8")).hexdigest()} if topic.request.scope else {}),
            "as_of": topic.request.as_of.isoformat() if topic.request.as_of else None,
            "sources": _research_source_fingerprints(sources),
            "budget": {key: value for key, value in _request_payload(request).items() if key != "question_override"},
            **({"planned_official_source_id": official_source_id(operation_id)}
               if request.auto_fetch_official else {}),
            "model": getattr(getattr(provider, "settings", None), "model", None),
            "provider": getattr(provider, "name", None),
            "provider_limits": {key: getattr(policy, key, None) for key in
                                ("max_input_tokens", "max_output_tokens", "timeout_seconds")},
            "system_prompt_sha256": sha256(RUNTIME_SYSTEM.encode("utf-8")).hexdigest(),
            "selection_method": RESEARCH_SELECTION_METHOD,
            "method_packs": ((RESEARCH_RUN_INPUTS.get(continuation_input['operation_id']) or {}).get('method_packs', [])
                             if continuation_input is not None else method_pack_records(effective_question)),
        })

    # Existing operation receipts have a v1 endpoint constraint. The v1 alias
    # above is a real route and keeps old receipt readers compatible.
    result, replayed = operations.submit_async(endpoint=f"/v1/research/topics/{topic_id}/runs",
        idempotency_key=idempotency_key, owner_subject=f"account:{account.account_id}",
        request_payload=run_request_payload, result_field="research", build_result=build,
        session_id=topic_id, before_accept=record_inputs, commit_result=commit)
    _mark_replay(response, replayed)
    stored = RESEARCH_RUN_INPUTS[result.operation.operation_id]
    return ResearchStartResponse(operation=result.operation, turn_id=stored["turn"]["turn_id"])


def _agent_turn_for(version: Any, operation_id: str) -> ResearchTurn | None:
    """The agent-saved turn whose latest operation is this one, if the version still ends with it."""
    if version.created_by != 'agent' or not version.content.turns:
        return None
    turn = version.content.turns[-1]
    return turn if turn.operation_ids[-1] == operation_id else None


def _record_failed_checkpoint_version(operation_id: str, runtime_state: Mapping[str, Any]) -> None:
    """Record the checkpoint version that failed locally so recovery requires explicit migration."""
    record_failed_checkpoint_version(RESEARCH_RUN_INPUTS, operation_id, runtime_state)


@app.get("/v2/research/topics/{topic_id}/runs/{operation_id}/recovery-status", include_in_schema=False)
def get_research_recovery_status(topic_id: str, operation_id: str, http_request: Request) -> dict:
    """Read eligibility without consuming the checkpoint; admission rechecks it."""
    account = _authenticated_account(http_request.headers.get('Authorization'))
    operations.require_owner(operation_id, f'account:{account.account_id}')
    try:
        topic = research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, 'not_found', 'Research topic not found.') from error
    inputs = RESEARCH_RUN_INPUTS.get(operation_id)
    operation = operations.store.get_operation(operation_id) or {}
    if inputs and inputs.get('owner_id') == account.account_id and inputs.get('topic_id') == topic_id and _replaces_turn(inputs):
        return {'can_continue': False, 'can_deliver': False,
                'reason': 'running' if operation.get('status') in {'pending', 'running'} else 'replaced_turn_run',
                'checkpoint': None, 'version_id': None}
    if inputs and inputs.get('owner_id') == account.account_id and inputs.get('topic_id') == topic_id and inputs.get('runtime_checkpoint'):
        checkpoint = inputs['runtime_checkpoint']
        pending = checkpoint.get('pending') or {}
        unknown = pending.get('kind') not in (None, 'tool_batch', 'model_returned')
        latest = topic.versions[-1]
        reason = None
        try:
            saved_request = ResearchRunRequest.model_validate(inputs['runtime_request'])
            provider, _ = _model_provider_for_request(http_request, research=True)
            if isinstance(provider, OpenAICompatibleLlmProvider):
                provider = provider.for_research(saved_request.max_input_tokens_per_call)
            elif saved_request.max_input_tokens_per_call is not None and callable(getattr(provider, 'complete_turn', None)):
                from dataclasses import replace
                provider.policy = replace(provider.policy, max_input_tokens=saved_request.max_input_tokens_per_call)
            sources = [research_sources.get(account.account_id, row['source_id']) for row in checkpoint['_sources']]
            question = saved_request.question_override or topic.request.question or DOCUMENT_OVERVIEW_QUESTION
            if (inputs.get('runtime_binding') != _runtime_provider_binding(provider, http_request)
                or _stored_form(checkpoint['_sources']) != _stored_form(_research_source_fingerprints(sources))
                or inputs.get('question_sha256') != sha256(question.encode('utf-8')).hexdigest()
                or inputs.get('scope_sha256') != (sha256(topic.request.scope.encode('utf-8')).hexdigest() if topic.request.scope else None)
                or inputs.get('as_of') != (topic.request.as_of.isoformat() if topic.request.as_of else None)
                or (latest.version_id != inputs['parent_version_id'] and
                    (latest.review != 'draft' or _agent_turn_for(latest, operation_id) is None))):
                reason = 'changed'
        except (ValueError, TypeError, KeyError, SourceNotFound, ApiProblem):
            reason = 'unavailable'
        saved_turn = _agent_turn_for(latest, operation_id)
        if operation.get('status') in {'pending', 'running'}: reason = 'running'
        elif inputs.get('runtime_continuation_operation_id'): reason = 'already_used'
        elif unknown: reason = 'unknown_external'
        elif checkpoint.get('submission') and getattr(saved_turn, 'status', None) == 'completed': reason = 'unavailable'
        elif reason is None and runtime_poison_pill_blocked(
            checkpoint,
            operation=operation,
            operation_events=operations.store.get_events(operation_id),
            inputs=inputs,
        ):
            reason = 'non_retryable_local_error'
        elif reason is None and runtime_truncation_loop_blocked(checkpoint, saved_turn=saved_turn): reason = 'repeated_truncation'
        elif reason is None and runtime_interruption_no_progress_blocked(checkpoint): reason = 'repeated_interruption_no_progress'
        elif reason is None and runtime_timeout_loop_blocked(checkpoint, saved_turn=saved_turn): reason = 'repeated_timeout'
        payload: dict[str, Any] = {
            'can_continue': reason is None and not checkpoint.get('submission'),
            'can_deliver': reason is None and bool(checkpoint.get('submission')),
            'reason': reason,
            'checkpoint': pending.get('kind') or 'runtime',
            'version_id': latest.version_id,
        }
        if reason == 'non_retryable_local_error':
            payload['recoverable'] = False
        return payload
    # Only native runtime checkpoints can be continued or delivered.
    return {'can_continue': False, 'can_deliver': False,
            'reason': 'running' if operation.get('status') in {'pending', 'running'} else 'no_checkpoint',
            'checkpoint': None, 'version_id': None}


@app.post("/v2/research/topics/{topic_id}/runs/{operation_id}/continue", response_model=OperationResponse,
          response_model_exclude_none=True, status_code=202, include_in_schema=False)
def continue_research_run(topic_id: str, operation_id: str, response: Response,
                          idempotency_key: IdempotencyKey, http_request: Request) -> OperationResponse:
    """Continue from a server-owned, fully settled research stage checkpoint."""
    account = _authenticated_account(http_request.headers.get('Authorization'))
    return _continue_research_for_account(topic_id, operation_id, response, idempotency_key, http_request, account)


def _continue_research_for_account(topic_id: str, operation_id: str, response: Response,
                                   idempotency_key: str, http_request: Request, account: LocalAccount,
                                   *, background: bool = False) -> OperationResponse:
    operations.require_owner(operation_id, f'account:{account.account_id}')
    try:
        topic = research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, 'not_found', 'Research topic not found.') from error
    inputs = RESEARCH_RUN_INPUTS.get(operation_id)
    if not inputs or inputs.get('owner_id') != account.account_id or inputs.get('topic_id') != topic_id:
        raise ApiProblem(404, 'not_found', 'Research run inputs not found.')
    if _replaces_turn(inputs) and not background:
        raise ApiProblem(409, 'lifecycle_conflict', 'A regenerated answer cannot be continued; regenerate it again.')
    # Resolve an already admitted continuation using its original, server-owned
    # request before checking today's version. A new key still goes through CAS.
    continuation_id = inputs.get('runtime_continuation_operation_id')
    if continuation_id:
        prior = RESEARCH_RUN_INPUTS.get(continuation_id) or {}
        if (prior.get('owner_id') == account.account_id and prior.get('topic_id') == topic_id
                and prior.get('runtime_previous_operation_id') == operation_id and prior.get('runtime_request')):
            try:
                replay_request = ResearchRunRequest.model_validate(prior['runtime_request'])
            except (ValueError, TypeError):
                replay_request = None
            if replay_request is not None:
                existing = operations.replay_async(
                    endpoint=f'/v1/research/topics/{topic_id}/runs', idempotency_key=idempotency_key,
                    owner_subject=f'account:{account.account_id}',
                    request_payload=replay_request.model_dump(mode='json', exclude_unset=True))
                if existing is not None:
                    if existing.operation.operation_id != continuation_id:
                        raise ApiProblem(409, 'idempotency_conflict', 'The idempotency key belongs to another research run.')
                    _mark_replay(response, True)
                    return existing
    checkpoint = inputs.get('runtime_checkpoint') or {}
    if not checkpoint or not inputs.get('runtime_request'):
        raise ApiProblem(409, 'lifecycle_conflict', 'No runtime checkpoint is available.')
    try:
        continuation_request = ResearchRunRequest.model_validate(inputs['runtime_request'])
        source_ids = [item['source_id'] for item in checkpoint['_sources']]
        for source_id in source_ids:
            research_sources.get(account.account_id, source_id)
    except (ValueError, TypeError, KeyError, SourceNotFound) as error:
        raise ApiProblem(409, 'lifecycle_conflict', 'The saved runtime inputs are unavailable.') from error
    parent = topic.versions[-1]
    saved_turn = _agent_turn_for(parent, operation_id)
    if checkpoint.get('submission') and saved_turn is not None and saved_turn.status == 'completed':
        raise ApiProblem(409, 'lifecycle_conflict', 'This research submission was already saved.')
    if runtime_poison_pill_blocked(
        checkpoint,
        operation=operations.store.get_operation(operation_id) or {},
        operation_events=operations.store.get_events(operation_id),
        inputs=inputs,
    ):
        raise ApiProblem(409, 'lifecycle_conflict', _POISON_PILL_MESSAGE)
    if runtime_truncation_loop_blocked(checkpoint, saved_turn=saved_turn):
        raise ApiProblem(409, 'lifecycle_conflict', _REPEATED_TRUNCATION_MESSAGE)
    if runtime_interruption_no_progress_blocked(checkpoint):
        raise ApiProblem(409, 'lifecycle_conflict', _REPEATED_INTERRUPTION_MESSAGE)
    if runtime_timeout_loop_blocked(checkpoint, saved_turn=saved_turn):
        raise ApiProblem(409, 'lifecycle_conflict', _REPEATED_TIMEOUT_MESSAGE)
    if parent.version_id != inputs['parent_version_id']:
        if parent.review != 'draft' or saved_turn is None:
            raise ApiProblem(409, 'lifecycle_conflict', 'Research changed after the checkpoint.')
        continuation_request = continuation_request.model_copy(update={'parent_version_id': parent.version_id})
    http_request.state.research_continuation = {'operation_id': operation_id, 'source_ids': source_ids,
        'captured_at': inputs.get('runtime_origin_captured_at', inputs['captured_at']), 'parent_version_id': parent.version_id}
    return _start_research_for_account(topic_id, continuation_request, response, idempotency_key,
                                       http_request, account, background=background)


@app.post("/v2/research/topics/{topic_id}/runs/{operation_id}/recover", response_model=OperationResponse,
          response_model_exclude_none=True, status_code=202, include_in_schema=False)
def recover_research_run(topic_id: str, operation_id: str, response: Response,
                         idempotency_key: IdempotencyKey, http_request: Request) -> OperationResponse:
    """Deliver a committed checkpoint without replaying uncertain external work."""
    account = _authenticated_account(http_request.headers.get('Authorization'))
    owner = f'account:{account.account_id}'
    operations.require_owner(operation_id, owner)
    runtime_inputs = RESEARCH_RUN_INPUTS.get(operation_id) or {}
    checkpoint = runtime_inputs.get('runtime_checkpoint')
    if not checkpoint or not checkpoint.get('submission'):
        raise ApiProblem(409, 'lifecycle_conflict', 'No completed runtime submission is available for recovery.')
    return continue_research_run(topic_id, operation_id, response, idempotency_key, http_request)


def _review_edits_after(versions: list[ResearchVersion], base_index: int) -> bool:
    """Whether a user-made version after ``base_index`` changed content (not just attached sources)."""
    for previous, version in zip(versions[base_index:], versions[base_index + 1:]):
        if version.created_by == "user" and version.content.model_dump(mode="json") != previous.content.model_dump(mode="json"):
            return True
    return False


def _replaces_turn(inputs: Mapping[str, Any] | None) -> bool:
    """Runs that regenerate an answer are not resumable: their partial state would land on the old head."""
    return bool(((inputs or {}).get("turn") or {}).get("replaces_turn_id"))


def _turn_http_error(error: Exception) -> ApiProblem:
    if isinstance(error, InstructionNotFound):
        return ApiProblem(404, 'not_found', 'Research turn not found.')
    if isinstance(error, InstructionInvalid):
        return ApiProblem(422, 'invalid_request', str(error))
    code = 'idempotency_conflict' if 'idempotency' in str(error) else 'lifecycle_conflict'
    return ApiProblem(409, code, str(error))


@app.post('/v2/research/topics/{topic_id}/turns/{turn_id}/instructions',
          status_code=201, response_model=ResearchInstruction, include_in_schema=False)
def append_research_instruction(topic_id: str, turn_id: str, request: ResearchInstructionInput,
                                http_request: Request, response: Response, idempotency_key: IdempotencyKey) -> dict:
    account = _authenticated_account(http_request.headers.get('Authorization'))
    try:
        research_topics.get(account.account_id, topic_id)
        identity = (account.account_id, topic_id, turn_id)
        item, created = turn_instructions.append(*identity, request.text, idempotency_key)
        doc = turn_instructions.read(*identity)
    except TopicNotFound as error:
        raise ApiProblem(404, 'not_found', 'Research topic not found.') from error
    except (InstructionNotFound, InstructionConflict, InstructionInvalid) as error:
        raise _turn_http_error(error) from error
    response.status_code = 201 if created else 200
    _mark_replay(response, not created)
    receipt = _instruction_view(doc)[item['sequence'] - 1]
    if created:
        run_id = doc['operation_id']
        record = RESEARCH_RUN_INPUTS.get(run_id)
        if record:
            progress = _build_research_run_progress_view(
                research_topics.get(account.account_id, topic_id), operations._cache_operation(run_id), record, after=0)
            research_live_broker.publish_progress(run_id, progress)
    return receipt


@app.post('/v2/research/topics/{topic_id}/turns/{turn_id}/stop', response_model=ResearchTurnStopped, include_in_schema=False)
def stop_research_turn(topic_id: str, turn_id: str, http_request: Request, idempotency_key: IdempotencyKey) -> dict:
    account = _authenticated_account(http_request.headers.get('Authorization'))
    try:
        research_topics.get(account.account_id, topic_id)
        doc = turn_instructions.stop(account.account_id, topic_id, turn_id, idempotency_key=idempotency_key)
    except TopicNotFound as error:
        raise ApiProblem(404, 'not_found', 'Research topic not found.') from error
    except (InstructionNotFound, InstructionConflict) as error:
        raise _turn_http_error(error) from error
    run_id = doc['operation_id']
    operation = operations._cache_operation(run_id)
    if operation.status in {'running', 'pending'}:
        operations.cancel(run_id, operation.cancel_token, idempotency_key, owner_subject=f'account:{account.account_id}')
    _deliver_cancelled_research(run_id, account.account_id)
    _update_turn_status(topic_id, run_id, 'stopped')
    RESEARCH_WORK.delete(run_id)
    return {'turn_id': turn_id, 'status': 'stopped'}


@app.post('/v2/research/topics/{topic_id}/turns/{turn_id}/accept-findings', include_in_schema=False)
def accept_research_findings(topic_id: str, turn_id: str, http_request: Request) -> dict:
    account = _authenticated_account(http_request.headers.get('Authorization'))
    try:
        topic = research_topics.get(account.account_id, topic_id)
        identity = (account.account_id, topic_id, turn_id)
        doc = turn_instructions.read(*identity)
        operation_id = doc['operation_id']
        version = next((v for v in reversed(topic.versions) if _agent_turn_for(v, operation_id)), None)
        operation = operations._cache_operation(operation_id)
        if version is None or operation.status in {'pending', 'running'}:
            raise InstructionConflict('findings are still being saved')
        turn = _agent_turn_for(version, operation_id)
        if turn is None or not (turn.report.strip() or (turn.partial_notes or '').strip() or any(p.answer.strip() for p in turn.question_parts)):
            raise InstructionConflict('no readable findings are available to accept')
        turn_instructions.accept_findings(*identity, operation_id, version.version_id)
    except TopicNotFound as error:
        raise ApiProblem(404, 'not_found', 'Research topic not found.') from error
    except (InstructionNotFound, InstructionConflict) as error:
        raise _turn_http_error(error) from error
    _update_turn_status(topic_id, operation_id, 'completed', version.version_id)
    return {'turn_id': turn_id, 'status': 'completed', 'version_id': version.version_id}


def _reconcile_research_work() -> None:
    """Only jobs admitted with automatic continuation are eligible for this sweep."""
    try:
        operations.recover_expired_leases()
    except Exception:
        logging.getLogger(__name__).exception("Unable to recover expired operation leases")
    try:
        _reconcile_orphaned_earnings_squads()
    except Exception:
        logging.getLogger(__name__).exception("Unable to reconcile orphaned earnings squad runs")
    if not RESEARCH_WORK:
        return
    for operation_id, work in list(RESEARCH_WORK.items()):
        try:
            _reconcile_research_attempt(operation_id, work)
        except Exception:
            # Storage/network outages on one topic must not starve other ready turns.
            logging.getLogger(__name__).exception('Unable to reconcile research attempt %s', operation_id)


def _reconcile_research_attempt(operation_id: str, work: dict, *, now: datetime | None = None) -> None:
    operation = operations.store.get_operation(operation_id) or {}
    if operation.get('status') in {'running', 'pending'}:
        return
    if work.get('resume_after') and datetime.fromisoformat(work['resume_after']) > (now or lifecycle_clock.now()):
        return
    account_id, topic_id, turn_id = (work[key] for key in ('account_id', 'topic_id', 'turn_id'))
    identity = (account_id, topic_id, turn_id)
    doc = TURN_RECORDS.get(turn_id) or {}
    if doc.get('operation_id') != operation_id or doc.get('account_id') != account_id:
        RESEARCH_WORK.delete(operation_id)
        return
    account = ACCOUNTS.get(account_id)
    if not account or account.deleted_at:
        RESEARCH_WORK.delete(operation_id)
        return
    try:
        topic = research_topics.get(account_id, topic_id)
    except TopicNotFound:
        RESEARCH_WORK.delete(operation_id)
        return
    if operation.get('status') == 'cancelled' or doc['status'] == 'stopped':
        if doc['status'] == 'open':
            turn_instructions.stop(*identity)
        _deliver_cancelled_research(operation_id, account_id)
        _update_turn_status(topic_id, operation_id, 'stopped')
        RESEARCH_WORK.delete(operation_id)
        return
    delivered = next((v for v in reversed(topic.versions) if _agent_turn_for(v, operation_id)), None)
    if delivered:
        _update_turn_status(topic_id, operation_id, 'awaiting_user' if doc['status'] == 'awaiting_user' else 'completed', delivered.version_id)
        RESEARCH_WORK.delete(operation_id)
        return
    inputs = RESEARCH_RUN_INPUTS.get(operation_id) or {}
    cp = inputs.get('runtime_checkpoint') or {}
    if topic.versions[-1].version_id != inputs.get('parent_version_id'):
        # A user edit supersedes the candidate. Never publish it over the new head.
        _update_turn_status(topic_id, operation_id, 'failed')
        RESEARCH_WORK.delete(operation_id)
        if doc['status'] == 'open':
            turn_instructions.seal(*identity, doc['sequence'], operation_id=operation_id)
        return
    if work.get('result') or (doc['status'] == 'sealed' and cp.get('submission')):
        def deliver() -> None:
            if RESEARCH_WORK.get(operation_id) is None:
                return
            if work.get('result'):
                source_records = cp.get('_sources', [])
                try:
                    current_sources = [research_sources.get(account_id, row['source_id']) for row in source_records]
                except SourceNotFound:
                    current_sources = []
                if _stored_form(source_records) != _stored_form(_research_source_fingerprints(current_sources)):
                    _update_turn_status(topic_id, operation_id, 'failed')
                    RESEARCH_WORK.delete(operation_id)
                    return
                candidate = ResearchRunResult.model_validate(work['result'])
                sources = list(dict.fromkeys([*topic.versions[-1].source_ids,
                    *(row['source_id'] for row in cp.get('_sources', []))]))
                chain = ResearchVersionChain(research_topics, account_id, topic_id, candidate.parent_version_id, sources)
                saved = chain.save(chain.current(), candidate.content)
                _update_turn_status(topic_id, operation_id, 'awaiting_user' if doc['status'] == 'awaiting_user' else 'completed', saved.version_id)
            else:
                _deliver_saved_research(operation_id, account_id, reason=None)
            RESEARCH_WORK.delete(operation_id)
        turn_instructions.fenced(*identity, operation_id, deliver, allow_sealed=True, allow_waiting=True)
        return
    pending = cp.get('pending') or {}
    last_failure = next((e for e in reversed(cp.get('events', [])) if e.get('kind') == 'model_failure'), {})
    cursor = pending.get('cursor', 0)
    calls = pending.get('calls', [])
    action = f"{pending.get('call_id')}:{cursor}"
    review_pending = cp.get('_semantic_pending') or {}
    returned_review = (pending.get('kind') == 'tool_batch' and type(cursor) is int and 0 <= cursor < len(calls)
                       and calls[cursor].get('name') == 'submit_research'
                       and (review_pending.get('phase') == 'returned' or cp.get('_semantic_replay_action') == action))
    claimed = (pending.get('kind') == 'tool_batch' and turn_instructions.action_status(
        *identity, operation_id, action,
        cp.get('model_instruction_sequence', 0)) == 'claimed_unknown' and not returned_review)
    if claimed or last_failure.get('external_state') == 'unknown' or pending.get('kind') not in {
            None, 'model_returned', 'tool_batch'}:
        def finish_unknown() -> None:
            _update_turn_status(topic_id, operation_id, 'failed')
            _deliver_saved_research(operation_id, account_id, reason='external_result_unknown')
            RESEARCH_WORK.delete(operation_id)
        turn_instructions.seal(*identity, doc['sequence'], operation_id=operation_id, persist=finish_unknown)
        return
    if not cp:
        # The seed lives in the admission journal; prior to the first checkpoint no model was sent.
        # An unstarted worker may be adopted using the same admission guard and original binding.
        retryable = any(ev.get('event') == 'operation.error' and ev.get('retryable') is True
                        for ev in operations.store.get_events(operation_id))
        if not retryable or inputs.get('recovery_pending') or not inputs.get('runtime_request'):
            def end_without_version() -> None:
                _update_turn_status(topic_id, operation_id, 'failed')
                RESEARCH_WORK.delete(operation_id)
            turn_instructions.seal(*identity, doc['sequence'], operation_id=operation_id,
                                   persist=end_without_version)
            return
    request = Request({'type': 'http', 'headers': [], 'method': 'POST', 'path': '/'})
    request.state.client_ip = work.get('client_ip', '0.0.0.0')
    request.state.session_id = work.get('session_id')
    try:
        if cp:
            _continue_research_for_account(topic_id, operation_id, Response(), 'auto-' + operation_id,
                                           request, account, background=True)
        else:
            seed = ResearchRunRequest.model_validate(inputs['runtime_request'])
            request.state.research_continuation = {
                'operation_id': operation_id, 'source_ids': [row['source_id'] for row in inputs['sources']],
                'captured_at': inputs['runtime_origin_captured_at'], 'parent_version_id': inputs['parent_version_id'],
                'unstarted': True}
            _start_research_for_account(topic_id, seed, Response(), 'auto-' + operation_id, request, account,
                                        background=True)
    except (InstructionConflict, ApiProblem) as error:
        if RESEARCH_WORK.get(operation_id) is None:
            return  # Another reconciler admitted the same continuation.
        if isinstance(error, ApiProblem) and error.retryable:
            return
        # Unknown external work or a changed binding cannot authorize another external call.
        def close_local() -> None:
            _update_turn_status(topic_id, operation_id, 'failed')
            _deliver_saved_research(operation_id, account_id, reason='recovery_unavailable')
            RESEARCH_WORK.delete(operation_id)
        turn_instructions.seal(*identity, doc['sequence'], operation_id=operation_id, persist=close_local)


def _topic_has_active_run(account_id: str, topic_id: str) -> bool:
    with _ACTIVE_SQUAD_LOCK:
        active = _ACTIVE_SQUAD_RUNS.get((account_id, topic_id))
        if active is not None:
            thread_obj = active.get("thread") if isinstance(active, Mapping) else None
            if thread_obj is None or (callable(getattr(thread_obj, "is_alive", None)) and thread_obj.is_alive()):
                return True
    if any(work['account_id'] == account_id and work['topic_id'] == topic_id for work in RESEARCH_WORK.values()):
        return True
    endpoint = f"/v1/research/topics/{topic_id}/runs"
    squad_state = EARNINGS_SQUAD_STATES.get(_squad_state_key(account_id, topic_id))
    for operation_id, session_id in operations.store.session_ids().items():
        if session_id != topic_id:
            continue
        record = operations.store.get_operation(operation_id)
        if not (record and record.get("endpoint") == endpoint and record.get("owner_subject") == f"account:{account_id}"
                and record.get("status") in {"pending", "running"}):
            continue
        inputs = RESEARCH_RUN_INPUTS.get(operation_id) or {}
        if (
            inputs.get("run_mode") == "earnings_squad"
            and isinstance(squad_state, Mapping)
            and squad_state.get("run_id") == inputs.get("squad_run_id")
            and squad_state.get("status") in {"completed", "failed", "cancelled"}
        ):
            continue
        return True
    return False


@app.patch("/v2/research/topics/{topic_id}", response_model=ResearchTopic, include_in_schema=False)
def update_research_topic(topic_id: str, request: TopicUpdate, authorization: str | None = Header(default=None)) -> ResearchTopic:
    """Rename or pin. The title is separate from the research question, which stays as asked."""
    account = _authenticated_account(authorization)
    try:
        return _visible_topic(research_topics.update(account.account_id, topic_id, request))
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error


@app.delete("/v2/research/topics/{topic_id}", response_model=TopicDeletion, include_in_schema=False)
def delete_research_topic(topic_id: str, authorization: str | None = Header(default=None)) -> TopicDeletion:
    """Soft delete: hidden at once, restorable until purge_after, purged afterwards."""
    account = _authenticated_account(authorization)
    try:
        research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error
    if _topic_has_active_run(account.account_id, topic_id):
        raise ApiProblem(409, "lifecycle_conflict", "Stop the running research before deleting it.")
    return research_topics.soft_delete(account.account_id, topic_id)


@app.post("/v2/research/topics/{topic_id}/fork", response_model=ResearchTopic, status_code=201, include_in_schema=False)
def fork_research_topic(topic_id: str, request: TopicForkInput, authorization: str | None = Header(default=None)) -> ResearchTopic:
    """A new research from one version up to a turn; it records where it came from."""
    account = _authenticated_account(authorization)
    try:
        return _visible_topic(research_topics.fork(account.account_id, topic_id, request))
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic or version not found.") from error
    except ValueError as error:
        raise ApiProblem(422, "invalid_request", "The selected turn is not in this version.") from error


@app.post("/v2/research/topics/{topic_id}/restore", response_model=ResearchTopic, include_in_schema=False)
def restore_research_topic(topic_id: str, authorization: str | None = Header(default=None)) -> ResearchTopic:
    account = _authenticated_account(authorization)
    try:
        return _visible_topic(research_topics.restore(account.account_id, topic_id))
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error


@app.get("/v2/research/topics/{topic_id}/runs", response_model=list[ResearchRunListEntry], response_model_exclude_none=True, include_in_schema=False)
def list_research_runs(topic_id: str, http_request: Request) -> list[OperationResponse]:
    account = _authenticated_account(http_request.headers.get("Authorization"))
    try:
        topic = research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error
    endpoint = f"/v1/research/topics/{topic_id}/runs"
    squad_state = EARNINGS_SQUAD_STATES.get(_squad_state_key(account.account_id, topic_id))
    result: list[OperationResponse] = []
    for operation_id, session_id in operations.store.session_ids().items():
        if session_id != topic_id:
            continue
        record = operations.store.get_operation(operation_id)
        if not record or record.get("endpoint") != endpoint or record.get("owner_subject") != f"account:{account.account_id}":
            continue
        op = operations._cache_operation(operation_id)
        turn_id = (TURN_RECORDS.get(operation_id) or {}).get('turn_id')
        doc = TURN_RECORDS.get(turn_id) if turn_id else None
        inputs = RESEARCH_RUN_INPUTS.get(operation_id) or {}
        fields: dict[str, Any] = {}
        if inputs.get("run_mode") == "earnings_squad":
            squad_status_now = (
                squad_state.get("status")
                if isinstance(squad_state, Mapping) and squad_state.get("run_id") == inputs.get("squad_run_id")
                else None
            )
            if op.status == "cancelled" or squad_status_now == "cancelled":
                logical_status = "stopped"
            elif squad_status_now in {"completed", "failed"}:
                logical_status = squad_status_now
            elif op.status in {"pending", "running"} or squad_status_now == "running":
                logical_status = "running"
            elif op.status == "completed":
                logical_status = "completed"
            else:
                logical_status = "failed"
            dossier_raw = squad_state.get("dossier") if isinstance(squad_state, Mapping) and isinstance(squad_state.get("dossier"), Mapping) else {}
            fields = {
                "turn_id": turn_id or (inputs.get("turn") or {}).get("turn_id"),
                "current_operation_id": operation_id,
                "logical_status": logical_status,
                "run_mode": "earnings_squad",
                "squad_run_id": inputs.get("squad_run_id"),
                "ticker": inputs.get("ticker") or dossier_raw.get("symbol"),
                "cik": inputs.get("cik") or dossier_raw.get("cik"),
                "entity_id": inputs.get("entity_id") or dossier_raw.get("entity_id"),
                "company_name": inputs.get("company_name") or dossier_raw.get("company_name"),
            }
        elif doc and doc['account_id'] == account.account_id:
            version = next((v for v in reversed(topic.versions) if _agent_turn_for(v, doc['operation_id'])), None)
            fields = {'turn_id': turn_id, 'current_operation_id': doc['operation_id'],
                      'logical_status': _logical_run_status(doc, op.status, version)}
        result.append(ResearchRunListEntry(operation=op, **fields))
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
    return {key: value for key, value in record.items() if key not in {"owner_id", "recovery_snapshot", "recovery_pending", "recovery_continuation_operation_id", "recovery_terminal_checkpoint_version_id", "recovery_terminal_stage_receipts", "private_validation_diagnostics", "private_acquisition_decisions", "runtime_checkpoint", "runtime_request", "runtime_binding", "runtime_continuation_operation_id", "failed_checkpoint_schema_version", "checkpoint_migrated", "checkpoint_repaired"}}


def _authorize_research_run_progress(
    account_id: str, topic_id: str, operation_id: str
) -> tuple[ResearchTopic, Operation, dict[str, Any]]:
    try:
        topic = research_topics.get(account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error
    operations.require_owner(operation_id, f"account:{account_id}")
    operation = operations._cache_operation(operation_id)
    if operation.endpoint != f"/v1/research/topics/{topic_id}/runs":
        raise ApiProblem(404, "not_found", "Research run not found.")
    inputs = RESEARCH_RUN_INPUTS.get(operation_id)
    if not inputs or inputs.get("owner_id") != account_id or inputs.get("topic_id") != topic_id:
        raise ApiProblem(404, "not_found", "Research run not found.")
    return topic, operation, inputs


def _build_research_run_progress_view(
    topic: ResearchTopic, operation: Operation, inputs: Mapping[str, Any], *, after: int
) -> dict[str, Any]:
    if inputs.get("run_mode") == "earnings_squad":
        progress = build_progress(
            operation_id=operation.operation_id,
            operation_status=str(operation.status),
            inputs=inputs,
            checkpoint=None,
            saved_turn=None,
            version_id=None,
            after=after,
        )
        squad_state = EARNINGS_SQUAD_STATES.get(_squad_state_key(topic.owner_id, topic.topic_id)) or {}
        squad_status = squad_state.get("status") if isinstance(squad_state, Mapping) else None
        dossier_raw = squad_state.get("dossier") if isinstance(squad_state, Mapping) and isinstance(squad_state.get("dossier"), Mapping) else {}
        if operation.status == "cancelled" or squad_status == "cancelled":
            logical = "stopped"
        elif operation.status in {"pending", "running"} or squad_status == "running":
            logical = "running"
        elif operation.status == "completed" or squad_status == "completed":
            logical = "completed"
        else:
            logical = "failed"
        progress.update({
            "logical_status": logical,
            "current_operation_id": operation.operation_id,
            "run_mode": "earnings_squad",
            "squad_run_id": inputs.get("squad_run_id"),
            "ticker": inputs.get("ticker") or dossier_raw.get("symbol"),
            "cik": inputs.get("cik") or dossier_raw.get("cik"),
            "version_id": dossier_raw.get("dossier_id"),
            "topic_title": topic.title,
        })
        return progress
    version = next(
        (
            v
            for v in reversed(topic.versions)
            if v.created_by == "agent" and v.content.turns and operation.operation_id in v.content.turns[-1].operation_ids
        ),
        None,
    )
    progress = build_progress(
        operation_id=operation.operation_id,
        operation_status=str(operation.status),
        inputs=inputs,
        checkpoint=inputs.get("runtime_checkpoint") or None,
        saved_turn=version.content.turns[-1] if version else None,
        version_id=version.version_id if version else None,
        after=after,
    )

    turn_id = (inputs.get('turn') or {}).get('turn_id')
    doc = TURN_RECORDS.get(turn_id) if turn_id else None
    if doc and doc['account_id'] == topic.owner_id and doc['topic_id'] == topic.topic_id:
        progress.update(_turn_progress_fields(doc, operation.status, version))
    # Lets an open page pick up a title summarised while the first turn runs.
    progress['topic_title'] = topic.title
    return progress


def _logical_run_status(doc: dict, operation_status: str, version: ResearchVersion | None) -> str:
    operation_id = doc['operation_id']
    if doc['status'] == 'stopped' or operation_status == 'cancelled':
        status = 'stopped'
    elif doc['status'] == 'awaiting_user':
        status = 'awaiting_user'
    elif version and _agent_turn_for(version, doc['operation_id']):
        status = 'completed'
    elif RESEARCH_WORK.get(operation_id) is not None:
        status = 'running'
    else:
        status = 'failed'
    return status


def _turn_progress_fields(doc: dict, operation_status: str, version: ResearchVersion | None) -> dict:
    return {'instructions': _instruction_view(doc), 'logical_status': _logical_run_status(doc, operation_status, version),
            'current_operation_id': doc['operation_id'], 'instruction_sequence': doc['sequence'],
            'clarification': doc.get('clarification') if doc['status'] == 'awaiting_user' else None}



@app.get("/v2/research/favicons/{host}", include_in_schema=False)
def get_research_favicon(host: str, authorization: str | None = Header(default=None)) -> Response:
    """A website's own icon, fetched by the server; the page cannot load images from other origins."""
    _authenticated_account(authorization)
    from .research_favicons import favicon, normalize_host
    normalized = normalize_host(host)
    icon = favicon(normalized) if normalized else None
    if icon is None:
        raise ApiProblem(404, "not_found", "No icon for this site.")
    return Response(content=icon[0], media_type=icon[1],
                    headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


@app.get("/v2/research/topics/{topic_id}/runs/{operation_id}/progress", include_in_schema=False)
def get_research_run_progress(topic_id: str, operation_id: str, http_request: Request,
                              after: Annotated[int, Query(ge=0)] = 0) -> dict:
    """Return a sanitized view of the latest persisted checkpoint (whitelisted fields only)."""
    account = _authenticated_account(http_request.headers.get("Authorization"))
    topic, operation, inputs = _authorize_research_run_progress(account.account_id, topic_id, operation_id)
    return _build_research_run_progress_view(topic, operation, inputs, after=after)


def _sse_terminal_frame(operation_id: str, status: str, version_id: str | None) -> str:
    return format_sse_frame(
        "run.terminal",
        {"operation_id": operation_id, "operation_status": status, "version_id": version_id},
        event_id=f"done:{status}",
    )


@app.get(
    "/v2/research/topics/{topic_id}/runs/{operation_id}/events",
    response_class=StreamingResponse,
    include_in_schema=False,
)
async def stream_research_run_events(
    topic_id: str,
    operation_id: str,
    http_request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Stream sanitized live research progress over Server-Sent Events."""
    account = _authenticated_account(http_request.headers.get("Authorization"))
    topic, operation, inputs = _authorize_research_run_progress(account.account_id, topic_id, operation_id)
    cursor_after, _ = parse_last_event_id(last_event_id)
    effective_after = max(after, cursor_after)

    def load_progress(progress_after: int) -> dict[str, Any]:
        topic_now = research_topics.get(account.account_id, topic_id)
        op_now = operations._cache_operation(operation_id)
        inputs_now = RESEARCH_RUN_INPUTS.get(operation_id) or inputs
        return _build_research_run_progress_view(topic_now, op_now, inputs_now, after=progress_after)

    sse_headers = {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    loop = asyncio.get_running_loop()
    subscriber = research_live_broker.subscribe(operation_id, account_id=account.account_id, loop=loop)
    try:
        try:
            snapshot = load_progress(effective_after)
        except TopicNotFound as error:
            raise ApiProblem(404, "not_found", "Research topic not found.") from error
    except BaseException:
        research_live_broker.unsubscribe(subscriber)
        raise
    if snapshot["operation_status"] in {"completed", "failed", "cancelled"}:
        research_live_broker.unsubscribe(subscriber)

        async def terminal_only_stream():
            yield format_sse_frame("progress.snapshot", snapshot, event_id=f"cp:{snapshot['event_count']}")
            yield _sse_terminal_frame(operation_id, snapshot["operation_status"], snapshot["version_id"])

        return StreamingResponse(terminal_only_stream(), media_type="text/event-stream", headers=sse_headers)

    async def live_stream():
        next_event_index = max(effective_after, int(snapshot.get("event_count") or 0))
        try:
            yield format_sse_frame("progress.snapshot", snapshot, event_id=f"cp:{snapshot['event_count']}")
            while True:
                if subscriber.needs_resync:
                    subscriber.needs_resync = False
                    resync = load_progress(next_event_index)
                    next_event_index = max(next_event_index, int(resync.get("event_count") or 0))
                    yield format_sse_frame("progress.snapshot", resync, event_id=f"cp:{resync['event_count']}")
                    if resync["operation_status"] in {"completed", "failed", "cancelled"}:
                        yield _sse_terminal_frame(operation_id, resync["operation_status"], resync["version_id"])
                        return
                try:
                    frame = await asyncio.wait_for(
                        subscriber.queue.get(),
                        timeout=research_live_broker.heartbeat_seconds,
                    )
                except asyncio.TimeoutError:
                    if await http_request.is_disconnected():
                        return
                    current_progress = load_progress(next_event_index)
                    for ev in current_progress["events"]:
                        ev_idx = ev.get("index")
                        if type(ev_idx) is int and ev_idx >= next_event_index:
                            next_event_index = ev_idx + 1
                            yield format_sse_frame(
                                "step.event",
                                {
                                    "operation_id": operation_id,
                                    "event": ev,
                                    "event_count": current_progress["event_count"],
                                    "usage": current_progress["usage"],
                                    "interim": current_progress["interim"],
                                    "updated_at": current_progress["updated_at"],
                                },
                                event_id=f"ev:{ev_idx}",
                            )
                    if current_progress["operation_status"] in {"completed", "failed", "cancelled"}:
                        yield _sse_terminal_frame(
                            operation_id, current_progress["operation_status"], current_progress["version_id"]
                        )
                        return
                    yield ": heartbeat\n\n"
                    continue
                event_name = frame.get("event")
                data = frame.get("data") or {}
                if event_name == "step.event":
                    ev_obj = data.get("event") if isinstance(data, dict) else None
                    ev_idx = ev_obj.get("index") if isinstance(ev_obj, dict) else None
                    if type(ev_idx) is int:
                        if ev_idx < next_event_index:
                            continue
                        next_event_index = ev_idx + 1
                    yield format_sse_frame("step.event", data, event_id=frame.get("id"))
                elif event_name == "run.terminal":
                    if subscriber.needs_resync:
                        subscriber.needs_resync = False
                        resync = load_progress(next_event_index)
                        next_event_index = max(next_event_index, int(resync.get("event_count") or 0))
                        yield format_sse_frame("progress.snapshot", resync, event_id=f"cp:{resync['event_count']}")
                    yield format_sse_frame("run.terminal", data, event_id=frame.get("id"))
                    return
                else:
                    yield format_sse_frame(str(event_name), data, event_id=frame.get("id"))
        finally:
            research_live_broker.unsubscribe(subscriber)

    return StreamingResponse(live_stream(), media_type="text/event-stream", headers=sse_headers)


@app.get('/v2/research/topics/{topic_id}/runs/{operation_id}/settlement', include_in_schema=False)
def get_research_run_settlement(topic_id: str, operation_id: str, http_request: Request) -> dict:
    """Account-scoped, read-only reconciliation without inferring provider usage."""
    account = _authenticated_account(http_request.headers.get('Authorization'))
    try:
        topic = research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, 'not_found', 'Research run not found.') from error
    operations.require_owner(operation_id, f'account:{account.account_id}')
    operation = operations.store.get_operation(operation_id)
    if not operation or operation.get('endpoint') != f'/v1/research/topics/{topic_id}/runs':
        raise ApiProblem(404, 'not_found', 'Research run not found.')
    record = RESEARCH_RUN_INPUTS.get(operation_id)
    if not record or record.get('owner_id') != account.account_id or record.get('topic_id') != topic_id:
        raise ApiProblem(404, 'not_found', 'Research run inputs not found.')
    call_ids = set((record.get('runtime_checkpoint') or {}).get('_accounting', {}).get('model_call_receipts', {}))
    prefix = f'model:{operation_id}'
    entries = [entry for key, entry in billing_ledger.usage.items()
               if (key == prefix or key.startswith(prefix + ':') or key in {f'model:{item}' for item in call_ids})
               and entry.account_id == account.account_id]
    ledger = {f'{state}_calls': sum(entry.status == state for entry in entries)
              for state in ('reserved', 'settled', 'released')}
    ledger['accounted_tokens'] = sum(entry.actual_tokens or 0 for entry in entries if entry.status == 'settled')
    latest = next((v for v in reversed(topic.versions) if v.created_by == 'agent' and v.content.turns
                   and operation_id in v.content.turns[-1].operation_ids), None)
    reported = latest.content.turns[-1].usage.total_tokens if latest else None
    pending = record.get('recovery_pending')
    return {'operation_status': operation.get('status'),
            'checkpoint_version_id': latest.version_id if latest else None,
            'model_ledger': ledger, 'unresolved_stage_count': len(pending) if isinstance(pending, list) else None,
            'reported_tokens': reported if type(reported) is int and reported >= 0 else None,
            'reported_tokens_scope': 'run_chain_including_prior_continuations',
            'cost_status': 'unknown',
            'interpretation': 'Ledger tokens are conservative accounted amounts, not proof of provider usage or cost.'}


# --------------------------------------------------------------------------- US Earnings Buy-Side Squad API (Phase A2)
class EarningsSquadApiRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str | None = Field(default=None, max_length=2000)
    entity_id: str | None = Field(default=None, max_length=120)
    cik: str | None = Field(default=None, max_length=16)
    symbol: str | None = Field(default=None, max_length=32)
    company_name: str | None = Field(default=None, max_length=300)
    sic_code: str | None = Field(default=None, max_length=8)
    sic_description: str | None = Field(default=None, max_length=200)
    period_label: str | None = Field(default=None, max_length=100)
    as_of: date | datetime | None = None
    close_price: Decimal | float | int | str | None = None
    filing_form: str | None = Field(default="10-Q", max_length=16)
    filing_accession: str | None = Field(default=None, max_length=40)
    prior_filing_accession: str | None = Field(default=None, max_length=40)
    fiscal_year: int | None = Field(default=None, ge=1900, le=2200)
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)
    period_financials: list[dict[str, Any]] = Field(default_factory=list)
    guidance_inputs: list[dict[str, Any]] = Field(default_factory=list)
    nongaap_bridges: list[dict[str, Any]] = Field(default_factory=list)
    execution_mode: Literal["sync", "async"] = "sync"
    background: bool = False
    run_id: str | None = Field(default=None, max_length=120)
    resume_from_checkpoint: bool = False
    resume_run_id: str | None = Field(default=None, max_length=120)
    user_instructions: list[str] = Field(default_factory=list)
    max_model_calls: int | None = Field(default=None, ge=1)
    max_search_calls: int | None = Field(default=None, ge=0)
    max_total_tokens: int | None = Field(default=None, ge=1)
    max_elapsed_seconds: float | None = Field(default=None, gt=0)
    slot_overrides: ModelSlotConfig | None = None


class EarningsSquadRunResponse(BaseModel):
    topic_id: str
    run_id: str
    operation_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    run_mode: Literal["earnings_squad"] = "earnings_squad"
    ticker: str | None = None
    cik: str | None = None
    dossier: EarningsDossier | None = None
    org_events: list[OrgGraphEvent] = Field(default_factory=list)
    slot_routing_summary: dict[str, Any] = Field(default_factory=dict)
    checkpoint_completed_stages: list[str] = Field(default_factory=list)
    error: str | None = None
    rejection_details: dict[str, Any] | None = None


class EarningsSquadDossierResponse(BaseModel):
    topic_id: str
    run_id: str
    operation_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    run_mode: Literal["earnings_squad"] = "earnings_squad"
    ticker: str | None = None
    cik: str | None = None
    dossier: EarningsDossier | None = None
    org_events: list[OrgGraphEvent] = Field(default_factory=list)
    slot_routing_summary: dict[str, Any] = Field(default_factory=dict)
    steering_instructions: list[UserSteeringInstruction] = Field(default_factory=list)
    checkpoint_completed_stages: list[str] = Field(default_factory=list)
    error: str | None = None
    rejection_details: dict[str, Any] | None = None


class EarningsSquadDeepReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = False


class EarningsSquadDeepReportResponse(BaseModel):
    topic_id: str
    run_id: str
    dossier_id: str
    dossier_sha256: str
    cached: bool
    deep_report_tool_calls: int = 0
    deep_report_markdown: str
    dossier: EarningsDossier


class EarningsSquadSteerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)


class EarningsSquadSteerResponse(BaseModel):
    topic_id: str
    run_id: str
    operation_id: str
    instruction: UserSteeringInstruction
    status: Literal["queued", "persisted_for_resume"]


def set_earnings_squad_runner_hook(hook: Any) -> None:
    """Install or clear an optional prebuilt/live squad runner hook on ``app.state``."""
    app.state.earnings_squad_hook = hook


def _squad_state_key(account_id: str, topic_id: str) -> str:
    return f"{account_id}:{topic_id}"


def _squad_checkpoint_key(account_id: str, topic_id: str, run_id: str) -> str:
    return f"{account_id}:{topic_id}:{run_id}"


class _RepoSquadCheckpointStore:
    """Repository-backed ``SquadCheckpointStore`` preserving subtask checkpoints safely."""

    def __init__(self, *, account_id: str, topic_id: str, state_lock_key: str) -> None:
        self._account_id = account_id
        self._topic_id = topic_id
        self._state_key = state_lock_key
        self._mem = InMemorySquadCheckpointStore()

    def save(self, checkpoint: SquadCheckpoint) -> None:
        self._mem.save(checkpoint)
        dumped = checkpoint.model_dump(mode="json")
        cp_key = _squad_checkpoint_key(self._account_id, self._topic_id, checkpoint.run_id)
        EARNINGS_SQUAD_CHECKPOINTS.put(cp_key, {
            "account_id": self._account_id,
            "topic_id": self._topic_id,
            "run_id": checkpoint.run_id,
            "checkpoint": dumped,
            "updated_at": lifecycle_clock.now().isoformat(),
        })

        def update_state() -> None:
            current = EARNINGS_SQUAD_STATES.get(self._state_key)
            if not isinstance(current, Mapping) or current.get("run_id") != checkpoint.run_id:
                return
            updated = dict(current)
            updated["checkpoint"] = dumped
            updated["checkpoint_completed_stages"] = list(checkpoint.completed_stages)
            updated["org_events"] = [ev.model_dump(mode="json") for ev in checkpoint.org_events]
            if checkpoint.dossier is not None:
                updated["dossier"] = checkpoint.dossier.model_dump(mode="json")
            updated["updated_at"] = lifecycle_clock.now().isoformat()
            EARNINGS_SQUAD_STATES.put(self._state_key, updated)

        EARNINGS_SQUAD_STATES.with_advisory_lock(f"squad-state:{self._state_key}", update_state)

    def load(self, run_id: str) -> SquadCheckpoint | None:
        loaded = self._mem.load(run_id)
        if loaded is not None:
            return loaded
        cp_key = _squad_checkpoint_key(self._account_id, self._topic_id, run_id)
        row = EARNINGS_SQUAD_CHECKPOINTS.get(cp_key)
        if isinstance(row, Mapping) and isinstance(row.get("checkpoint"), Mapping):
            return SquadCheckpoint.model_validate(row["checkpoint"])
        return None


_SQUAD_DEFAULT_TIMEOUT_SECONDS = 90.0
_SQUAD_STAGE_TIMEOUT_FLOORS: dict[str, float] = {
    "stage_0_prefetch": 60.0,
    "stage_1_plan": 90.0,
    "stage_2_workers": 90.0,
    "stage_2_5_review": 90.0,
    "stage_3a_bear": 120.0,
    "stage_3b_gate_memo": 120.0,
    "deep_report": 180.0,
    "steering": 90.0,
}
_SQUAD_SLOT_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "fast_extractor": 8192,
    "deep_reasoner": 8192,
    "bear_reviewer": 8192,
    "writer": 16384,
}
_SQUAD_STAGE_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "stage_1_plan": 4096,
    "stage_2_workers": 8192,
    "stage_2_5_review": 4096,
    "stage_3a_bear": 8192,
    "stage_3b_gate_memo": 8192,
    "deep_report": 16384,
    "steering": 4096,
}


class _CompositeCancelSignal:
    """Thread-safe cancel signal combining a local Event with an optional lazy poller."""

    def __init__(self, base_event: Any = None, checker: Callable[[], bool] | None = None) -> None:
        self._base_event = base_event
        self._checker = checker

    def is_set(self) -> bool:
        if self._base_event is not None and self._base_event.is_set():
            return True
        if self._checker is not None:
            try:
                if self._checker():
                    if self._base_event is not None and hasattr(self._base_event, "set"):
                        self._base_event.set()
                    return True
            except Exception:
                return False
        return False

    def set(self) -> None:
        if self._base_event is not None and hasattr(self._base_event, "set"):
            self._base_event.set()

    def wait(self, timeout: float | None = None) -> bool:
        if self.is_set():
            return True
        if timeout is None or timeout <= 0:
            return self.is_set()
        if self._checker is None and self._base_event is not None and hasattr(self._base_event, "wait"):
            return bool(self._base_event.wait(timeout))
        deadline = monotonic() + timeout
        while True:
            if self.is_set():
                return True
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            slice_sec = min(remaining, 0.1)
            if self._base_event is not None and hasattr(self._base_event, "wait"):
                self._base_event.wait(slice_sec)
            else:
                sleep(slice_sec)


def _squad_provider_timeout_floor() -> float:
    raw_squad = os.environ.get("FORESIGHTNINE_SQUAD_LLM_TIMEOUT_SECONDS", "").strip()
    if raw_squad:
        try:
            val = float(raw_squad)
            if math.isfinite(val) and 0 < val <= 600:
                return val
        except ValueError:
            pass
    raw_env = os.environ.get("FORESIGHTNINE_LLM_TIMEOUT_SECONDS", "").strip()
    if raw_env:
        from .openai_compatible import _llm_timeout_from_environment
        return _llm_timeout_from_environment()
    return 180.0


def _with_squad_provider_timeout_floor(provider: Any) -> Any:
    """Return a shallow copy of provider with squad timeout floor applied when env timeout is unset."""
    if provider is None:
        return None
    if os.environ.get("FORESIGHTNINE_LLM_TIMEOUT_SECONDS", "").strip() and not os.environ.get("FORESIGHTNINE_SQUAD_LLM_TIMEOUT_SECONDS", "").strip():
        return provider
    policy = getattr(provider, "policy", None)
    if not isinstance(policy, ProviderPolicy):
        return provider
    floor = _squad_provider_timeout_floor()
    if policy.timeout_seconds >= floor:
        return provider
    try:
        cloned = copy(provider)
        cloned.policy = dataclass_replace(policy, timeout_seconds=floor)
        return cloned
    except Exception:
        return provider


def _reconcile_orphaned_earnings_squads() -> int:
    """Reconcile orphaned 'running' squad states after an API restart into checkpoint-resumable 'failed'."""
    reconciled = 0
    for state_key, row in list(EARNINGS_SQUAD_STATES.items()):
        if not isinstance(row, Mapping) or row.get("status") != "running":
            continue
        account_id = str(row.get("account_id") or "")
        topic_id = str(row.get("topic_id") or "")
        op_id = str(row.get("operation_id") or "")
        if not account_id or not topic_id:
            continue
        with _ACTIVE_SQUAD_LOCK:
            active = _ACTIVE_SQUAD_RUNS.get((account_id, topic_id))
            thread_obj = active.get("thread") if isinstance(active, Mapping) else None
            is_live = (
                active is not None
                and active.get("operation_id") == op_id
                and (thread_obj is None or (callable(getattr(thread_obj, "is_alive", None)) and thread_obj.is_alive()))
            )
        if is_live:
            continue

        did_reconcile = False
        events_snapshot: list[Any] = []

        def _mark_orphaned() -> None:
            nonlocal did_reconcile, events_snapshot
            cur = EARNINGS_SQUAD_STATES.get(state_key)
            if not isinstance(cur, Mapping) or cur.get("status") != "running":
                return
            if op_id and str(cur.get("operation_id") or "") != op_id:
                return
            with _ACTIVE_SQUAD_LOCK:
                cur_active = _ACTIVE_SQUAD_RUNS.get((account_id, topic_id))
                cur_thread = cur_active.get("thread") if isinstance(cur_active, Mapping) else None
                still_live = (
                    cur_active is not None
                    and cur_active.get("operation_id") == op_id
                    and (cur_thread is None or (callable(getattr(cur_thread, "is_alive", None)) and cur_thread.is_alive()))
                )
                if still_live:
                    return
                if cur_active is not None and cur_active.get("operation_id") == op_id:
                    _ACTIVE_SQUAD_RUNS.pop((account_id, topic_id), None)
            completed_stages = list(cur.get("checkpoint_completed_stages") or [])
            cp_raw = cur.get("checkpoint")
            if not completed_stages and isinstance(cp_raw, Mapping):
                completed_stages = list(cp_raw.get("completed_stages") or [])
            err_msg = (
                f"Earnings squad run interrupted by API restart (saved checkpoints: {', '.join(completed_stages)}). Resume from checkpoint to continue."
                if completed_stages
                else "Earnings squad run interrupted by API restart before completing a checkpoint. Retry or resume from checkpoint."
            )
            updated = dict(cur)
            updated["status"] = "failed"
            updated["error"] = err_msg
            updated["error_code"] = "worker_interrupted_by_restart"
            updated["can_resume_checkpoint"] = True
            updated["updated_at"] = lifecycle_clock.now().isoformat()
            EARNINGS_SQUAD_STATES.put(state_key, updated)
            events_snapshot = list(updated.get("org_events") or [])
            did_reconcile = True

        EARNINGS_SQUAD_STATES.with_advisory_lock(f"squad-state:{state_key}", _mark_orphaned)
        if did_reconcile:
            reconciled += 1
            if op_id:
                _update_turn_status(topic_id, op_id, "failed")
                try:
                    research_live_broker.publish_terminal(
                        op_id,
                        operation_status="failed",
                        progress={
                            "org_events": events_snapshot,
                            "logical_status": "failed",
                            "current_operation_id": op_id,
                        },
                    )
                except Exception:
                    logging.getLogger(__name__).exception("Unable to publish terminal event for orphaned squad %s", op_id)
    return reconciled


class _AccountSlotModelRunner:
    """Adapter bridging ``SquadModelRunner`` to ForesightNine wire ``LlmProvider`` instances."""

    def __init__(
        self,
        *,
        providers_by_slot: Mapping[str, Any],
        default_provider: Any,
        cancel_event: Any = None,
        cancel_checker: Callable[[], bool] | None = None,
        budget: ResearchExecutionBudget | None = None,
        slot_max_output_tokens: Mapping[str, int] | None = None,
        slot_timeouts: Mapping[str, float] | None = None,
        default_timeout_seconds: float | None = None,
        max_transient_retries: int = 2,
        retry_base_seconds: float = 0.5,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._providers = dict(providers_by_slot)
        self._default_provider = default_provider
        self._cancel_event = cancel_event
        self._cancel_checker = cancel_checker
        self._budget = budget
        self._slot_max_output_tokens = dict(slot_max_output_tokens or {})
        self._slot_timeouts = dict(slot_timeouts or {})
        self._default_timeout_seconds = default_timeout_seconds
        self._max_transient_retries = max(0, int(max_transient_retries))
        self._retry_base_seconds = max(0.0, float(retry_base_seconds))
        self._sleeper = sleeper
        self._lock = RLock()
        self._next_gen = 0
        self._turns: dict[tuple[str, str, str], tuple[int, str, list[Any], Any]] = {}

    def bind_runtime(
        self,
        *,
        cancel_event: Any = None,
        cancel_checker: Callable[[], bool] | None = None,
        budget: ResearchExecutionBudget | None = None,
    ) -> None:
        with self._lock:
            if cancel_event is not None:
                self._cancel_event = cancel_event
            if cancel_checker is not None:
                self._cancel_checker = cancel_checker
            if budget is not None:
                self._budget = budget

    def _effective_cancel_signal(self) -> Any:
        if self._cancel_checker is not None:
            return _CompositeCancelSignal(self._cancel_event, self._cancel_checker)
        return self._cancel_event

    def _resolve_timeout_seconds(self, mreq: SlotModelRequest) -> float:
        if mreq.slot in self._slot_timeouts:
            base = float(self._slot_timeouts[mreq.slot])
        elif self._default_timeout_seconds is not None:
            base = float(self._default_timeout_seconds)
        else:
            raw_squad = os.environ.get("FORESIGHTNINE_SQUAD_LLM_TIMEOUT_SECONDS", "").strip()
            if raw_squad:
                try:
                    val = float(raw_squad)
                    if math.isfinite(val) and 0 < val <= 600:
                        base = val
                    else:
                        base = _SQUAD_STAGE_TIMEOUT_FLOORS.get(mreq.stage, _SQUAD_DEFAULT_TIMEOUT_SECONDS)
                except ValueError:
                    base = _SQUAD_STAGE_TIMEOUT_FLOORS.get(mreq.stage, _SQUAD_DEFAULT_TIMEOUT_SECONDS)
            else:
                base = _SQUAD_STAGE_TIMEOUT_FLOORS.get(mreq.stage, _SQUAD_DEFAULT_TIMEOUT_SECONDS)
        if self._budget is not None and math.isfinite(self._budget.deadline):
            remaining = self._budget.deadline - monotonic()
            if remaining <= 0:
                raise ProviderFailure("provider_budget_exhausted", "Research time budget is exhausted.", retryable=False)
            return min(base, remaining)
        return base

    def _resolve_max_output_tokens(self, mreq: SlotModelRequest) -> int:
        req_cap = getattr(mreq, "max_output_tokens", None)
        if type(req_cap) is int and req_cap > 0:
            return req_cap
        slot_cap = self._slot_max_output_tokens.get(mreq.slot)
        if type(slot_cap) is int and slot_cap > 0:
            return slot_cap
        return _SQUAD_STAGE_MAX_OUTPUT_TOKENS.get(mreq.stage) or _SQUAD_SLOT_MAX_OUTPUT_TOKENS.get(mreq.slot, 8192)

    def invoke(self, mreq: SlotModelRequest) -> SlotModelResponse:
        from .chat_research_adapter import ChatResearchAdapter
        from .model_protocol import (
            ModelAssistantContinuation,
            ModelToolDefinition,
            ModelToolResult,
            ModelTurnExchange,
            ModelTurnInput,
            extract_assistant_text,
        )

        provider = self._providers.get(mreq.slot) or self._default_provider
        if provider is None or not (hasattr(provider, "complete_turn") or hasattr(provider, "complete")):
            raise SquadConfigurationError(
                f"Configured provider for slot {mreq.slot!r} does not support research turns; "
                "configure personal research-verified model settings or install a squad runner hook.",
                code="provider_not_research_capable",
                stage=mreq.stage,
            )
        adapter = provider if hasattr(provider, "complete_turn") else ChatResearchAdapter(provider)
        tool_defs = tuple(
            ModelToolDefinition(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("input_schema") or {"type": "object", "properties": {}},
            )
            for t in mreq.tools
        )
        conv_id = getattr(mreq, "conversation_id", None) or "default"
        key = (mreq.stage, mreq.role, str(conv_id))
        with self._lock:
            self._next_gen += 1
            call_gen = self._next_gen
            if mreq.turn_index == 0 or key not in self._turns:
                init_user = (
                    mreq.messages[1]["content"]
                    if len(mreq.messages) > 1 and isinstance(mreq.messages[1].get("content"), str) and mreq.messages[1].get("content")
                    else mreq.user_prompt
                )
                history: list[Any] = []
                last_msg = None
            else:
                _prev_gen, init_user, prev_history, last_msg = self._turns[key]
                history = list(prev_history)
                if last_msg is not None:
                    trailing_results: list[ModelToolResult] = []
                    trailing_user_parts: list[str] = []
                    for msg in reversed(mreq.messages):
                        msg_role = msg.get("role")
                        if msg_role == "assistant":
                            break
                        if msg_role == "tool":
                            trailing_results.append(
                                ModelToolResult(
                                    tool_call_id=str(msg.get("tool_call_id") or ""),
                                    content=str(msg.get("content") or ""),
                                )
                            )
                        elif msg_role == "user":
                            raw_user = msg.get("content")
                            if isinstance(raw_user, list):
                                raw_user = extract_assistant_text(msg)
                            if isinstance(raw_user, str) and raw_user.strip():
                                trailing_user_parts.append(raw_user)
                    trailing_results.reverse()
                    trailing_user_parts.reverse()
                    user_followup = "\n\n".join(trailing_user_parts) if trailing_user_parts else None
                    if not trailing_results and user_followup is None and isinstance(mreq.user_prompt, str) and mreq.user_prompt.strip():
                        user_followup = mreq.user_prompt
                    if trailing_results or user_followup is not None:
                        history.append(
                            ModelTurnExchange(
                                ModelAssistantContinuation(last_msg),
                                tuple(trailing_results),
                                user_followup=user_followup,
                            )
                        )
            # Claim this generation immediately so any concurrent or superseded turn cannot clobber state.
            self._turns[key] = (call_gen, init_user, history, last_msg)

        cancel_signal = self._effective_cancel_signal()
        max_output_tokens = self._resolve_max_output_tokens(mreq)

        def _is_cancelled() -> bool:
            return cancel_signal is not None and bool(cancel_signal.is_set())

        def _is_superseded() -> bool:
            with self._lock:
                cur = self._turns.get(key)
                return cur is None or cur[0] != call_gen

        model_resp = None
        for attempt in range(self._max_transient_retries + 1):
            if _is_cancelled():
                raise ProviderFailure("provider_cancelled", "Provider request was cancelled.", retryable=False)
            if _is_superseded():
                raise ProviderFailure("provider_cancelled", "Turn superseded by steering reset.", retryable=False)
            turn_timeout = self._resolve_timeout_seconds(mreq)
            turn = ModelTurnInput(
                system=mreq.system_prompt,
                user=init_user,
                tools=tool_defs,
                history=tuple(history),
                response_schema=mreq.response_schema,
                response_schema_name=mreq.response_schema_name,
                max_output_tokens=max_output_tokens,
                timeout_seconds=turn_timeout,
                cancel_event=cancel_signal,
            )
            try:
                model_resp = adapter.complete_turn(turn)
                break
            except ProviderFailure as exc:
                if _is_cancelled() or exc.code == "provider_cancelled":
                    raise
                if _is_superseded():
                    raise ProviderFailure("provider_cancelled", "Turn superseded by steering reset.", retryable=False) from exc
                if not exc.retryable or attempt >= self._max_transient_retries:
                    if exc.code == "provider_request_rejected":
                        from .research_diagnostics import log_provider_rejection

                        log_provider_rejection(
                            type(adapter).__name__,
                            exc,
                            context={
                                "model": getattr(getattr(adapter, "settings", None), "model", None),
                                "stage": mreq.stage,
                                "role": mreq.role,
                                "conversation_id": str(conv_id),
                            },
                        )
                    raise
                base_delay = self._retry_base_seconds * (2 ** attempt)
                delay = min(8.0, base_delay) * random.uniform(0.75, 1.25)
                if self._budget is not None and math.isfinite(self._budget.deadline):
                    rem = self._budget.deadline - monotonic()
                    if rem <= 0 or delay >= rem:
                        raise
                if self._sleeper is not None:
                    self._sleeper(delay)
                elif cancel_signal is not None and hasattr(cancel_signal, "wait"):
                    cancel_signal.wait(delay)
                else:
                    sleep(delay)

        assert model_resp is not None
        with self._lock:
            cur = self._turns.get(key)
            if cur is not None and cur[0] == call_gen:
                self._turns[key] = (call_gen, init_user, history, model_resp.message)
        text_out = extract_assistant_text(model_resp.message)
        return SlotModelResponse(
            content=text_out,
            tool_calls=tuple(model_resp.tool_calls or ()),
            usage=model_resp.usage,
            finish_reason=str(model_resp.finish_reason or "stop"),
        )


def _topic_attached_sources_for_squad(
    account_id: str,
    topic: ResearchTopic,
    *,
    as_of: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, bool]]]:
    effective_as_of = as_of if as_of is not None else topic.request.as_of
    source_ids = list(dict.fromkeys([*topic.request.source_ids, *topic.versions[-1].source_ids]))
    loaded: list[ResearchSource] = []
    for sid in source_ids:
        try:
            loaded.append(research_sources.get(account_id, sid))
        except SourceNotFound:
            continue
    eligible, _ = prepare_sources(loaded, effective_as_of)
    safe_eligible = [
        src for src in eligible
        if src.permissions.model_input and src.permissions.display and not src.is_simulated
    ]
    source_by_id = {src.source_id: src for src in safe_eligible}
    texts = {src.source_id: src.text for src in safe_eligible}
    perms = {
        src.source_id: {
            "store": src.permissions.store,
            "display": src.permissions.display,
            "model_input": src.permissions.model_input,
            "export": src.permissions.export,
        }
        for src in safe_eligible
    }
    return source_by_id, texts, perms


def _invoke_squad_hook(
    hook: Any,
    *,
    account: LocalAccount,
    topic: ResearchTopic,
    request: EarningsSquadApiRunRequest | None,
    operation_id: str,
    run_id: str,
    checkpoint_store: SquadCheckpointStore,
    org_sink: Callable[[OrgGraphEvent], None],
) -> Any:
    if hook is None:
        return None
    if callable(hook):
        import inspect
        sig = inspect.signature(hook)
        candidate_kwargs: dict[str, Any] = {
            "account": account,
            "topic": topic,
            "request": request,
            "operation_id": operation_id,
            "run_id": run_id,
            "checkpoint_store": checkpoint_store,
            "org_sink": org_sink,
        }
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            return hook(**candidate_kwargs)
        filtered = {k: v for k, v in candidate_kwargs.items() if k in sig.parameters}
        return hook(**filtered)
    return hook


def _build_squad_slot_provider(
    account_id: str,
    slot_entry: Any,
    primary_settings: OpenAICompatibleSettings | None,
    primary_config: StoredModelConfig | None,
) -> Any:
    """Resolve an isolated wire provider for an explicitly overridden slot when possible."""
    if slot_entry is None or getattr(slot_entry, "fallback_used", True):
        return None
    if not getattr(slot_entry, "differs_from_primary", False):
        return None
    if primary_settings is None or primary_config is None:
        return None
    cred_ref = getattr(slot_entry, "credential_ref", None)
    if cred_ref not in (None, "account_primary"):
        return None
    proto = getattr(slot_entry, "protocol", None) or primary_config.protocol
    base_url = getattr(slot_entry, "base_url", None) or primary_settings.base_url
    model = getattr(slot_entry, "model", None) or primary_settings.model
    # Only reuse account primary key on the exact same origin host.
    if urlsplit(base_url).hostname != urlsplit(primary_settings.base_url).hostname:
        return None
    try:
        slot_settings = OpenAICompatibleSettings.from_account_values(
            base_url=base_url,
            model=model,
            api_key=primary_settings.api_key,
        )
    except ProviderFailure:
        return None
    native_supported = getattr(slot_entry, "native_tools_supported", None)
    if native_supported is None:
        slot_caps = getattr(slot_entry, "capabilities", None)
        native_supported = getattr(slot_caps, "native_tools", None) if slot_caps is not None else None
    slot_tools_verified = (
        bool(native_supported)
        if native_supported is not None
        else bool(primary_settings.tools_verified)
    )
    slot_gen_params = getattr(slot_entry, "generation_parameters", None)
    if slot_gen_params is None and proto == primary_config.protocol:
        slot_gen_params = primary_settings.generation_parameters or primary_config.generation_parameters
    slot_settings = dataclass_replace(
        slot_settings,
        tools_verified=slot_tools_verified,
        generation_parameters=slot_gen_params,
    )
    timeout_floor = _squad_provider_timeout_floor()
    if proto == "openai_responses":
        from .openai_responses import OpenAIResponsesProvider
        return _with_squad_provider_timeout_floor(OpenAIResponsesProvider(slot_settings))
    if proto == "anthropic_messages":
        from .anthropic_messages import AnthropicMessagesProvider
        primary_msg_opts = primary_config.messages_options
        slot_name = getattr(slot_entry, "slot", None)
        default_slot_max = _SQUAD_SLOT_MAX_OUTPUT_TOKENS.get(str(slot_name), 8192)
        gen_max = (
            (
                getattr(slot_gen_params, "max_output_tokens", None)
                or getattr(slot_gen_params, "max_completion_tokens", None)
                or getattr(slot_gen_params, "max_tokens", None)
            )
            if slot_gen_params is not None
            else None
        )
        max_tokens = (
            getattr(slot_entry, "messages_max_tokens", None)
            or gen_max
            or (primary_msg_opts.max_tokens if primary_msg_opts is not None else None)
            or default_slot_max
        )
        version = (
            getattr(slot_entry, "endpoint_version", None)
            or (primary_msg_opts.version if primary_msg_opts is not None else None)
            or "2023-06-01"
        )
        auth_scheme = (
            getattr(slot_entry, "messages_auth_scheme", None)
            or (primary_msg_opts.auth_scheme if primary_msg_opts is not None else None)
            or "x-api-key"
        )
        slot_effort = getattr(slot_entry, "messages_effort", None)
        effort = (
            slot_effort
            if slot_effort is not None
            else (primary_msg_opts.effort if primary_msg_opts is not None else None)
        )
        return AnthropicMessagesProvider(
            slot_settings,
            ProviderPolicy(max_output_tokens=max_tokens, timeout_seconds=timeout_floor),
            anthropic_version=version,
            auth_scheme=auth_scheme,
            max_tokens=max_tokens,
            effort=effort,
        )
    return OpenAICompatibleLlmProvider(
        slot_settings,
        policy=ProviderPolicy(timeout_seconds=timeout_floor),
    )


def _platform_primary_slot_profile(primary_provider: Any) -> Any:
    """Build a full ModelSlotBinding from a platform wire provider when available, else None."""
    from .research_model_slots import ModelSlotBinding

    settings = getattr(primary_provider, "settings", None)
    if settings is None:
        return None
    base_url = getattr(settings, "base_url", None)
    model = getattr(settings, "model", None)
    if not base_url or not model:
        return None
    cls_name = type(primary_provider).__name__
    if "AnthropicMessages" in cls_name:
        protocol = "anthropic_messages"
    elif "OpenAIResponses" in cls_name:
        protocol = "openai_responses"
    else:
        protocol = "openai_chat_completions"
    try:
        return ModelSlotBinding(
            protocol=protocol,
            base_url=base_url,
            model=model,
            credential_ref="platform_primary",
            tools_verified=True,
        )
    except ValueError:
        return None


def _resolve_earnings_squad_runtime(
    *,
    account: LocalAccount,
    topic: ResearchTopic,
    request: EarningsSquadApiRunRequest | None,
    http_request: Request,
    operation_id: str,
    run_id: str,
    checkpoint_store: _RepoSquadCheckpointStore,
    org_sink: Callable[[OrgGraphEvent], None],
    cancel_event: Event | None = None,
    cancel_checker: Callable[[], bool] | None = None,
) -> tuple[EarningsSquadOrchestrator, dict[str, Any]]:
    from .research_fin_tools import FinDataToolbox

    stored_slots = model_settings.model_slots(account.account_id)
    primary_profile = model_settings.primary_slot_profile(account.account_id)
    requested_slots = request.slot_overrides if (request and request.slot_overrides is not None) else stored_slots

    hook = getattr(app.state, "earnings_squad_hook", None)
    hook_res = _invoke_squad_hook(
        hook,
        account=account,
        topic=topic,
        request=request,
        operation_id=operation_id,
        run_id=run_id,
        checkpoint_store=checkpoint_store,
        org_sink=org_sink,
    )

    if isinstance(hook_res, EarningsSquadOrchestrator):
        orch = hook_res
        if org_sink not in orch._org_sinks:
            orch._org_sinks.append(org_sink)
        routing_summary = slot_routing_summary(requested_slots, primary_profile=primary_profile)
        return orch, routing_summary

    hook_dict: Mapping[str, Any] = hook_res if isinstance(hook_res, Mapping) else {}
    if isinstance(hook_dict.get("orchestrator"), EarningsSquadOrchestrator):
        orch = hook_dict["orchestrator"]
        if org_sink not in orch._org_sinks:
            orch._org_sinks.append(org_sink)
        routing_summary = slot_routing_summary(
            hook_dict.get("resolved_slots") or hook_dict.get("slot_overrides") or requested_slots,
            primary_profile=hook_dict.get("primary_profile") or primary_profile,
        )
        return orch, routing_summary

    effective_as_of = request.as_of if (request and request.as_of is not None) else topic.request.as_of
    source_by_id, init_texts, init_perms = _topic_attached_sources_for_squad(
        account.account_id,
        topic,
        as_of=effective_as_of,
    )

    effective_resolved_slots = hook_dict.get("resolved_slots")
    effective_slot_bindings = hook_dict.get("slot_bindings")
    effective_primary_profile = hook_dict.get("primary_profile") if "primary_profile" in hook_dict else primary_profile
    effective_slot_overrides = hook_dict.get("slot_overrides") if "slot_overrides" in hook_dict else requested_slots
    effective_cancel_event = hook_dict.get("cancel_event") or cancel_event

    model_runner = hook_dict.get("model_runner")
    if model_runner is None:
        primary_provider, is_personal = _model_provider_for_account(account, http_request, research=True)
        try:
            settings, config = model_settings.runtime_configuration(account.account_id)
        except ModelCredentialError as error:
            raise ApiProblem(error.status_code, error.code, error.message, retryable=error.status_code == 503) from error

        if not is_personal and effective_primary_profile is None:
            effective_primary_profile = _platform_primary_slot_profile(primary_provider)

        if effective_resolved_slots is None and effective_slot_bindings is None:
            if effective_primary_profile is not None:
                try:
                    effective_resolved_slots = resolve_model_slots(
                        effective_primary_profile,
                        effective_slot_overrides,
                        strict=True,
                    )
                except ModelSlotError as error:
                    raise ApiProblem(422, "invalid_request", str(error)) from error
            elif effective_slot_overrides is not None:
                # Platform mode without a full wire primary (e.g. dev/MockResearchProvider):
                # validate configured slots cleanly while letting unset slots inherit fallback.
                fallback_model = getattr(primary_provider, "name", None) or "platform_primary"
                try:
                    effective_slot_bindings = resolve_squad_slots(
                        primary_profile={"model": fallback_model, "native_tools": True},
                        slot_overrides=effective_slot_overrides,
                    )
                except SquadConfigurationError as error:
                    raise ApiProblem(422, "invalid_request", str(error)) from error

        slot_providers: dict[str, Any] = {}
        slot_max_tokens: dict[str, int] = {}
        for sname in ("fast_extractor", "deep_reasoner", "bear_reviewer", "writer"):
            cap: int | None = None
            slot_overridden = False
            if effective_resolved_slots is not None:
                slot_entry = effective_resolved_slots.slot_for(sname)
                slot_overridden = bool(getattr(slot_entry, "differs_from_primary", False))
                p_override = _build_squad_slot_provider(
                    account.account_id,
                    slot_entry,
                    settings,
                    config,
                )
                if p_override is not None:
                    slot_providers[sname] = p_override
                slot_gen = getattr(slot_entry, "generation_parameters", None)
                cap = (
                    getattr(slot_entry, "messages_max_tokens", None)
                    or (
                        getattr(slot_gen, "max_output_tokens", None)
                        or getattr(slot_gen, "max_completion_tokens", None)
                        or getattr(slot_gen, "max_tokens", None)
                        if slot_gen is not None
                        else None
                    )
                )
            if cap is None and config is not None and not slot_overridden:
                if config.messages_options is not None and config.messages_options.max_tokens:
                    cap = config.messages_options.max_tokens
                elif config.generation_parameters is not None:
                    cap = (
                        getattr(config.generation_parameters, "max_output_tokens", None)
                        or getattr(config.generation_parameters, "max_completion_tokens", None)
                        or getattr(config.generation_parameters, "max_tokens", None)
                    )
            if type(cap) is int and cap > 0:
                slot_max_tokens[sname] = cap
        model_runner = _AccountSlotModelRunner(
            providers_by_slot=slot_providers,
            default_provider=_with_squad_provider_timeout_floor(primary_provider),
            cancel_event=effective_cancel_event,
            cancel_checker=cancel_checker,
            budget=hook_dict.get("budget"),
            slot_max_output_tokens=slot_max_tokens,
        )

    prefetch_deps = hook_dict.get("prefetch_deps")
    tool_runner = hook_dict.get("tool_runner")
    if prefetch_deps is None:
        sec_agent_raw, sec_ok = _configured_sec_user_agent()
        market_key, market_owner = _resolve_market_key(account.account_id)
        tushare_key, tushare_owner = _resolve_tushare_key(account.account_id)
        fmp_key, fmp_owner = _resolve_fmp_key(account.account_id)
        fin_box = FinDataToolbox(
            account_id=account.account_id,
            market_key=market_key,
            credential_owner=market_owner or "user_byok",
            sec_user_agent=sec_agent_raw,
            tushare_key=tushare_key,
            tushare_owner=tushare_owner or "user_byok",
            fmp_key=fmp_key,
            fmp_owner=fmp_owner or "user_byok",
            persist=lambda item, **kwargs: research_sources.create(account.account_id, item, **kwargs),
        )
        sec_adapter = SecEdgarAdapter(user_agent=sec_agent_raw) if sec_ok and sec_agent_raw else None
        sec_deep = SecDeepAdapter(adapter=sec_adapter) if sec_adapter is not None else None
        fmp_adapter = (
            FmpEarningsAdapter(
                api_key=fmp_key,
                credential_owner=fmp_owner or "user_byok",
                account_id=account.account_id,
            )
            if fmp_key
            else None
        )
        ext_service = ExternalEarningsService(provider=fmp_adapter, account_id=account.account_id)
        if tool_runner is None:
            tool_runner = FinToolboxToolRunner(fin_box, source_by_id=source_by_id)

        def _resolve_live_market_price(entity_id: str | None, symbol: str | None) -> Decimal | None:
            from .research_fin_tools import MarketDataRequest, MarketSpec
            try:
                eid_text = (entity_id or "").strip() or None
                sym_text = (symbol or "").strip().upper() or None
                req_eid = eid_text if (eid_text and ":" in eid_text and not eid_text.lower().startswith("cik:")) else None
                req_exchange: str | None = None
                if req_eid is not None:
                    ex_part, sym_part = req_eid.split(":", 1)
                    if not sym_text and sym_part.strip():
                        sym_text = sym_part.strip().upper()
                    req_exchange = ex_part.strip().upper() or None
                if not req_eid and not sym_text:
                    return None
                if effective_as_of is not None:
                    cutoff_day = effective_as_of.date()
                    md_req = MarketDataRequest(
                        entity_id=req_eid,
                        symbol=None if req_eid else sym_text,
                        exchange=req_exchange,
                        interval="1day",
                        start_date=cutoff_day - timedelta(days=10),
                        end_date=cutoff_day,
                        rationale="Stage 0 latest close price lookup for Reverse-DCF valuation.",
                    )
                else:
                    md_req = MarketDataRequest(
                        entity_id=req_eid,
                        symbol=None if req_eid else sym_text,
                        exchange=req_exchange,
                        interval="1day",
                        lookback="5d",
                        rationale="Stage 0 latest close price lookup for Reverse-DCF valuation.",
                    )
                spec = fin_box.market_spec(md_req, {})
                if not isinstance(spec, MarketSpec):
                    return None
                outcome = fin_box.market_data(spec)
                if outcome is None or not isinstance(outcome.result, Mapping):
                    return None
                if outcome.result.get("status") not in ("ok", "partial"):
                    return None
                if outcome.source is not None and isinstance(getattr(outcome.source, "text", None), str):
                    lines = [ln.strip() for ln in outcome.source.text.splitlines() if ln.strip()]
                    for ln in reversed(lines):
                        match = re.search(r"\bC\s+([0-9]+(?:\.[0-9]+)?)", ln)
                        if match:
                            return Decimal(match.group(1))
                raw_close = outcome.result.get("close")
                if raw_close is None:
                    raw_close = outcome.result.get("close_price")
                if raw_close is not None:
                    return Decimal(str(raw_close))
                return None
            except Exception:
                return None

        prefetch_deps = PrefetchDependencies(
            sec_adapter=sec_adapter,
            sec_deep=sec_deep,
            external_earnings=ext_service,
            tool_runner=tool_runner,
            market_price_fn=_resolve_live_market_price,
            initial_source_texts=init_texts,
            initial_source_permissions=init_perms,
        )
    elif tool_runner is None:
        tool_runner = prefetch_deps.tool_runner

    try:
        orch = EarningsSquadOrchestrator(
            model_runner=model_runner,
            tool_runner=tool_runner,
            prefetch_deps=prefetch_deps,
            slot_bindings=effective_slot_bindings,
            resolved_slots=effective_resolved_slots,
            primary_profile=effective_primary_profile if effective_slot_bindings is None and effective_resolved_slots is None else None,
            slot_overrides=effective_slot_overrides if effective_slot_bindings is None and effective_resolved_slots is None else None,
            checkpoint_store=checkpoint_store,
            org_sinks=[org_sink],
            context=hook_dict.get("context"),
            budget=hook_dict.get("budget"),
            cancel_event=effective_cancel_event,
            max_worker_concurrency=int(hook_dict.get("max_worker_concurrency") or 3),
        )
    except SquadConfigurationError as error:
        raise ApiProblem(422, "invalid_request", str(error)) from error

    routing_summary = slot_routing_summary(
        effective_resolved_slots or effective_slot_overrides,
        primary_profile=effective_primary_profile,
    )
    return orch, routing_summary


class _SquadOperationOutcome(BaseModel):
    run_id: str
    dossier_id: str
    ticker: str | None = None
    cik: str | None = None


def _cancel_active_earnings_squad(account_id: str, topic_id: str, operation_id: str) -> None:
    if not topic_id:
        return
    with _ACTIVE_SQUAD_LOCK:
        active = _ACTIVE_SQUAD_RUNS.get((account_id, topic_id))
        active_orch = active.get("orchestrator") if active and active.get("operation_id") == operation_id else None
    if active_orch is not None and callable(getattr(active_orch, "cancel", None)):
        active_orch.cancel()
    state_key = _squad_state_key(account_id, topic_id)

    def mark_cancelled() -> None:
        cur = EARNINGS_SQUAD_STATES.get(state_key)
        if not isinstance(cur, Mapping) or cur.get("operation_id") != operation_id:
            return
        if cur.get("status") == "running":
            EARNINGS_SQUAD_STATES.put(
                state_key,
                {**dict(cur), "status": "cancelled", "updated_at": lifecycle_clock.now().isoformat()},
            )

    EARNINGS_SQUAD_STATES.with_advisory_lock(f"squad-state:{state_key}", mark_cancelled)
    _update_turn_status(topic_id, operation_id, "stopped")


def _prepare_earnings_squad_execution(
    *,
    account: LocalAccount,
    topic: ResearchTopic,
    topic_id: str,
    http_request: Request,
    request: EarningsSquadApiRunRequest,
    operation_id: str,
    run_id: str,
    routing_reason: str | None = None,
    cancel_checker: Callable[[], bool] | None = None,
) -> tuple[
    Callable[[], None],
    Callable[[], tuple[str, EarningsDossier | None, str | None, bool]],
    EarningsSquadOrchestrator,
    dict[str, Any],
    list[OrgGraphEvent],
    SquadCheckpoint | None,
]:
    state_key = _squad_state_key(account.account_id, topic_id)
    prior_state = EARNINGS_SQUAD_STATES.get(state_key)
    cp_store = _RepoSquadCheckpointStore(account_id=account.account_id, topic_id=topic_id, state_lock_key=state_key)

    question = (request.question or "").strip() or (topic.request.question or "").strip() or DOCUMENT_OVERVIEW_QUESTION
    effective_as_of = request.as_of if request.as_of is not None else topic.request.as_of
    fmp_key, _ = _resolve_fmp_key(account.account_id)
    try:
        validated_periods = [PeriodFinancials.model_validate(item) for item in request.period_financials]
        validated_guidance = [GuidanceMetricInput.model_validate(item) for item in request.guidance_inputs]
        validated_bridges = [NonGaapBridgeInput.model_validate(item) for item in request.nongaap_bridges]
    except Exception as error:
        raise ApiProblem(422, "invalid_request", f"Invalid earnings diagnostic input: {error}") from error

    prefetch_req = SquadPrefetchRequest(
        question=question,
        entity_id=request.entity_id,
        cik=request.cik,
        symbol=request.symbol,
        company_name=request.company_name,
        sic_code=request.sic_code,
        sic_description=request.sic_description,
        period_label=request.period_label,
        as_of=effective_as_of,
        account_id=account.account_id,
        fmp_byok_api_key=fmp_key,
        close_price=request.close_price,
        filing_form=request.filing_form,
        filing_accession=request.filing_accession,
        prior_filing_accession=request.prior_filing_accession,
        fiscal_year=request.fiscal_year,
        fiscal_quarter=request.fiscal_quarter,
        period_financials=validated_periods,
        guidance_inputs=validated_guidance,
        nongaap_bridges=validated_bridges,
    )

    event_lock = RLock()
    org_events_buf: list[OrgGraphEvent] = []

    def emit_org_event(ev: OrgGraphEvent) -> None:
        with event_lock:
            if not any(existing.sequence == ev.sequence for existing in org_events_buf):
                org_events_buf.append(ev)
        ev_dict = ev.model_dump(mode="json")

        def append_event() -> None:
            cur = EARNINGS_SQUAD_STATES.get(state_key)
            if not isinstance(cur, Mapping) or cur.get("run_id") != run_id:
                return
            updated = dict(cur)
            events_list = list(updated.get("org_events") or [])
            if not any(isinstance(item, Mapping) and item.get("sequence") == ev.sequence for item in events_list):
                events_list.append(ev_dict)
            updated["org_events"] = events_list
            updated["updated_at"] = lifecycle_clock.now().isoformat()
            EARNINGS_SQUAD_STATES.put(state_key, updated)

        EARNINGS_SQUAD_STATES.with_advisory_lock(f"squad-state:{state_key}", append_event)
        research_live_broker.publish_org_graph_event(operation_id, ev)

    squad_cancel_event = Event()
    orch, routing_summary = _resolve_earnings_squad_runtime(
        account=account,
        topic=topic,
        request=request,
        http_request=http_request,
        operation_id=operation_id,
        run_id=run_id,
        checkpoint_store=cp_store,
        org_sink=emit_org_event,
        cancel_event=squad_cancel_event,
        cancel_checker=cancel_checker,
    )

    # Apply explicit budget overrides if caller set any
    if any(v is not None for v in (request.max_model_calls, request.max_search_calls, request.max_total_tokens, request.max_elapsed_seconds)):
        deadline = monotonic() + request.max_elapsed_seconds if request.max_elapsed_seconds is not None else float("inf")
        orch._budget = ResearchExecutionBudget(
            request.max_model_calls,
            request.max_search_calls,
            request.max_total_tokens,
            deadline,
        )
    if isinstance(orch._model_runner, _AccountSlotModelRunner):
        orch._model_runner.bind_runtime(
            cancel_event=orch._cancel_event,
            cancel_checker=cancel_checker,
            budget=orch._budget,
        )

    resume_cp: SquadCheckpoint | None = None
    existing_steering: list[dict[str, Any]] = []
    if request.resume_from_checkpoint or request.resume_run_id:
        resume_cp = cp_store.load(run_id)
        if resume_cp is None and isinstance(prior_state, Mapping) and isinstance(prior_state.get("checkpoint"), Mapping):
            resume_cp = SquadCheckpoint.model_validate(prior_state["checkpoint"])
        if resume_cp is not None:
            org_events_buf.extend(resume_cp.org_events)
        if isinstance(prior_state, Mapping) and isinstance(prior_state.get("steering_instructions"), list):
            existing_steering = list(prior_state["steering_instructions"])

    seed_instructions: list[UserSteeringInstruction] = []
    for raw_item in existing_steering:
        try:
            seed_instructions.append(UserSteeringInstruction.model_validate(raw_item))
        except Exception:
            pass
    next_seq = max(
        [i.sequence for i in seed_instructions] + ([max((i.sequence for i in resume_cp.user_instructions), default=0)] if resume_cp else [0])
    ) + 1
    for text in request.user_instructions:
        cleaned = text.strip()
        if cleaned:
            instr = UserSteeringInstruction(sequence=next_seq, text=cleaned)
            seed_instructions.append(instr)
            existing_steering.append(instr.model_dump(mode="json"))
            next_seq += 1

    def init_state() -> None:
        EARNINGS_SQUAD_STATES.put(state_key, {
            "account_id": account.account_id,
            "topic_id": topic_id,
            "run_id": run_id,
            "operation_id": operation_id,
            "run_mode": "earnings_squad",
            "ticker": request.symbol,
            "cik": request.cik,
            "entity_id": request.entity_id,
            "company_name": request.company_name,
            "routing_reason": routing_reason,
            "status": "running",
            "dossier": resume_cp.dossier.model_dump(mode="json") if resume_cp and resume_cp.dossier else None,
            "org_events": [ev.model_dump(mode="json") for ev in org_events_buf],
            "slot_routing_summary": routing_summary,
            "steering_instructions": existing_steering,
            "checkpoint": resume_cp.model_dump(mode="json") if resume_cp else None,
            "checkpoint_completed_stages": list(resume_cp.completed_stages) if resume_cp else [],
            "error": None,
            "rejection_details": None,
            "updated_at": lifecycle_clock.now().isoformat(),
        })

    def persist_checkpoint_and_check_cancel(cp: SquadCheckpoint) -> None:
        cp_store.save(cp)
        if cancel_checker is not None and cancel_checker():
            orch.cancel()

    def execute_run() -> tuple[str, EarningsDossier | None, str | None, bool]:
        status_out: Literal["completed", "failed", "cancelled"] = "completed"
        dossier_out: EarningsDossier | None = None
        err_out: str | None = None
        rejection_details_out: dict[str, Any] | None = None
        config_error = False
        try:
            if cancel_checker is not None and cancel_checker():
                orch.cancel()
            dossier_out = orch.run(
                prefetch_req,
                run_id=run_id,
                resume_from=resume_cp,
                user_instructions=seed_instructions,
                on_stage_complete=lambda _stage, cp: persist_checkpoint_and_check_cancel(cp),
                on_worker_complete=lambda _role, _batch, cp: persist_checkpoint_and_check_cancel(cp),
            )
            status_out = "completed"
        except SquadCancelledError as exc:
            status_out = "cancelled"
            err_out = str(exc)
        except SquadConfigurationError as exc:
            status_out = "failed"
            err_out = str(exc)
            config_error = True
        except Exception as exc:
            status_out = "failed"
            err_out = str(exc)
            raw_rej = getattr(exc, "rejection_details", None)
            if isinstance(raw_rej, Mapping) and raw_rej:
                rejection_details_out = dict(raw_rej)
            elif getattr(exc, "code", None) == "provider_request_rejected":
                fallback_rej: dict[str, Any] = {}
                if getattr(exc, "upstream_status", None) is not None:
                    fallback_rej["status_code"] = exc.upstream_status
                if getattr(exc, "parameter", None):
                    fallback_rej["error_param"] = exc.parameter
                if fallback_rej:
                    rejection_details_out = fallback_rej
        finally:
            final_cp = cp_store.load(run_id)
            with event_lock:
                events_final = list(final_cp.org_events) if final_cp and final_cp.org_events else list(org_events_buf)

            def finalize_state() -> None:
                cur = EARNINGS_SQUAD_STATES.get(state_key) or {}
                if cur.get("operation_id") and cur.get("operation_id") != operation_id:
                    return
                updated = dict(cur)
                updated["status"] = status_out
                updated["run_mode"] = "earnings_squad"
                if dossier_out is not None:
                    updated["dossier"] = dossier_out.model_dump(mode="json")
                    updated["ticker"] = dossier_out.symbol or updated.get("ticker")
                    updated["cik"] = dossier_out.cik or updated.get("cik")
                    updated["entity_id"] = dossier_out.entity_id or updated.get("entity_id")
                    updated["company_name"] = dossier_out.company_name or updated.get("company_name")
                if final_cp is not None:
                    updated["checkpoint"] = final_cp.model_dump(mode="json")
                    updated["checkpoint_completed_stages"] = list(final_cp.completed_stages)
                updated["org_events"] = [ev.model_dump(mode="json") for ev in events_final]
                updated["error"] = err_out
                updated["rejection_details"] = rejection_details_out
                updated["updated_at"] = lifecycle_clock.now().isoformat()
                EARNINGS_SQUAD_STATES.put(state_key, updated)

            EARNINGS_SQUAD_STATES.with_advisory_lock(f"squad-state:{state_key}", finalize_state)
            with _ACTIVE_SQUAD_LOCK:
                active = _ACTIVE_SQUAD_RUNS.get((account.account_id, topic_id))
                if active and active.get("operation_id") == operation_id:
                    _ACTIVE_SQUAD_RUNS.pop((account.account_id, topic_id), None)
            terminal_progress: dict[str, Any] = {
                "org_events": [ev.model_dump(mode="json") for ev in events_final],
                "logical_status": "stopped" if status_out == "cancelled" else status_out,
                "current_operation_id": operation_id,
            }
            if rejection_details_out is not None:
                terminal_progress["rejection_details"] = rejection_details_out
            research_live_broker.publish_terminal(
                operation_id,
                operation_status=status_out,
                version_id=dossier_out.dossier_id if dossier_out is not None else None,
                progress=terminal_progress,
            )
        return status_out, dossier_out, err_out, config_error

    return init_state, execute_run, orch, routing_summary, org_events_buf, resume_cp


def _start_auto_routed_earnings_squad(
    *,
    topic_id: str,
    topic: ResearchTopic,
    request: ResearchRunRequest,
    run_request_payload: dict[str, Any],
    response: Response,
    idempotency_key: str,
    http_request: Request,
    account: LocalAccount,
    effective_question: str,
    question_origin: str,
    turn_meta: dict[str, Any],
    route_decision: EarningsSquadRouteDecision,
) -> ResearchStartResponse:
    squad_run_id = f"sqrun_{uuid4().hex[:12]}"
    state_key = _squad_state_key(account.account_id, topic_id)
    squad_req = EarningsSquadApiRunRequest(
        question=effective_question,
        entity_id=route_decision.entity_id,
        cik=route_decision.cik,
        symbol=route_decision.ticker,
        company_name=route_decision.company_name,
        as_of=topic.request.as_of,
        execution_mode="async",
        background=True,
        run_id=squad_run_id,
        max_model_calls=request.max_model_calls,
        max_search_calls=request.max_search_calls,
        max_total_tokens=request.max_total_tokens,
        max_elapsed_seconds=float(request.max_elapsed_seconds) if request.max_elapsed_seconds is not None else None,
    )
    identity = (account.account_id, topic_id, turn_meta["turn_id"])
    runtime_holder: dict[str, Any] = {}

    def admit_squad_operation(operation_id: str) -> None:
        current_status = TURN_STATUS.get(topic_id)
        if current_status and current_status.get("owner_id") == account.account_id and current_status.get("logical_status") == "awaiting_user":
            raise ApiProblem(409, "lifecycle_conflict", "Answer or stop the waiting research turn first.")
        if _topic_has_active_run(account.account_id, topic_id):
            raise ApiProblem(409, "lifecycle_conflict", "Another research run is already active for this topic.")
        if research_topics.get(account.account_id, topic_id).versions[-1].version_id != request.parent_version_id:
            raise ApiProblem(409, "lifecycle_conflict", "Research changed before the run was admitted.")

        init_state, execute_run, orch, routing_summary, _events_buf, _resume_cp = _prepare_earnings_squad_execution(
            account=account,
            topic=topic,
            topic_id=topic_id,
            http_request=http_request,
            request=squad_req,
            operation_id=operation_id,
            run_id=squad_run_id,
            routing_reason=route_decision.reason,
            cancel_checker=lambda: operations.cancel_event(operation_id).is_set(),
        )
        runtime_holder["execute_run"] = execute_run

        TURN_RECORDS.put(operation_id, {
            "account_id": account.account_id,
            "topic_id": topic_id,
            "turn_id": turn_meta["turn_id"],
        })
        try:
            turn_instructions.begin(*identity, operation_id)
        except (InstructionNotFound, InstructionConflict, InstructionInvalid) as error:
            raise _turn_http_error(error) from error

        with _ACTIVE_SQUAD_LOCK:
            _ACTIVE_SQUAD_RUNS[(account.account_id, topic_id)] = {
                "run_id": squad_run_id,
                "operation_id": operation_id,
                "orchestrator": orch,
            }
        try:
            EARNINGS_SQUAD_STATES.with_advisory_lock(f"squad-state:{state_key}", init_state)
        except Exception:
            with _ACTIVE_SQUAD_LOCK:
                cur_active = _ACTIVE_SQUAD_RUNS.get((account.account_id, topic_id))
                if cur_active and cur_active.get("operation_id") == operation_id:
                    _ACTIVE_SQUAD_RUNS.pop((account.account_id, topic_id), None)
            raise
        TURN_STATUS.put(topic_id, {
            "owner_id": account.account_id,
            "topic_id": topic_id,
            "turn_id": turn_meta["turn_id"],
            "current_operation_id": operation_id,
            "logical_status": "running",
            "run_mode": "earnings_squad",
            "squad_run_id": squad_run_id,
            "ticker": route_decision.ticker,
            "cik": route_decision.cik,
        })
        RESEARCH_RUN_INPUTS.put(operation_id, {
            "schema_version": "1",
            "operation_id": operation_id,
            "owner_id": account.account_id,
            "topic_id": topic_id,
            "parent_version_id": request.parent_version_id,
            "captured_at": lifecycle_clock.now().isoformat(),
            "question_origin": question_origin,
            "question": effective_question,
            "turn": {**turn_meta, "operation_ids": [*turn_meta["operation_ids"], operation_id]},
            "run_mode": "earnings_squad",
            "squad_run_id": squad_run_id,
            "ticker": route_decision.ticker,
            "cik": route_decision.cik,
            "entity_id": route_decision.entity_id,
            "company_name": route_decision.company_name,
            "routing_reason": route_decision.reason,
            "slot_routing_summary": routing_summary,
            "budget": {key: value for key, value in _request_payload(request).items() if key != "question_override"},
        })

    def build_squad_operation(operation_id: str) -> tuple[BaseModel, str]:
        execute_run = runtime_holder["execute_run"]
        status_out, dossier_out, err_out, is_config_err = execute_run()
        if status_out == "completed" and dossier_out is not None:
            outcome = _SquadOperationOutcome(
                run_id=squad_run_id,
                dossier_id=dossier_out.dossier_id,
                ticker=dossier_out.symbol or route_decision.ticker,
                cik=dossier_out.cik or route_decision.cik,
            )
            return outcome, dossier_out.dossier_id
        if status_out == "cancelled":
            _update_turn_status(topic_id, operation_id, "stopped")
            raise ApiProblem(500, "internal_error", err_out or "Earnings squad execution was cancelled.")
        _update_turn_status(topic_id, operation_id, "failed")
        if is_config_err:
            raise ApiProblem(422, "invalid_request", err_out or "Invalid model slot configuration.")
        raise ApiProblem(500, "internal_error", err_out or "Earnings squad execution failed.")

    def commit_squad_operation(operation_id: str, result: BaseModel) -> None:
        assert isinstance(result, _SquadOperationOutcome)
        _update_turn_status(topic_id, operation_id, "completed", result.dossier_id)

    op_resp, replayed = operations.submit_async(
        endpoint=f"/v1/research/topics/{topic_id}/runs",
        idempotency_key=idempotency_key,
        owner_subject=f"account:{account.account_id}",
        request_payload=run_request_payload,
        result_field="research",
        build_result=build_squad_operation,
        session_id=topic_id,
        before_accept=admit_squad_operation,
        commit_result=commit_squad_operation,
    )
    _mark_replay(response, replayed)
    stored = RESEARCH_RUN_INPUTS[op_resp.operation.operation_id]
    return _start_response_from_inputs(op_resp.operation, stored)


@app.post(
    "/v2/research/topics/{topic_id}/earnings-squad/run",
    response_model=EarningsSquadRunResponse,
    include_in_schema=False,
)
def run_earnings_squad(
    topic_id: str,
    http_request: Request,
    response: Response,
    request: EarningsSquadApiRunRequest = Body(default_factory=EarningsSquadApiRunRequest),
) -> EarningsSquadRunResponse:
    """Execute (or resume from checkpoint) the 6-stage Scheme 2 US Earnings Research Squad."""
    account = _authenticated_account(http_request.headers.get("Authorization"))
    try:
        topic = research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error

    state_key = _squad_state_key(account.account_id, topic_id)
    if _topic_has_active_run(account.account_id, topic_id):
        raise ApiProblem(409, "lifecycle_conflict", "An earnings squad run is already active for this topic.")

    prior_state = EARNINGS_SQUAD_STATES.get(state_key)
    resume_target_run_id = request.resume_run_id or (
        prior_state.get("run_id") if request.resume_from_checkpoint and isinstance(prior_state, Mapping) else None
    )
    run_id = request.run_id or resume_target_run_id or f"sqrun_{uuid4().hex[:12]}"
    operation_id = f"sqop_{uuid4().hex[:16]}"

    init_state, execute_run, orch, routing_summary, org_events_buf, resume_cp = _prepare_earnings_squad_execution(
        account=account,
        topic=topic,
        topic_id=topic_id,
        http_request=http_request,
        request=request,
        operation_id=operation_id,
        run_id=run_id,
    )
    with _ACTIVE_SQUAD_LOCK:
        _ACTIVE_SQUAD_RUNS[(account.account_id, topic_id)] = {
            "run_id": run_id,
            "operation_id": operation_id,
            "orchestrator": orch,
        }
    try:
        EARNINGS_SQUAD_STATES.with_advisory_lock(f"squad-state:{state_key}", init_state)
    except Exception:
        with _ACTIVE_SQUAD_LOCK:
            cur_active = _ACTIVE_SQUAD_RUNS.get((account.account_id, topic_id))
            if cur_active and cur_active.get("operation_id") == operation_id:
                _ACTIVE_SQUAD_RUNS.pop((account.account_id, topic_id), None)
        raise

    is_async = request.background or request.execution_mode == "async"
    if is_async:
        thread = Thread(target=execute_run, name=f"earnings-squad-{run_id}", daemon=True)
        with _ACTIVE_SQUAD_LOCK:
            if (account.account_id, topic_id) in _ACTIVE_SQUAD_RUNS:
                _ACTIVE_SQUAD_RUNS[(account.account_id, topic_id)]["thread"] = thread
        thread.start()
        response.status_code = 202
        return EarningsSquadRunResponse(
            topic_id=topic_id,
            run_id=run_id,
            operation_id=operation_id,
            status="running",
            run_mode="earnings_squad",
            ticker=request.symbol or (resume_cp.symbol if resume_cp else None),
            cik=request.cik or (resume_cp.cik if resume_cp else None),
            dossier=resume_cp.dossier if resume_cp else None,
            org_events=list(org_events_buf),
            slot_routing_summary=routing_summary,
            checkpoint_completed_stages=list(resume_cp.completed_stages) if resume_cp else [],
        )

    final_status, final_dossier, final_err, is_config_err = execute_run()
    final_row = EARNINGS_SQUAD_STATES.get(state_key) or {}
    if final_status == "failed" and final_dossier is None:
        if is_config_err:
            raise ApiProblem(422, "invalid_request", final_err or "Invalid model slot configuration.")
        raise ApiProblem(500, "internal_error", final_err or "Earnings squad execution failed.")
    return EarningsSquadRunResponse(
        topic_id=topic_id,
        run_id=run_id,
        operation_id=operation_id,
        status=final_status,
        run_mode="earnings_squad",
        ticker=(final_dossier.symbol if final_dossier else None) or final_row.get("ticker") or request.symbol,
        cik=(final_dossier.cik if final_dossier else None) or final_row.get("cik") or request.cik,
        dossier=final_dossier,
        org_events=[OrgGraphEvent.model_validate(ev) for ev in final_row.get("org_events") or []],
        slot_routing_summary=routing_summary,
        checkpoint_completed_stages=list(final_row.get("checkpoint_completed_stages") or []),
        error=final_err,
        rejection_details=dict(final_row["rejection_details"]) if isinstance(final_row.get("rejection_details"), Mapping) else None,
    )


@app.get(
    "/v2/research/topics/{topic_id}/earnings-squad/dossier",
    response_model=EarningsSquadDossierResponse,
    include_in_schema=False,
)
def get_earnings_squad_dossier(
    topic_id: str,
    http_request: Request,
) -> EarningsSquadDossierResponse:
    """Return locked ``EarningsDossier``, sequenced ``org_events``, and ``slot_routing_summary``."""
    account = _authenticated_account(http_request.headers.get("Authorization"))
    try:
        research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error

    state_key = _squad_state_key(account.account_id, topic_id)
    row = EARNINGS_SQUAD_STATES.get(state_key)
    if not isinstance(row, Mapping):
        raise ApiProblem(404, "not_found", "No earnings squad run found for this topic.")

    dossier_raw = row.get("dossier")
    dossier = EarningsDossier.model_validate(dossier_raw) if isinstance(dossier_raw, Mapping) else None
    org_events = [OrgGraphEvent.model_validate(ev) for ev in row.get("org_events") or []]
    org_events.sort(key=lambda e: e.sequence)
    steering = [
        UserSteeringInstruction.model_validate(item)
        for item in row.get("steering_instructions") or []
        if isinstance(item, Mapping)
    ]
    return EarningsSquadDossierResponse(
        topic_id=topic_id,
        run_id=str(row.get("run_id") or ""),
        operation_id=str(row.get("operation_id") or ""),
        status=str(row.get("status") or "completed"),
        run_mode="earnings_squad",
        ticker=row.get("ticker") or (dossier.symbol if dossier else None),
        cik=row.get("cik") or (dossier.cik if dossier else None),
        dossier=dossier,
        org_events=org_events,
        slot_routing_summary=dict(row.get("slot_routing_summary") or {}),
        steering_instructions=steering,
        checkpoint_completed_stages=list(row.get("checkpoint_completed_stages") or []),
        error=row.get("error"),
        rejection_details=dict(row["rejection_details"]) if isinstance(row.get("rejection_details"), Mapping) else None,
    )


@app.post(
    "/v2/research/topics/{topic_id}/earnings-squad/deep-report",
    response_model=EarningsSquadDeepReportResponse,
    response_model_exclude_none=True,
    include_in_schema=False,
)
def generate_earnings_squad_deep_report(
    topic_id: str,
    http_request: Request,
    request: EarningsSquadDeepReportRequest = Body(default_factory=EarningsSquadDeepReportRequest),
) -> EarningsSquadDeepReportResponse:
    """Render and cache Deep Report from a locked ``EarningsDossier`` (0 retrieval tool calls)."""
    account = _authenticated_account(http_request.headers.get("Authorization"))
    try:
        topic = research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error

    state_key = _squad_state_key(account.account_id, topic_id)
    row = EARNINGS_SQUAD_STATES.get(state_key)
    if not isinstance(row, Mapping):
        raise ApiProblem(404, "not_found", "No earnings squad run found for this topic.")
    dossier_raw = row.get("dossier")
    if not isinstance(dossier_raw, Mapping):
        raise ApiProblem(409, "lifecycle_conflict", "Locked EarningsDossier is not available yet.")
    dossier = EarningsDossier.model_validate(dossier_raw)
    if not dossier.locked:
        raise ApiProblem(409, "lifecycle_conflict", "EarningsDossier must be locked before rendering Deep Report.")

    if dossier.deep_report_markdown is not None and not request.force:
        return EarningsSquadDeepReportResponse(
            topic_id=topic_id,
            run_id=str(row.get("run_id") or dossier.run_id),
            dossier_id=dossier.dossier_id,
            dossier_sha256=dossier.dossier_sha256,
            cached=True,
            deep_report_tool_calls=dossier.slot_usage_summary.deep_report_tool_calls,
            deep_report_markdown=dossier.deep_report_markdown,
            dossier=dossier,
        )

    cp_store = _RepoSquadCheckpointStore(account_id=account.account_id, topic_id=topic_id, state_lock_key=state_key)
    run_id = str(row.get("run_id") or dossier.run_id)
    operation_id = str(row.get("operation_id") or f"sqop_{uuid4().hex[:12]}")

    def noop_sink(_ev: OrgGraphEvent) -> None:
        return None

    orch, _ = _resolve_earnings_squad_runtime(
        account=account,
        topic=topic,
        request=None,
        http_request=http_request,
        operation_id=operation_id,
        run_id=run_id,
        checkpoint_store=cp_store,
        org_sink=noop_sink,
    )
    try:
        updated_dossier = render_deep_report(
            dossier,
            model_runner=orch._model_runner,
            slot_bindings=orch.slot_bindings,
            force=request.force,
        )
    except SquadToolViolationError as error:
        raise ApiProblem(422, "invalid_request", str(error)) from error
    except SquadConfigurationError as error:
        raise ApiProblem(422, "invalid_request", str(error)) from error
    except SquadExecutionError as error:
        raise ApiProblem(422, "invalid_request", str(error)) from error

    def persist_deep_report() -> None:
        cur = EARNINGS_SQUAD_STATES.get(state_key)
        if not isinstance(cur, Mapping):
            return
        updated = dict(cur)
        updated["dossier"] = updated_dossier.model_dump(mode="json")
        if isinstance(updated.get("checkpoint"), Mapping):
            cp = dict(updated["checkpoint"])
            cp["dossier"] = updated["dossier"]
            updated["checkpoint"] = cp
        updated["updated_at"] = lifecycle_clock.now().isoformat()
        EARNINGS_SQUAD_STATES.put(state_key, updated)

    EARNINGS_SQUAD_STATES.with_advisory_lock(f"squad-state:{state_key}", persist_deep_report)
    assert updated_dossier.deep_report_markdown is not None
    return EarningsSquadDeepReportResponse(
        topic_id=topic_id,
        run_id=run_id,
        dossier_id=updated_dossier.dossier_id,
        dossier_sha256=updated_dossier.dossier_sha256,
        cached=False,
        deep_report_tool_calls=updated_dossier.slot_usage_summary.deep_report_tool_calls,
        deep_report_markdown=updated_dossier.deep_report_markdown,
        dossier=updated_dossier,
    )


@app.get(
    "/v2/research/topics/{topic_id}/earnings-squad/export",
    include_in_schema=False,
)
def export_earnings_squad_dossier(
    topic_id: str,
    http_request: Request,
    format: Literal["markdown", "csv", "xlsx"] = Query(default="markdown"),
    report_mode: MarkdownReportMode = Query(default="auto"),
) -> Response:
    """Export locked ``EarningsDossier`` as Markdown, CSV, or XLSX enforcing source export permissions."""
    account = _authenticated_account(http_request.headers.get("Authorization"))
    try:
        topic = research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error

    state_key = _squad_state_key(account.account_id, topic_id)
    row = EARNINGS_SQUAD_STATES.get(state_key)
    if not isinstance(row, Mapping):
        raise ApiProblem(404, "not_found", "No earnings squad run found for this topic.")
    dossier_raw = row.get("dossier")
    if not isinstance(dossier_raw, Mapping):
        raise ApiProblem(409, "lifecycle_conflict", "Locked EarningsDossier is not available yet.")
    dossier = EarningsDossier.model_validate(dossier_raw)

    extra_perms: dict[str, dict[str, bool]] = {}
    cp_raw = row.get("checkpoint")
    if isinstance(cp_raw, Mapping) and isinstance(cp_raw.get("source_permissions"), Mapping):
        for sid, perms in cp_raw["source_permissions"].items():
            if isinstance(perms, Mapping):
                extra_perms[str(sid)] = {str(k): bool(v) for k, v in perms.items()}

    referenced_sids = {
        *(ref.source_id for ref in dossier.snapshot.prefetched_sources),
        *(cit.source_id for card in dossier.evidence_cards for cit in card.citations),
        *topic.request.source_ids,
        *topic.versions[-1].source_ids,
    }
    for sid in referenced_sids:
        try:
            src = research_sources.get(account.account_id, sid)
            extra_perms[sid] = {
                "store": src.permissions.store,
                "display": src.permissions.display,
                "model_input": src.permissions.model_input,
                "export": src.permissions.export,
            }
        except SourceNotFound:
            continue

    try:
        exported = export_earnings_dossier(
            dossier,
            fmt=format,
            report_mode=report_mode,
            extra_source_permissions=extra_perms,
        )
    except ExportUnavailableError as error:
        raise ApiProblem(409, "lifecycle_conflict", str(error)) from error

    return Response(
        content=exported.content_bytes,
        media_type=exported.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{exported.filename}"',
            "Cache-Control": "no-store",
            "X-Dossier-Id": exported.dossier_id,
            "X-Export-Redacted": "false" if exported.exportable_unrestricted else "true",
        },
    )


@app.post(
    "/v2/research/topics/{topic_id}/earnings-squad/steer",
    status_code=202,
    response_model=EarningsSquadSteerResponse,
    response_model_exclude_none=True,
    include_in_schema=False,
)
def steer_earnings_squad(
    topic_id: str,
    request: EarningsSquadSteerRequest,
    http_request: Request,
) -> EarningsSquadSteerResponse:
    """Submit Lead-directed mid-run (or checkpoint-resume) user steering instruction."""
    account = _authenticated_account(http_request.headers.get("Authorization"))
    try:
        research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error

    cleaned = request.text.strip()
    if not cleaned:
        raise ApiProblem(422, "invalid_request", "Steering instruction text must not be blank.")

    state_key = _squad_state_key(account.account_id, topic_id)
    row = EARNINGS_SQUAD_STATES.get(state_key)
    if not isinstance(row, Mapping):
        raise ApiProblem(404, "not_found", "No earnings squad run found for this topic.")

    with _ACTIVE_SQUAD_LOCK:
        active = _ACTIVE_SQUAD_RUNS.get((account.account_id, topic_id))
        active_orch: EarningsSquadOrchestrator | None = active.get("orchestrator") if active else None

    if active_orch is not None:
        instr = active_orch.submit_steering_instruction(cleaned)
        delivery_status: Literal["queued", "persisted_for_resume"] = "queued"
    else:
        prior_instrs = row.get("steering_instructions") or []
        max_seq = max((int(item.get("sequence") or 0) for item in prior_instrs if isinstance(item, Mapping)), default=0)
        cp_raw = row.get("checkpoint")
        if isinstance(cp_raw, Mapping) and isinstance(cp_raw.get("user_instructions"), list):
            max_seq = max(
                max_seq,
                max((int(item.get("sequence") or 0) for item in cp_raw["user_instructions"] if isinstance(item, Mapping)), default=0),
            )
        instr = UserSteeringInstruction(sequence=max_seq + 1, text=cleaned)
        delivery_status = "persisted_for_resume"

    instr_dump = instr.model_dump(mode="json")
    run_id = str(row.get("run_id") or "")
    operation_id = str(row.get("operation_id") or "")

    def append_steering() -> None:
        cur = EARNINGS_SQUAD_STATES.get(state_key)
        if not isinstance(cur, Mapping):
            return
        updated = dict(cur)
        items = list(updated.get("steering_instructions") or [])
        if not any(isinstance(i, Mapping) and i.get("sequence") == instr.sequence for i in items):
            items.append(instr_dump)
        updated["steering_instructions"] = items
        if isinstance(updated.get("checkpoint"), Mapping):
            cp = dict(updated["checkpoint"])
            cp_instrs = list(cp.get("user_instructions") or [])
            if not any(isinstance(i, Mapping) and i.get("sequence") == instr.sequence for i in cp_instrs):
                cp_instrs.append(instr_dump)
            cp["user_instructions"] = cp_instrs
            updated["checkpoint"] = cp
            cp_key = _squad_checkpoint_key(account.account_id, topic_id, str(cp.get("run_id") or run_id))
            existing_cp_row = EARNINGS_SQUAD_CHECKPOINTS.get(cp_key)
            if isinstance(existing_cp_row, Mapping):
                EARNINGS_SQUAD_CHECKPOINTS.put(cp_key, {**dict(existing_cp_row), "checkpoint": cp})
        updated["updated_at"] = lifecycle_clock.now().isoformat()
        EARNINGS_SQUAD_STATES.put(state_key, updated)

    EARNINGS_SQUAD_STATES.with_advisory_lock(f"squad-state:{state_key}", append_steering)
    return EarningsSquadSteerResponse(
        topic_id=topic_id,
        run_id=run_id,
        operation_id=operation_id,
        instruction=instr,
        status=delivery_status,
    )


@app.get(
    "/v2/research/topics/{topic_id}/earnings-squad/events",
    response_class=StreamingResponse,
    include_in_schema=False,
)
async def stream_earnings_squad_events(
    topic_id: str,
    http_request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Stream structured ``org_graph_event`` SSE frames for the topic's active/latest squad run."""
    account = _authenticated_account(http_request.headers.get("Authorization"))
    try:
        research_topics.get(account.account_id, topic_id)
    except TopicNotFound as error:
        raise ApiProblem(404, "not_found", "Research topic not found.") from error

    cursor_after = parse_org_last_event_id(last_event_id)
    effective_after = max(after, cursor_after)
    state_key = _squad_state_key(account.account_id, topic_id)
    row = EARNINGS_SQUAD_STATES.get(state_key)
    if not isinstance(row, Mapping):
        raise ApiProblem(404, "not_found", "No earnings squad run found for this topic.")

    operation_id = str(row.get("operation_id") or "")
    sse_headers = {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }

    def snapshot_events_after(seq_floor: int) -> tuple[str, str | None, list[dict[str, Any]]]:
        cur = EARNINGS_SQUAD_STATES.get(state_key) or row
        status_now = str(cur.get("status") or "completed")
        dossier_raw = cur.get("dossier")
        ver_id = dossier_raw.get("dossier_id") if isinstance(dossier_raw, Mapping) else None
        raw_list = cur.get("org_events") if isinstance(cur.get("org_events"), list) else []
        filtered = []
        for raw_ev in raw_list:
            try:
                norm = normalize_org_graph_event(raw_ev)
            except ValueError:
                continue
            if norm["sequence"] > seq_floor:
                filtered.append(norm)
        filtered.sort(key=lambda item: item["sequence"])
        return status_now, ver_id, filtered

    loop = asyncio.get_running_loop()
    subscriber = research_live_broker.subscribe(operation_id, account_id=account.account_id, loop=loop)
    try:
        status_init, ver_init, replay_init = snapshot_events_after(effective_after)
    except BaseException:
        research_live_broker.unsubscribe(subscriber)
        raise

    if status_init in {"completed", "failed", "cancelled"}:
        research_live_broker.unsubscribe(subscriber)

        async def terminal_stream():
            for ev_data in replay_init:
                yield format_org_graph_sse_frame(operation_id, ev_data)
            yield _sse_terminal_frame(operation_id, status_init, ver_init)

        return StreamingResponse(terminal_stream(), media_type="text/event-stream", headers=sse_headers)

    async def live_squad_stream():
        last_sent_seq = effective_after
        try:
            for ev_data in replay_init:
                last_sent_seq = max(last_sent_seq, ev_data["sequence"])
                yield format_org_graph_sse_frame(operation_id, ev_data)
            while True:
                status_check, ver_check, missed = snapshot_events_after(last_sent_seq)
                for ev_data in missed:
                    last_sent_seq = max(last_sent_seq, ev_data["sequence"])
                    yield format_org_graph_sse_frame(operation_id, ev_data)
                if status_check in {"completed", "failed", "cancelled"}:
                    yield _sse_terminal_frame(operation_id, status_check, ver_check)
                    return
                try:
                    frame = await asyncio.wait_for(
                        subscriber.queue.get(),
                        timeout=research_live_broker.heartbeat_seconds,
                    )
                except asyncio.TimeoutError:
                    if await http_request.is_disconnected():
                        return
                    yield ": heartbeat\n\n"
                    continue
                event_name = frame.get("event")
                data = frame.get("data") or {}
                if event_name == "org_graph_event":
                    seq_val = data.get("sequence")
                    if type(seq_val) is int:
                        if seq_val <= last_sent_seq:
                            continue
                        last_sent_seq = seq_val
                    yield format_sse_frame("org_graph_event", data, event_id=frame.get("id"))
                elif event_name == "run.terminal":
                    _, _, tail_missed = snapshot_events_after(last_sent_seq)
                    for ev_data in tail_missed:
                        last_sent_seq = max(last_sent_seq, ev_data["sequence"])
                        yield format_org_graph_sse_frame(operation_id, ev_data)
                    yield format_sse_frame("run.terminal", data, event_id=frame.get("id"))
                    return
                else:
                    yield format_sse_frame(str(event_name), data, event_id=frame.get("id"))
        finally:
            research_live_broker.unsubscribe(subscriber)

    return StreamingResponse(live_squad_stream(), media_type="text/event-stream", headers=sse_headers)


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


def _test_tushare_token(account_id: str, token: str | None) -> str:
    """One bounded daily-bar read through the account's own Tushare token; no diagnostics are returned."""
    if not token:
        return "unavailable"
    from .fin_data.reachability import default_probe
    if not default_probe().reachable("tushare"):  # mainland-only host: fail fast instead of waiting for a timeout
        return "unavailable"
    from .fin_data.adapters.tushare import TushareAdapter
    from .fin_data.cn import CnCode
    end = lifecycle_clock.now().date()
    adapter = TushareAdapter(token=token, credential_owner="user_byok", account_id=account_id, timeout=10.0, now_fn=lifecycle_clock.now)
    result = adapter.bars(CnCode("600519", "SSE", False), start=end - timedelta(days=10), end=end)
    return "ready" if result.status in ("ok", "partial", "empty") else "unavailable"


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
    if request.provider_id == "tushare":
        return MarketProviderTestResponse(provider_id="tushare", status=_test_tushare_token(account.account_id, credentials.get("tushare")))
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


@app.post("/v1/account/model-config/models", response_model=ModelCatalog, response_model_exclude_none=True, include_in_schema=False)
def discover_personal_models(request: ModelDiscoveryRequest, response: Response,
                             authorization: str | None = Header(default=None, alias="Authorization")) -> ModelCatalog:
    account = _authenticated_account(authorization)
    response.headers["Cache-Control"] = "no-store"
    try:
        return discover_models(account.account_id, request, model_settings)
    except ModelCredentialError as error:
        raise _model_credential_problem(error) from error


@app.post("/v1/account/model-config/test", response_model=ModelConfigTestResponse, response_model_exclude_none=True, include_in_schema=False)
def test_model_config(
    response: Response,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> ModelConfigTestResponse:
    """Verify the selected model can issue a native tool call using a synthetic question."""
    from .chat_research_adapter import ChatResearchAdapter
    from .model_protocol import ModelToolDefinition, ModelTurnInput
    account = _authenticated_account(authorization)
    response.headers["Cache-Control"] = "no-store"
    try:
        settings, config = model_settings.runtime_configuration(account.account_id)
        if settings is None or config is None:
            return ModelConfigTestResponse(status="unavailable")
        # Only this synthetic call may try the capability before it is verified.
        provider = _personal_model_provider(dataclass_replace(settings, tools_verified=True), config, research=True)
        chat = config.protocol == "openai_chat_completions"
        adapter = ChatResearchAdapter(provider) if chat else provider
        tool = ModelToolDefinition("verify_connection", "Verify research tool support.", {
            "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"],
        })
        turn = ModelTurnInput("Use the provided tool.", "Call verify_connection with value ok.", (tool,), ())
        # Chat output-limit field support varies by endpoint. Use the same
        # minimal wire fields as a normal uncapped Chat turn.
        result = adapter.complete_turn(turn, timeout_seconds=30, max_output_tokens=256 if config.protocol == "anthropic_messages" else None)
        verified = (result.stop_class == "tool_calls" and len(result.tool_calls) == 1
                    and result.tool_calls[0].name == tool.name and result.tool_calls[0].arguments == {"value": "ok"})
    except ProviderFailure as error:
        return ModelConfigTestResponse(status="unavailable", upstream_status=error.upstream_status, parameter=error.parameter)
    except (ModelCredentialError, ApiProblem):
        return ModelConfigTestResponse(status="unavailable")
    recorded = model_settings.record_tools_result(config, verified)
    ready = verified and recorded
    return ModelConfigTestResponse(status="ready" if ready else "unavailable")


@app.get("/v1/account", include_in_schema=False)
def account_summary(authorization: str | None = Header(default=None, alias="Authorization")) -> JSONResponse:
    """Return server-authoritative plan and token quota state for the session."""

    account = _authenticated_account(authorization)
    return JSONResponse(
        {"account": {"account_id": account.account_id, "email": account.email}, **billing_ledger.snapshot(account.account_id, now=lifecycle_clock.now()),
         "research_model": configured_research_model()},
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
        thesis = research_provider.thesis(
            operation_id=operation_id,
            session_id=request.session_id,
            raw_text=request.thesis,
            capital=request.capital,
            base_currency=request.base_currency,
            private_evidence=request.user_private_evidence_claims,
        )
        challenge_id = deterministic_id("challenge", operation_id)
        draft = _generate_challenge_with_budget(provider, http_request, operation_id, request.session_id,
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
        draft = _generate_challenge_with_budget(provider, http_request, operation_id, request.session_id,
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
        turn = CHALLENGE_TURNS[challenge_id] + 1
        attached = tuple(item for item in EVIDENCE.values() if item.challenge_id == challenge_id)
        draft = _generate_challenge_with_budget(provider, http_request, operation_id, session_id,
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

    def build(operation_id: str) -> tuple[ChallengeRecord, str]:
        current = CHALLENGES.get(challenge_id)
        if not current:
            raise ApiProblem(404, "not_found", "Challenge not found.")
        if current.status not in {"CHALLENGING", "EVIDENCE_REQUIRED"}:
            raise ApiProblem(409, "invalid_state_transition", f"Message command cannot transition from {current.status}.")
        turn = CHALLENGE_TURNS[challenge_id] + 1
        attached = tuple(item for item in EVIDENCE.values() if item.challenge_id == challenge_id)
        draft = _generate_challenge_with_budget(provider, http_request, operation_id, session_id, ChallengeInput(
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
        draft = _generate_challenge_with_budget(provider, http_request, operation_id, session_id,
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
            live.prepare_call = lambda prompt, system: _prepare_scenario_model_call(http_request, operation_id, session_id, selected_provider, prompt, system)
            live.on_submission = lambda: setattr(http_request.state, 'model_submission_started', True)
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
            live.prepare_call = lambda prompt, system: _prepare_scenario_model_call(http_request, operation_id, session_id, selected_provider, prompt, system)
            live.on_submission = lambda: setattr(http_request.state, 'model_submission_started', True)
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


def _deliver_cancelled_research(operation_id: str, owner_id: str) -> None:
    _deliver_saved_research(operation_id, owner_id, reason="provider_cancelled")


def _deliver_saved_research(operation_id: str, owner_id: str, *, reason: str | None) -> None:
    """Publish the last durable observations without resuming or settling an external call."""
    from types import SimpleNamespace
    from .research_runtime import run_research_agent

    inputs = RESEARCH_RUN_INPUTS.get(operation_id) or {}
    if inputs.get("run_mode") == "earnings_squad":
        return
    checkpoint = inputs.get("runtime_checkpoint") or {}
    # Stopping a regeneration keeps the answer it was meant to replace.
    if _replaces_turn(inputs):
        return
    if inputs.get("owner_id") != owner_id or not checkpoint or (reason == "provider_cancelled" and not any(checkpoint.get(k) for k in (
        "facts", "calculations", "market_analyses", "draft_claims", "question_parts", "read_ranges"
    ))):
        return
    try:
        topic = research_topics.get(owner_id, inputs["topic_id"])
        parent = topic.versions[-1]
        if parent.version_id != inputs["parent_version_id"]:
            return
        sources = [research_sources.get(owner_id, row["source_id"]) for row in checkpoint["_sources"]]
        if _stored_form(checkpoint["_sources"]) != _stored_form(_research_source_fingerprints(sources)):
            return
        # The stopped branch consumes local observations only. The private checkpoint retains
        # the actual pending kind so a later recovery still sees any unknown external result.
        if reason is None and not checkpoint.get("submission"):
            raise ValueError("Local delivery requires a submitted checkpoint.")
        local_state = {**checkpoint, "stopped": reason}
        accounting = checkpoint.get("_accounting", {})
        context = SimpleNamespace(private_diagnostics={}, calls=accounting.get("calls", 0), usage=accounting.get("usage"),
                                  accounted_tokens=accounting.get("accounted_tokens"), search_calls=accounting.get("search_calls", 0),
                                  model_call_receipts=accounting.get("model_call_receipts", {}))
        outcome = run_research_agent(question=inputs["question"], sources=sources, model=None, context=context,
                                     as_of=topic.request.as_of, initial_state=local_state)
        chain = ResearchVersionChain(research_topics, owner_id, topic.topic_id, parent.version_id, [s.source_id for s in sources])
        meta = {**inputs["turn"], "operation_ids": [oid for oid in inputs["turn"]["operation_ids"] if oid != operation_id]}
        persister = ResearchTurnPersister(topic.topic_id, parent.version_id, topic.request.as_of, sources, chain, meta)
        result = persister.finalize_turn(operation_id=operation_id, parent_version=parent, outcome=outcome, runtime_state=local_state,
                                         context=context, finished_at=lifecycle_clock.now(), default_search_status="not_requested",
                                         official_fetch_status="partial" if checkpoint.get("acquired_source_ids") else "not_requested")
        record = TURN_RECORDS.get(meta['turn_id'])
        if record:
            result.content.turns[-1].lifecycle_status = 'stopped' if reason == 'provider_cancelled' else 'completed'
            result.content.turns[-1].user_instructions = _saved_instructions(record, checkpoint, result.content)
        saved = chain.save(parent, result.content)
        status = 'stopped' if reason == 'provider_cancelled' else 'completed'
        _update_turn_status(topic.topic_id, operation_id, status, saved.version_id)
    except (SourceNotFound, TopicNotFound, TopicVersionConflict):
        # Another writer won, or evidence was withdrawn. Never replace their revision.
        return


@app.post("/v1/operations/{operation_id}/cancel", response_model=OperationResponse, response_model_exclude_none=True, responses=ERROR_RESPONSES, operation_id="cancelOperation")
def cancel_operation(operation_id: str, request: CancelOperationRequest, response: Response, idempotency_key: IdempotencyKey, http_request: Request) -> OperationResponse:
    _validate_request(request, "CancelOperationRequest")
    owner = _operation_owner_subject(http_request)
    result, replayed = operations.cancel(operation_id, request.cancel_token, idempotency_key, owner_subject=owner)
    if result.operation.status == "cancelled" and result.operation.endpoint.startswith("/v1/research/topics/") and owner.startswith("account:"):
        account_id = owner.removeprefix("account:")
        inputs = RESEARCH_RUN_INPUTS.get(operation_id) or {}
        if inputs.get("run_mode") == "earnings_squad":
            _cancel_active_earnings_squad(account_id, str(inputs.get("topic_id") or ""), operation_id)
        else:
            _deliver_cancelled_research(operation_id, account_id)
    # Cancellation does not prove that a provider request was never sent.
    # Known-unsent paths release their own reservations; unknown usage remains.
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
