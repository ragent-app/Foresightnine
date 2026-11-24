"""Provider-agnostic external earnings data contracts (consensus & call transcripts).

Defines the normalized models, credential resolution policy (strict BYOK by default,
with an explicit opt-in flag for development/self-use platform env keys), and
graceful degradation envelopes pointing callers to SEC 8-K Exhibit 99.1 guidance
and user-supplied transcript uploads when external data is unavailable.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import os
import re
from typing import Any, Callable, Iterable, Literal, Mapping, Protocol, runtime_checkable
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .fin_data.models import Cost, Coverage, Permissions, Provenance, SuggestedAction, ToolResult
from .fin_data.policy import CredentialOwner, FinDataPolicy

FMP_PROVIDER_ID = "fmp"
FMP_API_KEY_ENV = "FORESIGHTNINE_FMP_API_KEY"
FMP_ALLOW_PLATFORM_KEY_ENV = "FORESIGHTNINE_FMP_ALLOW_PLATFORM_KEY"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})

_SECRET_QUERY_KEYS = frozenset({"apikey", "api_key", "token", "access_token", "key"})
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:apikey|api_key|token|access_token)=)[^&\s\"'<>]+",
    re.IGNORECASE,
)
_KV_SECRET_RE = re.compile(
    r"\b(apikey|api_key|token|access_token)\s*[:=]\s*(?:apikey\s+|bearer\s+)?[^\s,;\"'<>]+",
    re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(
    r"\b(authorization)\s*:\s*(?:apikey\s+|bearer\s+)?[^\s,;\"'<>]+",
    re.IGNORECASE,
)

_SECTION_HEADERS: dict[str, tuple[str, str]] = {
    "presentation": ("prepared_remarks", "Presentation"),
    "prepared remarks": ("prepared_remarks", "Prepared Remarks"),
    "opening remarks": ("prepared_remarks", "Opening Remarks"),
    "management discussion": ("prepared_remarks", "Management Discussion"),
    "question-and-answer session": ("qa", "Question-and-Answer Session"),
    "question and answer session": ("qa", "Question-and-Answer Session"),
    "questions and answers": ("qa", "Questions and Answers"),
    "questions & answers": ("qa", "Questions and Answers"),
    "q&a session": ("qa", "Q&A Session"),
    "q&a": ("qa", "Q&A"),
}

_SPEAKER_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 .,'&/\-]{0,70}?):\s*(.*)$")
_NON_SPEAKER_PREFIXES = frozenset({
    "note", "disclaimer", "forward-looking statements", "summary", "guidance",
    "source", "copyright", "http", "https", "step", "item", "exhibit",
    "table", "figure", "warning", "caution", "date", "time", "operator note",
})
_FISCAL_PREFIX_RE = re.compile(r"^(?:q[1-4]|fy\d{2,4})$", re.IGNORECASE)
_QA_TRANSITION_RE = re.compile(
    r"(?:question[- ]and[- ]answer|q\s*&\s*a\s+session|open\s+(?:the\s+)?(?:call|floor|lines?)\s+for\s+questions|"
    r"first\s+question\s+comes\s+from|poll\s+for\s+questions)",
    re.IGNORECASE,
)

CONSENSUS_FALLBACK_SUGGESTIONS: tuple[str, ...] = (
    "Fall back to prior and current SEC 8-K Exhibit 99.1 guidance vs. actuals (sec_earnings_release).",
    "Configure a personal FMP API key in Settings (or upload analyst estimates) to enable street consensus.",
)
TRANSCRIPT_FALLBACK_SUGGESTIONS: tuple[str, ...] = (
    "Upload the earnings call transcript or prepared remarks as a user_supplied document.",
    "Review SEC 8-K Exhibit 99.1 (earnings release) and 10-Q/10-K MD&A for management commentary.",
)


def default_third_party_permissions(*, owner_scoped: bool = False) -> Permissions:
    """Default permissions for BYOK / third-party earnings data: export stays disabled."""
    return Permissions(
        store=True,
        display=True,
        model_input=True,
        export=False,
        owner_scoped=owner_scoped,
    )


def parse_env_bool(value: str | None, *, default: bool = False) -> bool:
    """Parse an explicit boolean environment flag; blank or unrecognized values fall back safely."""
    if value is None:
        return default
    cleaned = value.strip().lower()
    if not cleaned:
        return default
    if cleaned in _TRUTHY:
        return True
    if cleaned in _FALSY:
        return False
    return default


def redact_secrets(text: str, secrets: Iterable[str] = (), *, max_length: int = 300) -> str:
    """Scrub API keys, query parameters, and auth headers from diagnostic text."""
    if not isinstance(text, str):
        text = str(text)
    cleaned = text
    for secret in secrets:
        if not secret:
            continue
        for variant in (secret, quote(secret, safe=""), quote_plus(secret)):
            if variant:
                cleaned = cleaned.replace(variant, "***")
    cleaned = _QUERY_SECRET_RE.sub(r"\1***", cleaned)
    cleaned = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}=***", cleaned)
    cleaned = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}: ***", cleaned)
    if max_length > 0:
        return cleaned[:max_length]
    return cleaned


def sanitize_endpoint_url(url: str) -> str:
    """Strip secret query parameters and userinfo from a URL before storing provenance or logging."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return redact_secrets(url, max_length=500)
    netloc = parts.netloc.rsplit("@", 1)[-1] if "@" in parts.netloc else parts.netloc
    filtered_query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _SECRET_QUERY_KEYS
    ]
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(filtered_query), ""))


