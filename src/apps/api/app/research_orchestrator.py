"""Bounded, task-directed selection over the existing research stages.

Suggestions from planning remain questions, never permissions or executable code.
The run context and stage handlers retain authority over every external call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Any

from .research_coordinator import ResearchExecutionContext
from .research_tools import get_research_tool
from .research_workflow import ResearchWorkflow


@dataclass(frozen=True)
class CoordinationObservation:
    source_count: int
    candidate_count: int
    has_analysis: bool
    claim_count: int
    has_counterpoint: bool
    has_gaps: bool
    generation_failed: bool
    analysis_pending: bool = False


@dataclass
class ResearchOrchestrator:
    context: ResearchExecutionContext
    workflow: ResearchWorkflow
    request: Any
    question: bool
    observe: Callable[[], CoordinationObservation]
    handlers: dict[str, Callable[[], None]]
    suggested_actions: Callable[[], tuple[str, ...]] = lambda: ()
    before_action: Callable[[str], None] = lambda _action: None
    after_action: Callable[[str, 'ResearchOrchestrator'], None] = lambda _action, _coordinator: None
    attempted: set[str] = field(default_factory=set)
    outcomes: dict[str, str] = field(default_factory=dict)
    omissions: dict[str, str] = field(default_factory=dict)
    max_actions: int | None = 7
    repeat_gap: bool = False
    can_repeat_gap: Callable[[], bool] = lambda: False
    actions_used: int = 0
    stop_reason: str | None = None
    acquisition_enabled: bool = False
    acquisition_due: bool = False
    acquisition_action: Callable[[], str | None] = lambda: None

    def _omit(self, name: str, status: str, reason: str) -> None:
        self.omissions[name] = status
        self.workflow.record_decision(name, status, reason)

    def omission_status(self, name: str, requested: bool) -> str:
        return self.omissions.get(name, 'unavailable' if requested else 'not_requested')

    def _choose(self, state: CoordinationObservation) -> tuple[str | None, str]:
        if 'planning' not in self.attempted:
            # The legacy planning handler supplies the explicit not_requested result.
            return 'planning', 'Prepare the optional plan within the authorized task.'
        if state.analysis_pending:
            return 'analysis', 'Continue the saved analysis page.'
        if self.acquisition_enabled:
            if self.acquisition_due:
                return 'acquisition_decision', 'Choose the next evidence action from the current state.'
            action = self.acquisition_action()
            if action == 'next_page':
                return 'acquisition_decision', 'Inspect the next omitted context page.'
            if action == 'discover':
                return 'source_discovery', 'Discover leads for the selected requirement.'
            if action == 'retrieve':
                return 'source_retrieval', 'Retrieve selected authorized candidates.'
            if action == 'structured':
                return 'source_retrieval', 'Retrieve an authorized structured observation.'
            if action == 'read':
                return ('gap_followup' if state.has_analysis else 'analysis'), 'Analyze the selected evidence.'
            if action == 'review':
                return 'support_review', 'Review current evidence links.'
            if action == 'counterpoint':
                return 'counterpoint', 'Investigate the counterpoint for the selected requirement.'
            return None, 'Acquisition stopped with unresolved or unavailable evidence.'
        if 'source_discovery' not in self.attempted and 'source_discovery' not in self.omissions:
            if ((not state.source_count or self.request.auto_fetch_official) and self.question
                    and self.request.discover_sources is not False and self.request.max_search_calls != 0):
                return 'source_discovery', 'Find leads for missing or explicitly requested sources.'
            self._omit('source_discovery', 'not_requested', 'Existing sources or task permissions do not require discovery.')
        if 'source_retrieval' not in self.attempted and 'source_retrieval' not in self.omissions:
            if self.request.auto_fetch_official:
                return 'source_retrieval', 'Retrieve eligible leads only under the explicit source permission.'
            self._omit('source_retrieval', 'not_requested' if not self.request.auto_fetch_official else 'not_needed',
                       'No authorized eligible candidate is available for retrieval.')
        if not state.has_analysis:
            return 'analysis', 'Produce the initial research or an explicit partial result.'
        candidates = [
            ('counterpoint', self.request.counterpoint_followup, not state.has_counterpoint),
            ('gap_followup', self.request.gap_followup, state.has_gaps),
            ('support_review', self.request.support_review, bool(state.claim_count)),
        ]
        suggestions = self.suggested_actions()
        candidates.sort(key=lambda item: (item[0] == 'support_review',
            suggestions.index(item[0]) if item[0] in suggestions else len(suggestions)))
        for name, requested, needed in candidates:
            if (name in self.attempted and not (name == 'gap_followup' and self.repeat_gap)) or name in self.omissions:
                continue
            if not requested:
                self._omit(name, 'not_requested', 'The task did not authorize this optional action.')
            elif state.generation_failed or (not state.claim_count and name != 'gap_followup'):
                self._omit(name, 'unavailable', 'The preceding analysis did not produce usable claims.')
            elif not needed:
                self._omit(name, 'not_needed', 'The current artifacts do not require this action.')
            elif self.context.now() >= self.context.budget.deadline:
                self._omit(name, 'budget_exhausted', 'The run time budget is exhausted.')
            elif self.context.budget.max_model_calls is not None and self.context.calls >= self.context.budget.max_model_calls:
                self._omit(name, 'budget_exhausted', 'The model call budget is exhausted.')
            elif self.context.cost_limited:
                self._omit(name, 'cost_unknown', 'The action cost cannot be bounded by the requested limit.')
            else:
                return name, 'Address an outstanding, authorized research need.'
        return None, 'No further authorized action can improve the current artifacts.'

    def run(self) -> None:
        while self.max_actions is None or self.actions_used < self.max_actions:
            self.context.ensure_running()
            action, reason = self._choose(self.observe())
            if action is None:
                state = self.observe()
                self.stop_reason = ('analysis_unavailable' if state.generation_failed else
                                    'remaining_gaps' if state.has_gaps else 'goal_or_available_steps_complete')
                break
            if action not in self.handlers or get_research_tool(action) is None:
                self._omit(action, 'unavailable', 'The capability is not registered or configured.')
                if action in {'planning', 'analysis'}:
                    self.stop_reason = 'required_capability_unavailable'
                    break
                continue
            self.workflow.record_decision(action, 'selected', reason)
            self.workflow.authorized_stages.add(action)
            if action == 'gap_followup' and self.request.gap_search:
                self.workflow.authorized_stages.add('gap_search')
            self.before_action(action)
            self.handlers[action]()
            self.attempted.add(action)
            self.actions_used += 1
            if self.acquisition_enabled:
                if action == 'planning':
                    self.acquisition_due = True
                elif action == 'acquisition_decision':
                    self.acquisition_due = False
                else:
                    self.acquisition_due = True
            # Need can change when a follow-up adds or revises artifacts.
            # Keep historical receipts but reconsider unattempted dynamic omissions.
            if action in {'analysis', 'counterpoint', 'gap_followup'}:
                self.omissions = {name: status for name, status in self.omissions.items()
                                  if status != 'not_needed' or name in self.attempted}
            self.outcomes[action] = self.workflow.receipts[-1].status
            if action == 'gap_followup':
                self.repeat_gap = self.can_repeat_gap()
            self.after_action(action, self)
            if action == 'planning' and self.suggested_actions():
                self.workflow.record_decision('plan_suggestions', 'observed', ', '.join(self.suggested_actions()))
        else:
            action, _ = self._choose(self.observe())
            state = self.observe()
            self.stop_reason = ('action_limit' if action is not None else
                                'analysis_unavailable' if state.generation_failed else
                                'remaining_gaps' if state.has_gaps else 'goal_or_available_steps_complete')
        self.workflow.record_decision('coordination', 'stopped', self.stop_reason or 'no_further_action')
