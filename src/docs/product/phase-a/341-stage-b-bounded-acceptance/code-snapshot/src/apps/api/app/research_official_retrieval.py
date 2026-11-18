"""Bounded, opt-in retrieval of authorized official source candidates.

The result is a stored unverified source, not independently confirmed evidence.
Provider access and storage are explicit capabilities supplied by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from .operations import ApiProblem
from .research_coordinator import ResearchExecutionContext
from .research_fetch import FetchFailure, FetchSourceRequest, fetch_official_pdf, fetch_official_text
from .research_runs import ResearchRunRequest
from .research_sec import SecFailure, fetch_sec_filing, validate_user_agent
from .research_sources import ResearchSource, ResearchSources, SourceInput, SourcePermissions


@dataclass
class OfficialRetrievalResult:
    status: str
    calls: int = 0
    notes: list[str] = field(default_factory=list)
    source: ResearchSource | None = None
    candidate_index: int | None = None


def official_source_id(operation_id: str) -> str:
    """Stable source identity for recovery after an interrupted run."""
    return 'source_' + sha256((operation_id + ':official-source:0').encode()).hexdigest()[:32]


def execute_official_retrieval(*, request: ResearchRunRequest, question: str | None,
                               as_of: datetime | None, available_count: int,
                               candidates: list[dict[str, Any]], sources: list[ResearchSource],
                               owner_id: str, operation_id: str, store: ResearchSources,
                               sec_agent_config: str | None, reserve_sec: Callable[[], bool],
                               context: ResearchExecutionContext) -> OfficialRetrievalResult:
    """Retrieve at most one explicitly authorized source under existing policy."""
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

    existing_urls = set()
    for source in sources:
        if source.url:
            parts = urlsplit(str(source.url))
            existing_urls.add(urlunsplit((parts.scheme, parts.netloc, parts.path, '', '')))
    status, notes = 'no_eligible_source', []
    permissions = SourcePermissions(store=True, display=True, model_input=True, export=True)
    for index, candidate in enumerate(candidates):
        if candidate['url'] in existing_urls:
            continue
        parts = urlsplit(candidate['url'])
        if (parts.scheme != 'https' or parts.hostname not in {'www.bea.gov', 'www.federalreserve.gov', 'www.sec.gov'}
                or parts.username is not None or parts.password is not None or parts.port not in (None, 443)):
            continue
        try:
            source_request = FetchSourceRequest(url=candidate['url'], title=candidate['title'][:500], permissions=permissions)
        except ValidationError:
            continue
        sec_agent = None
        if parts.hostname == 'www.sec.gov':
            try:
                sec_agent = validate_user_agent(sec_agent_config)
            except SecFailure:
                status = 'sec_contact_unconfigured'
                note = 'SEC access is not configured; other supported sources may still be available.'
                if note not in notes:
                    notes.append(note)
                continue
        if context.now() >= context.budget.deadline:
            return OfficialRetrievalResult('time_budget', notes=[*notes,
                'Official source retrieval did not start because the time budget was exhausted.'])
        calls = 0
        try:
            context.ensure_running()
            context.reserve_search()  # Global retrieval quota, separate from discovery-call budget.
            if sec_agent is not None and not reserve_sec():
                raise ApiProblem(429, 'rate_limited', 'SEC request limit reached.', retryable=True)
            remaining = context.budget.deadline - context.now()
            if remaining <= 0:
                return OfficialRetrievalResult('time_budget', notes=[*notes,
                    'Official source retrieval did not start because the time budget was exhausted.'])
            calls = 1
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
            source = store.create(owner_id, SourceInput(title=source_request.title, text=fetched.text,
                url=source_request.url, kind='official', permissions=permissions), **retrieval,
                response_sha256=fetched.response_sha256, response_content_type=content_type,
                source_id=official_source_id(operation_id))
            return OfficialRetrievalResult('completed', calls, [*notes,
                'The official document was retrieved with user-granted model permission; its content and dates remain unverified.'], source, index)
        except (FetchFailure, SecFailure, ValidationError, ValueError, ApiProblem):
            context.ensure_running()
            return OfficialRetrievalResult('failed', calls, [*notes,
                'The official source could not be retrieved; the search result remains an unverified lead.'])
    return OfficialRetrievalResult(status, notes=notes)