def _coerce_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid Decimal value")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Decimal value must be finite")
        return value
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"invalid Decimal value: {value!r}") from None
    if not dec.is_finite():
        raise ValueError("Decimal value must be finite")
    return dec


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EarningsConsensus(_FrozenModel):
    """Provider-agnostic consensus estimates and reported actuals for one fiscal period."""

    symbol: str = Field(min_length=1, max_length=32)
    fiscal_period: str = Field(min_length=1, max_length=32)
    period_end: date | None = None
    revenue_estimate_avg: Decimal | None = None
    revenue_estimate_low: Decimal | None = None
    revenue_estimate_high: Decimal | None = None
    eps_estimate_avg: Decimal | None = None
    eps_estimate_low: Decimal | None = None
    eps_estimate_high: Decimal | None = None
    revenue_actual: Decimal | None = None
    eps_actual: Decimal | None = None
    num_analysts: int | None = Field(default=None, ge=0)
    source_provider: str = Field(min_length=1, max_length=64)
    as_of: datetime | date | None = None
    permissions: Permissions = Field(default_factory=default_third_party_permissions)
    # Optional structured metadata for period alignment and source provenance
    fiscal_year: int | None = Field(default=None, ge=1900, le=2200)
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)
    period_type: Literal["quarter", "annual"] | None = None
    reported_date: date | None = None
    num_analysts_revenue: int | None = Field(default=None, ge=0)
    num_analysts_eps: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=16)

    @field_validator("symbol", "fiscal_period", "source_provider")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("field must not be blank")
        return cleaned

    @field_validator("symbol")
    @classmethod
    def _upper_symbol(cls, value: str) -> str:
        return value.upper()

    @field_validator(
        "revenue_estimate_avg",
        "revenue_estimate_low",
        "revenue_estimate_high",
        "eps_estimate_avg",
        "eps_estimate_low",
        "eps_estimate_high",
        "revenue_actual",
        "eps_actual",
        mode="before",
    )
    @classmethod
    def _validate_decimal(cls, value: Any) -> Decimal | None:
        return _coerce_decimal(value)

    @model_validator(mode="after")
    def _derive_num_analysts(self) -> "EarningsConsensus":
        if self.num_analysts is None:
            counts = [c for c in (self.num_analysts_eps, self.num_analysts_revenue) if c is not None]
            if counts:
                object.__setattr__(self, "num_analysts", max(counts))
        return self


