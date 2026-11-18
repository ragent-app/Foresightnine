"""Shared execution boundary for the existing research capabilities.

Capabilities describe the authority a stage needs; they do not grant it. The
caller supplies run-scoped authorization and availability, while each existing
handler remains responsible for its finer-grained source policy, budget,
cancellation and external-call controls. Search leads are not evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

T = TypeVar('T')


@dataclass(frozen=True)
class ToolCapability:
    name: str
    purpose: str
    permission: str
    external: bool
    input_kind: str
    output_kind: str
    # Existing local operations performed inside this stage, not separately
    # invocable stages or an assertion that every execution uses each one.
    local_operations: tuple[str, ...] = ()


# Names are stable stage capabilities, not provider names. New adapters can
# implement an existing capability without changing the coordination policy.
_CAPABILITIES = {item.name: item for item in (
    ToolCapability('planning', 'Plan within the authorized research scope', 'model_input', True, 'task', 'plan'),
    ToolCapability('acquisition_decision', 'Choose an evidence acquisition action', 'model_input', True, 'evidence_state', 'acquisition_decision'),
    ToolCapability('source_discovery', 'Find unverified source leads', 'search', True, 'query', 'source_candidates'),
    ToolCapability('gap_search', 'Find leads for an identified evidence gap', 'search', True, 'query', 'source_candidates'),
    ToolCapability('source_retrieval', 'Retrieve an eligible official document', 'official_retrieval', True, 'source_candidates', 'source_snapshot',
                   ('research_fetch.fetch_official_text', 'research_fetch.fetch_official_pdf', 'research_sec.fetch_sec_filing')),
    ToolCapability('analysis', 'Analyze authorized source snapshots', 'model_input', True, 'task_and_sources', 'research_analysis',
                   ('research_structured_data.extract_structured_observations', 'research_calculations.calculate')),
    ToolCapability('counterpoint', 'Explore counterarguments', 'model_input', True, 'analysis_and_sources', 'counterarguments'),
    ToolCapability('gap_followup', 'Investigate recorded evidence gaps', 'model_input', True, 'analysis_and_sources', 'research_analysis',
                   ('research_structured_data.extract_structured_observations', 'research_calculations.calculate')),
    ToolCapability('support_review', 'Review evidence supporting claims', 'model_input', True, 'claims_and_sources', 'claim_review'),
    ToolCapability('delivery_assessment', 'Assess completeness and limitations', 'local_processing', False, 'research_artifacts', 'assessment'),
)}


def get_research_tool(name: str) -> ToolCapability | None:
    """Return a declared capability, or None for an unsupported action."""
    return _CAPABILITIES.get(name)


def list_research_tools() -> tuple[ToolCapability, ...]:
    return tuple(_CAPABILITIES.values())


@dataclass(frozen=True)
class SourceReference:
    # Leads are distinct from stored sources and must not be treated as evidence.
    kind: str
    source_id: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class ToolResult(Generic[T]):
    capability: str
    status: str
    value: T | None = None
    reason: str | None = None
    retryable: bool | None = None
    source_ids: tuple[str, ...] = ()
    source_references: tuple[SourceReference, ...] = ()
    gaps: tuple[str, ...] = ()
    usage: int | None = None


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def execute_research_tool(name: str, handler: Callable[[], T], *, permitted: bool,
                          available: bool, status: Callable[[T], str] | None = None) -> ToolResult[T]:
    """Run a declared capability without changing its legacy stage value.

    A denied, unknown or unavailable capability never invokes the handler.
    Exceptions deliberately propagate so cancellation and existing lifecycle
    accounting cannot be mistaken for an ordinary empty result. The optional
    status mapper preserves stages whose status is derived by the workflow.
    """
    if get_research_tool(name) is None:
        return ToolResult(name, 'unavailable', reason='unknown_capability', gaps=('Capability is not registered.',))
    if not permitted:
        return ToolResult(name, 'denied', reason='permission_denied', gaps=('Required capability permission was not granted.',))
    if not available:
        return ToolResult(name, 'unavailable', reason='capability_unavailable', gaps=('Capability is unavailable for this run.',))

    value = handler()
    result_status = status(value) if status is not None else getattr(value, 'status', None)
    if not isinstance(result_status, str):
        result_status = 'completed'
    reason = getattr(value, 'reason', None) or getattr(value, 'unavailable_reason', None) or getattr(value, 'error', None)
    reason = reason if isinstance(reason, str) else None
    analysis = getattr(value, 'analysis', None)
    source_ids = _strings(getattr(analysis, 'source_ids', ()))
    # The review stage carries selected source segments rather than an
    # AnalysisResult; preserve those source identities too.
    segments = getattr(value, 'segments', ())
    if isinstance(segments, (list, tuple)):
        source_ids = tuple(dict.fromkeys((*source_ids, *(item['source_id'] for item in segments
            if isinstance(item, dict) and isinstance(item.get('source_id'), str) and item['source_id']))))
    source = getattr(value, 'source', None)
    source_id = getattr(source, 'source_id', None)
    if isinstance(source_id, str) and source_id not in source_ids:
        source_ids = (*source_ids, source_id)
    references = [SourceReference('stored_source', source_id=item) for item in source_ids]
    # Discovery returns candidate dictionaries, not sources. Preserve only the
    # sanitized address and any source ID already assigned by the caller.
    candidates = getattr(value, 'candidates', ())
    if isinstance(candidates, (list, tuple)):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            url, candidate_id = candidate.get('url'), candidate.get('source_id')
            url = url if isinstance(url, str) and url else None
            candidate_id = candidate_id if isinstance(candidate_id, str) and candidate_id else None
            if url or candidate_id:
                references.append(SourceReference('unverified_lead', candidate_id, url))
    gaps = tuple(dict.fromkeys((*_strings(getattr(value, 'notes', ())),
                                *_strings(getattr(value, 'limitations', ())),
                                *_strings(getattr(analysis, 'limitations', ())))))
    reported = getattr(value, 'tokens', None)
    if reported is None:
        reported = getattr(value, 'generation_tokens', None)
    usage = reported if type(reported) is int and reported >= 0 else None
    retryable = getattr(value, 'retryable', None)
    if type(retryable) is not bool:
        retryable = None
    return ToolResult(capability=name, status=result_status, value=value, reason=reason,
                      retryable=retryable, source_ids=source_ids,
                      source_references=tuple(dict.fromkeys(references)), gaps=gaps, usage=usage)
