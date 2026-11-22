"""Tool-batch dispatch, quotation validation, source read/search/outline helpers, and persistence boundary checks."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
import logging
import re
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from . import research_run_insights as insights
from .issuer_documents import (
    IssuerDocuments,
    IssuerDocumentsRequest,
    call_signature as issuer_call_signature,
    known_hosts_from_text,
)
from .phase3 import ProviderFailure
from .research_analysis import _quoted_numbers
from .research_calculations import (
    CalculationRequest,
    MULTI_FACT_FORMULAS,
    calculate,
    calculation_input_status,
)
from .research_charts import build_research_chart
from .research_context_manager import _record_failed_attempt, _turn_batch_usage
from .research_documents import page_for_offset, page_range_to_chars
from .research_fin_tools import FinDataToolbox
from .research_item_validator import ItemPersistenceValidationError, validate_research_item
from .research_library import ResearchLibrary
from .research_library_tool import run_search_library, sync_library
from .research_market_analytics import analyze_market_series
from .research_methods import MAX_PACK_CHARS, get_pack, list_available_packs, pack_fingerprint, render_for_prompt
from .research_news_tool import NewsOutcome, run_news_search
from .research_ocr import MAX_ONDEMAND_PAGES, OcrOutcome
from .research_source_catalog import canonical_source_url
from .research_source_policy import is_fred_source, source_eligibility
from .research_source_terms import terms_decision, user_initiated_scope
from .research_sources import ResearchSource
from .research_statement_extraction import extract_statements
from .research_tables import build_research_table
from .research_tools_registry import (
    ACQUISITION_TOOLS,
    COVERAGE_UPDATING_TOOLS,
    FIN_TOOLS,
    MAX_OUTLINE_TITLE_CHARS,
    MAX_READ_SOURCES_BATCH_CHARS,
    MAX_READ_SOURCES_TURN_CHARS,
    MAX_READ_SOURCE_SPAN_CHARS,
    MAX_SEARCH_SNIPPET_CHARS,
    MAX_TURN_TOOL_RESULT_CHARS,
    ReadPagesRequest,
    ReadRequest,
    OutlineSourceRequest,
    SEARCH_SNIPPET_TARGET_CHARS,
    SearchSourceRequest,
    SourceRead,
    TOOL_REGISTRY,
    clip_text as _clip,
    get_tool_spec,
    is_source_eligible as _is_source_eligible,
    safe_host as _safe_host,
    summarize_tool_activity,
    utc_now_iso as _now_iso,
)


# Stable gap codes. The UI localizes by code; the message is the English fallback.
GAP_CODES = {
    'no_cited_claims': 'No source-backed claims with verified locations were submitted; add or read relevant authorized material and continue research.',
    'claims_omitted_unverified_location': 'Some submitted claims were omitted because their source location could not be verified.',
    'search_response_invalid': 'Search returned results in an invalid format; the connector must be repaired before it can provide leads.',
    'search_quota_exhausted': 'The search service usage limit is reached, so no new leads could be discovered until it resets.',
    'search_rate_limited': 'The search services were rate limited, so no new leads could be discovered; a later retry may succeed.',
    'search_candidates_not_acquired': 'Search found candidate links, but their original material could not be acquired through the configured sources.',
    'historical_prices_unavailable': 'No authorized historical price series was available for the requested instrument and period.',
    'market_data_key_required': 'Market price data was unavailable because no personal Twelve Data key is saved for this account; prices cited from filings or news are not market data.',
    'provider_no_result': 'Provider request ended without a usable result.',
    'provider_timeout': 'The model request timed out before completing a response.',
    'provider_error': 'The model provider reported an error.',
    'source_selection_partial': ('Source selection is partial or unknown; absence from unseen material is not established. '
                                 'Review the selected ranges and obtain relevant additional material.'),
    'run_stopped': 'The prior run stopped and requires a new research run.',
    'external_result_unknown': 'A prior external call has an unknown result; reconcile before continuing.',
    'native_tools_unavailable': 'The configured model does not support the native research tool loop.',
    'incomplete_model_response': 'The model response could not safely be executed.',
    'settlement_failed': 'The model response was saved, but accounting needs reconciliation.',
    'cancelled': 'Research was cancelled.',
    'no_progress': 'Repeated action with unchanged evidence state.',
    'no_submission': 'The model stopped without submitting a research result.',
    'search_result_unknown': 'Search result is unknown; reconciliation is required.',
    'acquisition_result_unknown': 'Source acquisition outcome needs reconciliation.',
    'model_reported': 'Limitation reported by the model.',
    'source_discrepancy_material': 'Sources disagree on a recorded figure by more than the metric tolerance; the reference value and the other values are listed.',
    'source_discrepancy_minor': 'Sources differ slightly on a recorded figure (within a loose tolerance but not explained by rounding).',
}


def gap(code: str, message: str | None = None) -> dict:
    return {'code': code, 'message': GAP_CODES[code] if message is None else message}


def provider_gap(error: object) -> dict:
    """Map a provider failure to a stable gap while keeping the provider's own message."""
    if isinstance(error, Mapping):
        code = error.get('code')
        message = str(error.get('message') or '')
    else:
        code = getattr(error, 'code', None)
        message = str(error)
    if code == 'provider_timeout':
        return gap('provider_timeout', message or None)
    return gap(
        'provider_no_result' if message == GAP_CODES['provider_no_result'] or code == 'provider_no_result' else 'provider_error',
        message or None,
    )