class TranscriptTurn(_FrozenModel):
    """Single speaker utterance inside an earnings call transcript."""

    speaker: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1)
    section: str | None = Field(default=None, max_length=64)
    role: str | None = Field(default=None, max_length=64)


class TranscriptSection(_FrozenModel):
    """Logical section (e.g., prepared_remarks or qa) of an earnings call transcript."""

    name: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=120)
    speakers: list[str] = Field(default_factory=list)
    turns: list[TranscriptTurn] = Field(default_factory=list)
    text: str = ""


def _infer_speaker_role(speaker: str, section_name: str) -> str | None:
    lowered = speaker.lower()
    if lowered == "operator" or lowered.startswith("operator "):
        return "operator"
    if "analyst" in lowered:
        return "analyst"
    if any(title in lowered for title in ("ceo", "cfo", "coo", "cto", "president", "chief ", "vp ", "ir", "investor relations")):
        return "executive"
    if section_name == "qa":
        return "participant"
    return None


def _is_speaker_candidate(prefix: str) -> bool:
    cleaned = prefix.strip()
    if not cleaned or len(cleaned) > 70 or len(cleaned.split()) > 8:
        return False
    lowered = cleaned.lower()
    if lowered in _NON_SPEAKER_PREFIXES or _FISCAL_PREFIX_RE.match(cleaned):
        return False
    if any(ch.isdigit() for ch in cleaned):
        return False
    return True


def parse_transcript_sections(full_text: str) -> tuple[list[TranscriptSection], list[str]]:
    """Deterministically segment an earnings call transcript into sections and speakers."""
    normalized = full_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return [], []

    current_section = "prepared_remarks"
    section_titles: dict[str, str] = {"prepared_remarks": "Prepared Remarks", "qa": "Question-and-Answer Session"}
    turns_by_section: dict[str, list[TranscriptTurn]] = {}
    section_order: list[str] = []
    ordered_speakers: list[str] = []
    seen_speakers: set[str] = set()

    current_speaker: str | None = None
    current_lines: list[str] = []
    current_turn_section = current_section

    def flush_turn() -> None:
        nonlocal current_speaker, current_lines
        if current_speaker is None:
            current_lines = []
            return
        utterance = "\n".join(line for line in current_lines if line.strip()).strip()
        if utterance:
            sec = current_turn_section
            if sec not in turns_by_section:
                turns_by_section[sec] = []
                section_order.append(sec)
            turns_by_section[sec].append(
                TranscriptTurn(
                    speaker=current_speaker,
                    text=utterance,
                    section=sec,
                    role=_infer_speaker_role(current_speaker, sec),
                )
            )
            if current_speaker not in seen_speakers:
                seen_speakers.add(current_speaker)
                ordered_speakers.append(current_speaker)
        current_speaker = None
        current_lines = []

    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            if current_speaker is not None and current_lines:
                current_lines.append("")
            continue
        header_key = line.rstrip(":").strip().lower()
        if header_key in _SECTION_HEADERS:
            flush_turn()
            sec_name, sec_title = _SECTION_HEADERS[header_key]
            current_section = sec_name
            section_titles[sec_name] = sec_title
            continue

        match = _SPEAKER_LINE_RE.match(line)
        if match and _is_speaker_candidate(match.group(1)):
            flush_turn()
            speaker = match.group(1).strip()
            rest = match.group(2).strip()
            if speaker.lower().startswith("operator") and _QA_TRANSITION_RE.search(rest):
                current_section = "qa"
            current_speaker = speaker
            current_turn_section = current_section
            current_lines = [rest] if rest else []
            continue

        if current_speaker is not None:
            if current_speaker.lower().startswith("operator") and _QA_TRANSITION_RE.search(line):
                current_section = "qa"
                current_turn_section = "qa"
            current_lines.append(line)

    flush_turn()

    if not turns_by_section:
        return (
            [
                TranscriptSection(
                    name="full_transcript",
                    title="Full Transcript",
                    speakers=[],
                    turns=[],
                    text=normalized,
                )
            ],
            [],
        )

    sections: list[TranscriptSection] = []
    for sec_name in section_order:
        turns = turns_by_section[sec_name]
        sec_speakers: list[str] = []
        sec_seen: set[str] = set()
        for turn in turns:
            if turn.speaker not in sec_seen:
                sec_seen.add(turn.speaker)
                sec_speakers.append(turn.speaker)
        sec_text = "\n\n".join(f"{t.speaker}: {t.text}" for t in turns)
        sections.append(
            TranscriptSection(
                name=sec_name,
                title=section_titles.get(sec_name, sec_name.replace("_", " ").title()),
                speakers=sec_speakers,
                turns=turns,
                text=sec_text,
            )
        )
    return sections, ordered_speakers


