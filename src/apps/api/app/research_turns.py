"""Turn bookkeeping for multi-turn research.

A topic accumulates structured research state across turns. This module builds
the prior-state summary handed to the next agent run and merges one finished
turn into the version it started from. It never edits an earlier turn's items.
"""
from __future__ import annotations

from typing import Any

from .research_charts import ResearchChart
from .research_topics import (ResearchCalculation, ResearchClaim, ResearchContent, ResearchFact, ResearchGap,
                              ResearchTurn, ResearchVersion)


def prior_research_input(version: ResearchVersion) -> dict | None:
    """Return the prior state for the next run, or None when no turn exists yet."""
    content = version.content
    if not content.turns:
        return None
    return {
        'facts': [item.model_dump(mode='json') for item in content.facts],
        'calculations': [item.model_dump(mode='json') for item in content.calculations],
        'claims': [item.model_dump(mode='json') for item in content.claims],
        'charts': [
            {
                'chart_id': item.chart_id,
                'chart_type': item.chart_type,
                'title': item.title,
                'replaces_chart_id': item.replaces_chart_id,
            }
            for item in content.charts
        ],
        'gaps': [item.model_dump(mode='json') for item in content.gaps],
        'read_ranges': [item.model_dump(mode='json') for turn in content.turns for item in turn.read_ranges],
        'source_hashes': {row['source_id']: row.get('content_sha256') for row in content.sources
                          if isinstance(row, dict) and isinstance(row.get('source_id'), str)},
        'review': version.review, 'note': version.note, 'summary': content.summary,
    }


def _union(*lists: list[str]) -> list[str]:
    return list(dict.fromkeys(item for values in lists for item in values))


def merge_turn(parent: ResearchContent, turn: ResearchTurn, *, claims: list[dict], facts: list[dict],
               calculations: list[dict], sources: list[dict], source_candidates: list[dict],
               charts: list[dict] | None = None) -> ResearchContent:
    """Return the next version's content: the parent plus this turn, never altering earlier items.

    If the parent's last turn has the same turn_id (a continuation or delivery of a
    partially saved turn), that turn is replaced in place and items already saved are
    not duplicated. Run accounting and events are cumulative across a continuation, so
    usage keeps the larger value and events keep the longer sequence.
    """
    content = parent.model_copy(deep=True)
    turn = turn.model_copy(deep=True)
    tid = turn.turn_id
    existing_claims = {item.claim_id for item in content.claims}
    existing_facts = {item.fact_id for item in content.facts}
    existing_calcs = {item.calculation_id for item in content.calculations}
    existing_charts = {item.chart_id for item in content.charts}
    content.claims.extend(ResearchClaim.model_validate({**item, 'turn_id': tid}) for item in claims
                          if item['claim_id'] not in existing_claims)
    content.facts.extend(ResearchFact.model_validate({**item, 'turn_id': tid}) for item in facts
                         if item['fact_id'] not in existing_facts)
    content.calculations.extend(ResearchCalculation.model_validate({**item, 'turn_id': tid}) for item in calculations
                                if item['calculation_id'] not in existing_calcs)
    content.charts.extend(ResearchChart.model_validate({**item, 'turn_id': tid}) for item in (charts or ())
                          if item['chart_id'] not in existing_charts)
    if content.turns and content.turns[-1].turn_id == tid:
        old = content.turns[-1]
        turn.index, turn.question, turn.question_origin = old.index, old.question, old.question_origin
        turn.focus_citation, turn.parent_version_id, turn.started_at = old.focus_citation, old.parent_version_id, old.started_at
        turn.operation_ids = _union(old.operation_ids, turn.operation_ids)
        turn.claim_ids = _union(old.claim_ids, turn.claim_ids)
        turn.fact_ids = _union(old.fact_ids, turn.fact_ids)
        turn.calculation_ids = _union(old.calculation_ids, turn.calculation_ids)
        turn.chart_ids = _union(old.chart_ids, turn.chart_ids)
        turn.usage = turn.usage.model_copy(update={key: max(getattr(old.usage, key), getattr(turn.usage, key))
                                                   for key in ('model_calls', 'search_calls', 'total_tokens')})
        turn.events = turn.events if turn.events[:len(old.events)] == old.events else [*old.events, *turn.events]
        content.turns[-1] = turn
    else:
        content.turns.append(turn)
    known = {row.get('source_id') for row in content.sources if isinstance(row, dict)}
    content.sources.extend(row for row in sources if row.get('source_id') not in known)
    content.source_candidates = source_candidates[:10]
    content.gaps = [item.model_copy(deep=True) for item in turn.gaps]
    return content


def gap_rows(items: list[dict[str, Any]]) -> list[ResearchGap]:
    return [ResearchGap(code=item['code'], message=item['message']) for item in items]
