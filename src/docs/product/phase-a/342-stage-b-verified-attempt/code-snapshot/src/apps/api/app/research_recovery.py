"""Conservative recovery of an already committed research checkpoint.

This path performs no external calls. A checkpoint does not prove the outcome
of calls made after it, so it cannot authorize their automatic replay.
"""
from __future__ import annotations

from dataclasses import MISSING, dataclass, fields
from typing import Any
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from .operations import ApiProblem
from .research_topics import ResearchContent, ResearchTopic
from .storage import JsonDocumentRepository, _drop_legacy_null_fields


class RecoveryBudget(BaseModel):
    model_config = ConfigDict(extra='forbid')
    max_model_calls: int = Field(ge=0, strict=True)
    max_search_calls: int = Field(ge=0, strict=True)
    max_total_tokens: int = Field(ge=0, strict=True)
    model_calls: int = Field(ge=0, strict=True)
    search_calls: int = Field(ge=0, strict=True)
    accounted_tokens: int = Field(ge=0, strict=True)
    reported_tokens: int | None = Field(default=None, ge=0, strict=True)
    # Absolute original deadline: time spent offline also consumes the budget.
    deadline_at: datetime


class RecoverySnapshot(BaseModel):
    """Durable stage boundary; content and analysis must describe one checkpoint."""
    model_config = ConfigDict(extra='forbid')
    schema_version: str = '1'
    checkpoint_version_id: str = Field(min_length=1)
    next_stage: str = Field(min_length=1)
    completed_stages: list[str]
    analysis: dict[str, Any]
    claims: list[dict[str, Any]]
    planning_steps: list[str]
    source_ids: list[str]
    budget: RecoveryBudget
    plan: dict[str, Any]
    # Snapshot of server-side source hashes and permissions, never from client.
    source_fingerprints: list[dict[str, Any]]


class AnalysisSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid')
    claims: list[dict[str, Any]]
    counterarguments: list[dict[str, Any]]
    limitations: list[str]
    source_ids: list[str]
    total_tokens: int | None = None
    facts: list[dict[str, Any]]
    calculations: list[dict[str, Any]]
    prompt_sha256: str | None = None
    prompt_characters: int
    prompt_bytes: int
    segments: list[dict[str, Any]]
    research_questions: list[dict[str, Any]]
    available_fact_ids: list[str]
    evidence_needs: list[dict[str, Any]] = Field(default_factory=list)
    absence_observations: list[dict[str, Any]] = Field(default_factory=list)
    requirement_links: list[dict[str, Any]] = Field(default_factory=list)
    discovered_requirements: list[dict[str, Any]] = Field(default_factory=list)
    selection_diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    pdf_structure_diagnostics: list[dict[str, Any]] = Field(default_factory=list)


