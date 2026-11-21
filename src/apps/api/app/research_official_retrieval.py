"""Bounded, opt-in retrieval of authorized public HTTPS and official source candidates.

The result is a stored unverified source, not independently confirmed evidence.
Provider access and storage are explicit capabilities supplied by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from .operations import ApiProblem
from .research_coordinator import ResearchExecutionContext
from .research_fetch import (
    FetchFailure,
    FetchSourceRequest,
    PUBLIC_DOMAIN_OFFICIAL_HOSTS,
    fetch_allowed_hosts,
    fetch_official_pdf as _default_fetch_official_pdf,
    fetch_official_text as _default_fetch_official_text,
    fetch_source_document,
    is_host_blocked,
    is_sec_host,
)
from .research_runs import ResearchRunRequest
from .research_sec import SecFailure, fetch_sec_filing, validate_user_agent
from .research_source_catalog import canonical_source_url, official_document_capability
from .research_source_policy import is_fred_source
from .research_source_terms import terms_decision, user_initiated_basis
from .research_sources import ResearchSource, ResearchSources, SourceInput, SourcePermissions

# Keep module-level names patchable by existing tests while dispatching unpatched calls through fetch_source_document.
fetch_official_pdf = _default_fetch_official_pdf
fetch_official_text = _default_fetch_official_text

_FAILURE_DETAILS: dict[str, tuple[str, str]] = {
    'http_forbidden_or_challenge': (
        'Upstream site rejected the request (401/403 or anti-bot challenge); the search result remains an unverified lead.',
        'Upstream returned HTTP 401/403 or an access challenge.',
    ),
    'not_found': (
        'The source URL returned 404/410 Not Found; the search result remains an unverified lead.',
        'Upstream returned HTTP 404/410 Not Found.',
    ),
    'source_rate_limited': (
        'The upstream host rate-limited the request (429); the search result remains an unverified lead.',
        'Upstream rate limit reached (HTTP 429).',
    ),
    'source_too_large': (
        'The source exceeded the download size limit (5 MB for web pages, a larger configured cap for PDFs) or the 2,000,000-character text limit; the search result remains an unverified lead.',
        'Response or extracted text exceeded size limits.',
    ),
    'ocr_required': (
        'The PDF has no usable text layer (it appears to be scanned) and needs OCR; the search result remains an unverified lead.',
        'PDF has no extractable text and needs OCR.',
    ),
    'source_format_unsupported': (
        'The response content type is unsupported; only HTML, plain text, and PDF are supported.',
        'Unsupported response content type.',
    ),
    'source_blocked_policy': (
        'The source host or redirect target is blocked by retrieval policy.',
        'Source blocked by retrieval policy.',
    ),
    'source_blocked_terms': (
        "This site's terms do not permit automated access; it was not fetched. Use the issuer's own investor-relations copy, an SEC filing, or ask the user to upload the document.",
        "Site terms do not permit automated access.",
    ),
    'source_timeout': (
        'The source retrieval timed out; the search result remains an unverified lead.',
        'Source retrieval timed out.',
    ),
    'source_unavailable': (
        'The source could not be reached or redirected unsafely; the search result remains an unverified lead.',
        'Source unavailable or unsafe redirect target.',
    ),
    'source_invalid': (
        'The retrieved document was empty or invalid; the search result remains an unverified lead.',
        'Retrieved document was empty or invalid.',
    ),
}


@dataclass
class OfficialRetrievalResult:
    status: str
    calls: int = 0
    notes: list[str] = field(default_factory=list)
    source: ResearchSource | None = None
    candidate_index: int | None = None
    retrieved: list[tuple[ResearchSource, int]] = field(default_factory=list)
    outcomes: list[dict[str, Any]] = field(default_factory=list)


def official_source_id(operation_id: str, index: int = 0) -> str:
    """Stable source identity for recovery after an interrupted run."""
    return 'source_' + sha256((operation_id + f':official-source:{index}').encode()).hexdigest()[:32]


def _upgrade_http_to_https(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme.lower() == 'http'
        and parts.hostname
        and parts.username is None
        and parts.password is None
        and parts.port in (None, 80)
    ):
        host = parts.hostname.lower()
        netloc = f'[{host}]' if ':' in host else host
        return urlunsplit(('https', netloc, parts.path, parts.query, parts.fragment))
    return url


def _restricted_reason(address: str) -> tuple[str, str]:
    if is_fred_source(address):
        return 'source_blocked_policy', 'FRED sources are not authorized.'
    terms = terms_decision(address)
    if not terms.allowed:
        return 'source_blocked_terms', terms.message
    hostname = (urlsplit(address).hostname or '').lower().rstrip('.')
    if hostname:
        if is_host_blocked(hostname):
            return 'source_blocked_policy', 'Host is blocked by fetch policy.'
        allowed = fetch_allowed_hosts()
        if allowed is not None and hostname != 'www.sec.gov' and hostname not in allowed:
            return 'source_blocked_policy', 'Host is not in the configured fetch allowlist.'
    return 'unsupported_source', 'Source URL is not a supported public HTTPS endpoint.'


def _retrieval_fields(is_official: bool, content_type: str, fetched: Any) -> dict[str, Any]:
    if content_type == 'application/pdf':
        return {
            'retrieval_method': 'user_pdf' if is_official else 'web_pdf',
            'original_pdf_base64': fetched.original_pdf_base64,
            'pdf_text_order': 'layout',
        }
    return {
        'retrieval_method': 'official_html' if is_official else 'web_html',
        'original_html_base64': getattr(fetched, 'original_html_base64', None),
    }


def _failure_note_and_detail(reason: str, error: Exception) -> tuple[str, str]:
    default_note = 'An official source could not be retrieved; the search result remains an unverified lead.'
    note, fallback_detail = _FAILURE_DETAILS.get(reason, (default_note, reason))
    raw_detail = getattr(error, 'detail', None)
    detail = raw_detail if isinstance(raw_detail, str) and raw_detail else fallback_detail
    return note, detail


def execute_official_retrieval(*, request: ResearchRunRequest, question: str | None,
                               as_of: datetime | None, available_count: int,
                               candidates: list[dict[str, Any]], sources: list[ResearchSource],
                               owner_id: str, operation_id: str, store: ResearchSources,
                               sec_agent_config: str | None, reserve_sec: Callable[[], bool],
                               context: ResearchExecutionContext, batch_id: str | None = None,
                               ocr_usage: Any = None) -> OfficialRetrievalResult:
    """Retrieve eligible candidates from the bounded discovery batch under existing policy.

    ``ocr_usage`` (``research_ocr.OcrUsageHooks``) lets scanned-PDF ingestion use the vision OCR engine through
    the run's reserve/settle accounting; each call is recorded in ``ocr_usage.events``. Without it only local
    OCR engines run.
    """
    if not request.auto_fetch_official:
        return OfficialRetrievalResult('not_requested')
    if not question:
        return OfficialRetrievalResult('question_missing', notes=[
            'Automatic source discovery requires a user-provided question; the document overview did not start a search.'])
    if as_of is not None:
        return OfficialRetrievalResult('historical_cutoff', notes=[
            'Automatic retrieval is unavailable for historical cutoffs because revision history is not established.'])
    if request.max_cost_usd is not None:
        return OfficialRetrievalResult('cost_unknown')
    if request.selected_source_ids == []:
        return OfficialRetrievalResult('empty_selection')
    if available_count >= 100:
        return OfficialRetrievalResult('source_capacity', notes=[
            'Official source retrieval was skipped because this research already has the maximum number of sources.'])

    known_sources = list(sources)
    outcomes: list[dict[str, Any]] = []
    created_count = 0

    def outcome(
        index: int,
        outcome_status: str,
        reason: str | None = None,
        source_id: str | None = None,
        *,
        external_state: str = 'not_started',
        error_detail: str | None = None,
        final_url: str | None = None,
    ) -> None:
        row: dict[str, Any] = {
            'candidate_index': index,
            'status': outcome_status,
            'reason': reason,
            'source_id': source_id,
            'external_state': external_state,
        }
        if error_detail is not None:
            row['error_detail'] = error_detail
        if final_url is not None:
            row['final_url'] = final_url
        outcomes.append(row)

    def add_note(message: str) -> None:
        if message not in notes:
            notes.append(message)

    def record_sec_unconfigured(index: int) -> None:
        nonlocal status
        outcome(
            index,
            'unconfigured',
            'sec_contact_unconfigured',
            error_detail='SEC User-Agent contact is not configured.',
        )
        if status == 'no_eligible_source':
            status = 'sec_contact_unconfigured'
        add_note('SEC access is not configured; other supported sources may still be available.')

    def record_sec_host_restricted(index: int) -> None:
        outcome(
            index,
            'restricted',
            'source_unavailable',
            error_detail='Only direct www.sec.gov EDGAR filing HTML paths or dedicated SEC XBRL tools are supported for *.sec.gov hosts.',
        )
        add_note('SEC URLs outside direct www.sec.gov EDGAR filing HTML paths require dedicated SEC filing/XBRL handling and were not fetched.')

    status, notes = 'no_eligible_source', []
    retrieved: list[tuple[ResearchSource, int]] = []
    calls = 0
    had_failure = False
    for index, candidate in enumerate(candidates):
        try:
            raw_url = candidate['url']
            if not isinstance(raw_url, str):
                raise TypeError('Candidate URL must be a string.')
            address = canonical_source_url(_upgrade_http_to_https(raw_url))
            parts = urlsplit(address)
            capability = official_document_capability(address, sec_available=bool(sec_agent_config))
        except (KeyError, TypeError, ValueError):
            outcome(index, 'invalid', 'invalid_url', error_detail='Candidate URL is malformed.')
            continue
        requested_host = (parts.hostname or '').lower().rstrip('.')
        if capability is None:
            reason, detail = _restricted_reason(address)
            if reason != 'source_blocked_policy' and requested_host and is_sec_host(requested_host):
                try:
                    validate_user_agent(sec_agent_config)
                except SecFailure:
                    record_sec_unconfigured(index)
                    continue
                record_sec_host_restricted(index)
                continue
            outcome(index, 'restricted', reason, error_detail=detail)
            if reason in ('source_blocked_policy', 'source_blocked_terms'):
                add_note(_FAILURE_DETAILS[reason][0])
            continue
        is_official_host = requested_host in PUBLIC_DOMAIN_OFFICIAL_HOSTS
        request_permissions = SourcePermissions(store=True, display=True, model_input=True, export=is_official_host)
        try:
            source_request = FetchSourceRequest(url=address, title=candidate['title'][:500], permissions=request_permissions)
        except (ValidationError, KeyError, TypeError):
            outcome(index, 'invalid', 'invalid_candidate', error_detail='Candidate title or URL is invalid.')
            continue
        sec_agent = None
        if is_sec_host(requested_host):
            try:
                sec_agent = validate_user_agent(sec_agent_config)
            except SecFailure:
                record_sec_unconfigured(index)
                continue
            if requested_host != 'www.sec.gov':
                record_sec_host_restricted(index)
                continue
        if context.now() >= context.budget.deadline:
            for pending in range(index, len(candidates)):
                outcome(pending, 'unprocessed', 'time_budget')
            if retrieved:
                had_failure = True
                notes.append('The time budget prevented completing the remaining official source batch.')
                break
            return OfficialRetrievalResult('time_budget', notes=[*notes,
                'Official source retrieval did not start because the time budget was exhausted.'], outcomes=outcomes)
        started = False
        try:
            context.ensure_running()
            context.reserve_search()  # Global retrieval quota, separate from discovery-call budget.
            if sec_agent is not None and not reserve_sec():
                raise ApiProblem(429, 'rate_limited', 'SEC request limit reached.', retryable=True)
            remaining = context.budget.deadline - context.now()
            if remaining <= 0:
                for pending in range(index, len(candidates)):
                    outcome(pending, 'unprocessed', 'time_budget')
                if retrieved:
                    had_failure = True
                    notes.append('The time budget prevented completing the remaining official source batch.')
                    break
                return OfficialRetrievalResult('time_budget', notes=[*notes,
                    'Official source retrieval did not start because the time budget was exhausted.'], outcomes=outcomes)
            calls += 1
            started = True
            redirect_count = 0
            if sec_agent is not None:
                fetched = fetch_sec_filing(source_request, sec_agent, timeout_seconds=min(15.0, remaining))
                final_url = canonical_source_url(str(source_request.url))
                is_official = True
                content_type = 'text/html'
                retrieval = {'retrieval_method': 'sec_filing_html', 'original_html_base64': fetched.original_base64}
            elif parts.path.casefold().endswith('.pdf') and fetch_official_pdf is not _default_fetch_official_pdf:
                fetched = fetch_official_pdf(source_request, timeout_seconds=min(15.0, remaining))
                final_url = canonical_source_url(str(source_request.url))
                is_official = is_official_host
                content_type = fetched.content_type
                retrieval = _retrieval_fields(is_official, content_type, fetched)
            elif not parts.path.casefold().endswith('.pdf') and fetch_official_text is not _default_fetch_official_text:
                fetched = fetch_official_text(source_request, timeout_seconds=min(15.0, remaining))
                final_url = canonical_source_url(str(source_request.url))
                is_official = is_official_host
                content_type = fetched.content_type
                retrieval = _retrieval_fields(is_official, content_type, fetched)
            else:
                fetch_kwargs: dict[str, Any] = {}
                if ocr_usage is not None:
                    identity = operation_id if batch_id is None else operation_id + ':batch:' + batch_id
                    fetch_kwargs['ocr_usage'] = ocr_usage.for_source(
                        operation_id, official_source_id(identity, created_count))
                fetched = fetch_source_document(source_request, timeout_seconds=min(15.0, remaining), **fetch_kwargs)
                final_url = canonical_source_url(fetched.final_url)
                final_host = (urlsplit(final_url).hostname or '').lower().rstrip('.')
                is_official = is_official_host and final_host in PUBLIC_DOMAIN_OFFICIAL_HOSTS
                content_type = fetched.content_type
                redirect_count = getattr(fetched, 'redirect_count', 0)
                retrieval = _retrieval_fields(is_official, content_type, fetched)
            date_info = getattr(fetched, 'published_date', None)
            if isinstance(date_info, dict) and date_info:
                provenance_dates = {k: v for k, v in {
                    'published_at_source': date_info.get('source'), 'published_at_timezone': date_info.get('timezone'),
                    'published_at_confidence': date_info.get('confidence'), 'published_at_precision': date_info.get('precision'),
                    'modified_at': date_info.get('modified_at')}.items() if v}
            else:
                provenance_dates = {}
            effective_permissions = SourcePermissions(store=True, display=True, model_input=True, export=is_official)
            effective_kind = 'official' if is_official else 'user_supplied'
            provenance: dict[str, Any] = {}
            if not is_official:
                provenance = {
                    'final_url': final_url,
                    'web_retrieval': {
                        'requested_url': address,
                        'final_url': final_url,
                        'redirect_count': redirect_count,
                        'content_type': content_type,
                    },
                }
            elif final_url != canonical_source_url(str(source_request.url)):
                provenance = {'final_url': final_url}
            if provenance_dates:
                provenance = {**provenance, **provenance_dates}
            initiated = user_initiated_basis(address)
            if initiated is not None:
                provenance = {**provenance, **initiated}
            document_pages = getattr(fetched, 'pages', None)
            if isinstance(document_pages, dict) and document_pages:
                provenance = {**provenance, 'document_pages': document_pages}
            published_dt = None
            if isinstance(date_info, dict) and isinstance(date_info.get('published_at'), str):
                try:
                    parsed_dt = datetime.fromisoformat(date_info['published_at'])
                    # Naive values (timezone unknown) are stored as UTC with the uncertainty kept in provenance.
                    published_dt = parsed_dt if parsed_dt.tzinfo else parsed_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    published_dt = None
            context.ensure_running()
            # A URL alone is not a version: re-fetch and compare raw response
            # hashes, falling back to extracted text only where no raw hash exists.
            text_hash = sha256(fetched.text.encode('utf-8')).hexdigest()
            candidate_urls = {address, canonical_source_url(str(source_request.url)), final_url}
            duplicate = next((source for source in known_sources
                if getattr(source, 'url', None) and canonical_source_url(str(source.url)) in candidate_urls
                and getattr(source, 'permissions', None) == effective_permissions
                and ((getattr(source, 'response_sha256', None) and source.response_sha256 == fetched.response_sha256)
                     or (not getattr(source, 'response_sha256', None) and source.content_sha256 == text_hash))), None)
            if duplicate is not None:
                source = duplicate
                outcome(index, 'duplicate', 'same_source_version', source.source_id,
                        external_state='returned', final_url=final_url)
            else:
                identity_operation = operation_id if batch_id is None else operation_id + ':batch:' + batch_id
                source = store.create(
                    owner_id,
                    SourceInput(
                        title=source_request.title,
                        text=fetched.text,
                        url=final_url,
                        kind=effective_kind,
                        permissions=effective_permissions,
                        **({'published_at': published_dt} if published_dt is not None else {}),
                    ),
                    **retrieval,
                    **({'provenance': provenance} if provenance else {}),
                    response_sha256=fetched.response_sha256,
                    response_content_type=content_type,
                    source_id=official_source_id(identity_operation, created_count),
                )
                created_count += 1
                known_sources.append(source)
                outcome(index, 'retrieved', source_id=source.source_id,
                        external_state='returned', final_url=final_url)
            retrieved.append((source, index))
            if available_count + created_count >= 100:
                for remaining_index in range(index + 1, len(candidates)):
                    outcome(remaining_index, 'unprocessed', 'source_capacity')
                break
        except (FetchFailure, SecFailure, ValidationError, ValueError, ApiProblem) as error:
            context.ensure_running()
            had_failure = True
            reason = str(getattr(error, 'code', None) or getattr(error, 'error_code', None) or 'retrieval_failed')
            external_state = ('not_started' if not started else
                              'unknown' if reason in {'source_timeout', 'source_unavailable'} else 'returned')
            note, detail = _failure_note_and_detail(reason, error)
            outcome(index, 'failed', reason, external_state=external_state, error_detail=detail)
            add_note(note)
            continue
    if retrieved:
        if any(getattr(item, 'kind', None) == 'official' for item, _ in retrieved):
            add_note('Official documents were retrieved with model permission; content and dates remain unverified.')
        if any(getattr(item, 'kind', None) != 'official' for item, _ in retrieved):
            add_note('Web documents were retrieved with model permission (export disabled); content and dates remain unverified.')
        return OfficialRetrievalResult('partial' if had_failure else 'completed', calls, notes,
            retrieved[0][0], retrieved[0][1], retrieved, outcomes)
    if had_failure:
        return OfficialRetrievalResult('failed', calls, notes, outcomes=outcomes)
    return OfficialRetrievalResult(status, notes=notes, outcomes=outcomes)