class QuotationLocateError(ValueError):
    """A quotation could not be located; carries a machine-readable kind for per-item reporting."""

    def __init__(self, message: str, kind: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.kind = kind
        self.detail = detail or {}


class QuotationBatchError(ValueError):
    """Several items of one submission failed validation; nothing from the batch is committed."""

    def __init__(self, message: str, failures: list[dict[str, Any]], total_failed: int):
        super().__init__(message)
        self.failures = failures
        self.total_failed = total_failed


MAX_LISTED_QUOTATION_FAILURES = 20
_QUOTE_HINT_CHARS = 300
_FOLD_MAP = {
    **{ord(c): "'" for c in '\u2018\u2019\u201a\u201b\u2032\u02bc'},
    **{ord(c): '"' for c in '\u201c\u201d\u201e\u201f\u2033'},
    **{ord(c): '-' for c in '\u2010\u2011\u2012\u2013\u2014\u2212'},
    **{cp: chr(cp - 0xFEE0) for cp in range(0xFF01, 0xFF5F)},
    0x3000: ' ',
}


def _fold_char(ch: str) -> str:
    return _FOLD_MAP.get(ord(ch), ch)


_NORMALIZE_MAX_SOURCE_CHARS = 8_000_000
_ISO_CURRENCY_LIKE_RE = re.compile(r'^[A-Z]{3}$')
_STRUCTURED_RETRIEVAL_METHODS = frozenset({'market_data', 'sec_companyfacts', 'sec_companyconcept', 'structured_financials'})


def _normalize_with_map(text: str) -> tuple[str, list[int], list[int]]:
    """Collapse whitespace and drop trailing zeros after a decimal point, mapping back to source offsets."""
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    pos = 0
    for match in re.finditer(r'\s+|(\d+)(\.\d*?)0+(?!\d)', text):
        for i in range(pos, match.start()):
            out.append(_fold_char(text[i])); starts.append(i); ends.append(i + 1)
        if match.group(1) is None:
            out.append(' '); starts.append(match.start()); ends.append(match.end())
        else:
            kept = match.group(1) + (match.group(2) if match.group(2) != '.' else '')
            for j, ch in enumerate(kept):
                out.append(_fold_char(ch)); starts.append(match.start() + j)
                ends.append(match.end() if j == len(kept) - 1 else match.start() + j + 1)
        pos = match.end()
    for i in range(pos, len(text)):
        out.append(_fold_char(text[i])); starts.append(i); ends.append(i + 1)
    return ''.join(out), starts, ends


def _quotation_failure(
    index: int,
    label: str,
    source: ResearchSource | None,
    source_id: str,
    quotation: str,
    error: ValueError,
    ranges: list[dict[str, Any]],
) -> dict[str, Any]:
    """Describe one failing item: index, source, quotation prefix, failure kind and a bounded repair hint."""
    failure: dict[str, Any] = {
        'item': f'{label}[{index}]',
        'index': index,
        'source_id': source_id,
        'quotation_start': quotation[:80],
    }
    if not isinstance(error, QuotationLocateError):
        failure['kind'] = 'source_unavailable' if 'source is unavailable' in str(error) else 'invalid_item'
        failure['message'] = str(error)[:200]
        failure['_message'] = str(error)
        return failure
    failure['kind'] = error.kind
    failure['_message'] = str(error)
    failure.update(error.detail)
    if error.kind != 'not_found_anywhere' or source is None:
        return failure
    text = source.text
    lo, hi = 0, min(len(quotation), 1000)
    while lo < hi:  # longest prefix present in the source; bounded binary search
        mid = (lo + hi + 1) // 2
        if quotation[:mid] in text:
            lo = mid
        else:
            hi = mid - 1
    if lo >= 4:
        at = text.find(quotation[:lo])
        snippet_start = at if lo <= _QUOTE_HINT_CHARS - 80 else at + lo - 100
        failure['closest_source_text'] = text[snippet_start:snippet_start + min(len(quotation) + 20, _QUOTE_HINT_CHARS)]
        failure['closest_source_offset'] = snippet_start
        failure['diverges_at'] = lo
    if len(text) <= _NORMALIZE_MAX_SOURCE_CHARS:
        norm_quote = _normalize_with_map(quotation.strip())[0]
        if norm_quote:
            norm_text, starts, ends = _normalize_with_map(text)
            first = norm_text.find(norm_quote)
            if first >= 0 and norm_text.find(norm_quote, first + 1) < 0:
                s0, e0 = starts[first], ends[first + len(norm_quote) - 1]
                if text[s0:e0] != quotation:
                    failure['suggested_span'] = [s0, e0]
                    if e0 - s0 <= _QUOTE_HINT_CHARS:
                        failure['suggested_quotation'] = text[s0:e0]
                    failure['suggestion_in_read_ranges'] = any(r['start'] <= s0 and e0 <= r['end'] for r in ranges)
    return failure


def _quotation_batch_error(label: str, failures: list[dict[str, Any]], total_items: int) -> QuotationBatchError:
    first_message = str(failures[0].get('_message') or '')[:400] if failures else ''
    failures = [{k: v for k, v in f.items() if k != '_message'} for f in failures]
    listed = failures[:MAX_LISTED_QUOTATION_FAILURES]
    more = len(failures) - len(listed)
    message = (
        f'{len(failures)} of {total_items} {label} failed validation and nothing was committed; '
        f'every {label} not listed in `failures` passed. Fix each listed item and resubmit the full set. '
        'Where suggested_quotation is given, copy it exactly; it differs from your text only in whitespace or '
        'trailing decimal zeros.' + (f' {more} more failures not listed.' if more > 0 else '') +
        (f' First failure ({listed[0]["item"]}): {first_message}' if first_message else '')
    )
    return QuotationBatchError(message, listed, len(failures))


def invalid_reason(error: Exception) -> str:
    """A compact, field-level explanation the model can act on (no library URLs or input dumps)."""
    if isinstance(error, ValidationError):
        parts = []
        for item in error.errors(include_url=False, include_input=False)[:8]:
            path = ''.join(f'[{x}]' if isinstance(x, int) else (f'.{x}' if i else str(x)) for i, x in enumerate(item.get('loc', ())))
            message = 'required field is missing' if item.get('type') == 'missing' else item.get('msg', 'invalid value')
            parts.append(f'{path or "arguments"}: {message}')
        more = len(error.errors()) - len(parts)
        return 'Invalid arguments: ' + '; '.join(parts) + (f'; and {more} more' if more > 0 else '') + '.'
    return str(error)


def validate_persisted_tool_item(
    tool_name: str,
    item: Any,
    *,
    item_index: int | None = None,
    turn_id: str | None = None,
) -> BaseModel:
    """Validate one tool-produced output dict against the tool's declared ``persistence_model``."""
    spec = get_tool_spec(tool_name)
    if spec is None or spec.persistence_model is None or spec.persistence_item_kind is None:
        raise ValueError(f'Tool {tool_name!r} does not declare a persistence_model.')
    validated = validate_research_item(
        item,
        item_kind=spec.persistence_item_kind,
        turn_id=turn_id,
        item_index=item_index,
    )
    if not isinstance(validated, spec.persistence_model):
        raise TypeError(
            f'Validated item for tool {tool_name!r} produced {type(validated)!r}, '
            f'expected {spec.persistence_model!r}.'
        )
    return validated


def validate_persisted_tool_batch(
    tool_name: str,
    items: Sequence[Any],
    *,
    turn_id: str | None = None,
) -> list[BaseModel]:
    """Validate an entire sequence of tool-produced items atomically before mutating state."""
    return [
        validate_persisted_tool_item(tool_name, item, item_index=idx, turn_id=turn_id)
        for idx, item in enumerate(items)
    ]


def _acquisition_failure_reason(
    details: dict[str, Any],
    outcomes: list[Any],
    source: ResearchSource | None,
    as_of: datetime | None,
) -> str:
    """Choose the most specific non-empty failure reason for an acquire_sources row."""
    reason = details.get('reason')
    if isinstance(reason, str) and reason:
        return reason
    for item in outcomes:
        if isinstance(item, dict) and isinstance(item.get('reason'), str) and item['reason']:
            return item['reason']
    if source is not None:
        eligibility = source_eligibility([source], as_of)[0][1]
        if eligibility != 'eligible':
            return eligibility
    status = details.get('status')
    if isinstance(status, str) and status and status not in ('completed', 'acquired'):
        return status
    return 'source_cannot_be_acquired'


def _build_acquire_result(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble acquire_sources feedback with per-URL rows and aggregated failure details."""
    result: dict[str, Any] = {'results': rows}
    notes = list(dict.fromkeys(note for row in rows for note in (row.get('notes') or []) if isinstance(note, str) and note))
    outcomes = [item for row in rows for item in (row.get('outcomes') or [])]
    failure_reasons = list(dict.fromkeys(
        row['reason'] for row in rows
        if row.get('status') != 'acquired' and isinstance(row.get('reason'), str) and row['reason']
    ))
    if notes:
        result['notes'] = notes
    if outcomes:
        result['outcomes'] = outcomes
    if failure_reasons:
        result['failure_reasons'] = failure_reasons
    if rows and not any(row.get('status') == 'acquired' for row in rows):
        top_reason = failure_reasons[0] if len(failure_reasons) == 1 else 'source_cannot_be_acquired'
        result.update(status='unavailable', reason=top_reason)
        if top_reason == 'lead_not_discovered':
            result['hint'] = 'Only pass URLs returned by news_search.'
    return result


TOOL_FAILURE_GUIDANCE: dict[str, str] = {
    'market_data_key_required': 'No personal Twelve Data key is saved for this account. Tell the user to add one in Settings; use prices from filings or news and label them as not market data.',
    'license_platform_key_not_permitted': 'Market data from the platform key is not permitted; only the user\'s own key is used.',
    'range_after_latest_session': 'The requested range starts after the latest completed session. Retry with end_date no later than the suggested end_date, or use lookback.',
    'no_bars': 'No bars were returned for this symbol, interval and range. Check the entity_id and exchange with resolve_entity, or widen the range.',
    'symbol_not_found': 'The provider does not know this symbol on that exchange. Run resolve_entity and pass its entity_id.',
    'provider_plan_restriction': 'The user\'s market-data plan does not include this request. Report the gap or use another interval or market.',
    'invalid_api_key': 'The saved market-data key was rejected. Tell the user to check it in Settings.',
    'daily_quota_exhausted': 'The daily market-data quota is exhausted. Continue with other evidence and report the gap.',
    'provider_rate_limited': 'The data provider rate limit was reached. Do not retry immediately; continue with other evidence.',
    'rate_limited': 'The data provider rate limit was reached. Wait for other work or use another source.',
    'entity_ambiguous': 'More than one issuer matched. Call resolve_entity and pass the chosen entity_id.',
    'entity_not_found': 'No exact match. Call resolve_entity with the company name.',
    'entity_not_resolved': 'This entity has no known symbol or CIK in this run. Call resolve_entity first.',
    'no_sec_filings_for_entity': 'SEC data covers US-registered issuers only. Use news_search and acquire_sources for the official disclosures of this company.',
    'no_xbrl_facts': 'The SEC has no XBRL facts for this company. Read its filings with filings and acquire_sources.',
    'no_matching_lines': 'No requested line was reported under the standard tag mapping. Read the filing itself.',
    'cik_not_found': 'The SEC has no such CIK. Call resolve_entity.',
    'no_matching_filings': 'No filings matched. Widen the dates or forms, or check the entity.',
    'not_in_policy': 'This data category is not enabled in this deployment. Use another path.',
    'time_budget': 'The remaining run time is too short for another request of this kind.',
    'unit_or_period_not_found_in_returned_records': 'The company has data but not for this unit or period. Pick a period from available_periods and a unit from available_units when present.',
    'tag_not_reported': 'The entity does not report this tag. Try alternative common tags such as Revenues, RevenueFromContractWithCustomerExcludingAssessedTax or SalesRevenueNet, and check available_units.',
    'source_too_large': 'The source exceeds the size limit and was not stored. Use another source for the same facts or a more specific page.',
    'ocr_required': 'The document is image-only and no OCR engine produced text for it at retrieval. Use other sources or other pages.',
    'original_not_retained': 'The original PDF was not retained for this source, so its image-only pages cannot be recognised. Use other sources or pages with text.',
    'no_ocr_engine': 'No OCR engine is available in this deployment. Use other sources or pages with text.',
    'ocr_no_text': 'OCR produced no usable text for these pages. Use other sources or other pages.',
    'version_not_stored': 'The recognised text could not be stored as a new source version. Use other sources or other pages.',
    'source_timeout': 'The source timed out. The outcome is not retried automatically; use another source or report the gap.',
    'source_unavailable': 'The source could not be reached. Use another source or report the gap.',
    'issuer_website_not_found': "Wikidata has no official website for this company. Use news_search to find the investor-relations page; pass a website_url only if it was supplied by the user or a lead.",
    'issuer_website_ambiguous': 'Several companies match. Call issuer_documents again with ticker and market, or with website_url set to one candidate website from this result.',
    'issuer_site_robots_disallow': "The issuer website's robots.txt does not allow automated access. Use news_search for other copies, or ask the user to upload the document.",
    'issuer_site_unreachable': 'The issuer website could not be reached from the server network (many mainland China sites are only reachable from mainland networks). Nothing was crawled. Do not repeat the call immediately; use news_search, ask the user to upload the document, or suggest a mainland relay.',
    'issuer_site_robots_unavailable': 'The issuer website refused or failed the robots.txt request (HTTP 401/403/429/5xx), so access rules could not be confirmed and nothing was crawled. Retrying later may work; meanwhile use news_search or ask the user to upload the document.',
    'issuer_site_not_html': 'The issuer website did not return a web page. Use news_search or ask the user to upload the document.',
    'issuer_no_documents_found': 'No document links were found within the crawl budget. The site may list documents through scripts; use news_search or ask the user to upload the document.',
    'website_not_known': 'website_url must be a site already seen in this run. Resolve the issuer by name or ticker instead.',
    'issuer_documents_unavailable': 'issuer_documents is not available in this run (retrieval is disabled or the run has a cutoff or cost cap). Use news_search or user uploads.',
    'wikidata_unavailable': 'Wikidata could not be reached. Do not repeat the call immediately; use news_search to find the issuer website.',
    'wikidata_timeout': 'Wikidata did not answer in time. Do not repeat the call immediately; use news_search to find the issuer website.',
    'wikidata_rate_limited': 'Wikidata asked for fewer requests. Do not repeat the call immediately; use news_search to find the issuer website.',
    'source_user_initiated_only': 'This site is available only for single documents the user supplies themselves. Ask the user to paste the exact document link or upload the file.',
    'source_blocked_terms': "This site's terms do not permit automated access. Use the issuer's own investor-relations website copy, an SEC filing if the company files with the SEC, or ask the user to upload the document.",
    'http_forbidden_or_challenge': 'The site refused automated access. Use another source for the same facts.',
    'not_found': 'The page does not exist at this URL. Discover another source instead of guessing URLs.',
    'source_rate_limited': 'The source rate limit was reached. Wait for other work or use another source.',
    'lead_not_discovered': 'Only pass URLs returned by news_search, filings or issuer_documents, or a URL the user wrote. Run one of them first.',
    'search_quota_exhausted': 'The search usage limit is reached (see the message for the reset time). Do not repeat news_search; continue with already acquired sources and structured data, and report the gap.',
    'search_rate_limited': 'Search services are rate limited right now. Do not repeat the same search immediately; continue with other evidence and report the gap.',
    'search_provider_rate_limited': 'The search service rate limit was reached. Do not repeat the same search immediately; continue with other evidence.',
    'gdelt_rate_limited': 'GDELT is rate limited (the service is reachable). Do not repeat the same search immediately; continue with other evidence.',
    'statements_not_found': 'No statement table was recognised in this source. Use search_library for the statement titles, read_pages for the pages, or acquire the separate financial report.',
    'search_provider_error': 'The search provider reported an error (see the message). Try a different query once, otherwise continue with other evidence.',
    'search_no_results': 'The search returned no results. Retry with a different, more specific query.',
    'search_sources_denied': 'All results were denied or unusable. Retry with a different query or publisher name.',
}
_GUIDED_STATUSES = frozenset({
    'failed', 'unavailable', 'unknown', 'denied_or_unavailable', 'ocr_unavailable',
    'denied', 'rate_limited', 'empty', 'restricted',
})
_QUESTION_URL_RE = re.compile(r'https?://[A-Za-z0-9\-._~:/?#\[\]@!$&*+,;=%()]+', re.I)
MAX_QUESTION_LEADS = 10


def _user_question_leads(question: str) -> list[dict[str, Any]]:
    """HTTPS URLs the user wrote in the question become user-provided leads (initiator user), acquirable once by the agent."""
    leads: list[dict[str, Any]] = []
    for match in _QUESTION_URL_RE.finditer(question or ''):
        raw = match.group(0).rstrip('.,;:!?)]}')
        try:
            url = canonical_source_url(raw)
        except ValueError:
            continue
        parts = urlsplit(url)
        if parts.scheme != 'https' or not parts.hostname or len(url) > 500 or is_fred_source(url):
            continue
        if any(lead['url'] == url for lead in leads):
            continue
        decision = terms_decision(url, initiator='user')
        leads.append({
            'url': url,
            'title': (parts.hostname + parts.path)[:200],
            'published_at': None,
            'domain': parts.hostname,
            'source_type': 'user_supplied',
            'initiator': 'user',
            'discovered_by': 'user_question',
            **({} if decision.allowed else {'acquirable': False, 'terms_status': decision.status}),
        })
        if len(leads) >= MAX_QUESTION_LEADS:
            break
    return leads


def _duplicate_acquisition_result(ids: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        'status': 'duplicate_action',
        'reused': True,
        'source_ids': list(ids),
        'message': (
            'This exact acquisition already succeeded; the existing source is reused and nothing was fetched again. '
            'Read or search that source instead of repeating the call.'
        ),
    }
    if len(ids) == 1:
        result['source_id'] = ids[0]
    return result


def _result_source_ids(result: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    if isinstance(result.get('source_id'), str) and result.get('status') in ('acquired', 'ok', 'partial'):
        ids.append(result['source_id'])
    rows = result.get('results')
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, Mapping) and row.get('status') == 'acquired' and isinstance(row.get('source_id'), str):
                if row['source_id'] not in ids:
                    ids.append(row['source_id'])
    return ids


def _with_guidance(result: dict[str, Any]) -> dict[str, Any]:
    """Add a table message to a failure-like tool result (and its per-URL rows) lacking one."""
    if not isinstance(result, dict):
        return result
    if result.get('status') in _GUIDED_STATUSES and not result.get('message'):
        text = TOOL_FAILURE_GUIDANCE.get(str(result.get('reason') or ''))
        if text:
            result['message'] = text
    rows = result.get('results')
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                _with_guidance(row)
    return result


_UNKNOWN_READ_MESSAGE = (
    'The outcome of this read-only request is unknown (no confirmed response) and it was not retried '
    'automatically. Use another source or report the gap.'
)


def _fit_summary(summary: dict[str, Any]) -> dict[str, Any]:
    def encoded_len(obj: dict[str, Any]) -> int:
        return len(json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))

    if encoded_len(summary) < 2048:
        return summary
    for list_key in ('leads_preview', 'items', 'facts_preview', 'calcs_preview'):
        if isinstance(summary.get(list_key), list):
            items = list(summary[list_key])
            while items and encoded_len({**summary, list_key: items}) >= 2048:
                items.pop()
            summary = {**summary, list_key: items}
            if encoded_len(summary) < 2048:
                return summary
    for str_key in ('title', 'expression', 'entity', 'instrument_id'):
        if isinstance(summary.get(str_key), str) and len(summary[str_key]) > 16:
            value = summary[str_key]
            while len(value) > 16 and encoded_len({**summary, str_key: value}) >= 2048:
                value = value[: len(value) // 2]
            summary = {**summary, str_key: value}
            if encoded_len(summary) < 2048:
                return summary
    return summary


_MARKDOWN_HEADING_RE = re.compile(r'^(#{1,6})\s+(\S[^\n]*)$')
_SETEXT_EQ_RE = re.compile(r'^={3,}\s*$')
_SETEXT_DASH_RE = re.compile(r'^-{3,}\s*$')
_SEC_PART_RE = re.compile(r'^(?:PART|Part)\s+(?:I{1,3}|IV|V|VI|[1-6])(?:\s*[.:\-—–]\s*[^\n]{0,120})?$')
_SEC_ITEM_RE = re.compile(r'^(?:ITEM|Item)\s+\d{1,2}[A-Z]?(?:\s*[.:\-—–]\s*[^\n]{0,160})?$')
_FINANCIAL_STATEMENT_RE = re.compile(
    r'^(?:'
    r'(?:unaudited\s+)?(?:condensed\s+)?(?:consolidated\s+)?(?:'
    r'statements?\s+of\s+(?:'
    r'(?:income|operations|earnings|loss|comprehensive\s+(?:income|loss|income\s*\(?loss\)?)|'
    r'cash\s+flows?|financial\s+position|'
    r'(?:changes\s+in\s+)?(?:shareholders[\'\u2019]?|stockholders[\'\u2019]?|owners[\'\u2019]?|members[\'\u2019]?)\s*equity)'
    r')'
    r'|balance\s+sheets?'
    r')'
    r'|(?:合并)?(?:资产负债表|利润表|损益表|现金流量表|所有者权益变动表|股东权益变动表)'
    r')'
    r'(?:\s*(?:\([^)]{1,40}\)|[-—–:]\s+[^\n]{1,60}))?$',
    re.IGNORECASE,
)
_NOTE_RE = re.compile(r'^(?:NOTE|Note)\s+\d{1,3}[A-Za-z]?(?:\s*[.:\-—–]\s*[^\n]{1,120})?$')
_SECTION_RE = re.compile(
    r'^(?:Section|SECTION|Chapter|CHAPTER|Appendix|APPENDIX|Exhibit|EXHIBIT|Schedule|SCHEDULE)\s+'
    r'[A-Z0-9IVX]+(?:\s*[.:\-—–]\s*[^\n]{1,120})?$'
)
_CJK_SECTION_RE = re.compile(
    r'^(?:第[一二三四五六七八九十百零\d]+[章节部分条编](?:[、.:\s]+[^\n]{1,100})?'
    r'|[一二三四五六七八九十]+[、.．]\s*[^\n]{1,80})$'
)
_TOC_TITLE_RE = re.compile(r'^(?:TABLE\s+OF\s+CONTENTS|INDEX\s+TO\s+FINANCIAL\s+STATEMENTS|目录)$', re.IGNORECASE)
_NUMBERED_HEADING_RE = re.compile(r'^\d{1,2}(?:\.\d{1,2}){0,2}[.)]\s+[A-Z\u4e00-\u9fff][^\n]{2,80}$')
_SPAN_MARKER_RE = re.compile(r'^\[(?:col|row)span=\d+\]\s*')


def _unwrap_pipe_heading(stripped: str) -> tuple[str, int, bool] | None:
    """Unwrap single-cell pipe headers or 2-cell TOC rows without touching data rows."""
    if not (stripped.startswith('|') and stripped.endswith('|')):
        return None
    inner = stripped[1:-1]
    if '|' in inner:
        cells = [_SPAN_MARKER_RE.sub('', c.strip()).strip() for c in inner.split('|')]
        non_empty = [c for c in cells if c]
        if len(non_empty) == 2 and re.fullmatch(r'(?:F-)?\d{1,4}', non_empty[1]):
            pos = stripped.find(non_empty[0])
            return (non_empty[0], pos, True) if pos >= 0 else None
        if len(non_empty) != 1:
            return None
        clean = non_empty[0]
    else:
        clean = _SPAN_MARKER_RE.sub('', inner.strip()).strip()
    if not clean:
        return None
    pos = stripped.find(clean)
    return (clean, pos, False) if pos >= 0 else None


def _looks_like_uppercase_heading(text: str) -> bool:
    if not (3 <= len(text) <= 90):
        return False
    if any(ch in text for ch in ('|', '$', '%', '=')) or 'http://' in text.lower() or 'https://' in text.lower():
        return False
    if text[-1] in '.,;?!':
        return False
    ascii_upper = sum('A' <= c <= 'Z' for c in text)
    ascii_lower = sum('a' <= c <= 'z' for c in text)
    if ascii_upper < 3 or ascii_lower > 0:
        return False
    letters = sum(c.isalpha() for c in text)
    return letters >= len(text) * 0.45 and (' ' in text or letters >= 6)


def _classify_outline_candidate(text: str, *, from_pipe: bool, is_toc_row: bool) -> tuple[str, int] | None:
    """Return (kind, tier) for a structural boundary line, or None if not a heading."""
    if not text or len(text) > 180:
        return None
    if not from_pipe:
        md = _MARKDOWN_HEADING_RE.match(text)
        if md:
            level = len(md.group(1))
            return 'markdown_header', 1 if level <= 2 else 2
    if _SEC_PART_RE.match(text):
        return ('toc_entry', 4) if is_toc_row else ('sec_part', 1)
    if _SEC_ITEM_RE.match(text):
        if len(text) > 120 and text.endswith('.') and sum(c.islower() for c in text) > sum(c.isupper() for c in text) * 2:
            return None
        return ('toc_entry', 4) if is_toc_row else ('sec_item', 1)
    if _FINANCIAL_STATEMENT_RE.match(text):
        return ('toc_entry', 4) if is_toc_row else ('financial_statement', 1)
    if is_toc_row:
        return None
    if _TOC_TITLE_RE.match(text):
        return 'toc', 2
    if _NOTE_RE.match(text):
        return 'note', 2
    if _SECTION_RE.match(text) or _CJK_SECTION_RE.match(text):
        return 'section', 2
    if from_pipe:
        return None
    if _NUMBERED_HEADING_RE.match(text) and not any(ch in text for ch in ('|', '$', '%', '=', ',')) and not text.endswith('.'):
        return 'heading', 3
    if _looks_like_uppercase_heading(text):
        return 'heading', 3
    return None


def _select_bounded_outline(candidates: list[dict[str, Any]], max_entries: int) -> list[dict[str, Any]]:
    if len(candidates) <= max_entries:
        return [{'title': c['title'], 'line': c['line'], 'start': c['start'], 'kind': c['kind']} for c in candidates]
    chosen: list[dict[str, Any]] = []
    remaining = max_entries
    for tier in (1, 2, 3, 4):
        if remaining <= 0:
            break
        bucket = [c for c in candidates if c['tier'] == tier]
        if not bucket:
            continue
        if len(bucket) <= remaining:
            chosen.extend(bucket)
            remaining -= len(bucket)
        else:
            if remaining == 1:
                chosen.append(bucket[0])
            else:
                indexes = [i * (len(bucket) - 1) // (remaining - 1) for i in range(remaining)]
                chosen.extend(bucket[idx] for idx in indexes)
            remaining = 0
    chosen.sort(key=lambda item: item['start'])
    return [{'title': c['title'], 'line': c['line'], 'start': c['start'], 'kind': c['kind']} for c in chosen]


def _document_pages(source: ResearchSource | None) -> dict[str, Any] | None:
    provenance = getattr(source, 'provenance', None)
    pages = provenance.get('document_pages') if isinstance(provenance, Mapping) else None
    if isinstance(pages, dict) and isinstance(pages.get('pages'), list) and pages['pages']:
        return pages
    return None


LOW_OCR_CONFIDENCE = 70.0


def _ocr_page_meta(document_pages: Mapping[str, Any], number: int) -> dict[str, Any]:
    meta = (document_pages.get('ocr') or {}).get(str(number)) if isinstance(document_pages.get('ocr'), Mapping) else None
    return meta if isinstance(meta, Mapping) else {}


def _ocr_page_needs_check(document_pages: Mapping[str, Any], number: int) -> bool:
    meta = _ocr_page_meta(document_pages, number)
    confidence = meta.get('mean_confidence')
    low = meta.get('low_confidence_count')
    return ((isinstance(confidence, (int, float)) and confidence < LOW_OCR_CONFIDENCE)
            or (type(low) is int and low > 0))


def _extract_source_outline(source: ResearchSource, *, max_entries: int) -> dict[str, Any]:
    text = source.text
    candidates: list[dict[str, Any]] = []
    offset = 0
    prev_plain: tuple[int, int, str] | None = None
    for line_no, raw_line in enumerate(text.splitlines(keepends=True), start=1):
        line_len = len(raw_line)
        stripped_leading = len(raw_line) - len(raw_line.lstrip())
        stripped = raw_line.strip()
        line_start = offset + stripped_leading
        offset += line_len
        if not stripped:
            prev_plain = None
            continue
        if prev_plain is not None and (_SETEXT_EQ_RE.match(stripped) or _SETEXT_DASH_RE.match(stripped)):
            p_line, p_start, p_text = prev_plain
            if not candidates or candidates[-1]['line'] != p_line:
                tier = 1 if stripped.startswith('=') else 2
                candidates.append({'title': _clip(p_text, MAX_OUTLINE_TITLE_CHARS), 'line': p_line, 'start': p_start, 'kind': 'markdown_header', 'tier': tier})
            prev_plain = None
            continue
        pipe_unwrapped = _unwrap_pipe_heading(stripped)
        if pipe_unwrapped is not None:
            inner_text, inner_offset, is_toc_row = pipe_unwrapped
            classified = _classify_outline_candidate(inner_text, from_pipe=True, is_toc_row=is_toc_row)
            if classified is not None:
                kind, tier = classified
                candidates.append({'title': _clip(inner_text, MAX_OUTLINE_TITLE_CHARS), 'line': line_no, 'start': line_start + inner_offset, 'kind': kind, 'tier': tier})
            prev_plain = None
            continue
        classified = _classify_outline_candidate(stripped, from_pipe=False, is_toc_row=False)
        if classified is not None:
            kind, tier = classified
            candidates.append({'title': _clip(stripped, MAX_OUTLINE_TITLE_CHARS), 'line': line_no, 'start': line_start, 'kind': kind, 'tier': tier})
            prev_plain = None
            continue
        if len(stripped) <= 120 and '|' not in stripped and not stripped.startswith(('-', '=', '*', '#', '>')):
            prev_plain = (line_no, line_start, stripped)
        else:
            prev_plain = None

    entries = _select_bounded_outline(candidates, max_entries)
    result: dict[str, Any] = {
        'source_id': source.source_id,
        'title': source.title,
        'status': 'completed',
        'total_characters': len(text),
        'content_sha256': source.content_sha256,
        'entry_count': len(entries),
        'total_detected_entries': len(candidates),
        'truncated': len(candidates) > len(entries),
        'entries': entries,
    }
    document_pages = _document_pages(source)
    if document_pages is not None:
        pages = document_pages['pages']
        counts: dict[str, int] = {}
        needs_ocr: list[int] = []
        for number, entry in enumerate(pages, 1):
            status = entry[2] if isinstance(entry, (list, tuple)) and len(entry) >= 3 else 'failed'
            counts[status] = counts.get(status, 0) + 1
            if status == 'needs_ocr' and len(needs_ocr) < 20:
                needs_ocr.append(number)
        result['page_count'] = document_pages.get('page_count') if type(document_pages.get('page_count')) is int else len(pages)
        result['page_status_summary'] = {'counts': counts, 'needs_ocr_pages': needs_ocr}
        ocr_numbers = [
            n for n, entry in enumerate(pages, 1)
            if isinstance(entry, (list, tuple)) and len(entry) >= 3 and entry[2] == 'ocr'
        ]
        if ocr_numbers:
            confidences = [
                c for c in (_ocr_page_meta(document_pages, n).get('mean_confidence') for n in ocr_numbers)
                if isinstance(c, (int, float))
            ]
            result['page_status_summary']['ocr'] = {
                'pages': ocr_numbers[:50],
                'low_confidence_pages': [n for n in ocr_numbers if _ocr_page_needs_check(document_pages, n)][:50],
                'mean_confidence': round(sum(confidences) / len(confidences), 1) if confidences else None,
            }
        if document_pages.get('text_limit_reached'):
            result['page_status_summary']['text_limit_reached'] = True
        for entry in entries:
            number = page_for_offset(document_pages, entry['start'])
            if number is not None:
                entry['page'] = number
    if not entries:
        result['note'] = (
            'No structural headings or section boundaries were detected; '
            'use search_source with keywords or read_sources with bounded [start, end] slices.'
        )
    return result


def _find_exact_spans(text: str, query: str, *, case_sensitive: bool) -> list[tuple[int, int]]:
    if case_sensitive:
        spans: list[tuple[int, int]] = []
        pos = 0
        qlen = len(query)
        while True:
            idx = text.find(query, pos)
            if idx < 0:
                break
            spans.append((idx, idx + qlen))
            pos = idx + max(1, qlen)
        return spans
    return [(m.start(), m.end()) for m in re.finditer(re.escape(query), text, re.IGNORECASE)]


def _find_whitespace_spans(text: str, query: str, *, case_sensitive: bool) -> list[tuple[int, int]]:
    tokens = query.strip().split()
    if not tokens or (len(tokens) == 1 and query == tokens[0]):
        return []
    pattern = r'\s+'.join(re.escape(tok) for tok in tokens)
    flags = 0 if case_sensitive else re.IGNORECASE
    return [(m.start(), m.end()) for m in re.finditer(pattern, text, flags)]


def _find_token_sequence_spans(text: str, query: str, *, case_sensitive: bool) -> list[tuple[int, int]]:
    tokens = re.findall(r'[^\W_]+(?:[.,][^\W_]+)*', query)
    if not (2 <= len(tokens) <= 16):
        return []
    parts = [
        f'(?<![A-Za-z0-9]){re.escape(tok)}(?![A-Za-z0-9])' if tok.isascii() and tok.isalnum() else re.escape(tok)
        for tok in tokens
    ]
    pattern = r'[\W_]{1,24}'.join(parts)
    flags = 0 if case_sensitive else re.IGNORECASE
    return [(m.start(), m.end()) for m in re.finditer(pattern, text, flags)]


def _snippet_window(text: str, start: int, end: int) -> tuple[int, int, str]:
    match_len = end - start
    if match_len >= MAX_SEARCH_SNIPPET_CHARS:
        snip_start = start
        snip_end = min(len(text), start + MAX_SEARCH_SNIPPET_CHARS)
        return snip_start, snip_end, text[snip_start:snip_end]
    target = max(SEARCH_SNIPPET_TARGET_CHARS, match_len)
    pad = (target - match_len) // 2
    snip_start = max(0, start - pad)
    snip_end = min(len(text), end + (target - match_len - (start - snip_start)))
    if snip_end - snip_start < target:
        snip_start = max(0, snip_end - target)
    return snip_start, snip_end, text[snip_start:snip_end]


def _search_source_text(source: ResearchSource, *, query: str, max_hits: int, case_sensitive: bool) -> dict[str, Any]:
    text = source.text
    match_mode = 'exact'
    spans = _find_exact_spans(text, query, case_sensitive=case_sensitive)
    if not spans:
        ws_spans = _find_whitespace_spans(text, query, case_sensitive=case_sensitive)
        if ws_spans:
            spans = ws_spans
            match_mode = 'whitespace_normalized'
    if not spans:
        tok_spans = _find_token_sequence_spans(text, query, case_sensitive=case_sensitive)
        if tok_spans:
            spans = tok_spans
            match_mode = 'token_sequence'
    if not spans:
        match_mode = 'none'

    hits: list[dict[str, Any]] = []
    for start, end in spans[:max_hits]:
        snip_start, snip_end, snippet = _snippet_window(text, start, end)
        hits.append({
            'start': start,
            'end': end,
            'snippet': snippet,
            'snippet_start': snip_start,
            'snippet_end': snip_end,
            'match_mode': match_mode,
        })
    return {
        'source_id': source.source_id,
        'title': source.title,
        'status': 'completed',
        'query': query,
        'case_sensitive': case_sensitive,
        'match_mode': match_mode,
        'total_characters': len(text),
        'total_matches': len(spans),
        'returned_hits': len(hits),
        'truncated': len(spans) > len(hits),
        'hits': hits,
    }


def _outline_source(
    req: OutlineSourceRequest,
    *,
    source_by_id: Mapping[str, ResearchSource],
    as_of: datetime | None,
) -> dict[str, Any]:
    source = source_by_id.get(req.source_id)
    if not _is_source_eligible(source, as_of):
        return {'source_id': req.source_id, 'status': 'denied_or_unavailable'}
    assert source is not None
    return _extract_source_outline(source, max_entries=req.max_entries)


def _search_source(
    req: SearchSourceRequest,
    *,
    source_by_id: Mapping[str, ResearchSource],
    as_of: datetime | None,
) -> dict[str, Any]:
    source = source_by_id.get(req.source_id)
    if not _is_source_eligible(source, as_of):
        return {'source_id': req.source_id, 'status': 'denied_or_unavailable'}
    assert source is not None
    return _search_source_text(source, query=req.query, max_hits=req.max_hits, case_sensitive=req.case_sensitive)


def _read_sources(
    req: ReadRequest,
    *,
    source_by_id: Mapping[str, ResearchSource],
    as_of: datetime | None,
    read_ranges: list[dict[str, Any]],
    already_read_turn_chars: int = 0,
    remaining_bulk_turn_chars: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    planned: list[dict[str, Any]] = []
    checked_reads: list[dict[str, Any]] = []
    oversized_items: list[dict[str, Any]] = []
    eligible_totals: dict[str, int] = {}
    total_requested_chars = 0

    for item in req.reads:
        source = source_by_id.get(item.source_id)
        if not _is_source_eligible(source, as_of):
            planned.append({'eligible': False, 'source_id': item.source_id})
            checked_reads.append({'source_id': item.source_id, 'status': 'denied_or_unavailable'})
            continue
        assert source is not None
        total_chars = len(source.text)
        start = item.start
        end = item.end if item.end is not None else total_chars
        span_chars = end - start
        if 0 <= start < total_chars and span_chars > MAX_READ_SOURCE_SPAN_CHARS:
            detail = {
                'source_id': source.source_id,
                'start': start,
                'end': end,
                'requested_characters': span_chars,
                'total_characters': total_chars,
                'exceeds_span_limit': True,
            }
            oversized_items.append(detail)
            checked_reads.append(detail)
            eligible_totals[source.source_id] = total_chars
            total_requested_chars += span_chars
            continue
        if not 0 <= start < end <= total_chars:
            raise ValueError(
                f'Invalid source range for {source.source_id}: requested [{start}, {end}) '
                f'with total_characters={total_chars}.'
            )
        eligible_totals[source.source_id] = total_chars
        total_requested_chars += span_chars
        checked_reads.append({
            'source_id': source.source_id,
            'start': start,
            'end': end,
            'requested_characters': span_chars,
            'total_characters': total_chars,
            'exceeds_span_limit': False,
        })
        planned.append({'eligible': True, 'source': source, 'start': start, 'end': end})

    if oversized_items or total_requested_chars > MAX_READ_SOURCES_BATCH_CHARS:
        if len(eligible_totals) == 1:
            reported_total = next(iter(eligible_totals.values()))
        elif len(oversized_items) == 1:
            reported_total = oversized_items[0]['total_characters']
        else:
            reported_total = sum(eligible_totals.values())
        error_code = 'read_span_too_large' if oversized_items else 'read_batch_too_large'
        if oversized_items:
            first = oversized_items[0]
            reason = (
                f"Requested read span ({first['requested_characters']} characters) for source {first['source_id']} "
                f"(total_characters={first['total_characters']}) exceeds max_span_characters ({MAX_READ_SOURCE_SPAN_CHARS}). "
                f"Call outline_source or search_source first to locate target offsets, then read smaller [start, end] slices "
                f"(<= {MAX_READ_SOURCE_SPAN_CHARS} characters per slice, <= {MAX_READ_SOURCES_BATCH_CHARS} characters per batch)."
            )
        else:
            reason = (
                f"Total requested read batch ({total_requested_chars} characters) exceeds max_batch_characters "
                f"({MAX_READ_SOURCES_BATCH_CHARS}). Call outline_source or search_source first to narrow targets, "
                f"then read smaller [start, end] slices (<= {MAX_READ_SOURCE_SPAN_CHARS} characters per slice, "
                f"<= {MAX_READ_SOURCES_BATCH_CHARS} characters per batch)."
            )
        return {
            'status': 'invalid',
            'error': error_code,
            'reason': reason,
            'instruction': (
                f'Call outline_source or search_source first to locate relevant sections or terms, '
                f'then call read_sources with smaller [start, end] slices '
                f'(<= {MAX_READ_SOURCE_SPAN_CHARS} characters per slice, <= {MAX_READ_SOURCES_BATCH_CHARS} characters total per call).'
            ),
            'total_characters': reported_total,
            'requested_characters': total_requested_chars,
            'max_span_characters': MAX_READ_SOURCE_SPAN_CHARS,
            'max_batch_characters': MAX_READ_SOURCES_BATCH_CHARS,
            'reads': checked_reads,
        }, None

    remaining_turn_read = max(0, MAX_READ_SOURCES_TURN_CHARS - max(0, already_read_turn_chars))
    if total_requested_chars > 0 and (
        already_read_turn_chars + total_requested_chars > MAX_READ_SOURCES_TURN_CHARS
        or (remaining_bulk_turn_chars is not None and total_requested_chars > remaining_bulk_turn_chars)
    ):
        exceeds_bulk = (
            remaining_bulk_turn_chars is not None
            and total_requested_chars > remaining_bulk_turn_chars
            and already_read_turn_chars + total_requested_chars <= MAX_READ_SOURCES_TURN_CHARS
        )
        error_code = 'turn_tool_result_budget_exceeded' if exceeds_bulk else 'read_turn_budget_exceeded'
        return {
            'status': 'invalid',
            'error': error_code,
            'reason': (
                f'Cumulative read_sources characters in this turn ({already_read_turn_chars + total_requested_chars}) '
                f'exceeds per-turn budget ({MAX_READ_SOURCES_TURN_CHARS}; remaining {remaining_turn_read} characters). '
                f'Do not split large reads across parallel read_sources calls in the same turn.'
            ),
            'instruction': (
                f'Record key facts from already-read material via record_facts first, or narrow further reads '
                f'in the next turn using search_source or outline_source (remaining turn read budget: {remaining_turn_read} characters).'
            ),
            'turn_read_characters': max(0, already_read_turn_chars),
            'requested_characters': total_requested_chars,
            'remaining_turn_characters': remaining_turn_read,
            'max_turn_characters': MAX_READ_SOURCES_TURN_CHARS,
            'reads': checked_reads,
        }, None

    results = []
    read_items = []
    for entry in planned:
        if not entry['eligible']:
            results.append({'source_id': entry['source_id'], 'status': 'denied_or_unavailable'})
            continue
        source = entry['source']
        start, end = entry['start'], entry['end']
        span = {'source_id': source.source_id, 'start': start, 'end': end}
        if span not in read_ranges:
            read_ranges.append(span)
        results.append({
            **span,
            'status': 'read',
            'text': source.text[start:end],
            'content_sha256': source.content_sha256,
            'total_characters': len(source.text),
            'truncated': start > 0 or end < len(source.text),
        })
        read_items.append({
            'source_id': source.source_id,
            'title': _clip(source.title, 160),
            'start': start,
            'end': end,
            'total_characters': len(source.text),
        })
    tool_summary = {
        'kind': 'read_sources',
        'read_count': len(read_items),
        'items': read_items[:5],
    }
    return {'results': results, 'rationale': req.rationale}, tool_summary


_OCR_UNAVAILABLE_MESSAGE = (
    'These pages have no extractable text and need OCR, which is not available in this version. '
    'Read other pages or use other sources for the same facts.'
)
_OCR_PENDING_MESSAGE = (
    'Some pages are image-only and could not be recognised (OCR was unavailable, denied or incomplete for them). '
    'Read other pages or use other sources for the same facts.'
)


def _read_pages(
    req: ReadPagesRequest,
    *,
    source_by_id: Mapping[str, ResearchSource],
    as_of: datetime | None,
    read_ranges: list[dict[str, Any]],
    already_read_turn_chars: int = 0,
    remaining_bulk_turn_chars: int | None = None,
    ocr_available: bool | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    source = source_by_id.get(req.source_id)
    if not _is_source_eligible(source, as_of):
        return {'source_id': req.source_id, 'status': 'denied_or_unavailable'}, None
    assert source is not None
    document_pages = _document_pages(source)
    if document_pages is None:
        return {
            'status': 'invalid',
            'error': 'no_page_map',
            'source_id': req.source_id,
            'reason': 'This source has no page map.',
            'instruction': 'Use read_sources with [start, end] character slices (outline_source or search_source locate offsets).',
        }, None
    pages = document_pages['pages']
    page_count = len(pages)
    if req.first_page > page_count:
        return {
            'status': 'invalid',
            'error': 'page_out_of_range',
            'source_id': req.source_id,
            'page_count': page_count,
            'reason': f'first_page {req.first_page} is beyond the last page ({page_count}).',
        }, None
    last_page = min(req.last_page, page_count)
    statuses: list[dict[str, Any]] = []
    for number in range(req.first_page, last_page + 1):
        entry = pages[number - 1]
        row: dict[str, Any] = {
            'page': number,
            'status': entry[2] if isinstance(entry, (list, tuple)) and len(entry) >= 3 else 'failed',
        }
        if row['status'] == 'ocr':
            meta = _ocr_page_meta(document_pages, number)
            row['ocr_mean_confidence'] = meta.get('mean_confidence')
            if meta.get('engine'):
                row['ocr_engine'] = meta['engine']
        statuses.append(row)
    text_pages = [row['page'] for row in statuses if row['status'] in ('text', 'ocr')]

    def status_fields() -> dict[str, Any]:
        counts: dict[str, int] = {}
        for row in statuses:
            counts[row['status']] = counts.get(row['status'], 0) + 1
        fields: dict[str, Any] = {
            'source_id': source.source_id,
            'first_page': req.first_page,
            'last_page': last_page,
            'page_count': page_count,
            'page_status_counts': counts,
        }
        if len(statuses) <= 200:
            fields['page_statuses'] = statuses
        else:
            fields['page_statuses'] = [row for row in statuses if row['status'] != 'text'][:200]
            fields['page_statuses_truncated'] = True
        notes = []
        if counts.get('needs_ocr'):
            notes.append(_OCR_UNAVAILABLE_MESSAGE if ocr_available is not True else _OCR_PENDING_MESSAGE)
        check_pages = [
            row['page'] for row in statuses
            if row['status'] == 'ocr' and _ocr_page_needs_check(document_pages, row['page'])
        ]
        if check_pages:
            notes.append(f'Pages {check_pages[:20]} were recognised by OCR with low confidence; verify numbers against the original page image.')
        if counts.get('not_included') or counts.get('failed'):
            notes.append('Pages with status not_included or failed were not extracted and cannot be read from this source.')
        if notes:
            fields['message'] = ' '.join(notes)
        return fields

    if not text_pages:
        status = 'needs_ocr' if any(row['status'] == 'needs_ocr' for row in statuses) else 'unavailable'
        return {**status_fields(), 'status': status, 'results': [], 'reason': 'no_readable_text_pages'}, None

    remaining_turn = max(0, MAX_READ_SOURCES_TURN_CHARS - max(0, already_read_turn_chars))
    limit = min(MAX_READ_SOURCE_SPAN_CHARS, MAX_READ_SOURCES_BATCH_CHARS, remaining_turn)
    if remaining_bulk_turn_chars is not None:
        limit = min(limit, max(0, remaining_bulk_turn_chars))
    first_start = pages[text_pages[0] - 1][0]
    chosen_last = None
    for number in text_pages:
        if pages[number - 1][1] - first_start <= limit:
            chosen_last = number
        else:
            break
    total = len(source.text)
    partial_page: dict[str, Any] | None = None
    if chosen_last is None:
        page_end = pages[text_pages[0] - 1][1]
        if limit <= 0:
            start, end = first_start, min(page_end, total)
        else:
            start, end = first_start, min(first_start + limit, total)
            partial_page = {'page': text_pages[0], 'read_end': end, 'page_end': page_end}
        read_last = text_pages[0]
    else:
        bounds = page_range_to_chars(document_pages, text_pages[0], chosen_last)
        assert bounds is not None
        start, end = bounds[0], min(bounds[1], total)
        read_last = chosen_last
    if start >= end:
        return {**status_fields(), 'status': 'unavailable', 'results': [], 'reason': 'no_readable_text_pages'}, None
    result, summary = _read_sources(
        ReadRequest(reads=[SourceRead(source_id=source.source_id, start=start, end=end)], rationale=req.rationale),
        source_by_id=source_by_id,
        as_of=as_of,
        read_ranges=read_ranges,
        already_read_turn_chars=already_read_turn_chars,
        remaining_bulk_turn_chars=remaining_bulk_turn_chars,
    )
    if result.get('status') == 'invalid':
        return result, None
    fields = status_fields()
    unread = [n for n in text_pages if n > read_last]
    result = {**result, **fields, 'pages_read': [text_pages[0], read_last]}
    if partial_page is not None:
        result['partial_page'] = partial_page
        result['message'] = (
            f"Page {partial_page['page']} is longer than the read limit; only characters "
            f"[{start}, {end}) were returned. Continue with read_sources on [{end}, {partial_page['page_end']})."
        )
        unread = list(unread)
    if unread:
        result['next_page'] = unread[0]
        result['truncated_by_limit'] = True
    if summary is not None:
        summary = {**summary, 'pages': [text_pages[0], read_last]}
    return result, summary


def _user_available_capabilities(state: Mapping[str, Any], fin_data: FinDataToolbox | None = None) -> dict[str, Any]:
    raw = state.get('user')
    if isinstance(raw, Mapping):
        caps = raw.get('available_capabilities')
        if isinstance(caps, Mapping):
            return dict(caps)
    elif isinstance(raw, str) and raw.strip().startswith('{'):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, Mapping) and isinstance(parsed.get('available_capabilities'), Mapping):
                return dict(parsed['available_capabilities'])
        except (TypeError, ValueError):
            pass
    return fin_data.capabilities() if fin_data is not None else {}


def locate_quotation(
    source: ResearchSource,
    quotation: str,
    start: int | None,
    end: int | None,
    *,
    ranges: list[dict[str, Any]],
) -> tuple[int, int]:
    """Locate an exact quotation inside already-read ranges of ``source``."""
    matches: set[tuple[int, int]] = set()
    for span in ranges:
        offset = span['start']
        while True:
            offset = source.text.find(quotation, offset, span['end'])
            if offset < 0:
                break
            matches.add((offset, offset + len(quotation)))
            offset += 1

    def outside_hint() -> tuple[str, str, list[dict[str, Any]]]:
        elsewhere: list[tuple[int, int]] = []
        pos = 0
        while len(elsewhere) < 5:
            pos = source.text.find(quotation, pos)
            if pos < 0:
                break
            elsewhere.append((pos, pos + len(quotation)))
            pos += 1
        if not elsewhere:
            return (
                ' The quotation text does not occur verbatim anywhere in this source; check whitespace, line breaks '
                'and escaped characters (copy the text exactly as returned by the read tool).',
                'not_found_anywhere',
                [],
            )
        pages = _document_pages(source)
        where = []
        found_at = []
        for first, last in elsewhere:
            number = page_for_offset(pages, first) if pages is not None else None
            where.append(f'[{first}, {last})' + (f' (page {number})' if number is not None else ''))
            found_at.append({'start': first, 'end': last, **({'page': number} if number is not None else {})})
        return (
            ' The quotation exists in this source at ' + ', '.join(where) + ' OUTSIDE the ranges read so far; '
            'read that range with read_sources' + (' or read_pages' if pages is not None else '') +
            ' before citing it. Do not change the quotation text.',
            'not_in_read_ranges',
            found_at,
        )

    if start is not None or end is not None:
        outside_text, outside_kind, outside_at = outside_hint() if not matches else ('', 'offset_mismatch', [])
        found = (
            'The quotation occurs at ' + json.dumps(sorted(matches)) + ' within read ranges; '
            if matches else 'The quotation was not found verbatim within read ranges; '
        )
        hint = found + 'omit both offsets to let the system locate it.' + outside_text
        kind = 'offset_mismatch' if matches else outside_kind
        detail = {'offsets': [list(m) for m in sorted(matches)[:5]]} if matches else {'found_at': outside_at}
        if start is None or end is None or not any(r['start'] <= start < end <= r['end'] for r in ranges):
            raise QuotationLocateError('Both offsets must lie within a previously read range. ' + hint, kind, detail)
        if source.text[start:end] != quotation:
            raise QuotationLocateError('Quotation does not match the specified location. ' + hint, kind, detail)
        return start, end

    if len(matches) != 1:
        if matches:
            raise QuotationLocateError(
                'Quotation must match uniquely within read ranges; candidate offsets: ' + json.dumps(sorted(matches)),
                'not_unique',
                {'offsets': [list(m) for m in sorted(matches)[:5]]},
            )
        outside_text, outside_kind, outside_at = outside_hint()
        raise QuotationLocateError(
            'Quotation must match uniquely within read ranges; candidate offsets: [].' + outside_text,
            outside_kind,
            {'found_at': outside_at},
        )
    return next(iter(matches))


def build_turn_baseline(state: Mapping[str, Any], source_by_id: Mapping[str, Any]) -> dict[str, int]:
    """Snapshot per-turn artifact/source counts to detect zero-yield turns."""
    return {
        'facts': len(state.get('facts', [])),
        'calculations': len(state.get('calculations', [])),
        'charts': len(state.get('charts', [])),
        'tables': len(state.get('tables', [])),
        'market_analyses': int(state.get('market_analyses_count') or 0),
        'sources': len(source_by_id),
    }


def build_tool_batch_pending(
    state: Mapping[str, Any],
    batch_call_id: str,
    raw_calls: list[dict],
    next_cursor: int,
    *,
    source_by_id: Mapping[str, ResearchSource],
    as_of: datetime | None = None,
    started_at: str | None = None,
    baseline: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Construct the durable ``tool_batch`` pending marker for cursor resume & live activity UI."""
    pending_batch: dict[str, Any] = {
        'kind': 'tool_batch',
        'call_id': batch_call_id,
        'cursor': next_cursor,
        'calls': raw_calls,
    }
    effective_baseline = baseline
    if effective_baseline is None and isinstance(state.get('pending'), Mapping):
        existing_base = state['pending'].get('baseline')
        if isinstance(existing_base, Mapping):
            effective_baseline = existing_base
    if isinstance(effective_baseline, Mapping):
        pending_batch['baseline'] = dict(effective_baseline)
    if 0 <= next_cursor < len(raw_calls) and isinstance(raw_calls[next_cursor], Mapping):
        current_call = raw_calls[next_cursor]
        rationale, target = summarize_tool_activity(
            str(current_call.get('name') or ''),
            current_call.get('arguments'),
            source_by_id=source_by_id,
            as_of=as_of,
        )
        pending_batch['rationale'] = rationale
        pending_batch['target'] = target
        pending_batch['started_at'] = started_at or _now_iso()
    return pending_batch


@dataclass(frozen=True)
class ToolDependencies:
    """Injectable tool implementations preserving module-level monkeypatch points in tests."""
    calculate: Callable[..., dict[str, Any]] = calculate
    calculation_input_status: Callable[..., str] = calculation_input_status
    build_research_chart: Callable[..., Any] = build_research_chart
    build_research_table: Callable[..., Any] = build_research_table
    analyze_market_series: Callable[..., dict[str, Any]] = analyze_market_series
    extract_statements: Callable[..., tuple[dict[str, Any], dict[str, Any] | None]] = extract_statements
    run_search_library: Callable[..., tuple[dict[str, Any], dict[str, Any] | None]] = run_search_library
    sync_library: Callable[..., Any] = sync_library
    run_news_search: Callable[..., NewsOutcome] = run_news_search
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger('app.research_runtime'))


@dataclass
class ToolCallResult:
    """Structured outcome from executing one tool call inside a batch."""
    result: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] | None = None
    duplicate_kind: str | None = None
    transient_rate_limited: bool = False
    early_outcome: Any | None = None
    handled_continue: bool = False