class EarningsTranscript(_FrozenModel):
    """Provider-agnostic earnings call transcript with deterministic hash and speaker segmentation."""

    symbol: str = Field(min_length=1, max_length=32)
    fiscal_year: int = Field(ge=1900, le=2200)
    fiscal_quarter: int = Field(ge=1, le=4)
    call_date: datetime | date | None = None
    sections: list[TranscriptSection] = Field(default_factory=list)
    speakers: list[str] = Field(default_factory=list)
    full_text: str = Field(min_length=1)
    content_sha256: str = ""
    source_provider: str = Field(min_length=1, max_length=64)
    as_of: datetime | date | None = None
    permissions: Permissions = Field(default_factory=default_third_party_permissions)

    @field_validator("symbol", "source_provider")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("field must not be blank")
        return cleaned

    @field_validator("symbol")
    @classmethod
    def _upper_symbol(cls, value: str) -> str:
        return value.upper()

    @field_validator("full_text")
    @classmethod
    def _nonblank_full_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("full_text must not be blank")
        return cleaned

    @model_validator(mode="after")
    def _ensure_sha256(self) -> "EarningsTranscript":
        expected = sha256(self.full_text.encode("utf-8")).hexdigest()
        if not self.content_sha256:
            object.__setattr__(self, "content_sha256", expected)
        else:
            normalized = self.content_sha256.strip().lower()
            if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
                raise ValueError("content_sha256 must be a 64-character lowercase hex digest")
            object.__setattr__(self, "content_sha256", normalized)
        return self

    @classmethod
    def from_raw_text(
        cls,
        *,
        symbol: str,
        fiscal_year: int,
        fiscal_quarter: int,
        full_text: str,
        source_provider: str,
        call_date: datetime | date | None = None,
        as_of: datetime | date | None = None,
        permissions: Permissions | None = None,
    ) -> "EarningsTranscript":
        cleaned = full_text.strip()
        sections, speakers = parse_transcript_sections(cleaned)
        return cls(
            symbol=symbol,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            call_date=call_date,
            sections=sections,
            speakers=speakers,
            full_text=cleaned,
            content_sha256=sha256(cleaned.encode("utf-8")).hexdigest(),
            source_provider=source_provider,
            as_of=as_of,
            permissions=permissions if permissions is not None else default_third_party_permissions(),
        )


@runtime_checkable
class ExternalEarningsProvider(Protocol):
    """Provider-agnostic interface for external earnings consensus and call transcripts."""

    provider_id: str

    def fetch_consensus(
        self,
        symbol: str,
        *,
        period: Literal["quarter", "annual"] = "quarter",
        limit: int = 8,
        include_actuals: bool = True,
        api_key: str | None = None,
        credential_owner: CredentialOwner = "user_byok",
        account_id: str | None = None,
    ) -> ToolResult: ...

    def fetch_transcript(
        self,
        symbol: str,
        *,
        fiscal_year: int | None = None,
        fiscal_quarter: int | None = None,
        api_key: str | None = None,
        credential_owner: CredentialOwner = "user_byok",
        account_id: str | None = None,
    ) -> ToolResult: ...


