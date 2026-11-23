"""Version the user's requirements independently from the evidence or model context."""
from __future__ import annotations

from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing_extensions import Annotated

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]


class UserAnchor(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sequence: int = Field(ge=0, description='0 for the original question; otherwise an exact user instruction sequence.')
    excerpt: ShortText


class RequirementProposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: UserAnchor
    deliverable: ShortText
    evidence_needed: ShortText
    priority: Literal['core', 'optional'] = 'core'
    user_conditions: list[UserAnchor] = Field(default_factory=list, max_length=16)
    assumptions: list[ShortText] = Field(default_factory=list, max_length=16)


class PlannedRequirement(RequirementProposal):
    requirement_id: str
    instruction_sequence: int = Field(ge=0)
    revision: int = Field(ge=1)


class RequirementPlanInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    requirements: list[RequirementProposal] = Field(min_length=1, max_length=32)


def save_requirement_plan(question: str, state: dict, request: RequirementPlanInput) -> list[dict]:
    messages = {row['sequence']: row['text'] for row in state.get('user_instructions', [])}
    messages[0] = question
    prior = state.get('requirement_plan', [])
    revision = max((row['revision'] for row in prior), default=0) + 1
    rows = []
    ids = set()
    for proposal in request.requirements:
        for anchor in [proposal.source, *proposal.user_conditions]:
            if anchor.sequence not in messages or anchor.excerpt not in messages[anchor.sequence]:
                raise ValueError('A user condition or requested deliverable must quote the corresponding user message exactly.')
        identity = f'{proposal.source.sequence}\0{proposal.source.excerpt}\0{proposal.deliverable}'
        rid = 'goal-' + sha256(identity.encode()).hexdigest()[:24]
        if rid in ids:
            raise ValueError('Each planned deliverable must be distinct.')
        ids.add(rid)
        rows.append(PlannedRequirement(**proposal.model_dump(), requirement_id=rid, revision=revision,
                                      instruction_sequence=max(messages)).model_dump())
    if prior and [dict(row, revision=revision) for row in prior] == rows:
        return prior
    if prior:
        state.setdefault('requirement_plan_history', []).append(prior)
    state['requirement_plan'] = rows
    return rows