@dataclass
class ToolExecutionContext:
    """Mutable per-run execution context shared across tool handlers in a batch."""
    question: str
    state: dict[str, Any]
    sources: list[ResearchSource]
    source_by_id: dict[str, ResearchSource]
    context: Any
    as_of: datetime | None
    seen: set[str]
    discover: Callable[[str], Any] | None = None
    acquire: Callable[[str, str], Any] | None = None
    fin_data: FinDataToolbox | None = None
    ocr_pages: Callable[[ResearchSource, list[int]], OcrOutcome] | None = None
    news_providers: list[Any] = field(default_factory=list)
    issuer_docs: IssuerDocuments | None = None
    library_box: list[ResearchLibrary] = field(default_factory=list)
    save: Callable[[], None] = lambda: None
    outcome_builder: Callable[..., Any] | None = None
    can_afford_presubmit_followup: Callable[[], bool] = lambda: True
    emit_reset: Callable[[int | None, str], None] = lambda *_: None
    deps: ToolDependencies = field(default_factory=ToolDependencies)
    batch_call_id: str = ''
    batch_calls: list[Any] = field(default_factory=list)
    batch_call_seq: int | None = None
    active_call_seq: int | None = None

    def tool_activity_meta(self, name: str, tool_args: Any) -> tuple[str | None, str | None]:
        return summarize_tool_activity(
            name,
            tool_args,
            source_by_id=self.source_by_id,
            as_of=self.as_of,
        )

    def tool_batch_pending(
        self,
        next_cursor: int,
        *,
        started_at: str | None = None,
        baseline: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        return build_tool_batch_pending(
            self.state,
            self.batch_call_id,
            [vars(c) for c in self.batch_calls],
            next_cursor,
            source_by_id=self.source_by_id,
            as_of=self.as_of,
            started_at=started_at,
            baseline=baseline,
        )

    def append_result(self, call_id: str, result: Mapping[str, Any]) -> None:
        self.state['history'][-1]['results'].append({
            'tool_call_id': call_id,
            'content': json.dumps(result, ensure_ascii=False, sort_keys=True, default=str),
            'is_error': result.get('status') in ('invalid', 'failed'),
        })

    def reusable_ids(self, key: str) -> list[str]:
        ids = (self.state.get('acquired_actions') or {}).get(key)
        return list(ids) if isinstance(ids, list) and ids and all(i in self.source_by_id for i in ids) else []

    def acquire_key(self, url: str) -> str:
        try:
            return 'acquire|' + canonical_source_url(url)
        except ValueError:
            return 'acquire|' + url

    def head_source_id(self, source_id: str) -> str:
        versions = self.state.get('source_versions') or {}
        for _ in range(50):
            nxt = versions.get(source_id)
            if nxt is None or nxt not in self.source_by_id:
                break
            source_id = nxt
        return source_id

    @staticmethod
    def version_note(requested: str, head: str) -> dict[str, Any]:
        return {
            'requested_source_id': requested,
            'source_id': head,
            'message': (
                'The requested source has a newer version with OCR text; this result is from the newer version. '
                'Use its source_id for offsets, reads and citations. Earlier reads and facts keep citing the older version.'
            ),
        }

    def ranges_for_source(self, source_id: str) -> list[dict[str, Any]]:
        return [
            r for r in [
                *self.state['read_ranges'],
                *self.state.get('prior_read_ranges', []),
                *self.state.get('focus_ranges', []),
            ]
            if r['source_id'] == source_id
        ]

    def locate(self, source: ResearchSource, quotation: str, start: int | None, end: int | None) -> tuple[int, int]:
        return locate_quotation(source, quotation, start, end, ranges=self.ranges_for_source(source.source_id))

    def quotation_failure(
        self,
        index: int,
        label: str,
        source: ResearchSource | None,
        source_id: str,
        quotation: str,
        error: ValueError,
    ) -> dict[str, Any]:
        return _quotation_failure(index, label, source, source_id, quotation, error, self.ranges_for_source(source_id))


@dataclass(frozen=True)
class _CallInvocation:
    call: Any
    req: Any
    args: dict[str, Any]
    index: int
    started_at: str
    signature: str
    turn_read_chars: int
    turn_bulk_result_chars: int


def _ocr_key(source_id: str, pages: list[int]) -> str:
    return 'ocr|' + source_id + '|' + ','.join(str(p) for p in sorted(set(pages)))


def _auto_ocr_pages(ctx: ToolExecutionContext, head_id: str, first_page: int, last_page: int) -> list[int]:
    source = ctx.source_by_id.get(head_id)
    document_pages = _document_pages(source) if _is_source_eligible(source, ctx.as_of) else None
    if document_pages is None:
        return []
    entries = document_pages['pages']
    attempts = ctx.state.get('ocr_attempts') or {}
    return [
        n for n in range(first_page, min(last_page, len(entries)) + 1)
        if isinstance(entries[n - 1], (list, tuple)) and len(entries[n - 1]) >= 3
        and entries[n - 1][2] == 'needs_ocr' and f'{head_id}|{n}' not in attempts
    ][:MAX_ONDEMAND_PAGES]


def _run_ocr(
    ctx: ToolExecutionContext,
    requested_id: str,
    head_id: str,
    pages: list[int],
    *,
    marker: dict[str, Any],
    restore: Callable[[], None],
) -> tuple[dict[str, Any], str | None]:
    """OCR needs_ocr pages of the current version of a source into a new stored version; never replays a success."""
    source = ctx.source_by_id.get(head_id)
    if not _is_source_eligible(source, ctx.as_of):
        return {'source_id': requested_id, 'status': 'denied_or_unavailable'}, None
    assert source is not None
    keys = list(dict.fromkeys([_ocr_key(requested_id, pages), _ocr_key(head_id, pages)]))
    for key in keys:
        ids = ctx.reusable_ids(key)
        if ids:
            return {
                'status': 'duplicate_action',
                'reused': True,
                'source_id': ids[0],
                'source_ids': ids,
                'message': (
                    'These pages were already recognised; the existing OCR source version is reused and nothing '
                    'was run again. Read it with read_pages.'
                ),
            }, 'action'
    if _document_pages(source) is None:
        return {
            'status': 'invalid',
            'error': 'no_page_map',
            'source_id': requested_id,
            'reason': 'This source has no page map, so there are no pages to recognise.',
        }, None
    if ctx.ocr_pages is None:
        return {
            'status': 'ocr_unavailable',
            'reason': 'no_ocr_engine',
            'source_id': requested_id,
            'message': 'OCR is not available in this run.',
        }, None
    ctx.state['pending'] = marker
    ctx.save()
    try:
        returned = ctx.ocr_pages(source, list(pages))
    except Exception as error:
        returned = OcrOutcome(
            'failed',
            reason='ocr_error',
            message=f'OCR could not be completed ({type(error).__name__}); it was not retried.',
        )
    restore()
    for ev in returned.events or []:
        if isinstance(ev, Mapping):
            ctx.state['events'].append(dict(ev))
    attempts = ctx.state.setdefault('ocr_attempts', {})
    for number in pages:
        attempts[f'{head_id}|{number}'] = returned.status
    new = returned.source
    if returned.status in ('completed', 'partial') and isinstance(new, ResearchSource) and _is_source_eligible(new, ctx.as_of):
        ctx.source_by_id[new.source_id] = new
        if all(item.source_id != new.source_id for item in ctx.sources):
            ctx.sources.append(new)
        ctx.state.setdefault('source_versions', {})[head_id] = new.source_id
        acquired_list = ctx.state.setdefault('acquired_source_ids', [])
        if new.source_id not in acquired_list:
            acquired_list.append(new.source_id)
        for key in keys:
            ctx.state.setdefault('acquired_actions', {})[key] = [new.source_id]
        recognised = [row['page'] for row in returned.pages if row.get('status') == 'ocr']
        return {
            'status': returned.status,
            'source_id': new.source_id,
            'previous_source_id': head_id,
            'title': new.title,
            'total_characters': len(new.text),
            'content_sha256': new.content_sha256,
            'pages': returned.pages,
            'message': (
                f'Pages {recognised} now have OCR text in the new source version {new.source_id}. Use that source_id for '
                'read_pages, outline_source and search_source; earlier reads and facts keep citing the previous version.'
            ),
        }, None
    if returned.status in ('completed', 'partial'):
        return {'status': 'failed', 'reason': 'version_not_stored', 'source_id': requested_id, 'pages': returned.pages}, None
    status = {'ocr_unavailable': 'ocr_unavailable', 'nothing_to_do': 'unchanged'}.get(returned.status, 'failed')
    result: dict[str, Any] = {'status': status, 'reason': returned.reason, 'source_id': requested_id, 'pages': returned.pages}
    if returned.message:
        result['message'] = returned.message
    return result, None


def _record_discovered_leads(ctx: ToolExecutionContext, call_id: str, leads: Sequence[Mapping[str, Any]], *, skip_fred: bool) -> None:
    existing_leads = {
        lead['url'] for lead in ctx.state.get('leads', [])
        if isinstance(lead, dict) and isinstance(lead.get('url'), str)
    }
    for lead in leads:
        url = lead['url']
        if url not in existing_leads and (not skip_fred or not is_fred_source(url)):
            ctx.state.setdefault('leads', []).append(lead)
            existing_leads.add(url)
        references = ctx.state.setdefault('lead_discovery_calls', {}).setdefault(url, [])
        if call_id not in references:
            references.append(call_id)


def _exec_read_sources(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    result, summary = _read_sources(
        inv.req,
        source_by_id=ctx.source_by_id,
        as_of=ctx.as_of,
        read_ranges=ctx.state['read_ranges'],
        already_read_turn_chars=inv.turn_read_chars,
        remaining_bulk_turn_chars=max(0, MAX_TURN_TOOL_RESULT_CHARS - inv.turn_bulk_result_chars),
    )
    return ToolCallResult(result=result, summary=summary)


def _exec_request_ocr(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    head_id = ctx.head_source_id(inv.req.source_id)
    t_rationale, t_target = ctx.tool_activity_meta(inv.call.name, inv.args)
    result, duplicate_kind = _run_ocr(
        ctx,
        inv.req.source_id,
        head_id,
        list(dict.fromkeys(inv.req.pages)),
        marker={'kind': 'ocr', 'call_id': inv.call.id, 'rationale': t_rationale, 'target': t_target, 'started_at': inv.started_at},
        restore=lambda: ctx.state.__setitem__('pending', ctx.tool_batch_pending(inv.index, started_at=inv.started_at)),
    )
    if head_id != inv.req.source_id and result.get('status') != 'denied_or_unavailable':
        result['source_version'] = ctx.version_note(inv.req.source_id, head_id)
    return ToolCallResult(result=result, duplicate_kind=duplicate_kind)


def _exec_read_pages(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    head_id = ctx.head_source_id(inv.req.source_id)
    ocr_note: dict[str, Any] | None = None
    if ctx.ocr_pages is not None:
        wanted_pages = _auto_ocr_pages(ctx, head_id, inv.req.first_page, inv.req.last_page)
        if wanted_pages:
            t_rationale, t_target = ctx.tool_activity_meta(inv.call.name, inv.args)
            ocr_note, _ = _run_ocr(
                ctx,
                inv.req.source_id,
                head_id,
                wanted_pages,
                marker={'kind': 'ocr', 'call_id': inv.call.id, 'rationale': t_rationale, 'target': t_target, 'started_at': inv.started_at},
                restore=lambda: ctx.state.__setitem__('pending', ctx.tool_batch_pending(inv.index, started_at=inv.started_at)),
            )
            if ocr_note.get('status') in ('completed', 'partial'):
                head_id = ocr_note['source_id']
    read_req = inv.req if head_id == inv.req.source_id else inv.req.model_copy(update={'source_id': head_id})
    result, summary = _read_pages(
        read_req,
        source_by_id=ctx.source_by_id,
        as_of=ctx.as_of,
        read_ranges=ctx.state['read_ranges'],
        already_read_turn_chars=inv.turn_read_chars,
        remaining_bulk_turn_chars=max(0, MAX_TURN_TOOL_RESULT_CHARS - inv.turn_bulk_result_chars),
        ocr_available=ctx.ocr_pages is not None,
    )
    if head_id != inv.req.source_id and result.get('status') != 'denied_or_unavailable':
        result['source_version'] = ctx.version_note(inv.req.source_id, head_id)
    if ocr_note is not None:
        result['ocr'] = {
            key: ocr_note[key]
            for key in ('status', 'reason', 'message', 'source_id', 'previous_source_id', 'pages')
            if key in ocr_note
        }
    return ToolCallResult(result=result, summary=summary)


def _exec_extract_statements(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    head_id = ctx.head_source_id(inv.req.source_id)
    ext_source = ctx.source_by_id.get(head_id)
    if not _is_source_eligible(ext_source, ctx.as_of):
        return ToolCallResult(result={'source_id': inv.req.source_id, 'status': 'denied_or_unavailable'})
    bulk_left = max(0, MAX_TURN_TOOL_RESULT_CHARS - inv.turn_bulk_result_chars)
    result, summary = ctx.deps.extract_statements(
        inv.req.model_copy(update={'source_id': head_id}),
        ext_source,
        document_pages=_document_pages(ext_source),
        read_ranges=ctx.state['read_ranges'],
        read_budget_chars=max(0, min(MAX_READ_SOURCES_TURN_CHARS - inv.turn_read_chars, bulk_left - 8_000)),
        result_budget_chars=min(30_000, max(2_000, bulk_left - 2_000)),
        needs_check=_ocr_page_needs_check,
        ocr_available=ctx.ocr_pages is not None,
    )
    if head_id != inv.req.source_id and result.get('status') != 'denied_or_unavailable':
        result['source_version'] = ctx.version_note(inv.req.source_id, head_id)
    return ToolCallResult(result=result, summary=summary)


def _exec_outline_source(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    head_id = ctx.head_source_id(inv.req.source_id)
    result = _outline_source(
        inv.req if head_id == inv.req.source_id else inv.req.model_copy(update={'source_id': head_id}),
        source_by_id=ctx.source_by_id,
        as_of=ctx.as_of,
    )
    if head_id != inv.req.source_id and result.get('status') == 'completed':
        result['source_version'] = ctx.version_note(inv.req.source_id, head_id)
    return ToolCallResult(result=result)


def _exec_search_source(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    head_id = ctx.head_source_id(inv.req.source_id)
    result = _search_source(
        inv.req if head_id == inv.req.source_id else inv.req.model_copy(update={'source_id': head_id}),
        source_by_id=ctx.source_by_id,
        as_of=ctx.as_of,
    )
    if head_id != inv.req.source_id and isinstance(result, dict) and result.get('status') != 'denied_or_unavailable':
        result['source_version'] = ctx.version_note(inv.req.source_id, head_id)
    return ToolCallResult(result=result)


def _exec_search_library(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    try:
        if not ctx.library_box:
            ctx.library_box.append(ResearchLibrary())
        ctx.deps.sync_library(ctx.library_box[0], ctx.source_by_id, ctx.state.get('source_versions') or {}, ctx.as_of)
        result, summary = ctx.deps.run_search_library(
            inv.req,
            library=ctx.library_box[0],
            source_by_id=ctx.source_by_id,
            versions=ctx.state.get('source_versions') or {},
            as_of=ctx.as_of,
            needs_check=lambda src, page: (lambda dp: dp is not None and _ocr_page_needs_check(dp, page))(_document_pages(src)),
        )
    except Exception:
        result, summary = {
            'status': 'failed',
            'reason': 'library_unavailable',
            'message': 'The source library could not be searched; use search_source instead.',
        }, None
    return ToolCallResult(result=result, summary=summary)


def _exec_news_search(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    if not ctx.news_providers or ctx.state.get('search_invalid_response'):
        reason = 'search_response_invalid' if ctx.state.get('search_invalid_response') else 'search_not_authorized_or_unavailable'
        return ToolCallResult(result={'status': 'unavailable', 'reason': reason})
    try:
        ctx.context.start_search()
    except ProviderFailure as error:
        result = {'status': 'failed', 'reason': error.code}
        ctx.append_result(inv.call.id, result)
        ctx.state['events'].append({
            'kind': 'tool',
            'tool_call_id': inv.call.id,
            'name': inv.call.name,
            'status': 'failed',
            'reason': error.code,
            'rationale': inv.req.rationale,
        })
        ctx.state['pending'] = ctx.tool_batch_pending(inv.index + 1)
        ctx.save()
        return ToolCallResult(handled_continue=True)
    t_rationale, t_target = ctx.tool_activity_meta(inv.call.name, inv.args)
    ctx.state['pending'] = {
        'kind': 'search',
        'call_id': inv.call.id,
        'rationale': t_rationale,
        'target': t_target,
        'started_at': inv.started_at,
    }
    ctx.save()
    news = ctx.deps.run_news_search(
        inv.req,
        providers=ctx.news_providers,
        as_of=ctx.as_of,
        blocked_message=TOOL_FAILURE_GUIDANCE['source_blocked_terms'],
        redirect_message=(
            'Some results were opaque redirect links and cannot be acquired. '
            'Search again with a more specific query, e.g. quote one of the unresolved_titles '
            'or add the publisher or site name.'
        ),
    )
    if news.unknown_failure is not None:
        assert ctx.outcome_builder is not None
        return ToolCallResult(
            early_outcome=ctx.outcome_builder('partial', news.unknown_failure.code, gaps=[gap('search_result_unknown')])
        )
    if news.invalid_response:
        ctx.state['search_invalid_response'] = True
    result, summary = news.result, news.summary
    if news.denied_count is not None and news.result.get('status') == 'failed':
        summary = None
    _record_discovered_leads(ctx, inv.call.id, news.lead_rows, skip_fred=False)
    known_attr = ctx.state.setdefault('attributions', [])
    for attribution in news.attributions:
        if attribution not in known_attr:
            known_attr.append(attribution)
    ctx.state['pending'] = ctx.tool_batch_pending(inv.index, started_at=inv.started_at)
    return ToolCallResult(result=result, summary=summary)


def _exec_acquire_sources(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    acquire_keys = [ctx.acquire_key(u) for u in inv.req.urls]
    if acquire_keys and all(ctx.reusable_ids(k) for k in acquire_keys):
        reused = _duplicate_acquisition_result(list(dict.fromkeys(sid for k in acquire_keys for sid in ctx.reusable_ids(k))))
        return ToolCallResult(result=reused, duplicate_kind='action')
    rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    t_rationale, t_target = ctx.tool_activity_meta(inv.call.name, inv.args)
    for url in inv.req.urls:
        host = _safe_host(url)
        if ctx.acquire is None:
            rows.append({'url': url, 'status': 'unavailable', 'reason': 'acquisition_not_authorized_or_disabled'})
            summary_rows.append({
                'title': host or '未命名来源',
                'host': host,
                'status': 'unavailable',
                'reason': 'acquisition_not_authorized_or_disabled',
                'total_characters': None,
            })
            continue
        try:
            requested_url = canonical_source_url(url)
        except ValueError:
            requested_url = url
        lead = next((x for x in ctx.state.get('leads', []) if isinstance(x, dict) and x.get('url') == requested_url), None)
        if lead is None:
            rows.append({
                'url': url,
                'status': 'denied_or_unavailable',
                'reason': 'lead_not_discovered',
                'hint': 'Only pass URLs returned by news_search, filings or issuer_documents, or a URL the user wrote.',
            })
            summary_rows.append({
                'title': host or '未命名来源',
                'host': host,
                'status': 'denied_or_unavailable',
                'reason': 'lead_not_discovered',
                'total_characters': None,
            })
            continue
        user_lead = lead.get('initiator') == 'user'
        terms = terms_decision(requested_url, initiator='user' if user_lead else None)
        if not terms.allowed or (lead.get('acquirable') is False and not user_lead):
            blocked_reason = 'source_user_initiated_only' if terms.status == 'user_initiated_only' else 'source_blocked_terms'
            rows.append({'url': url, 'status': 'denied_or_unavailable', 'reason': blocked_reason, 'external_state': 'not_started'})
            summary_rows.append({
                'title': host or '未命名来源',
                'host': host,
                'status': 'denied_or_unavailable',
                'reason': blocked_reason,
                'total_characters': None,
            })
            continue
        acquired_ids = ctx.state.setdefault('acquired_lead_sources', {})
        prior_source = ctx.source_by_id.get(acquired_ids.get(requested_url))
        if prior_source is not None:
            rows.append({
                'url': url,
                'source_id': prior_source.source_id,
                'title': prior_source.title,
                'total_characters': len(prior_source.text),
                'content_sha256': prior_source.content_sha256,
                'status': 'acquired',
                'reused': True,
            })
            summary_rows.append({
                'title': _clip(prior_source.title, 160),
                'host': host,
                'status': 'acquired',
                'reason': None,
                'total_characters': len(prior_source.text),
            })
            continue
        ctx.state['pending'] = {
            'kind': 'acquire',
            'call_id': inv.call.id,
            'rationale': t_rationale,
            'target': t_target,
            'started_at': inv.started_at,
        }
        ctx.save()
        if user_lead:
            # The user wrote this URL: one single-document retrieval, no crawling (terms personal mode).
            with user_initiated_scope(requested_url):
                acquired = ctx.acquire(requested_url, str(lead.get('title') or ''))
        else:
            acquired = ctx.acquire(requested_url, str(lead.get('title') or ''))
        if isinstance(acquired, tuple) and len(acquired) == 2:
            source, details = acquired
        else:
            source, details = acquired, {}
        details = details if isinstance(details, dict) else {}
        for ocr_event in details.get('ocr_events') or []:
            if isinstance(ocr_event, Mapping):
                ctx.state['events'].append(dict(ocr_event))
        outcomes = details.get('outcomes') if isinstance(details.get('outcomes'), list) else []
        notes = details.get('notes') if isinstance(details.get('notes'), list) else []
        unknown_rows = [row for row in outcomes if isinstance(row, dict) and row.get('external_state') == 'unknown']
        ctx.state['pending'] = ctx.tool_batch_pending(inv.index, started_at=inv.started_at)
        if source is None or source_eligibility([source], ctx.as_of)[0][1] != 'eligible':
            row_reason = _acquisition_failure_reason(details, outcomes, source, ctx.as_of)
            row_status = details.get('status', 'denied_or_unavailable')
            failure_row: dict[str, Any] = {
                'url': url,
                'status': row_status,
                'reason': row_reason,
                'notes': notes,
                'outcomes': outcomes,
            }
            if unknown_rows:
                # A GET is side-effect free: record the failure, do not retry, keep going.
                row_status = 'failed'
                row_reason = next(
                    (r['reason'] for r in unknown_rows if isinstance(r.get('reason'), str) and r['reason']),
                    'source_unavailable',
                )
                failure_row.update(status=row_status, reason=row_reason, external_state='unknown', message=_UNKNOWN_READ_MESSAGE)
            rows.append(failure_row)
            failed_accesses = ctx.state.setdefault('failed_source_accesses', [])
            failed_entry = {
                'url': _clip(requested_url, 300),
                'host': _clip(host, 120) if host else None,
                'status': str(row_status)[:100] if isinstance(row_status, str) and row_status else 'denied_or_unavailable',
                'reason': str(row_reason)[:100] if isinstance(row_reason, str) and row_reason else None,
            }
            if failed_entry not in failed_accesses:
                failed_accesses.append(failed_entry)
                if len(failed_accesses) > 50:
                    del failed_accesses[:-50]
            lead_title = (
                lead.get('title')
                if isinstance(lead.get('title'), str) and lead.get('title').strip()
                and not is_fred_source(str(lead.get('url') or ''))
                else None
            )
            summary_rows.append({
                'title': _clip(lead_title, 160) if lead_title else (host or '未命名来源'),
                'host': host,
                'status': str(row_status)[:100] if isinstance(row_status, str) and row_status else 'denied_or_unavailable',
                'reason': str(row_reason)[:100] if isinstance(row_reason, str) and row_reason else None,
                'total_characters': None,
            })
            continue
        ctx.source_by_id[source.source_id] = source
        ctx.sources.append(source)
        acquired_ids[requested_url] = source.source_id
        if isinstance(lead.get('cluster_id'), str) and lead['cluster_id']:
            ctx.state.setdefault('source_clusters', {})[source.source_id] = lead['cluster_id']
        ctx.state.setdefault('acquired_actions', {})['acquire|' + requested_url] = [source.source_id]
        acquired_source_list = ctx.state.setdefault('acquired_source_ids', [])
        if source.source_id not in acquired_source_list:
            acquired_source_list.append(source.source_id)
        rows.append({
            'url': url,
            'source_id': source.source_id,
            'title': source.title,
            'total_characters': len(source.text),
            'content_sha256': source.content_sha256,
            'status': 'acquired',
            'notes': notes,
            'outcomes': outcomes,
        })
        summary_rows.append({
            'title': _clip(source.title, 160),
            'host': host,
            'status': 'acquired',
            'reason': None,
            'total_characters': len(source.text),
        })
    result = _build_acquire_result(rows)
    acquired_count = sum(1 for item in summary_rows if item.get('status') == 'acquired')
    summary = {
        'kind': 'acquire_sources',
        'acquired_count': acquired_count,
        'failed_count': len(summary_rows) - acquired_count,
        'items': summary_rows[:5],
    }
    transient_rate_limited = (
        any(r.get('status') != 'acquired' and r.get('reason') == 'source_rate_limited' for r in rows)
        and not any(r.get('status') == 'acquired' and not r.get('reused') for r in rows)
    )
    return ToolCallResult(result=result, summary=summary, transient_rate_limited=transient_rate_limited)


def _exec_issuer_documents(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    """Issuer website discovery and bounded IR crawl. Read-only GETs; an unknown outcome is recorded, never replayed."""
    if ctx.issuer_docs is None:
        return ToolCallResult(result={'status': 'unavailable', 'reason': 'issuer_documents_unavailable'})
    req: IssuerDocumentsRequest = inv.req
    key = issuer_call_signature(req)
    done = ctx.state.setdefault('issuer_documents_calls', {})
    if key in done:
        return ToolCallResult(
            result={
                'status': 'duplicate_action',
                'reused': True,
                'document_count': done[key],
                'message': (
                    'This exact issuer_documents call already ran; its document leads are already in this run. '
                    'Pass those URLs to acquire_sources instead of repeating the crawl.'
                ),
            },
            duplicate_kind='action',
        )
    t_rationale, t_target = ctx.tool_activity_meta('issuer_documents', inv.args)
    ctx.state['pending'] = {
        'kind': 'issuer_documents',
        'call_id': inv.call.id,
        'rationale': t_rationale,
        'target': t_target,
        'started_at': inv.started_at,
    }
    ctx.save()
    known = list(known_hosts_from_text(ctx.question))
    known += [
        lead.get('domain') or (urlsplit(lead['url']).hostname or '')
        for lead in ctx.state.get('leads', [])
        if isinstance(lead, dict) and isinstance(lead.get('url'), str)
    ]
    known += [h for h in ctx.state.get('issuer_site_hosts', []) if isinstance(h, str)]
    produced = ctx.issuer_docs.run(req, ctx.state.setdefault('entity_candidates', {}), known)
    ctx.state['pending'] = ctx.tool_batch_pending(inv.index, started_at=inv.started_at)
    result = produced.result
    hosts = ctx.state.setdefault('issuer_site_hosts', [])
    for host in produced.site_hosts:
        if host and host not in hosts:
            hosts.append(host)
    del hosts[:-50]
    _record_discovered_leads(ctx, inv.call.id, produced.leads, skip_fred=True)
    if result.get('status') in ('ok', 'partial', 'empty', 'denied') or result.get('external_state') == 'unknown':
        done[key] = len(result.get('documents') or [])
    summary = {
        'kind': 'data_tool',
        'tool': 'issuer_documents',
        'target': t_target,
        'status': _clip(result.get('status', 'completed'), 100),
        'title': _clip(produced.summary_title, 160) if produced.summary_title else None,
        'total_characters': None,
    }
    return ToolCallResult(result=result, summary=summary, transient_rate_limited=result.get('status') == 'rate_limited')


def _exec_fin_tool(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    """Run one financial data tool. Read-only, quota-limited calls: an unknown outcome is recorded, never replayed."""
    if ctx.fin_data is None:
        return ToolCallResult(result={'status': 'unavailable', 'reason': 'fin_data_unavailable'})
    name = inv.call.name
    entities = ctx.state.setdefault('entity_candidates', {})
    key: str | None = None
    spec: Any = None
    if name == 'market_data':
        spec = ctx.fin_data.market_spec(inv.req, entities)
        if isinstance(spec, dict):
            return ToolCallResult(result=spec)
        key = spec.key
    elif name == 'company_financials':
        key = ctx.fin_data.financials_key(inv.req)
    if key is not None:
        ids = ctx.reusable_ids(key)
        if ids:
            return ToolCallResult(result=_duplicate_acquisition_result(ids), duplicate_kind='action')
    t_rationale, t_target = ctx.tool_activity_meta(name, inv.args)
    ctx.state['pending'] = {
        'kind': name,
        'call_id': inv.call.id,
        'rationale': t_rationale,
        'target': t_target,
        'started_at': inv.started_at,
    }
    ctx.save()
    if name == 'resolve_entity':
        produced = ctx.fin_data.resolve_entity(inv.req, entities)
    elif name == 'market_data':
        produced = ctx.fin_data.market_data(spec)
    elif name == 'company_financials':
        produced = ctx.fin_data.company_financials(inv.req, entities)
    else:
        produced = ctx.fin_data.filings(inv.req, entities)
    ctx.state['pending'] = ctx.tool_batch_pending(inv.index, started_at=inv.started_at)
    result = produced.result
    if ctx.fin_data.on_result is not None:
        ctx.fin_data.on_result(name, result)
    for entity_id, info in produced.entities.items():
        entities[entity_id] = info
    while len(entities) > 200:
        del entities[next(iter(entities))]
    _record_discovered_leads(ctx, inv.call.id, produced.leads, skip_fred=True)
    source = produced.source
    if source is not None:
        if source_eligibility([source], ctx.as_of)[0][1] != 'eligible':
            result = {'status': 'unavailable', 'reason': 'source_not_authorized_or_unavailable'}
        else:
            if source.source_id not in ctx.source_by_id:
                ctx.sources.append(source)
            ctx.source_by_id[source.source_id] = source
            acquired_list = ctx.state.setdefault('acquired_source_ids', [])
            if source.source_id not in acquired_list:
                acquired_list.append(source.source_id)
            actions = ctx.state.setdefault('acquired_actions', {})
            for action_key in [k for k in [key, *produced.extra_keys] if k]:
                actions[action_key] = [source.source_id]
    summary = {
        'kind': 'data_tool',
        'tool': name,
        'target': t_target,
        'status': _clip(result.get('status', 'completed'), 100),
        'title': _clip(result['title'], 160) if isinstance(result.get('title'), str) else None,
        'total_characters': result['total_characters'] if type(result.get('total_characters')) is int else None,
    }
    return ToolCallResult(result=result, summary=summary, transient_rate_limited=result.get('status') == 'rate_limited')


def _exec_record_facts(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    rows: list[dict[str, Any]] = []
    staged: list[dict[str, Any]] = []
    staged_entries: list[tuple[int, dict[str, Any]]] = []
    ocr_warnings: list[dict[str, Any]] = []
    existing = [*ctx.state.get('prior_facts', []), *ctx.state.get('facts', [])]
    fact_failures: list[dict[str, Any]] = []
    for fact_index, fact in enumerate(inv.req.facts):
        source = ctx.source_by_id.get(fact.source_id)
        try:
            if source is None or source_eligibility([source], ctx.as_of)[0][1] != 'eligible':
                raise ValueError('Fact source is unavailable or unauthorized.')
            start, end = ctx.locate(source, fact.quotation, fact.character_start, fact.character_end)
            if fact.value not in _quoted_numbers(fact.quotation):
                raise ValueError('Fact value does not match source quotation.')
        except ValueError as item_error:
            fact_failures.append(ctx.quotation_failure(fact_index, 'facts', source, fact.source_id, fact.quotation, item_error))
            continue
        candidate = {**fact.model_dump(mode='json'), 'character_start': start, 'character_end': end}
        if (
            not candidate.get('currency')
            and _ISO_CURRENCY_LIKE_RE.match(fact.unit or '')
            and (source.kind == 'market_data' or source.retrieval_method in _STRUCTURED_RETRIEVAL_METHODS)
        ):
            # Structured price/SEC records carry the currency in the unit; keep the fact identity consistent.
            candidate['currency'] = fact.unit
        fact_pages = _document_pages(source)
        fact_page = page_for_offset(fact_pages, start) if fact_pages is not None else None
        if fact_page is not None and fact_pages['pages'][fact_page - 1][2] == 'ocr':
            meta = _ocr_page_meta(fact_pages, fact_page)
            confidence = meta.get('mean_confidence')
            candidate['extraction_method'] = 'ocr'
            if meta.get('engine'):
                candidate['ocr_engine'] = meta['engine']
            if isinstance(confidence, (int, float)):
                candidate['ocr_mean_confidence'] = float(confidence)
            if _ocr_page_needs_check(fact_pages, fact_page):
                ocr_warnings.append({
                    'label': fact.label,
                    'source_id': fact.source_id,
                    'page': fact_page,
                    'ocr_mean_confidence': candidate.get('ocr_mean_confidence'),
                    'message': (
                        f'Page {fact_page} was recognised by OCR with low confidence; verify this '
                        'value against the original page image.'
                    ),
                })
        identity_fields = ('source_id', 'character_start', 'character_end', 'unit', 'currency', 'period', 'label')
        row = next(
            (
                item for item in existing + staged
                if all(item.get(k) == candidate[k] for k in identity_fields)
                and Decimal(str(item.get('value'))) == fact.value
            ),
            None,
        )
        if row is None:
            row = {
                'fact_id': 'fact-' + uuid4().hex,
                **candidate,
                'location': {'character_start': start, 'character_end': end},
                'verification': 'numeric_and_location_match_semantics_not_verified',
            }
            staged.append(row)
            staged_entries.append((fact_index, row))
        rows.append(row)
    # Validate the entire batch before changing durable state.
    if fact_failures:
        raise _quotation_batch_error('facts', fact_failures, len(inv.req.facts))
    for fact_index, fact_row in staged_entries:
        validate_persisted_tool_item('record_facts', fact_row, item_index=fact_index)
    ctx.state.setdefault('facts', []).extend(staged)
    result: dict[str, Any] = {'facts': rows}
    if ocr_warnings:
        result['warnings'] = ocr_warnings
    rec_summary = insights.reconciliation_for_model(insights.reconcile_run(ctx.state, ctx.source_by_id))
    if rec_summary is not None:
        result['reconciliation'] = rec_summary
    summary = {
        'kind': 'record_facts',
        'recorded_count': len(rows),
        'facts_preview': [
            {
                'label': _clip(row['label'], 120),
                'value': _clip(str(row['value']), 64),
                'unit': _clip(row['unit'], 64),
                'currency': _clip(row['currency'], 16) if isinstance(row.get('currency'), str) and row['currency'] else None,
                'period': _clip(row['period'], 64),
            }
            for row in rows[:5]
        ],
    }
    return ToolCallResult(result=result, summary=summary)


def _exec_calculate_financials(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    rows: list[dict[str, Any]] = []
    facts = {item['fact_id']: item for item in [*ctx.state.get('prior_facts', []), *ctx.state.get('facts', [])]}
    keys = ('fact_id', 'value', 'unit', 'currency', 'period')
    for calculation in inv.req.calculations:
        calc_payload: dict[str, Any] = {
            'formula': calculation.formula,
            'decimal_places': calculation.decimal_places,
        }
        if calculation.periods is not None:
            calc_payload['periods'] = calculation.periods
        if calculation.weights is not None:
            calc_payload['weights'] = calculation.weights
        if calculation.fact_ids is not None:
            selected = [facts.get(fid) for fid in calculation.fact_ids]
            if any(item is None for item in selected):
                raise ValueError('Calculation requires recorded fact IDs.')
            fact_dicts = [{k: item[k] for k in keys} for item in selected]  # type: ignore[index]
            if calculation.formula in MULTI_FACT_FORMULAS:
                calc_payload['facts'] = fact_dicts
            else:
                calc_payload['first'], calc_payload['second'] = fact_dicts[0], fact_dicts[1]
        else:
            first = facts.get(calculation.first_fact_id or '')
            second = facts.get(calculation.second_fact_id or '')
            if first is None or second is None:
                raise ValueError('Calculation requires recorded fact IDs.')
            first_dict = {k: first[k] for k in keys}
            second_dict = {k: second[k] for k in keys}
            if calculation.formula in MULTI_FACT_FORMULAS:
                calc_payload['facts'] = [first_dict, second_dict]
            else:
                calc_payload['first'] = first_dict
                calc_payload['second'] = second_dict
        request = CalculationRequest.model_validate(calc_payload)
        row = {'calculation_id': 'calc-' + uuid4().hex, **ctx.deps.calculate(request)}
        # Same consistency check as user-created calculations, so agent rows do not
        # wait for a later user save to show their input status (363 acceptance).
        row['input_status'] = ctx.deps.calculation_input_status(row, list(facts.values()))
        validate_persisted_tool_item('calculate_financials', row, item_index=len(rows))
        rows.append(row)
    ctx.state['calculations'].extend(rows)
    summary = {
        'kind': 'calculate_financials',
        'calculated_count': len(rows),
        'calcs_preview': [
            {
                'formula_id': _clip(row['formula_id'], 100),
                'value': _clip(str(row['value']), 64),
                'unit': _clip(row['unit'], 64),
                'currency': _clip(row['currency'], 16) if isinstance(row.get('currency'), str) and row['currency'] else None,
                'expression': _clip(row['expression'], 200),
            }
            for row in rows[:5]
        ],
    }
    return ToolCallResult(result={'calculations': rows}, summary=summary)


def _exec_create_chart(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    all_facts = [*ctx.state.get('prior_facts', []), *ctx.state.get('facts', [])]
    all_calcs = [*ctx.state.get('prior_calculations', []), *ctx.state.get('calculations', [])]
    existing_charts = [
        {'chart_id': cid} for cid in ctx.state.get('prior_chart_ids', [])
    ] + list(ctx.state.get('charts', []))
    chart = ctx.deps.build_research_chart(
        inv.req,
        facts=all_facts,
        calculations=all_calcs,
        sources=ctx.source_by_id,
        claims=list(ctx.state.get('prior_claims_meta', [])),
        existing_charts=existing_charts,
        as_of=ctx.as_of,
        allow_simulated=False,
    )
    chart_dict = chart.model_dump(mode='json') if hasattr(chart, 'model_dump') else chart
    chart = validate_persisted_tool_item('create_chart', chart_dict)
    ctx.state.setdefault('charts', []).append(chart_dict)
    if chart.causal_graph is not None:  # type: ignore[attr-defined]
        result = {
            'status': 'created',
            'chart_id': chart.chart_id,  # type: ignore[attr-defined]
            'chart_type': chart.chart_type,  # type: ignore[attr-defined]
            'title': chart.title,  # type: ignore[attr-defined]
            'subtitle': chart.subtitle,  # type: ignore[attr-defined]
            'node_count': len(chart.causal_graph.nodes),  # type: ignore[attr-defined]
            'edge_count': len(chart.causal_graph.edges),  # type: ignore[attr-defined]
            'has_projected_points': chart.has_projected_points,  # type: ignore[attr-defined]
            'replaces_chart_id': chart.replaces_chart_id,  # type: ignore[attr-defined]
            'source_ids': chart.source_ids,  # type: ignore[attr-defined]
            'embed_marker': f'[[chart:{chart.chart_id}]]',  # type: ignore[attr-defined]
        }
        summary_series_count = len(chart.causal_graph.nodes)  # type: ignore[attr-defined]
        summary_point_count = len(chart.causal_graph.edges)  # type: ignore[attr-defined]
    else:
        result = {
            'status': 'created',
            'chart_id': chart.chart_id,  # type: ignore[attr-defined]
            'chart_type': chart.chart_type,  # type: ignore[attr-defined]
            'title': chart.title,  # type: ignore[attr-defined]
            'subtitle': chart.subtitle,  # type: ignore[attr-defined]
            'axes': chart_dict['axes'],
            'series_summary': [
                {
                    'name': s['name'],
                    'mark': s['mark'],
                    'y_axis_id': s['y_axis_id'],
                    'point_count': len(s['points']),
                    'first_x': s['points'][0]['x'] if s['points'] else None,
                    'last_x': s['points'][-1]['x'] if s['points'] else None,
                }
                for s in chart_dict['series']
            ],
            'replaces_chart_id': chart.replaces_chart_id,  # type: ignore[attr-defined]
            'source_ids': chart.source_ids,  # type: ignore[attr-defined]
            'embed_marker': f'[[chart:{chart.chart_id}]]',  # type: ignore[attr-defined]
        }
        summary_series_count = len(chart.series)  # type: ignore[attr-defined]
        summary_point_count = sum(len(s.points) for s in chart.series)  # type: ignore[attr-defined]
    summary = {
        'kind': 'create_chart',
        'chart_id': chart.chart_id,  # type: ignore[attr-defined]
        'chart_type': chart.chart_type,  # type: ignore[attr-defined]
        'title': _clip(chart.title, 160),  # type: ignore[attr-defined]
        'series_count': summary_series_count,
        'point_count': summary_point_count,
    }
    return ToolCallResult(result=result, summary=summary)


def _exec_create_table(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    all_facts = [*ctx.state.get('prior_facts', []), *ctx.state.get('facts', [])]
    all_calcs = [*ctx.state.get('prior_calculations', []), *ctx.state.get('calculations', [])]
    existing_tables = [
        {'table_id': tid} for tid in ctx.state.get('prior_table_ids', [])
    ] + list(ctx.state.get('tables', []))
    table = ctx.deps.build_research_table(
        inv.req,
        facts=all_facts,
        calculations=all_calcs,
        sources=ctx.source_by_id,
        existing_tables=existing_tables,
        as_of=ctx.as_of,
        allow_simulated=False,
    )
    table_dict = table.model_dump(mode='json')
    ctx.state.setdefault('tables', []).append(table_dict)
    result = {
        'status': 'created',
        'table_id': table.table_id,
        'table_type': table.table_type,
        'title': table.title,
        'subtitle': table.subtitle,
        'columns': [
            {'key': c.key, 'label': c.label, 'align': c.align, 'unit': c.unit, 'currency': c.currency}
            for c in table.columns
        ],
        'row_count': len(table.rows),
        'replaces_table_id': table.replaces_table_id,
        'source_ids': table.source_ids,
        'markdown': table.markdown,
        'embed_marker': f'[[table:{table.table_id}]]',
    }
    summary = {
        'kind': 'create_table',
        'table_id': table.table_id,
        'table_type': table.table_type,
        'title': _clip(table.title, 160),
        'column_count': len(table.columns),
        'row_count': len(table.rows),
    }
    return ToolCallResult(result=result, summary=summary)


def _exec_analyze_market_series(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    analysis = ctx.deps.analyze_market_series(
        inv.req,
        sources=ctx.source_by_id,
        as_of=ctx.as_of,
        allow_simulated=False,
    )
    analyzed_ids = ctx.state.setdefault('analyzed_market_source_ids', [])
    for sid in analysis.get('source_ids', []):
        if sid not in analyzed_ids:
            analyzed_ids.append(sid)
    ctx.state['market_analyses_count'] = int(ctx.state.get('market_analyses_count') or 0) + 1
    summary = {
        'kind': 'analyze_market_series',
        'source_id': analysis['source_id'],
        'instrument': _clip(analysis['instrument'], 80),
        'bar_count': analysis['bar_count'],
        'latest_close': analysis['latest_close'],
    }
    return ToolCallResult(result={'status': 'ok', **analysis}, summary=summary)


def _exec_activate_method_pack(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    pack = get_pack(inv.req.pack_id)
    if pack is None:
        available = [p['pack_id'] for p in list_available_packs()]
        raise ValueError(f'Unknown method pack {inv.req.pack_id!r}. Available packs: {", ".join(available)}.')
    pack_records = ctx.state.setdefault('method_packs', [])
    fp = pack_fingerprint(pack)
    record = {
        'pack_id': pack.id,
        'version': pack.version,
        'fingerprint': fp,
        'activated_mid_run': True,
    }
    already_active = any(
        isinstance(item, Mapping) and item.get('pack_id') == pack.id
        for item in pack_records
    )
    if not already_active:
        pack_records.append(record)
    caps = _user_available_capabilities(ctx.state, ctx.fin_data)
    rendered = render_for_prompt(pack, available_capabilities=caps, max_chars=MAX_PACK_CHARS)
    result = {
        'status': 'already_active' if already_active else 'activated',
        'pack_id': pack.id,
        'version': pack.version,
        'fingerprint': fp,
        'title_en': pack.title_en,
        'title_zh': pack.title_zh,
        'method_pack_text': rendered,
        'required_checks': [r.id for r in pack.evidence_requirements if r.required],
        'min_search_actions': pack.counter_evidence.min_search_actions,
    }
    summary = {
        'kind': 'activate_method_pack',
        'pack_id': pack.id,
        'fingerprint': fp,
        'already_active': already_active,
    }
    return ToolCallResult(result=result, summary=summary)


def _exec_submit_research(ctx: ToolExecutionContext, inv: _CallInvocation) -> ToolCallResult:
    req = inv.req
    claims: list[dict[str, Any]] = []
    rejected_claims = 0
    prior_ids = set(ctx.state.get('prior_claim_ids', []))
    for claim in req.claims:
        unknown_ids = [claim_id for claim_id in claim.challenges_claim_ids if claim_id not in prior_ids]
        if unknown_ids:
            raise ValueError('challenges_claim_ids must name claims listed in prior_research; unknown: ' + json.dumps(unknown_ids))
    claim_failures: list[dict[str, Any]] = []
    for claim_index, claim in enumerate(req.claims):
        source = ctx.source_by_id.get(claim.source_id)
        try:
            if source is None or source_eligibility([source], ctx.as_of)[0][1] != 'eligible':
                raise ValueError('Claim source is unavailable or unauthorized.')
            start, end = ctx.locate(source, claim.quotation, claim.character_start, claim.character_end)
        except ValueError as item_error:
            if req.status == 'completed':
                claim_failures.append(ctx.quotation_failure(claim_index, 'claims', source, claim.source_id, claim.quotation, item_error))
                continue
            rejected_claims += 1
            continue
        claims.append({
            'claim_id': 'claim-' + uuid4().hex,
            'role': claim.role,
            'text': claim.text,
            'citations': [{'source_id': claim.source_id, 'quotation': claim.quotation, 'location': {'character_start': start, 'character_end': end}}],
            'challenges_claim_ids': list(dict.fromkeys(claim.challenges_claim_ids)),
        })
    if claim_failures:
        raise _quotation_batch_error('claims', claim_failures, len(req.claims))
    if not claims and req.status == 'completed' and not ctx.state.get('prior_cited_claims'):
        raise ValueError('At least one source-backed claim is required for completed research.')
    validate_persisted_tool_batch('submit_research', claims)
    skip_presubmit = (
        req.status == 'partial'
        and (any(text.strip() for text in req.limitations) or not claims)
    )
    gate_eval = None
    if (
        int(ctx.state.get('presubmit_nudges') or 0) < 1
        and not skip_presubmit
        and ctx.can_afford_presubmit_followup()
    ):
        gate_eval = insights.evaluate_presubmit_gate(
            ctx.state,
            ctx.source_by_id,
            report=req.report,
            claims=claims,
            limitations=req.limitations,
            has_fin_data=ctx.fin_data is not None,
        )
    if gate_eval and gate_eval.get('needs_attention'):
        ctx.state['presubmit_nudges'] = int(ctx.state.get('presubmit_nudges') or 0) + 1
        ctx.state['coverage_attention_active'] = True
        if inv.signature:
            ctx.seen.discard(inv.signature)
            ctx.state['seen'] = sorted(ctx.seen)
        if ctx.active_call_seq is not None:
            ctx.emit_reset(ctx.active_call_seq, 'submission_rejected')
            ctx.active_call_seq = None
        result = {
            'status': 'needs_attention',
            'reason': 'presubmit_review_required',
            'message': (
                'Pre-submit check found unmet requirements, counter-evidence checks, or material conflicts. '
                'Perform one targeted follow-up or explicitly explain the open gaps in limitations, then resubmit.'
            ),
            'missing_items': gate_eval['missing_items'],
            'unaddressed_conflicts': gate_eval['unaddressed_conflicts'],
            'presubmit_nudges': ctx.state['presubmit_nudges'],
            'max_presubmit_nudges': 1,
        }
        summary = {
            'kind': 'submit_research',
            'status': 'needs_attention',
            'missing_count': len(gate_eval['missing_items']),
            'conflict_count': len(gate_eval['unaddressed_conflicts']),
        }
        return ToolCallResult(result=result, summary=summary)

    gaps = []
    if not claims and req.status != 'completed':
        gaps.append(gap('no_cited_claims'))
    if rejected_claims:
        gaps.append(gap('claims_omitted_unverified_location'))
    gaps.extend(gap('model_reported', text) for text in req.limitations if text.strip())
    status = req.status
    report = req.report if status == 'completed' else ''
    partial_notes = (req.report.strip() or None) if status == 'partial' else None
    ctx.state['events'].append({'kind': 'submission', 'stopping_reason': req.stopping_reason, 'status': status})
    ctx.state['pending'] = None
    # Partial output contains only located claims and saved observations;
    # unvalidated free-form report text is not delivered as a conclusion.
    ctx.state['submission'] = {
        'status': status,
        'report': report,
        'partial_notes': partial_notes,
        'claims': claims,
        'gaps': gaps,
        'stopping_reason': req.stopping_reason,
    }
    ctx.emit_reset(ctx.batch_call_seq, 'submission_partial' if status == 'partial' else 'call_completed')
    ctx.active_call_seq = None
    ctx.save()
    assert ctx.outcome_builder is not None
    return ToolCallResult(
        early_outcome=ctx.outcome_builder(
            status,
            'submitted',
            report=report,
            claims=claims,
            gaps=gaps,
            stopping_note=req.stopping_reason,
            partial_notes=partial_notes,
        )
    )


ToolHandler = Callable[[ToolExecutionContext, _CallInvocation], ToolCallResult]

TOOL_HANDLERS: dict[str, ToolHandler] = {
    'read_sources': _exec_read_sources,
    'outline_source': _exec_outline_source,
    'search_source': _exec_search_source,
    'search_library': _exec_search_library,
    'news_search': _exec_news_search,
    'acquire_sources': _exec_acquire_sources,
    'resolve_entity': _exec_fin_tool,
    'market_data': _exec_fin_tool,
    'company_financials': _exec_fin_tool,
    'filings': _exec_fin_tool,
    'issuer_documents': _exec_issuer_documents,
    'record_facts': _exec_record_facts,
    'calculate_financials': _exec_calculate_financials,
    'create_chart': _exec_create_chart,
    'analyze_market_series': _exec_analyze_market_series,
    'create_table': _exec_create_table,
    'read_pages': _exec_read_pages,
    'request_ocr': _exec_request_ocr,
    'activate_method_pack': _exec_activate_method_pack,
    'extract_statements': _exec_extract_statements,
    'submit_research': _exec_submit_research,
}


def compute_action_signature(name: str, action: Mapping[str, Any], state: Mapping[str, Any]) -> str:
    """Compute the evidence-aware deduplication signature for a validated tool call."""
    return sha256(json.dumps(
        {
            'name': name,
            'args': action,
            'reads': state['read_ranges'],
            'facts': [{k: v for k, v in f.items() if k != 'fact_id'} for f in state.get('facts', [])],
            'charts': [{'chart_id': c.get('chart_id'), 'replaces_chart_id': c.get('replaces_chart_id')} for c in state.get('charts', [])],
            'tables': [{'table_id': t.get('table_id'), 'replaces_table_id': t.get('replaces_table_id')} for t in state.get('tables', [])],
            'lead_urls': [lead.get('url') for lead in state.get('leads', []) if isinstance(lead, dict)],
            'acquired': list(state.get('acquired_source_ids', [])),
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    ).encode()).hexdigest()


def compute_invalid_signature(name: str, args: Mapping[str, Any], raw_arguments: Any, state: Mapping[str, Any]) -> str:
    """Compute the deduplication signature for an invalid tool attempt."""
    invalid_action = {k: v for k, v in args.items() if k != 'rationale'} if args else raw_arguments
    return sha256(json.dumps(
        {
            'name': name,
            'action': invalid_action,
            'reads': state['read_ranges'],
            'facts': [{k: v for k, v in f.items() if k != 'fact_id'} for f in state.get('facts', [])],
            'charts': [{'chart_id': c.get('chart_id'), 'replaces_chart_id': c.get('replaces_chart_id')} for c in state.get('charts', [])],
            'tables': [{'table_id': t.get('table_id'), 'replaces_table_id': t.get('replaces_table_id')} for t in state.get('tables', [])],
            'leads': state.get('leads', []),
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    ).encode()).hexdigest()


def execute_tool_batch(
    ctx: ToolExecutionContext,
    calls: list[Any],
    *,
    cursor: int = 0,
) -> tuple[Any | None, int | None]:
    """Execute a batch of native model tool calls starting at ``cursor``.

    Returns ``(early_outcome, active_call_seq)``. When ``early_outcome`` is non-None,
    the caller should return it immediately from ``run_research_agent``.
    """
    assert ctx.outcome_builder is not None
    ctx.batch_calls = calls
    if calls and cursor < len(calls) and calls[cursor].name != 'submit_research' and ctx.active_call_seq is not None:
        ctx.emit_reset(ctx.active_call_seq, 'tool_started')
        ctx.active_call_seq = None

    for index in range(cursor, len(calls)):
        call = calls[index]
        ctx.context.ensure_running()
        args: dict[str, Any] = {}
        transient_rate_limited = False
        rate_limit_retries = ctx.state.setdefault('rate_limit_retries', [])
        current_pending = ctx.state.get('pending') if isinstance(ctx.state.get('pending'), dict) else {}
        tool_started_at = (
            current_pending['started_at']
            if current_pending.get('kind') == 'tool_batch' and current_pending.get('cursor') == index
            and isinstance(current_pending.get('started_at'), str)
            else _now_iso()
        )
        tool_summary: dict[str, Any] | None = None
        duplicate_kind: str | None = None
        signature = ''
        result: dict[str, Any] = {}
        try:
            if call.arguments is None:
                raise ValueError('Tool arguments must be a valid JSON object.')
            args = call.arguments
            spec = get_tool_spec(call.name)
            handler = TOOL_HANDLERS.get(call.name)
            if spec is None or handler is None:
                raise ValueError('Unknown tool.')
            req = spec.input_model.model_validate(args)
            action = {k: v for k, v in args.items() if k != 'rationale'}
            signature = compute_action_signature(call.name, action, ctx.state)
            is_duplicate = signature in ctx.seen
            if not is_duplicate:
                ctx.seen.add(signature)
                ctx.state['seen'] = sorted(ctx.seen)
            turn_read_chars, turn_bulk_result_chars = _turn_batch_usage(ctx.state)
            if is_duplicate:
                duplicate_kind = 'action'
                result = {
                    'status': 'duplicate_action',
                    'message': (
                        'This exact call was already made with no change in the evidence state since; '
                        'its earlier result is unchanged and is not repeated. Use that result or take a different action.'
                    ),
                }
                reused_ids = ctx.state.get('acquired_by_signature', {}).get(signature)
                if spec.is_acquisition_tool and reused_ids:
                    result['reused'] = True
                    result['source_ids'] = list(reused_ids)
                    if len(reused_ids) == 1:
                        result['source_id'] = reused_ids[0]
            elif spec.compaction_role == 'bulk' and turn_bulk_result_chars >= MAX_TURN_TOOL_RESULT_CHARS:
                result = {
                    'status': 'invalid',
                    'error': 'turn_tool_result_budget_exceeded',
                    'reason': (
                        f'Cumulative bulk tool result size in this turn ({turn_bulk_result_chars} characters) '
                        f'reached the per-turn limit ({MAX_TURN_TOOL_RESULT_CHARS}). '
                        f'Record facts from already-retrieved material first or continue in the next turn.'
                    ),
                    'instruction': 'Call record_facts or calculate_financials on existing evidence, or finish the turn before issuing more bulk reads/queries.',
                    'turn_tool_result_characters': turn_bulk_result_chars,
                    'max_turn_tool_result_characters': MAX_TURN_TOOL_RESULT_CHARS,
                }
            else:
                inv = _CallInvocation(
                    call=call,
                    req=req,
                    args=args,
                    index=index,
                    started_at=tool_started_at,
                    signature=signature,
                    turn_read_chars=turn_read_chars,
                    turn_bulk_result_chars=turn_bulk_result_chars,
                )
                call_out = handler(ctx, inv)
                if call_out.early_outcome is not None:
                    return call_out.early_outcome, ctx.active_call_seq
                if call_out.handled_continue:
                    continue
                result = call_out.result
                tool_summary = call_out.summary
                duplicate_kind = call_out.duplicate_kind
                transient_rate_limited = call_out.transient_rate_limited
        except ProviderFailure as error:
            if error.code in ('provider_output_truncated', 'provider_context_limit', 'provider_timeout') or not error.retryable:
                _record_failed_attempt(ctx.state, call_id=call.id, reason=error.code, error=error)
                ctx.save()
            ctx.emit_reset(ctx.batch_call_seq, 'failed')
            ctx.active_call_seq = None
            return ctx.outcome_builder('partial', error.code, gaps=[provider_gap(error)]), ctx.active_call_seq
        except (ValueError, ValidationError) as error:
            if call.name == 'submit_research':
                ctx.emit_reset(ctx.batch_call_seq, 'submission_rejected')
                ctx.active_call_seq = None
            if isinstance(error, ItemPersistenceValidationError):
                ctx.deps.logger.warning(
                    'research_tool_item_validation_failed tool=%s item_kind=%s item_index=%s validation_errors=%s',
                    call.name,
                    error.item_kind,
                    error.item_index,
                    error.validation_errors,
                )
            invalid_signature = compute_invalid_signature(call.name, args, call.raw_arguments, ctx.state)
            invalid_seen = ctx.state.setdefault('invalid_seen', [])
            result = {'status': 'invalid', 'reason': invalid_reason(error)}
            if isinstance(error, ItemPersistenceValidationError):
                result['error'] = 'persistence_validation_failed'
                result['item_kind'] = error.item_kind
                result['validation_errors'] = error.validation_errors
            if isinstance(error, QuotationBatchError):
                result['failures'] = error.failures
            if invalid_signature in invalid_seen:
                duplicate_kind = 'invalid'
                result['message'] = 'This invalid call was already rejected with the same arguments; change the arguments instead of resending them.'
            else:
                invalid_seen.append(invalid_signature)

        if transient_rate_limited and signature not in rate_limit_retries:
            ctx.seen.discard(signature)
            ctx.state['seen'] = sorted(ctx.seen)
            rate_limit_retries.append(signature)
        if (
            signature
            and call.name in ACQUISITION_TOOLS
            and isinstance(result, dict)
            and (duplicate_kind is None or result.get('reused') is True)
        ):
            acquired_here = (
                list(result['source_ids'])
                if result.get('reused') is True and isinstance(result.get('source_ids'), list)
                else _result_source_ids(result)
            )
            if acquired_here:
                by_sig = ctx.state.setdefault('acquired_by_signature', {})
                by_sig[signature] = acquired_here
                if len(by_sig) > 200:
                    for old in list(by_sig)[:len(by_sig) - 200]:
                        del by_sig[old]
        _with_guidance(result)
        ctx.append_result(call.id, result)
        event: dict[str, Any] = {
            'kind': 'tool',
            'tool_call_id': call.id,
            'name': call.name,
            'status': result.get('status', 'completed'),
            'rationale': args.get('rationale')[:300] if isinstance(args.get('rationale'), str) else None,
        }
        if call.name == 'news_search' and type(result.get('denied_count')) is int:
            event['denied_count'] = result['denied_count']
            event['denied_reasons'] = result['denied_reasons']
        if call.name in {'news_search', 'acquire_sources', 'issuer_documents'} | FIN_TOOLS:
            reason = result.get('reason')
            if isinstance(reason, str) and len(reason) <= 100 and all(c.isalnum() or c == '_' for c in reason):
                event['reason'] = reason
        if call.name == 'request_ocr' or (call.name == 'read_pages' and isinstance(result.get('ocr'), dict)):
            ocr_part = result if call.name == 'request_ocr' else result['ocr']
            event['ocr'] = {
                'status': ocr_part.get('status'),
                'source_id': ocr_part.get('source_id'),
                'previous_source_id': ocr_part.get('previous_source_id'),
                'pages': [
                    row.get('page') for row in (ocr_part.get('pages') or [])
                    if isinstance(row, Mapping) and row.get('status') == 'ocr'
                ][:MAX_ONDEMAND_PAGES],
            }
        if tool_summary is not None:
            event['summary'] = _fit_summary(tool_summary)
        if duplicate_kind is not None:
            event['duplicate'] = duplicate_kind
        ctx.state['events'].append(event)
        if duplicate_kind is None and isinstance(result, Mapping) and result.get('status') != 'invalid':
            if call.name in COVERAGE_UPDATING_TOOLS or (call.name == 'search_source' and ctx.state.get('method_packs')):
                ctx.state['coverage_attention_active'] = True
        ctx.state['pending'] = ctx.tool_batch_pending(index + 1)
        ctx.save()

    # Stop only after two CONSECUTIVE responses made no new move (every call repeated an earlier
    # action or an already-rejected invalid call). The first such response still gets its results
    # (a repeated call may return a useful reused result) plus a warning.
    duplicate_by_call = {
        ev.get('tool_call_id'): ev.get('duplicate')
        for ev in ctx.state['events']
        if isinstance(ev, Mapping) and ev.get('kind') == 'tool' and ev.get('duplicate')
    }
    if calls and all(c.id in duplicate_by_call for c in calls):
        ctx.state['duplicate_streak'] = int(ctx.state.get('duplicate_streak') or 0) + 1
        if ctx.state['duplicate_streak'] >= 2:
            ctx.state['pending'] = None
            ctx.state['stopped'] = 'no_progress'
            ctx.save()
            invalid_only = all(duplicate_by_call[c.id] == 'invalid' for c in calls)
            msg = 'Repeated invalid action with unchanged evidence state.' if invalid_only else None
            return ctx.outcome_builder('partial', 'no_progress', gaps=[gap('no_progress', msg)]), ctx.active_call_seq
        last_result = (
            ctx.state['history'][-1]['results'][-1]
            if ctx.state['history'] and ctx.state['history'][-1]['results']
            else None
        )
        if last_result is not None:
            try:
                payload = json.loads(last_result['content'])
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict):
                payload['run_notice'] = (
                    'Every call in this response repeated an earlier action. If your next response again '
                    'contains only repeated calls, the run will stop. Use the earlier results or take a '
                    'different action, or submit_research.'
                )
                last_result['content'] = json.dumps(payload, ensure_ascii=False, default=str)
    else:
        ctx.state['duplicate_streak'] = 0
    pending_batch = ctx.state.get('pending')
    baseline = pending_batch.get('baseline') if isinstance(pending_batch, Mapping) else None
    if isinstance(baseline, Mapping):
        produced_new = (
            len(ctx.state.get('facts', [])) > int(baseline.get('facts', 0))
            or len(ctx.state.get('calculations', [])) > int(baseline.get('calculations', 0))
            or len(ctx.state.get('charts', [])) > int(baseline.get('charts', 0))
            or len(ctx.state.get('tables', [])) > int(baseline.get('tables', 0))
            or int(ctx.state.get('market_analyses_count') or 0) > int(baseline.get('market_analyses', 0))
            or len(ctx.source_by_id) > int(baseline.get('sources', 0))
        )
        ctx.state['zero_yield_turns'] = 0 if produced_new else (int(ctx.state.get('zero_yield_turns') or 0) + 1)
    ctx.state['pending'] = None
    ctx.save()
    return None, ctx.active_call_seq
