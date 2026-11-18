"""Run-scoped orchestration boundary for research stages.

Stage receipts record execution, not factual correctness. Stage handlers are
supplied explicitly and retain their own permissions, budgets and checkpoints.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

from .research_coordinator import ResearchExecutionContext
from .research_tools import execute_research_tool, get_research_tool

T = TypeVar('T')


@dataclass(frozen=True)
class StageReceipt:
    stage: str
    status: str
    model_calls_admitted: int
    search_calls_admitted: int
    elapsed_ms: int | None
    schema_version: str = '1'
    reason: str | None = None
    source_ids: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    usage: int | None = None
    source_references: tuple[dict[str, str], ...] = ()
    retryable: bool | None = None
    model_call_receipts: tuple[dict[str, Any], ...] = ()


@dataclass
class ActiveStage:
    stage: str
    start_calls: int
    start_searches: int
    started_at: float
    prior_model_call_ids: frozenset[str] = field(default_factory=frozenset)
    status: str = 'in_progress'
    reason: str | None = None
    source_ids: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    usage: int | None = None
    source_references: tuple[dict[str, str], ...] = ()
    retryable: bool | None = None


@dataclass
class ResearchWorkflow:
    """Coordinate explicit stage handlers through one run context."""
    context: ResearchExecutionContext
    receipts: list[StageReceipt] = field(default_factory=list)
    active: list[ActiveStage] = field(default_factory=list)
    authorized_stages: set[str] = field(default_factory=lambda: {'delivery_assessment'})

    @contextmanager
    def stage(self, name: str) -> Iterator[ActiveStage]:
        """Track one stage, including any checkpoint saved before it ends."""
        self.context.ensure_running()
        stage = ActiveStage(name, self.context.calls, self.context.search_calls, self.context.now(),
                            frozenset(self.context.model_call_receipts))
        self.active.append(stage)
        try:
            yield stage
        except BaseException:
            if stage.status == 'in_progress':
                stage.status = 'failed'
            raise
        finally:
            self.active.remove(stage)
            self.receipts.append(self._receipt(stage))

    def _receipt(self, stage: ActiveStage) -> StageReceipt:
        calls = tuple({'call_id': call_id, **dict(outcome)} for call_id, outcome in self.context.model_call_receipts.items()
                      if call_id not in stage.prior_model_call_ids and outcome.get('provider') != 'not_invoked')
        return StageReceipt(stage=stage.stage, status=stage.status,
            model_calls_admitted=self.context.calls - stage.start_calls,
            search_calls_admitted=self.context.search_calls - stage.start_searches,
            elapsed_ms=max(0, int((self.context.now() - stage.started_at) * 1000)),
            reason=stage.reason, source_ids=stage.source_ids, gaps=stage.gaps, usage=stage.usage,
            source_references=stage.source_references, retryable=stage.retryable, model_call_receipts=calls)

    def run(self, stage: str, handler: Callable[[], T], status: Callable[[T], str]) -> T:
        with self.stage(stage) as active:
            result = execute_research_tool(stage, handler, permitted=stage in self.authorized_stages,
                                           available=get_research_tool(stage) is not None, status=status)
            active.status = result.status
            active.reason, active.source_ids, active.gaps, active.usage = result.reason, result.source_ids, result.gaps, result.usage
            active.source_references = tuple(asdict(item) for item in result.source_references)
            active.retryable = result.retryable
            if result.status == 'failed':
                for call_id, outcome in self.context.model_call_receipts.items():
                    if (call_id not in active.prior_model_call_ids and outcome.get('provider') == 'returned'
                            and outcome.get('processing_error') is None):
                        self.context.mark_model_processing_error(call_id, 'post_response_processing_failure')
            if result.value is None:
                raise RuntimeError(f"Research capability {stage} returned no value: {result.reason}")
            return result.value

    def record_decision(self, stage: str, status: str, reason: str) -> None:
        self.receipts.append(StageReceipt(stage, status, 0, 0, 0, reason=reason))

    def snapshot(self) -> list[dict[str, Any]]:
        return [asdict(item) for item in self.receipts] + [asdict(self._receipt(item)) for item in self.active]
