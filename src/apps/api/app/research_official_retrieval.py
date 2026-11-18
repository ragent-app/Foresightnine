"""Bounded, opt-in retrieval of authorized official source candidates.

The result is a stored unverified source, not independently confirmed evidence.
Provider access and storage are explicit capabilities supplied by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Any, Callable
from urllib.parse import urlsplit

from pydantic import ValidationError

from .operations import ApiProblem
from .research_coordinator import ResearchExecutionContext
from .research_fetch import FetchFailure, FetchSourceRequest, fetch_official_pdf, fetch_official_text
from .research_runs import ResearchRunRequest
from .research_sec import SecFailure, fetch_sec_filing, validate_user_agent
from .research_sources import ResearchSource, ResearchSources, SourceInput, SourcePermissions
from .research_source_catalog import canonical_source_url, official_document_capability


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


def execute_official_retrieval(*, request: ResearchRunRequest, question: str | None,
                               as_of: datetime | None, available_count: int,
                               candidates: list[dict[str, Any]], sources: list[ResearchSource],
                               owner_id: str, operation_id: str, store: ResearchSources,
                               sec_agent_config: str | None, reserve_sec: Callable[[], bool],
                               context: ResearchExecutionContext, batch_id: str | None = None) -> OfficialRetrievalResult:
    """Retrieve eligible candidates from the bounded discovery batch under existing policy."""
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
    def outcome(index: int, status: str, reason: str | None = None, source_id: str | None = None, *, external_state: str = 'not_started') -> None:
        outcomes.append({'candidate_index': index, 'status': status, 'reason': reason, 'source_id': source_id, 'external_state': external_state})
    status, notes = 'no_eligible_source', []
    retrieved: list[tuple[ResearchSource, int]] = []
    calls = 0
    had_failure = False
    permissions = SourcePermissions(store=True, display=True, model_input=True, export=True)
    for index, candidate in enumerate(candidates):
        try:
            address = canonical_source_url(candidate['url'])
            parts = urlsplit(address)
            capability = official_document_capability(address, sec_available=bool(sec_agent_config))
        except (KeyError, TypeError, ValueError):
            outcome(index, 'invalid', 'invalid_url')
            continue
        if capability is None:
            outcome(index, 'restricted', 'unsupported_source')
            continue
        try:
            source_request = FetchSourceRequest(url=address, title=candidate['title'][:500], permissions=permissions)
        except (ValidationError, KeyError, TypeError):
            outcome(index, 'invalid', 'invalid_candidate')
            continue
        sec_agent = None
        if parts.hostname == 'www.sec.gov':
            try:
                sec_agent = validate_user_agent(sec_agent_config)
            except SecFailure:
                outcome(index, 'unconfigured', 'sec_contact_unconfigured')
                status = 'sec_contact_unconfigured'
                note = 'SEC access is not configured; other supported sources may still be available.'
                if note not in notes:
                    notes.append(note)
                continue
        if context.now() >= context.budget.deadline:
            if retrieved:
                had_failure = True
                notes.append('The time budget prevented completing the remaining official source batch.')
                for pending in range(index, len(candidates)):
                    outcome(pending, 'unprocessed', 'time_budget')
                break
            for pending in range(index, len(candidates)):
                outcome(pending, 'unprocessed', 'time_budget')
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
                if retrieved:
                    had_failure = True
                    notes.append('The time budget prevented completing the remaining official source batch.')
                    for pending in range(index, len(candidates)):
                        outcome(pending, 'unprocessed', 'time_budget')
                    break
                for pending in range(index, len(candidates)):
                    outcome(pending, 'unprocessed', 'time_budget')
                return OfficialRetrievalResult('time_budget', notes=[*notes,
                    'Official source retrieval did not start because the time budget was exhausted.'], outcomes=outcomes)
            calls += 1
            started = True
            if sec_agent is not None:
                fetched = fetch_sec_filing(source_request, sec_agent, timeout_seconds=min(15.0, remaining))
                retrieval = {'retrieval_method': 'sec_filing_html', 'original_html_base64': fetched.original_base64}
                content_type = 'text/html'
            elif parts.path.casefold().endswith('.pdf'):
                fetched = fetch_official_pdf(source_request, timeout_seconds=min(15.0, remaining))
                retrieval = {'retrieval_method': 'user_pdf', 'original_pdf_base64': fetched.original_pdf_base64,
                             'pdf_text_order': 'layout'}
                content_type = fetched.content_type
            else:
                fetched = fetch_official_text(source_request, timeout_seconds=min(15.0, remaining))
                retrieval = {'retrieval_method': 'official_html', 'original_html_base64': fetched.original_html_base64}
                content_type = fetched.content_type
            context.ensure_running()
            # A URL alone is not a version: re-fetch and compare raw response
            # hashes, falling back to extracted text only where no raw hash exists.
            text_hash = sha256(fetched.text.encode('utf-8')).hexdigest()
            duplicate = next((source for source in known_sources
                if getattr(source, 'url', None) and canonical_source_url(str(source.url)) == address
                and getattr(source, 'permissions', None) == permissions
                and ((getattr(source, 'response_sha256', None) and source.response_sha256 == fetched.response_sha256)
                     or (not getattr(source, 'response_sha256', None) and source.content_sha256 == text_hash))), None)
            if duplicate is not None:
                source = duplicate
                outcome(index, 'duplicate', 'same_source_version', source.source_id, external_state='returned')
            else:
                identity_operation = operation_id if batch_id is None else operation_id + ':batch:' + batch_id
                source = store.create(owner_id, SourceInput(title=source_request.title, text=fetched.text,
                    url=source_request.url, kind='official', permissions=permissions), **retrieval,
                    response_sha256=fetched.response_sha256, response_content_type=content_type,
                    source_id=official_source_id(identity_operation, created_count))
                created_count += 1
                known_sources.append(source)
                outcome(index, 'retrieved', source_id=source.source_id, external_state='returned')
            retrieved.append((source, index))
            if available_count + created_count >= 100:
                for remaining_index in range(index + 1, len(candidates)):
                    outcome(remaining_index, 'unprocessed', 'source_capacity')
                break
        except (FetchFailure, SecFailure, ValidationError, ValueError, ApiProblem) as error:
            context.ensure_running()
            had_failure = True
            reason = getattr(error, 'code', None) or getattr(error, 'error_code', None) or 'retrieval_failed'
            external_state = ('not_started' if not started else
                              'unknown' if reason in {'source_timeout', 'source_unavailable'} else 'returned')
            outcome(index, 'failed', str(reason), external_state=external_state)
            notes.append('An official source could not be retrieved; the search result remains an unverified lead.')
            continue
    if retrieved:
        return OfficialRetrievalResult('partial' if had_failure else 'completed', calls, [*notes,
            'Official documents were retrieved with model permission; content and dates remain unverified.'],
            retrieved[0][0], retrieved[0][1], retrieved, outcomes)
    if had_failure:
        return OfficialRetrievalResult('failed', calls, notes, outcomes=outcomes)
    return OfficialRetrievalResult(status, notes=notes, outcomes=outcomes)
