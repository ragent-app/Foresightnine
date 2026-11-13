"""Shared publication of research checkpoints within an operation lifecycle."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .operations import ApiProblem
from .research_analysis import AnalysisResult
from .research_artifacts import assemble_research_content
from .research_coordinator import ResearchExecutionContext
from .research_lifecycle import ResearchVersionChain
from .research_topics import TopicNotFound, TopicVersionConflict


@dataclass
class ResearchCheckpointPublisher:
    operation_id: str
    versions: ResearchVersionChain
    context: ResearchExecutionContext
    candidates: list[dict[str, Any]]
    sources: Callable[[list[str], object], list[dict[str, Any]]]
    core: Callable[[AnalysisResult], dict[str, Any]]
    metadata: Callable[[AnalysisResult], dict[str, Any]]
    admit: Callable[[Callable[[], None]], bool]

    def publish(self, *, checkpoint: str, analysis: AnalysisResult, claims: list[dict[str, Any]],
                limitations: list[str], extra: dict[str, Any] | None = None) -> None:
        """Save one immutable, account-owned partial result if the run is active."""
        def save() -> None:
            try:
                parent = self.versions.current()
                content = assemble_research_content(summary=parent.content.summary,
                    source_candidates=self.candidates, sources=self.sources(analysis.source_ids, analysis.segments),
                    analysis=analysis, claims=claims, limitations=limitations,
                    run={'operation_id': self.operation_id, 'status': 'partial', 'checkpoint': checkpoint,
                         **self.core(analysis), **self.metadata(analysis), **(extra or {})})
                self.versions.save(parent, content)
            except (TopicNotFound, TopicVersionConflict) as error:
                raise ApiProblem(409, 'lifecycle_conflict', 'Research changed before the checkpoint was saved.') from error
        if not self.admit(save):
            self.context.ensure_running()
            raise ApiProblem(409, 'lifecycle_conflict', 'Research ended before the checkpoint was saved.')