class StageState(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request: dict[str, Any]
    planning_result: dict[str, Any]
    base_prompt_text: str | None = None
    max_prompt_chars: int = Field(ge=0)
    generation_usage: int | None = Field(default=None, ge=0)
    generation_status: str
    analysis_error_detail: str | None = None
    unavailable_reason: str | None = None
    question_origin: str
    effective_question: str
    focus: tuple[str, int, int] | None = None
    discovered: list[dict[str, Any]]
    search_receipts: list[dict[str, Any]]
    discovery_notes: list[str]
    search_status: str
    official_fetch_status: str
    official_fetch_calls: int = Field(ge=0)
    workflow_receipts: list[dict[str, Any]]
    # Version 1 snapshots predate these fields and describe analysis only.
    supplemental_citation_leads: list[dict[str, Any]] = Field(default_factory=list)
    stage_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    coordinator: dict[str, Any] = Field(default_factory=dict)


class CoordinatorState(BaseModel):
    model_config = ConfigDict(extra='forbid')
    attempted: list[str]
    outcomes: dict[str, str]
    omissions: dict[str, str]
    actions_used: int = Field(ge=0, le=7, strict=True)


_FOLLOWUP_STAGES = frozenset({'counterpoint', 'gap_followup', 'support_review'})
_FOUNDATION_STAGES = frozenset({'planning', 'source_discovery', 'source_retrieval', 'analysis'})


def model_receipts_confirmed(receipt: Any) -> bool:
    """Every admitted invocation must have returned and durably settled."""
    calls = getattr(receipt, 'model_calls_admitted', None)
    outcomes = getattr(receipt, 'model_call_receipts', ())
    return (type(calls) is int and calls >= 0 and len(outcomes) == calls and
            all(item.get('provider') == 'returned' and item.get('settlement') == 'settled'
                for item in outcomes))


def stage_result_settled(*, stage: str, receipt: Any, result: Any,
                         search_receipts: list[dict[str, Any]]) -> bool:
    """Whether this stage's outcome and admitted external usage are known.

    A missing/failed call receipt stays pending even when a handler returned a
    partial result. This does not certify the quality of the research output.
    """
    if stage not in _FOLLOWUP_STAGES or receipt is None or result is None:
        return False
    status = getattr(result, 'status', None)
    if status != getattr(receipt, 'status', None):
        return False
    if status not in {'completed', 'complete', 'partial', 'no_evidence', 'not_requested',
                      'not_needed', 'capacity_reached', 'not_reviewable', 'unavailable',
                      'budget_exhausted', 'input_budget', 'input_unavailable', 'failed'}:
        return False
    if not model_receipts_confirmed(receipt):
        return False
    if stage == 'gap_followup':
        search_status = getattr(result, 'search_status', None)
        if search_status not in {'completed', 'partial', 'not_requested', 'not_started', 'no_gaps'}:
            return False
        if any(item.get('stage') == 'gap' and item.get('status') not in {'completed', 'partial'}
               and item.get('attempted') for item in search_receipts):
            return False
    if stage == 'support_review':
        for item in getattr(result, 'isolated_records', []):
            if item.get('status') not in {'completed', 'not_started', 'failed'}:
                return False
            if item.get('status') in {'completed', 'failed'} and (type(item.get('reported_tokens')) is not int or item['reported_tokens'] < 0):
                return False
    return True


@dataclass(frozen=True)
class RestoredStageState:
    state: StageState
    request: Any
    analysis: Any
    planning_result: Any
    claims: list[dict[str, Any]]
    stage_results: dict[str, Any]
    coordinator: CoordinatorState


def restore_stage_state(*, snapshot: RecoverySnapshot, inputs: dict[str, Any],
                        topic: ResearchTopic) -> RestoredStageState:
    """Decode a committed stage boundary from server-owned state."""
    from .research_analysis import AnalysisResult
    from .research_coordinator import PlanningResult
    from .research_runs import DOCUMENT_OVERVIEW_QUESTION, ResearchRunRequest
    from .research_counterpoint_stage import CounterpointResult
    from .research_gap_stage import GapStageResult
    from .research_review_stage import ReviewStageResult
    try:
        state = StageState.model_validate(snapshot.plan)
        analysis_data = AnalysisSnapshot.model_validate(snapshot.analysis)
        request = ResearchRunRequest.model_validate(state.request)
        original = ResearchRunRequest.model_validate({**inputs['budget'],
            **({'question_override': request.question_override} if request.question_override is not None else {})})
        planning = PlanningResult(**state.planning_result)
        if snapshot.schema_version == '1':
            if state.stage_results or state.coordinator:
                raise ValueError('Legacy checkpoint contains incompatible stage state.')
            prior = {item.get('stage') for item in state.workflow_receipts
                     if item.get('status') == 'selected' and item.get('stage') in _FOUNDATION_STAGES}
            prior.update({'planning', 'analysis'})
            coordinator = CoordinatorState(attempted=sorted(prior), outcomes={}, omissions={}, actions_used=len(prior))
        elif snapshot.schema_version == '2':
            coordinator = CoordinatorState.model_validate(state.coordinator)
        else:
            raise ValueError('Unknown checkpoint schema.')
        stage_types = {'counterpoint': CounterpointResult, 'gap_followup': GapStageResult,
                       'support_review': ReviewStageResult}
        results: dict[str, Any] = {}
        for stage, raw in state.stage_results.items():
            cls = stage_types.get(stage)
            if cls is None:
                raise ValueError('Incomplete stage output.')
            stage_fields = fields(cls)
            required = {item.name for item in stage_fields if item.default is MISSING and item.default_factory is MISSING}
            if not required.issubset(raw) or not set(raw).issubset({item.name for item in stage_fields}):
                raise ValueError('Incomplete stage output.')
            data = deepcopy(raw)
            if 'analysis' in data:
                data['analysis'] = AnalysisResult(**AnalysisSnapshot.model_validate(data['analysis']).model_dump())
            results[stage] = cls(**data)
        attempted = set(coordinator.attempted)
        if (len(attempted) != len(coordinator.attempted)
                or not attempted.issubset(_FOUNDATION_STAGES | _FOLLOWUP_STAGES)
                or not _FOUNDATION_STAGES.intersection({'planning', 'analysis'}).issubset(attempted)
                or not attempted.issubset(set(snapshot.completed_stages))
                or set(results) != attempted.intersection(_FOLLOWUP_STAGES)
                or not set(coordinator.outcomes).issubset(attempted)
                or set(coordinator.omissions).intersection(attempted)
                or coordinator.actions_used < len(attempted)):
            raise ValueError('Inconsistent coordination state.')
        expected_question = request.question_override or topic.request.question or DOCUMENT_OVERVIEW_QUESTION
        expected_origin = 'user' if request.question_override or topic.request.question else 'document_overview_default'
        scope_hash = sha256(topic.request.scope.encode('utf-8')).hexdigest() if topic.request.scope else None
        as_of = topic.request.as_of.isoformat() if topic.request.as_of else None
        if (request != original or state.effective_question != expected_question or state.question_origin != expected_origin
                or sha256(expected_question.encode('utf-8')).hexdigest() != inputs.get('question_sha256')
                or scope_hash != inputs.get('scope_sha256') or as_of != inputs.get('as_of')
                or list(planning.steps) != snapshot.planning_steps or state.generation_status != 'completed'
                or state.unavailable_reason is not None or state.base_prompt_text is None
                or not {'planning', 'source_discovery', 'source_retrieval', 'analysis'}.issubset(snapshot.completed_stages)
                or snapshot.next_stage not in {'counterpoint', 'gap_followup', 'support_review', 'delivery_assessment'}
                or not set(analysis_data.source_ids).issubset(set(snapshot.source_ids))):
            raise ValueError('Snapshot is inconsistent with the authorized research.')
        return RestoredStageState(state, request, AnalysisResult(**analysis_data.model_dump()),
                                  planning, deepcopy(snapshot.claims), results, coordinator)
    except (ValueError, TypeError, KeyError) as error:
        raise ApiProblem(409, 'lifecycle_conflict', 'The committed stage cannot be restored safely.') from error


@dataclass(frozen=True)
class Continuation:
    snapshot: RecoverySnapshot
    remaining_seconds: float
    cost_limited: bool


def prepare_continuation(*, topic: ResearchTopic, operation: dict[str, Any], owner_id: str,
                         operation_id: str, inputs: dict[str, Any], snapshot: RecoverySnapshot,
                         source_fingerprints: list[dict[str, Any]], now: datetime) -> Continuation:
    """Authorize a server-owned snapshot without resetting limits or permissions.

    The caller must read snapshot through RecoveryJournal.read(), under the
    research-operation admission lock, and recheck the topic version at commit.
    """
    prepare_recovery(topic=topic, operation=operation, inputs=inputs,
                     owner_id=owner_id, operation_id=operation_id)
    current = topic.versions[-1]
    if current.version_id != snapshot.checkpoint_version_id:
        raise ApiProblem(409, 'lifecycle_conflict', 'Research changed since the continuation snapshot.')
    if snapshot.schema_version not in {'1', '2'} or snapshot.next_stage not in {
        'planning', 'source_discovery', 'source_retrieval', 'analysis', 'counterpoint',
        'gap_followup', 'support_review', 'delivery_assessment',
    } or snapshot.next_stage in snapshot.completed_stages:
        raise ApiProblem(409, 'lifecycle_conflict', 'The saved stage cannot be continued safely.')
    original_sources = inputs.get('sources')
    # PostgreSQL document serialization drops null-valued mapping fields;
    # compare the same canonical representation for both storage backends.
    normalized_original = _drop_legacy_null_fields(original_sources)
    normalized_snapshot = _drop_legacy_null_fields(snapshot.source_fingerprints)
    normalized_current = _drop_legacy_null_fields(source_fingerprints)
    original_by_id = {item.get('source_id'): item for item in normalized_original if isinstance(item, dict)} if isinstance(normalized_original, list) else {}
    snapshot_by_id = {item.get('source_id'): item for item in normalized_snapshot}
    if (not isinstance(original_sources, list) or len(original_by_id) != len(original_sources)
            or len(snapshot_by_id) != len(snapshot.source_fingerprints)
            or any(snapshot_by_id.get(key) != value for key, value in original_by_id.items())
            or normalized_current != normalized_snapshot
            or snapshot.source_ids != [item.get('source_id') for item in snapshot.source_fingerprints]
            or not set(snapshot.source_ids).issubset(set(current.source_ids))):
        raise ApiProblem(409, 'lifecycle_conflict', 'Source content or permissions changed since this run.')
    budget = snapshot.budget
    original_budget = inputs.get('budget', {})
    if (not isinstance(original_budget, dict)
            or budget.max_model_calls != original_budget.get('max_model_calls')
            or budget.max_search_calls != original_budget.get('max_search_calls')
            or budget.max_total_tokens != original_budget.get('max_total_tokens')
            or budget.model_calls > budget.max_model_calls or budget.search_calls > budget.max_search_calls
            or budget.accounted_tokens > budget.max_total_tokens
            or (budget.reported_tokens is not None and budget.reported_tokens > budget.accounted_tokens)
            or budget.deadline_at.utcoffset() is None or now.utcoffset() is None):
        raise ApiProblem(409, 'lifecycle_conflict', 'The saved execution budget cannot be verified.')
    try:
        captured = datetime.fromisoformat(str(inputs['captured_at']))
        original_seconds = original_budget['max_elapsed_seconds']
        expected_deadline = captured.timestamp() + int(original_seconds)
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ApiProblem(409, 'lifecycle_conflict', 'The saved deadline cannot be verified.') from error
    if captured.utcoffset() is None or budget.deadline_at.timestamp() > expected_deadline:
        raise ApiProblem(409, 'lifecycle_conflict', 'The saved deadline exceeds the original budget.')
    remaining = (budget.deadline_at.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise ApiProblem(409, 'lifecycle_conflict', 'The original research time budget is exhausted.')
    return Continuation(snapshot, remaining, original_budget.get('max_cost_usd') is not None)


def continuation_context(continuation: Continuation, *,
                         ensure_running: Callable[[], None], cancel_event: Callable[[], Any],
                         reserve: Callable[[str, int], None], settle: Callable[[str, int | None], None],
                         reserve_search: Callable[[], None]) -> Any:
    """Restore counters into the existing execution context; never reset budget."""
    from .research_budget import ResearchExecutionBudget
    from .research_coordinator import ResearchExecutionContext
    saved = continuation.snapshot.budget
    remaining = (saved.deadline_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise ApiProblem(409, 'lifecycle_conflict', 'The original research time budget is exhausted.')
    return ResearchExecutionContext(
        budget=ResearchExecutionBudget(saved.max_model_calls, saved.max_search_calls,
                                       saved.max_total_tokens, monotonic() + remaining),
        cost_limited=continuation.cost_limited, ensure_running=ensure_running, cancel_event=cancel_event,
        reserve=reserve, settle=settle, reserve_search=reserve_search,
        calls=saved.model_calls, usage=saved.reported_tokens,
        accounted_tokens=saved.accounted_tokens, search_calls=saved.search_calls)


def recovery_status(*, topic: ResearchTopic, operation: dict[str, Any], inputs: dict[str, Any] | None,
                    owner_id: str, operation_id: str, now: datetime,
                    source_fingerprints: list[dict[str, Any]] | None,
                    provider_matches: bool) -> dict[str, Any]:
    """Report a read-only eligibility estimate; admission rechecks atomically."""
    if (topic.owner_id != owner_id or operation.get('owner_subject') != f'account:{owner_id}'
            or operation.get('operation_id') != operation_id
            or operation.get('endpoint') != f'/v1/research/topics/{topic.topic_id}/runs'):
        raise ApiProblem(404, 'not_found', 'Research run not found.')
    latest = topic.versions[-1]
    same_checkpoint = (latest.created_by == 'agent' and latest.review == 'draft'
                       and latest.content.run.get('operation_id') == operation_id
                       and bool(latest.content.run.get('checkpoint')))
    status = operation.get('status')
    result: dict[str, Any] = {'can_continue': False, 'can_deliver': False, 'reason': None,
                              'checkpoint': latest.content.run.get('checkpoint') if same_checkpoint else None,
                              'version_id': latest.version_id if same_checkpoint else None}
    if status in {'pending', 'running'}:
        result['reason'] = 'running'
        return result
    if status not in {'failed', 'cancelled'}:
        result['reason'] = 'unavailable'
        return result
    if not inputs or inputs.get('owner_id') != owner_id or inputs.get('topic_id') != topic.topic_id or inputs.get('operation_id') != operation_id:
        result['reason'] = 'no_checkpoint'
        return result
    if not same_checkpoint:
        result['reason'] = 'changed'
        return result
    result['can_deliver'] = True
    if inputs.get('recovery_pending'):
        result['reason'] = 'unknown_external'
        return result
    raw = inputs.get('recovery_snapshot')
    if not raw:
        result['reason'] = 'no_checkpoint'
        return result
    if inputs.get('recovery_continuation_operation_id'):
        result['reason'] = 'already_used'
        return result
    try:
        snapshot = RecoverySnapshot.model_validate(raw)
        if snapshot.checkpoint_version_id != latest.version_id:
            result['reason'] = 'changed'
            return result
        if snapshot.budget.deadline_at.utcoffset() is None or now.utcoffset() is None:
            result['reason'] = 'unavailable'
            return result
        if snapshot.budget.deadline_at <= now:
            result['reason'] = 'budget_exhausted'
            return result
        if not provider_matches or source_fingerprints is None:
            result['reason'] = 'unavailable'
            return result
        if _drop_legacy_null_fields(source_fingerprints) != _drop_legacy_null_fields(snapshot.source_fingerprints):
            result['reason'] = 'changed'
            return result
        prepare_continuation(topic=topic, operation=operation, owner_id=owner_id, operation_id=operation_id,
                             inputs=inputs, snapshot=snapshot, source_fingerprints=source_fingerprints, now=now)
        restore_stage_state(snapshot=snapshot, inputs=inputs, topic=topic)
    except (ApiProblem, ValueError, TypeError):
        result['reason'] = 'unavailable'
        return result
    result['can_continue'] = True
    return result


class RecoveryJournal:
    """Persist uncertain external work before it starts, under an operation lock.

    A stage checkpoint must be committed to the topic before commit_checkpoint.
    A crash between those writes leaves a conservative unresolved marker.
    """
    def __init__(self, repository: JsonDocumentRepository, operation_id: str, owner_id: str) -> None:
        self.repository, self.operation_id, self.owner_id = repository, operation_id, owner_id

    def _update(self, change: Any) -> Any:
        def locked() -> tuple[Any, ApiProblem | None]:
            try:
                record = self.repository.get(self.operation_id)
                if not record or record.get('owner_id') != self.owner_id:
                    raise ApiProblem(404, 'not_found', 'Research run inputs not found.')
                record = deepcopy(record)
                result = change(record)
                self.repository.put(self.operation_id, record)
                return result, None
            except ApiProblem as error:
                return None, error
        result, error = self.repository.with_advisory_lock(f'research-recovery:{self.operation_id}', locked)
        if error is not None:
            raise error
        return result

    def begin_external(self, *, call_id: str, kind: str, stage: str) -> None:
        """Mark a call as unresolved before admission or provider invocation."""
        def change(record: dict[str, Any]) -> None:
            pending = record.setdefault('recovery_pending', [])
            if any(item.get('call_id') == call_id for item in pending):
                raise ApiProblem(409, 'lifecycle_conflict', 'External call is already unresolved.')
            pending.append({'call_id': call_id, 'kind': kind, 'stage': stage})
        self._update(change)

    def claim_continuation(self, new_operation_id: str) -> None:
        """Consume a snapshot once, inside the new operation's admission transaction.

        Even if that operation later fails before producing a new checkpoint,
        the old snapshot cannot silently authorize another paid attempt.
        """
        def change(record: dict[str, Any]) -> None:
            if record.get('recovery_pending') or not record.get('recovery_snapshot'):
                raise ApiProblem(409, 'lifecycle_conflict', 'This checkpoint cannot be continued safely.')
            existing = record.get('recovery_continuation_operation_id')
            if existing and existing != new_operation_id:
                raise ApiProblem(409, 'lifecycle_conflict', 'This checkpoint was already used for continuation.')
            record['recovery_continuation_operation_id'] = new_operation_id
        self._update(change)

    def commit_checkpoint(self, snapshot: RecoverySnapshot, *, committed_call_ids: list[str]) -> None:
        """Clear only calls whose outputs and settlements are in this checkpoint.

        The caller must leave unknown outcomes out of committed_call_ids. Other
        unresolved calls continue to block automatic continuation.
        """
        def change(record: dict[str, Any]) -> None:
            pending = record.get('recovery_pending', [])
            known = {item.get('call_id') for item in pending}
            if len(committed_call_ids) != len(set(committed_call_ids)) or not set(committed_call_ids).issubset(known):
                raise ApiProblem(409, 'lifecycle_conflict', 'Checkpoint call receipts do not match the journal.')
            record['recovery_snapshot'] = snapshot.model_dump(mode='json')
            record['recovery_pending'] = [item for item in pending if item.get('call_id') not in committed_call_ids]
        self._update(change)

    def commit_terminal_stage_outcomes(self, *, checkpoint_version_id: str,
                                       committed_call_ids: list[str], receipts: list[dict[str, Any]]) -> None:
        """Record known failed initial analysis without creating a resumable snapshot.

        The caller has already persisted the partial checkpoint. This path may
        only be used before a resumable snapshot exists, so clearing a known
        call cannot make an earlier snapshot replay that call.
        """
        def change(record: dict[str, Any]) -> None:
            pending = record.get('recovery_pending', [])
            known = {item.get('call_id') for item in pending}
            if (record.get('recovery_snapshot') is not None or not checkpoint_version_id or
                    len(committed_call_ids) != len(set(committed_call_ids)) or
                    not set(committed_call_ids).issubset(known)):
                raise ApiProblem(409, 'lifecycle_conflict', 'Terminal stage receipts do not match the journal.')
            record['recovery_terminal_checkpoint_version_id'] = checkpoint_version_id
            record['recovery_terminal_stage_receipts'] = deepcopy(receipts)
            record['recovery_pending'] = [item for item in pending if item.get('call_id') not in committed_call_ids]
        self._update(change)

    def read(self) -> RecoverySnapshot:
        def read_locked() -> tuple[RecoverySnapshot | None, ApiProblem | None]:
            record = self.repository.get(self.operation_id)
            if not record or record.get('owner_id') != self.owner_id:
                return None, ApiProblem(404, 'not_found', 'Research run inputs not found.')
            if record.get('recovery_pending'):
                return None, ApiProblem(409, 'lifecycle_conflict', 'An external call has an unknown outcome; automatic continuation is unavailable.')
            raw = record.get('recovery_snapshot')
            if not raw:
                return None, ApiProblem(409, 'lifecycle_conflict', 'No continuation snapshot is available.')
            try:
                return RecoverySnapshot.model_validate(deepcopy(raw)), None
            except ValueError:
                return None, ApiProblem(409, 'lifecycle_conflict', 'Continuation snapshot is invalid.')
        snapshot, error = self.repository.with_advisory_lock(f'research-recovery:{self.operation_id}', read_locked)
        if error is not None:
            raise error
        assert snapshot is not None
        return snapshot


@dataclass(frozen=True)
class RecoveryPlan:
    parent_version_id: str
    source_ids: list[str]
    content: ResearchContent


def prepare_recovery(*, topic: ResearchTopic, operation: dict[str, Any],
                     inputs: dict[str, Any] | None, owner_id: str,
                     operation_id: str) -> RecoveryPlan:
    """Validate ownership and version, then preserve the last committed output.

    Admission and final write must independently recheck the returned parent.
    Old inputs/checkpoints without sufficient provenance are not recoverable.
    """
    endpoint = f'/v1/research/topics/{topic.topic_id}/runs'
    if (topic.owner_id != owner_id or operation.get('owner_subject') != f'account:{owner_id}'
            or operation.get('operation_id') != operation_id or operation.get('endpoint') != endpoint
            or not inputs or inputs.get('owner_id') != owner_id or inputs.get('topic_id') != topic.topic_id
            or inputs.get('operation_id') != operation_id):
        raise ApiProblem(404, 'not_found', 'Research run not found.')
    if operation.get('status') not in {'failed', 'cancelled'}:
        raise ApiProblem(409, 'lifecycle_conflict', 'Only a failed or cancelled run can be recovered.')
    latest = topic.versions[-1]
    run = latest.content.run
    if (latest.created_by != 'agent' or latest.review != 'draft'
            or run.get('operation_id') != operation_id or not run.get('checkpoint')):
        raise ApiProblem(409, 'lifecycle_conflict',
                         'No current recoverable checkpoint exists; the research may have changed.')
    content = latest.content.model_copy(deep=True)
    content.run = {**content.run, 'status': 'partial', 'recovery_of_operation_id': operation_id,
                   'recovery_checkpoint': run['checkpoint'], 'recovery_mode': 'committed_artifacts_only',
                   'external_call_status_after_checkpoint': 'unknown',
                   'recovery_external_calls': 0}
    note = ('Recovered committed results only. Work after the checkpoint and any external call outcome '
            'are unknown; no external call was repeated.')
    if note not in content.limitations:
        if len(content.limitations) >= 500:
            raise ApiProblem(409, 'lifecycle_conflict', 'The checkpoint has no room for recovery limitations.')
        content.limitations.append(note)
    return RecoveryPlan(latest.version_id, list(latest.source_ids), content)


def recovered_content(plan: RecoveryPlan, operation_id: str) -> ResearchContent:
    """Bind the preserved artifact to the new operation, without mutating it."""
    content = plan.content.model_copy(deep=True)
    content.run = {**content.run, 'operation_id': operation_id}
    return content
