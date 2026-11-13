"""Run-scoped orchestration boundary for research stages.

Stage receipts record execution, not factual correctness. Stage handlers are
supplied explicitly and retain their own permissions, budgets and checkpoints.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

from .research_coordinator import ResearchExecutionContext

T = TypeVar('T')


@dataclass(frozen=True)
class StageReceipt:
    stage: str
    status: str
    model_calls_admitted: int
    search_calls_admitted: int
    elapsed_ms: int | None
    schema_version: str = '1'


@dataclass
class ActiveStage:
    stage: str
    start_calls: int
    start_searches: int
    started_at: float
    status: str = 'in_progress'


@dataclass
class ResearchWorkflow:
    """Coordinate explicit stage handlers through one run context."""
    context: ResearchExecutionContext
    receipts: list[StageReceipt] = field(default_factory=list)
    active: list[ActiveStage] = field(default_factory=list)

    @contextmanager
    def stage(self, name: str) -> Iterator[ActiveStage]:
        """Track one stage, including any checkpoint saved before it ends."""
        self.context.ensure_running()
        stage = ActiveStage(name, self.context.calls, self.context.search_calls, self.context.now())
        self.active.append(stage)
        try:
            yield stage
        except BaseException:
            stage.status = 'failed'
            raise
        finally:
            self.active.remove(stage)
            self.receipts.append(self._receipt(stage))

    def _receipt(self, stage: ActiveStage) -> StageReceipt:
        return StageReceipt(stage=stage.stage, status=stage.status,
            model_calls_admitted=self.context.calls - stage.start_calls,
            search_calls_admitted=self.context.search_calls - stage.start_searches,
            elapsed_ms=max(0, int((self.context.now() - stage.started_at) * 1000)))

    def run(self, stage: str, handler: Callable[[], T], status: Callable[[T], str]) -> T:
        with self.stage(stage) as active:
            result = handler()
            active.status = status(result)
            return result

    def snapshot(self) -> list[dict[str, Any]]:
        return [asdict(item) for item in self.receipts] + [asdict(self._receipt(item)) for item in self.active]