class ResolvedExternalCredential(_FrozenModel):
    """Result of BYOK vs. gated platform credential resolution (never exposes the secret)."""

    provider_id: str
    api_key: str | None = Field(default=None, repr=False)
    credential_owner: CredentialOwner | None = None
    account_id: str | None = None
    allow_platform_key: bool = False
    reason: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.credential_owner)

    def __str__(self) -> str:
        return (
            f"ResolvedExternalCredential(provider_id={self.provider_id!r}, "
            f"configured={self.configured}, owner={self.credential_owner!r}, "
            f"allow_platform_key={self.allow_platform_key})"
        )


def resolve_external_credential(
    *,
    provider_id: str = FMP_PROVIDER_ID,
    byok_api_key: str | None = None,
    byok_resolver: Callable[[str | None, str], str | None] | None = None,
    account_id: str | None = None,
    allow_platform_key: bool | None = None,
    platform_key_env: str = FMP_API_KEY_ENV,
    allow_platform_env: str = FMP_ALLOW_PLATFORM_KEY_ENV,
    env: Mapping[str, str] | None = None,
) -> ResolvedExternalCredential:
    """Resolve user BYOK key first; fall back to platform env key ONLY when explicitly allowed."""
    environ = os.environ if env is None else env
    allow_platform = (
        allow_platform_key
        if allow_platform_key is not None
        else parse_env_bool(environ.get(allow_platform_env), default=False)
    )

    own_key = byok_api_key.strip() if isinstance(byok_api_key, str) else None
    if not own_key and byok_resolver is not None:
        resolved = byok_resolver(account_id, provider_id)
        own_key = resolved.strip() if isinstance(resolved, str) else None

    if own_key:
        return ResolvedExternalCredential(
            provider_id=provider_id,
            api_key=own_key,
            credential_owner="user_byok",
            account_id=account_id,
            allow_platform_key=allow_platform,
            reason=None,
        )

    if allow_platform:
        platform_key = (environ.get(platform_key_env) or "").strip() or None
        if platform_key:
            return ResolvedExternalCredential(
                provider_id=provider_id,
                api_key=platform_key,
                credential_owner="platform",
                account_id=account_id,
                allow_platform_key=True,
                reason=None,
            )
        return ResolvedExternalCredential(
            provider_id=provider_id,
            api_key=None,
            credential_owner=None,
            account_id=account_id,
            allow_platform_key=True,
            reason="api_key_not_configured",
        )

    return ResolvedExternalCredential(
        provider_id=provider_id,
        api_key=None,
        credential_owner=None,
        account_id=account_id,
        allow_platform_key=False,
        reason="byok_key_required",
    )


class _ExternalResultBase(_FrozenModel):
    status: Literal["ok", "partial", "unavailable"]
    degraded: bool = False
    reason: str
    message: str = ""
    retryable: bool = False
    provider: str | None = None
    provider_status: str | None = None
    credential_owner: CredentialOwner | None = None
    permissions: Permissions = Field(default_factory=Permissions.none)
    provenance: Provenance | None = None
    cost: Cost = Field(default_factory=Cost)
    coverage: Coverage | None = None
    suggested_actions: list[SuggestedAction] = Field(default_factory=list)
    fallback_suggestions: list[str] = Field(default_factory=list)


class ConsensusResult(_ExternalResultBase):
    """Service envelope for external earnings consensus lookups."""

    consensus: list[EarningsConsensus] = Field(default_factory=list)


class TranscriptResult(_ExternalResultBase):
    """Service envelope for external earnings call transcript lookups."""

    transcript: EarningsTranscript | None = None


