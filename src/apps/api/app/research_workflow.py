"""Run-scoped orchestration boundary for research stages.

Stage receipts record execution, not factual correctness. Stage handlers are
supplied explicitly and retain their own permissions, budgets and checkpoints.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, TypeVar

from .research_coordinator import ResearchExecutionContext

T = TypeVar('T')


@dataclass(frozen=True)
class StageReceipt:
    stage: str
    status: str
    model_calls: int | None
    search_calls: int | None
    elapsed_ms: int | None


@dataclass
class ResearchWorkflow:
    """Coordinate explicit stage handlers through one run context."""
    context: ResearchExecutionContext
    receipts: list[StageReceipt] = field(default_factory=list)

    def run(self, stage: str, handler: Callable[[], T], status: Callable[[T], str]) -> T:
        self.context.ensure_running()
        before_models, before_searches, started = self.context.calls, self.context.search_calls, self.context.now()
        outcome = 'failed'
        try:
            result = handler()
            outcome = status(result)
            return result
        finally:
            self.receipts.append(StageReceipt(stage=stage, status=outcome,
                model_calls=self.context.calls - before_models,
                search_calls=self.context.search_calls - before_searches,
                elapsed_ms=max(0, int((self.context.now() - started) * 1000))))

    def record(self, stage: str, status: str) -> None:
        """Record an externally coordinated stage without inventing its timing."""
        self.receipts.append(StageReceipt(stage=stage, status=status,
                                          model_calls=None, search_calls=None, elapsed_ms=None))

    def snapshot(self) -> list[dict[str, Any]]:
        return [asdict(item) for item in self.receipts]
