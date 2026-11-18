"""Common input admission counts source text, system and message framing."""
from app.research_coordinator import (
    RESEARCH_INPUT_ENVELOPE_BYTES,
    research_prompt_budget,
    research_prompt_fits,
)
from app.research_analysis import SYSTEM_PROMPT
from app.research_planning import PLAN_SYSTEM_PROMPT
from app.research_verification import REVIEW_SYSTEM_PROMPT


def test_prompt_byte_budget_is_shared_and_strict_at_the_boundary():
    for system in (SYSTEM_PROMPT, PLAN_SYSTEM_PROMPT, REVIEW_SYSTEM_PROMPT):
        limit = len(system.encode('utf-8')) + RESEARCH_INPUT_ENVELOPE_BYTES + 9
        assert research_prompt_budget(limit, system) == 9
        assert research_prompt_fits('abc中文', system, limit)
        assert not research_prompt_fits('abc中文x', system, limit)
        assert not research_prompt_fits('x', system, len(system.encode('utf-8')) + RESEARCH_INPUT_ENVELOPE_BYTES)


def test_prompt_budget_fails_closed_for_invalid_and_undersized_limits():
    for limit in (0, -1, True):
        assert research_prompt_budget(limit, SYSTEM_PROMPT) == 0
        assert not research_prompt_fits('x', SYSTEM_PROMPT, limit)
        assert not research_prompt_fits('', SYSTEM_PROMPT, limit)