def _degradation_message(kind: Literal["consensus", "transcript"], reason: str, detail: str, symbol: str) -> str:
    label = "Street consensus" if kind == "consensus" else "Earnings call transcript"
    if reason == "byok_key_required":
        base = (
            f"{label} for {symbol} is unavailable: strict BYOK mode is active and no personal FMP API key "
            "is configured for this account."
        )
    elif reason == "api_key_not_configured":
        base = f"{label} for {symbol} is unavailable: no FMP API key is configured."
    elif reason in (
        "symbol_not_found",
        "no_consensus_data",
        "ticker_uncovered",
        "transcript_not_found",
        "transcript_unavailable",
    ):
        base = f"{label} for {symbol} is unavailable: ticker or period is not covered by the external provider."
    elif reason in ("provider_rate_limited", "rate_limited", "daily_quota_exhausted"):
        base = f"{label} for {symbol} is temporarily unavailable due to provider rate limits."
    elif reason in ("invalid_api_key",):
        base = f"{label} for {symbol} is unavailable: the configured external API key was rejected."
    elif reason in ("provider_plan_restriction", "license_platform_key_not_permitted", "not_in_policy", "forbidden"):
        base = f"{label} for {symbol} is unavailable due to provider plan or policy restrictions."
    else:
        base = f"{label} for {symbol} is unavailable ({reason})."
    return f"{base} {detail}".strip() if detail and detail not in base else base


def _fallback_actions(kind: Literal["consensus", "transcript"], existing: list[SuggestedAction] | None = None) -> list[SuggestedAction]:
    actions = list(existing or [])
    if kind == "consensus":
        actions.append(
            SuggestedAction(
                action="sec_earnings_release",
                hint="Use prior and current SEC 8-K Exhibit 99.1 guidance vs. actuals as the benchmark.",
            )
        )
        actions.append(
            SuggestedAction(
                action="user_upload_or_byok",
                hint="Configure a personal FMP key in Settings or upload consensus figures.",
            )
        )
    else:
        actions.append(
            SuggestedAction(
                action="user_upload_transcript",
                hint="Upload the earnings call transcript or prepared remarks for this topic.",
            )
        )
        actions.append(
            SuggestedAction(
                action="sec_earnings_release",
                hint="Review SEC 8-K Exhibit 99.1 and 10-Q/10-K MD&A for management commentary.",
            )
        )
    return actions


