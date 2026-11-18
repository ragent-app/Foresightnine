"""Phase 3 providers and evidence safety boundary.

This module keeps provider selection and the evidence safety boundary separate.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
import ipaddress
import json
import math
import os
import re
import socket
import subprocess
from threading import BoundedSemaphore, Event, Lock
from time import monotonic, sleep
from typing import Any, Callable
from urllib.parse import urlsplit

from .models import RubricScore
from .services import content_hash


RUBRIC_CRITERIA = (
    "premise_clarity", "time_boundary", "trigger_observability", "causal_chain",
    "counterfactuals", "evidence_quality", "asset_transmission", "already_priced",
    "falsifiability", "user_confirmation",
)
_INJECTION = re.compile(
    r"(?i)((ignore|disregard|override|forget)\s+(all\s+)?(previous|prior|earlier)\s+(prompts?|instructions?|directions?)|"
    r"system\s+prompt|reveal\s+(your\s+)?instructions|"
    r"developer\s+message|jailbreak|do\s+not\s+follow)"
    r"|((approve|confirm|comply)\s+(this|the|it|my|our)\s+(claim|request|output|answer)?\s*(regardless|without|now))"
    r"|(compliance\s+(mode|override|instruction|check)|approval\s+override)"
)


class ProviderFailure(RuntimeError):
    """A safe, user-facing provider failure; never carries raw provider output."""

    def __init__(self, code: str, message: str, *, retryable: bool, reported_tokens: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.reported_tokens = reported_tokens if type(reported_tokens) is int and reported_tokens >= 0 else None


class PromptInjectionDetected(ProviderFailure):
    def __init__(self) -> None:
        super().__init__("prompt_injection_detected", "Untrusted content contains prompt-control instructions.", retryable=False)


@dataclass(frozen=True)
class ProviderPolicy:
    max_rounds: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_usd: float | None = None
    timeout_seconds: float = 20.0
    max_attempts: int = 3
    retry_base_seconds: float = 0.05


@dataclass(frozen=True)
class ChallengeInput:
    operation_id: str
    challenge_id: str
    thesis_id: str
    claim: str
    turn: int
    status: str
    private_evidence_declared: bool
    evidence_ids: tuple[str, ...] = ()
    evidence_grades: tuple[tuple[str, str], ...] = ()
    untrusted_text: tuple[str, ...] = ()
    cancel_event: Event | None = None


@dataclass(frozen=True)
class ChallengeDraft:
    claims: tuple[str, ...]
    objections: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    questions: tuple[str, ...]
    rubric_scores: tuple[RubricScore, ...]
    unresolved_issues: tuple[str, ...]
    citation_ids: tuple[str, ...]
    public_fact_citation_ids: tuple[str, ...]
    native_search_used: bool
    provenance: str
    total_tokens: int | None = None


@dataclass(frozen=True)
class SearchEvidenceBundle:
    """The complete client-side fallback bundle, hashed before it enters a record."""

    url: str | None
    title: str
    published_at: datetime | None
    retrieved_at: datetime
    excerpt: str
    user_note: str
    supplied_content_hash: str
    search_provider: str | None = None
    search_result_hash: str | None = None

    def hash_material(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "published_at": self.published_at,
            "retrieved_at": self.retrieved_at,
            "excerpt": self.excerpt,
            "user_note": self.user_note,
            "search_provider": self.search_provider,
            "search_result_hash": self.search_result_hash,
        }

    def expected_content_hash(self) -> str:
        return content_hash(self.hash_material())


class LlmProvider(ABC):
    """Business abstraction; adapters return a validated neutral draft only."""

    name: str

    @property
    @abstractmethod
    def supports_native_search(self) -> bool: ...

    @abstractmethod
    def generate_challenge(self, request: ChallengeInput) -> ChallengeDraft: ...


class SearchEvidenceProvider(ABC):
    """Search abstraction with a safe client-bundle fallback.

    Search discovery never turns a remote snippet into a fact automatically.
    A caller must render the candidate, retain its source fields, and obtain an
    explicit evidence submission before it can enter a challenge record.
    """

    @property
    def supports_project_search(self) -> bool:
        return False

    @abstractmethod
    def validate_client_bundle(self, bundle: SearchEvidenceBundle) -> bool: ...


class ClientSearchEvidenceProvider(SearchEvidenceProvider):
    """Validates submitted URLs without ever server-fetching untrusted evidence."""

    name = "client-evidence-bundle"
    max_url_length = 2_048

    @staticmethod
    def _safe_address(address: str) -> bool:
        parsed = ipaddress.ip_address(address)
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
            parsed = parsed.ipv4_mapped
        return not (parsed.is_private or parsed.is_loopback or parsed.is_link_local or parsed.is_multicast or parsed.is_reserved or parsed.is_unspecified)

    @staticmethod
    def _literal_address(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        try:
            return ipaddress.ip_address(hostname)
        except ValueError:
            # Some URL clients accept legacy decimal/abbreviated IPv4 forms such
            # as 2130706433 and 127.1. Canonicalise them here before deciding.
            if not re.fullmatch(r"\d+(?:\.\d+){0,3}", hostname):
                return None
            parts = [int(part, 10) for part in hostname.split(".")]
            limits = (0xFFFFFFFF, 0xFFFFFF, 0xFFFF, 0xFF)
            if not 1 <= len(parts) <= 4 or any(value < 0 or value > limits[index] for index, value in enumerate(parts)):
                return None
            if len(parts) == 1:
                value = parts[0]
            elif len(parts) == 2:
                value = (parts[0] << 24) | parts[1]
            elif len(parts) == 3:
                value = (parts[0] << 24) | (parts[1] << 16) | parts[2]
            else:
                value = (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
            return ipaddress.IPv4Address(value)

    def validate_url(self, url: str) -> None:
        if len(url) > self.max_url_length:
            raise ProviderFailure("unsafe_evidence_url", "Evidence URL exceeds the safety limit.", retryable=False)
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ProviderFailure("unsafe_evidence_url", "Evidence URL must be an absolute HTTP(S) URL without user info.", retryable=False)
        try:
            port = parsed.port
        except ValueError as error:
            raise ProviderFailure("unsafe_evidence_url", "Evidence URL port is invalid.", retryable=False) from error
        hostname = parsed.hostname.lower()
        if len(hostname) > 253 or hostname == "localhost" or hostname.endswith(".local") or port not in {None, 80, 443}:
            raise ProviderFailure("unsafe_evidence_url", "Evidence URL host or port is not permitted.", retryable=False)
        literal = self._literal_address(hostname)
        if literal is None:
            # Client evidence is not fetched by this service.  A future fetcher must
            # call validate_redirect_target for every redirect after DNS resolution.
            return
        if not self._safe_address(str(literal)):
            raise ProviderFailure("unsafe_evidence_url", "Evidence URL resolves to a private or reserved address.", retryable=False)

    def validate_redirect_target(self, url: str, resolver: Callable[[str, int | None], list[str]] | None = None) -> None:
        """Validate a redirect target before a future server-side fetch follows it."""
        self.validate_url(url)
        parsed = urlsplit(url)
        if resolver is None:
            resolver = lambda host, port: list({item[4][0] for item in socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)})
        try:
            addresses = resolver(parsed.hostname or "", parsed.port)
        except OSError as error:
            raise ProviderFailure("unsafe_evidence_url", "Evidence host could not be resolved safely.", retryable=False) from error
        if not addresses or any(not self._safe_address(address) for address in addresses):
            raise ProviderFailure("unsafe_evidence_url", "Evidence redirect resolves to a private or reserved address.", retryable=False)

    def validate_client_bundle(self, bundle: SearchEvidenceBundle) -> bool:
        if bundle.supplied_content_hash != bundle.expected_content_hash():
            raise ProviderFailure("invalid_evidence_bundle", "Evidence bundle content hash does not match its canonical fields.", retryable=False)
        if bundle.url is None:
            return False
        if bundle.published_at is None:
            raise ProviderFailure("invalid_evidence_bundle", "URL evidence requires a publication timestamp.", retryable=False)
        self.validate_url(bundle.url)
        return True


def build_search_evidence_provider() -> SearchEvidenceProvider:
    """Select only explicit, server-owned project search providers.

    Unknown configuration retains the client bundle path rather than sending a
    user query or credential to a guessed endpoint.  The GLM Coding MCP profile
    has a fixed vendor endpoint and reads its secret only when ``search`` is
    called by the server process.
    """

    selected = os.environ.get("FORESIGHTNINE_SEARCH_PROVIDER", "client").strip().lower()
    if selected in {"", "client", "client_bundle", "none"}:
        return ClientSearchEvidenceProvider()
    if selected in {"glm_coding_mcp", "zhipu_glm_coding_mcp"}:
        from .glm_mcp_search import GlmCodingMcpSearchEvidenceProvider

        return GlmCodingMcpSearchEvidenceProvider.from_environment()
    return ClientSearchEvidenceProvider()


def _estimated_tokens(value: str) -> int:
    return max(1, (len(value) + 3) // 4)


def _reject_injection(*values: str) -> None:
    if any(_INJECTION.search(value) for value in values):
        raise PromptInjectionDetected()


_REVIEW_ITEM_PREFIX = re.compile(r"^\s*(?:(?:\d{1,2}|[一二三四五六七八九十]+)[.\u3001):：-]|[-*•])\s*")
_REVIEW_ITEM_SPLIT = re.compile(r"(?:\r?\n)+\s*(?=(?:(?:\d{1,2}|[一二三四五六七八九十]+)[.\u3001):：-]|[-*•])\s*)")
_REVIEW_ITEM_END = re.compile(r"[.!?。！？；;:：]\s*$")


def _normalize_review_items(values: list[str], *, maximum: int) -> tuple[str, ...]:
    """Make model list fields finite and presentation-safe without dropping text.

    Compatible providers occasionally return a numbered list inside one JSON
    string or split a short subject heading from its explanation.  Flatten the
    former, join the latter, and fold only genuine overflow into the final
    point.  The operation removes numbering markup but retains every semantic
    fragment from the provider response.
    """
    items: list[str] = []
    for value in values:
        for fragment in _REVIEW_ITEM_SPLIT.split(value):
            cleaned = _REVIEW_ITEM_PREFIX.sub("", fragment.strip(), count=1).strip()
            if cleaned:
                items.append(cleaned)
    while len(items) > maximum:
        heading_index = next(
            (
                index
                for index, item in enumerate(items[:-1])
                if len(item) <= 32 and _REVIEW_ITEM_END.search(item) is None
            ),
            None,
        )
        if heading_index is None:
            break
        heading = items[heading_index]
        separator = "：" if re.search(r"[\u3400-\u9fff]", heading) else ": "
        items[heading_index : heading_index + 2] = [f"{heading}{separator}{items[heading_index + 1]}"]
    if len(items) > maximum:
        delimiter = "；" if any(re.search(r"[\u3400-\u9fff]", item) for item in items[maximum - 1 :]) else "; "
        items[maximum - 1 :] = [delimiter.join(items[maximum - 1 :])]
    return tuple(items)


def _draft_from_payload(
    payload: Any,
    request: ChallengeInput,
    *,
    native_search_used: bool = False,
    usage: tuple[int, float] | None = None,
) -> ChallengeDraft:
    if not isinstance(payload, dict):
        raise ProviderFailure("invalid_provider_output", "Provider returned no structured result.", retryable=False)
    required = {
        "claims", "objections", "missing_evidence", "questions", "rubric_scores", "unresolved_issues",
        "citation_ids", "public_fact_citation_ids", "native_search_used",
    }
    if payload.keys() != required:
        raise ProviderFailure("invalid_provider_output", "Provider structured result is incomplete.", retryable=False)
    list_fields = ("claims", "objections", "missing_evidence", "questions", "unresolved_issues", "citation_ids", "public_fact_citation_ids")
    if any(type(payload[field]) is not list for field in list_fields) or type(payload["rubric_scores"]) is not list or type(payload["native_search_used"]) is not bool:
        raise ProviderFailure("invalid_provider_output", "Provider structured result has invalid field types.", retryable=False)
    if any(any(type(item) is not str for item in payload[field]) for field in list_fields):
        raise ProviderFailure("invalid_provider_output", "Provider structured result contains non-string list items.", retryable=False)
    try:
        claims = _normalize_review_items(payload["claims"], maximum=3)
        objections = _normalize_review_items(payload["objections"], maximum=5)
        missing_evidence = _normalize_review_items(payload["missing_evidence"], maximum=5)
        questions = _normalize_review_items(payload["questions"], maximum=3)
        unresolved = _normalize_review_items(payload["unresolved_issues"], maximum=4)
        citations = tuple(payload["citation_ids"])
        public_citations = tuple(payload["public_fact_citation_ids"])
        if any(type(item) is not dict or set(item) != {"criterion", "score", "rationale"} or type(item["criterion"]) is not str or type(item["score"]) is not int or type(item["rationale"]) is not str for item in payload["rubric_scores"]):
            raise TypeError("non-strict rubric")
        scores = tuple(RubricScore(**item) for item in payload["rubric_scores"])
    except (TypeError, ValueError) as error:
        raise ProviderFailure("invalid_provider_output", "Provider structured result has invalid fields.", retryable=False) from error
    all_text = (*claims, *objections, *missing_evidence, *questions, *unresolved)
    if not claims or not objections or not 1 <= len(questions) <= 3 or any(not isinstance(text, str) or not text.strip() for text in all_text):
        raise ProviderFailure("invalid_provider_output", "Provider structured result violates challenge content limits.", retryable=False)
    if len(scores) != 10 or {score.criterion for score in scores} != set(RUBRIC_CRITERIA) or any(score.score not in range(5) for score in scores):
        raise ProviderFailure("invalid_provider_output", "Provider rubric must contain each of the ten 0–4 criteria exactly once.", retryable=False)
    evidence = dict(request.evidence_grades)
    if any(not isinstance(item, str) or item not in evidence for item in citations):
        raise ProviderFailure("citation_mismatch", "Provider cited evidence that is not attached to this challenge.", retryable=False)
    if not set(public_citations).issubset(citations):
        raise ProviderFailure("citation_mismatch", "Public-fact citations must be a subset of attached citations.", retryable=False)
    if request.evidence_ids and not citations:
        raise ProviderFailure("citation_mismatch", "A review with independently reviewable evidence must cite that evidence.", retryable=False)
    if any(evidence[item] in {"D", "E"} for item in public_citations):
        raise ProviderFailure("citation_mismatch", "Private or model-derived evidence cannot be represented as a public fact.", retryable=False)
    _reject_injection(request.claim, *request.untrusted_text, *all_text)
    return ChallengeDraft(
        claims=claims, objections=objections, missing_evidence=missing_evidence, questions=questions,
        rubric_scores=scores, unresolved_issues=unresolved, citation_ids=citations,
        public_fact_citation_ids=public_citations, native_search_used=native_search_used,
        provenance="provider_output_validated; native_search_verified" if native_search_used else "provider_output_validated; native_search_not_verified",
        total_tokens=usage[0] if usage is not None else None,
    )


class DeterministicMockLlmProvider(LlmProvider):
    name = "deterministic_mock"

    @property
    def supports_native_search(self) -> bool:
        return False

    def generate_challenge(self, request: ChallengeInput) -> ChallengeDraft:
        _reject_injection(request.claim, *request.untrusted_text)
        evidence_is_public = bool(request.evidence_ids)
        score = 3 if evidence_is_public else 2
        payload = {
            "claims": [request.claim],
            "objections": ["A competing explanation, transmission break, or already-priced expectation could invalidate this scenario."],
            "missing_evidence": [] if evidence_is_public else ["Add dated independently reviewable evidence for the asserted base rate."],
            "questions": ["What observable result, by what date, would falsify the stated causal chain?"],
            "rubric_scores": [{"criterion": criterion, "score": score, "rationale": "Deterministic mock review remains evidence-conservative."} for criterion in RUBRIC_CRITERIA],
            "unresolved_issues": [] if evidence_is_public else ["No independently reviewable public evidence is attached."],
            "citation_ids": list(request.evidence_ids),
            "public_fact_citation_ids": list(request.evidence_ids),
            "native_search_used": False,
        }
        return _draft_from_payload(payload, request, native_search_used=False)


class UnavailableLlmProvider(LlmProvider):
    """Safe terminal provider for disallowed or unknown deployment settings."""

    name = "unavailable"

    @property
    def supports_native_search(self) -> bool:
        return False

    def generate_challenge(self, request: ChallengeInput) -> ChallengeDraft:
        raise ProviderFailure(
            "provider_unavailable",
            "LLM provider is unavailable because its configuration is not permitted.",
            retryable=False,
        )


def build_llm_provider() -> LlmProvider:
    """Select an explicitly configured provider without falling back to a mock."""
    selected = os.environ.get("FORESIGHTNINE_LLM_PROVIDER", "mock").strip().lower()
    environment = os.environ.get("FORESIGHTNINE_ENV", "development").strip().lower()
    hardened = os.environ.get("FORESIGHTNINE_PUBLIC_HARDENING", "").strip().lower() in {"1", "true", "yes"} or environment == "production"
    if selected in {"", "mock", "deterministic_mock"}:
        return UnavailableLlmProvider() if hardened else DeterministicMockLlmProvider()
    if selected in {"openai_compatible", "openai_chat_completions"}:
        from .openai_compatible import OpenAICompatibleLlmProvider

        return OpenAICompatibleLlmProvider.from_environment()
    # Unknown deployment configuration must never silently become simulated data.
    return UnavailableLlmProvider()
