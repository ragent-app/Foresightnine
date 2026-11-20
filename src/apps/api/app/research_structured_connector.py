"""Authorized structured-source bridge. Suggestions never grant access."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .operations import ApiProblem
from .research_coordinator import ResearchExecutionContext
from .research_sec import SecConceptRequest, SecFailure, SecIdentityRequest, fetch_sec_concept, search_sec_identities, validate_user_agent
from .research_sources import ResearchSource, ResearchSources, SourceInput, SourcePermissions


class StructuredQuery(BaseModel):
    """Generic envelope: provider-specific fields live only in parameters."""
    model_config = ConfigDict(extra='forbid')
    source_kind: str = Field(min_length=1, max_length=80)
    parameters: dict[str, Any] = Field(default_factory=dict)


class SecQueryParameters(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    entity_query: str = Field(min_length=1, max_length=100)
    taxonomy: Literal['us-gaap', 'ifrs-full', 'dei']
    tag: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z][A-Za-z0-9_]*$')
    unit: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9-]+$')
    period_start: date
    period_end: date

    @model_validator(mode='after')
    def ordered_period(self) -> SecQueryParameters:
        if self.period_start > self.period_end:
            raise ValueError('Period start is after period end.')
        return self


@dataclass
class StructuredQueryResult:
    status: str
    reason: str | None = None
    source: ResearchSource | None = None
    identity_receipt: dict[str, Any] | None = None
    semantic_status: str = 'not_verified'
    calls: int = 0
    external_state: str = 'not_started'
    available_periods: list[dict] | None = None
    available_units: list[str] | None = None
    coverage: dict | None = None


_PERIOD_KEYS = ('start', 'end', 'fp', 'fy', 'form', 'filed', 'frame')


def _period_view(row: dict) -> dict:
    return {key: row[key] for key in _PERIOD_KEYS if row.get(key) is not None}


def _period_list(rows: list[dict], limit: int) -> list[dict]:
    ordered = sorted(rows, key=lambda row: (str(row.get('end', '')), str(row.get('filed', ''))), reverse=True)
    seen: set[tuple] = set()
    result = []
    for row in ordered:
        view = _period_view(row)
        key = (view.get('start'), view.get('end'), view.get('fp'), view.get('fy'), view.get('form'), view.get('filed'))
        if key in seen:
            continue
        seen.add(key)
        result.append(view)
        if len(result) >= limit:
            break
    return result


def execute_structured_query(query: StructuredQuery, *, owner_id: str, operation_id: str,
                             store: ResearchSources, sec_agent_config: str | None,
                             reserve_sec: Callable[[], bool], context: ResearchExecutionContext,
                             permitted: bool, as_of: datetime | None = None) -> StructuredQueryResult:
    if not permitted:
        return StructuredQueryResult('restricted', 'permission_denied')
    if query.source_kind != 'sec_company_concept':
        return StructuredQueryResult('unavailable', 'unsupported_connector')
    if as_of is not None:
        return StructuredQueryResult('restricted', 'historical_cutoff')
    if context.cost_limited:
        return StructuredQueryResult('restricted', 'cost_unknown')
    try:
        params = SecQueryParameters.model_validate(query.parameters)
        agent = validate_user_agent(sec_agent_config)
    except ValidationError:
        return StructuredQueryResult('unknown', 'incomplete_or_invalid_query')
    except SecFailure:
        return StructuredQueryResult('unconfigured', 'sec_contact_unconfigured')
    calls = 0
    receipt = None
    external_state = 'not_started'
    try:
        def admit() -> None:
            context.ensure_running()
            # Existing SEC adapters have a fixed 15-second per-call timeout;
            # do not start one when it cannot fit a user deadline.
            if context.budget.deadline - context.now() < 15:
                raise SecFailure('time_budget')
            context.reserve_search()
            if not reserve_sec():
                raise SecFailure('source_rate_limited')
        admit()
        calls += 1
        external_state = 'unknown'
        identity = search_sec_identities(SecIdentityRequest(query=params.entity_query), agent)
        external_state = 'returned'
        matches = [item for item in identity.get('candidates', []) if isinstance(item, dict) and
                   params.entity_query.casefold() in {str(item.get('name', '')).casefold(),
                                                    str(item.get('ticker', '')).casefold(),
                                                    str(item.get('cik', '')).lstrip('0').casefold()}]
        if len(matches) != 1 or len(identity.get('candidates', [])) >= 10:
            return StructuredQueryResult('unknown', 'identity_not_unique', calls=calls, external_state=external_state)
        match = matches[0]
        receipt = {key: identity.get(key) for key in ('source_url', 'retrieved_at', 'response_sha256', 'limitation')}
        receipt['match'] = {key: match.get(key) for key in ('cik', 'ticker', 'name', 'exchange')}
        permissions = SourcePermissions(store=True, display=True, model_input=True, export=True)
        request = SecConceptRequest(cik=match['cik'], taxonomy=params.taxonomy, tag=params.tag,
                                    unit=params.unit, max_records=100, permissions=permissions)
        admit()
        calls += 1
        external_state = 'unknown'
        data = fetch_sec_concept(request, agent)
        external_state = 'returned'
        rows = []
        unit_rows = []
        for line in data.text.splitlines():
            if line.startswith('{'):
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if row.get('unit') == params.unit:
                    unit_rows.append(row)
                if (row.get('unit') == params.unit and row.get('start') == params.period_start.isoformat()
                        and row.get('end') == params.period_end.isoformat()):
                    rows.append(row)
        if not rows:
            return StructuredQueryResult('unknown', 'unit_or_period_not_found_in_returned_records', identity_receipt=receipt, calls=calls, external_state=external_state,
                                         available_periods=_period_list(unit_rows, 12) or None)
        provenance = {'connector': query.source_kind, 'query': query.model_dump(mode='json'),
                      'identity_receipt': receipt, 'unit_period_match': 'exact_returned_observation',
                      'semantic_status': 'not_verified',
                      'limitations': ['Current SEC identity mapping; historical identity and metric equivalence are not established.',
                                      'SEC source is a bounded selection; filing date is not exact public availability.']}
        identity_key = operation_id + json.dumps(query.model_dump(mode='json'), sort_keys=True) + data.response_sha256
        source = store.create(owner_id, SourceInput(title=data.title, text=data.text, url=data.url,
                              kind='official', permissions=permissions), retrieval_method='sec_companyconcept',
                              response_sha256=data.response_sha256, response_content_type='application/json',
                              source_id='source_' + sha256(identity_key.encode()).hexdigest()[:32], provenance=provenance)
        latest_end = max((str(row.get('end', '')) for row in unit_rows), default=None)
        latest_filed = max((str(row.get('filed', '')) for row in unit_rows), default=None)
        coverage = {'latest_end': latest_end, 'latest_filed': latest_filed, 'record_count': len(unit_rows),
                    'recent': _period_list(unit_rows, 8)}
        return StructuredQueryResult('completed', source=source, identity_receipt=receipt, calls=calls,
                                     external_state=external_state, coverage=coverage)
    except (SecFailure, ApiProblem, ValueError, KeyError, TypeError) as error:
        context.ensure_running()
        reason = getattr(error, 'code', None) or getattr(error, 'error_code', None) or 'source_invalid'
        if external_state == 'unknown' and reason not in {'source_timeout', 'source_unavailable'}:
            external_state = 'returned'
        if reason == 'source_tag_not_found':
            return StructuredQueryResult('unknown', 'tag_not_reported', identity_receipt=receipt, calls=calls,
                                         external_state='returned')
        units = getattr(error, 'available_units', None)
        return StructuredQueryResult('unknown' if reason == 'source_no_matching_facts' else 'failed',
                                     str(reason), identity_receipt=receipt, calls=calls, external_state=external_state,
                                     available_units=list(units) if units else None)