class ExternalEarningsService:
    """Provider-agnostic service resolving credentials and degrading gracefully on gaps."""

    def __init__(
        self,
        *,
        provider: ExternalEarningsProvider | None = None,
        allow_platform_key: bool | None = None,
        env: Mapping[str, str] | None = None,
        byok_resolver: Callable[[str | None, str], str | None] | None = None,
        account_id: str | None = None,
        policy: FinDataPolicy | None = None,
        platform_key_env: str = FMP_API_KEY_ENV,
        allow_platform_env: str = FMP_ALLOW_PLATFORM_KEY_ENV,
        transport: Any | None = None,
        limiter: Any | None = None,
        timeout: float | None = None,
        api_version: Literal["stable", "v3"] = "stable",
    ) -> None:
        self._provider = provider
        self._allow_platform_key = allow_platform_key
        self._env = env
        self._byok_resolver = byok_resolver
        self._account_id = account_id
        self._policy = policy
        self._platform_key_env = platform_key_env
        self._allow_platform_env = allow_platform_env
        self._transport = transport
        self._limiter = limiter
        self._timeout = timeout
        self._api_version: Literal["stable", "v3"] = api_version

    def _active_provider(self) -> ExternalEarningsProvider:
        if self._provider is None:
            from .fin_data.adapters.fmp import FmpEarningsAdapter

            kwargs: dict[str, Any] = {
                "allow_platform_key": self._allow_platform_key,
                "env": self._env,
                "policy": self._policy,
                "transport": self._transport,
                "limiter": self._limiter,
                "api_version": self._api_version,
            }
            if self._timeout is not None:
                kwargs["timeout"] = self._timeout
            self._provider = FmpEarningsAdapter(**kwargs)
            return self._provider
        binder = getattr(self._provider, "with_runtime_config", None)
        if callable(binder):
            return binder(
                allow_platform_key=self._allow_platform_key,
                env=self._env,
                policy=self._policy,
            )
        return self._provider

    def resolve_credentials(
        self,
        *,
        byok_api_key: str | None = None,
        account_id: str | None = None,
    ) -> ResolvedExternalCredential:
        provider_id = getattr(self._provider, "provider_id", FMP_PROVIDER_ID)
        return resolve_external_credential(
            provider_id=provider_id,
            byok_api_key=byok_api_key,
            byok_resolver=self._byok_resolver,
            account_id=account_id if account_id is not None else self._account_id,
            allow_platform_key=self._allow_platform_key,
            platform_key_env=self._platform_key_env,
            allow_platform_env=self._allow_platform_env,
            env=self._env,
        )

    def capabilities(
        self,
        *,
        byok_api_key: str | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        cred = self.resolve_credentials(byok_api_key=byok_api_key, account_id=account_id)
        return {
            "provider": cred.provider_id,
            "consensus_available": cred.configured,
            "transcript_available": cred.configured,
            "credential_owner": cred.credential_owner,
            "allow_platform_key": cred.allow_platform_key,
            "reason": cred.reason,
        }

    def get_consensus(
        self,
        symbol: str,
        *,
        period: Literal["quarter", "annual"] = "quarter",
        limit: int = 8,
        include_actuals: bool = True,
        byok_api_key: str | None = None,
        account_id: str | None = None,
    ) -> ConsensusResult:
        clean_symbol = symbol.strip().upper()
        cred = self.resolve_credentials(byok_api_key=byok_api_key, account_id=account_id)
        if not cred.configured:
            reason = cred.reason or "api_key_not_configured"
            return ConsensusResult(
                status="unavailable",
                degraded=True,
                reason=reason,
                message=_degradation_message("consensus", reason, "", clean_symbol or symbol),
                provider=cred.provider_id,
                permissions=Permissions.none(),
                suggested_actions=_fallback_actions("consensus"),
                fallback_suggestions=list(CONSENSUS_FALLBACK_SUGGESTIONS),
            )

        provider = self._active_provider()
        res = provider.fetch_consensus(
            clean_symbol,
            period=period,
            limit=limit,
            include_actuals=include_actuals,
            api_key=cred.api_key,
            credential_owner=cred.credential_owner or "user_byok",
            account_id=cred.account_id,
        )
        return self._to_consensus_result(res, clean_symbol or symbol, cred)

    def get_transcript(
        self,
        symbol: str,
        *,
        fiscal_year: int | None = None,
        fiscal_quarter: int | None = None,
        byok_api_key: str | None = None,
        account_id: str | None = None,
    ) -> TranscriptResult:
        clean_symbol = symbol.strip().upper()
        cred = self.resolve_credentials(byok_api_key=byok_api_key, account_id=account_id)
        if not cred.configured:
            reason = cred.reason or "api_key_not_configured"
            return TranscriptResult(
                status="unavailable",
                degraded=True,
                reason=reason,
                message=_degradation_message("transcript", reason, "", clean_symbol or symbol),
                provider=cred.provider_id,
                permissions=Permissions.none(),
                suggested_actions=_fallback_actions("transcript"),
                fallback_suggestions=list(TRANSCRIPT_FALLBACK_SUGGESTIONS),
            )

        provider = self._active_provider()
        res = provider.fetch_transcript(
            clean_symbol,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            api_key=cred.api_key,
            credential_owner=cred.credential_owner or "user_byok",
            account_id=cred.account_id,
        )
        return self._to_transcript_result(res, clean_symbol or symbol, cred)

    @staticmethod
    def _to_consensus_result(
        res: ToolResult,
        symbol: str,
        cred: ResolvedExternalCredential,
    ) -> ConsensusResult:
        secrets = (cred.api_key,) if cred.api_key else ()
        safe_msg = redact_secrets(res.message, secrets)
        if res.status in ("ok", "partial") and res.data:
            records = [item for item in res.data if isinstance(item, EarningsConsensus)]
            if records:
                return ConsensusResult(
                    status="partial" if res.status == "partial" else "ok",
                    degraded=False,
                    reason=res.reason,
                    message=safe_msg,
                    retryable=res.retryable,
                    provider=cred.provider_id,
                    provider_status=res.status,
                    credential_owner=cred.credential_owner,
                    permissions=res.permissions,
                    provenance=res.provenance,
                    cost=res.cost,
                    coverage=res.coverage,
                    suggested_actions=list(res.suggested_actions),
                    consensus=records,
                )

        if res.status in ("ok", "partial") or (res.status == "empty" and res.reason in ("symbol_not_found", "no_consensus_data")):
            reason = "ticker_uncovered"
        else:
            reason = res.reason
        return ConsensusResult(
            status="unavailable",
            degraded=True,
            reason=reason,
            message=_degradation_message("consensus", reason, safe_msg, symbol),
            retryable=res.retryable,
            provider=cred.provider_id,
            provider_status=res.status,
            credential_owner=cred.credential_owner,
            permissions=Permissions.none(),
            provenance=res.provenance,
            cost=res.cost,
            coverage=res.coverage,
            suggested_actions=_fallback_actions("consensus", list(res.suggested_actions)),
            fallback_suggestions=list(CONSENSUS_FALLBACK_SUGGESTIONS),
        )

    @staticmethod
    def _to_transcript_result(
        res: ToolResult,
        symbol: str,
        cred: ResolvedExternalCredential,
    ) -> TranscriptResult:
        secrets = (cred.api_key,) if cred.api_key else ()
        safe_msg = redact_secrets(res.message, secrets)
        if res.status in ("ok", "partial") and res.data:
            transcript = next((item for item in res.data if isinstance(item, EarningsTranscript)), None)
            if transcript is not None:
                return TranscriptResult(
                    status="partial" if res.status == "partial" else "ok",
                    degraded=False,
                    reason=res.reason,
                    message=safe_msg,
                    retryable=res.retryable,
                    provider=cred.provider_id,
                    provider_status=res.status,
                    credential_owner=cred.credential_owner,
                    permissions=res.permissions,
                    provenance=res.provenance,
                    cost=res.cost,
                    coverage=res.coverage,
                    suggested_actions=list(res.suggested_actions),
                    transcript=transcript,
                )

        if res.status in ("ok", "partial", "empty"):
            reason = "transcript_unavailable"
        else:
            reason = res.reason
        return TranscriptResult(
            status="unavailable",
            degraded=True,
            reason=reason,
            message=_degradation_message("transcript", reason, safe_msg, symbol),
            retryable=res.retryable,
            provider=cred.provider_id,
            provider_status=res.status,
            credential_owner=cred.credential_owner,
            permissions=Permissions.none(),
            provenance=res.provenance,
            cost=res.cost,
            coverage=res.coverage,
            suggested_actions=_fallback_actions("transcript", list(res.suggested_actions)),
            fallback_suggestions=list(TRANSCRIPT_FALLBACK_SUGGESTIONS),
        )


__all__ = [
    "CONSENSUS_FALLBACK_SUGGESTIONS",
    "ConsensusResult",
    "EarningsConsensus",
    "EarningsTranscript",
    "ExternalEarningsProvider",
    "ExternalEarningsService",
    "FMP_ALLOW_PLATFORM_KEY_ENV",
    "FMP_API_KEY_ENV",
    "FMP_PROVIDER_ID",
    "Permissions",
    "ResolvedExternalCredential",
    "TRANSCRIPT_FALLBACK_SUGGESTIONS",
    "TranscriptResult",
    "TranscriptSection",
    "TranscriptTurn",
    "default_third_party_permissions",
    "parse_env_bool",
    "parse_transcript_sections",
    "redact_secrets",
    "resolve_external_credential",
    "sanitize_endpoint_url",
]
