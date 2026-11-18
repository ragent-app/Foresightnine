"""Shared admission rules for a bounded research execution.

This is a local policy, not a provider billing ledger or a reservation. Callers
must still reserve external calls atomically and record unknown actual costs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ModelBudgetReason = Literal['call_budget', 'token_budget', 'time_budget', 'provider_limit']
SearchBudgetReason = Literal['search_budget', 'time_budget']


@dataclass(frozen=True)
class ResearchExecutionBudget:
    max_model_calls: int | None
    max_search_calls: int | None
    max_total_tokens: int | None
    deadline: float

    def model_exhaustion(self, *, calls: int, usage: int | None, per_call: int,
                         now: float, required_calls: int = 1) -> ModelBudgetReason | None:
        if per_call <= 0 or required_calls <= 0 or calls < 0 or (usage is not None and usage < 0):
            return 'provider_limit'
        if self.max_model_calls is not None and calls + required_calls > self.max_model_calls:
            return 'call_budget'
        charged = (usage if usage is not None else calls * per_call) if calls else 0
        if self.max_total_tokens is not None and charged + required_calls * per_call > self.max_total_tokens:
            return 'token_budget'
        if now >= self.deadline:
            return 'time_budget'
        return None

    def search_exhaustion(self, *, calls: int, now: float) -> SearchBudgetReason | None:
        if self.max_search_calls is not None and calls >= self.max_search_calls:
            return 'search_budget'
        if now >= self.deadline:
            return 'time_budget'
        return None
