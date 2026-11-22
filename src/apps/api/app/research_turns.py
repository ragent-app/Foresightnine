"""Turn bookkeeping for multi-turn research.

A topic accumulates structured research state across turns. This module builds
the prior-state summary handed to the next agent run and merges one finished
turn into the version it started from. It never edits an earlier turn's items.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from pydantic import BaseModel, ValidationError

from .research_charts import ResearchChart
from .research_diagnostics import safe_exception_diagnostic
from .research_topics import (ResearchCalculation, ResearchClaim, ResearchContent, ResearchFact, ResearchGap,
                              ResearchTurn, ResearchVersion)

_logger = logging.getLogger(__name__)
_PERSISTENCE_DROPPED_GAPS: dict[str, str] = {
    'calculation_persistence_dropped': 'One or more calculations could not be persisted due to schema validation.',
    'chart_persistence_dropped': 'One or more charts could not be persisted due to schema validation.',
}


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


def _merge_isolated_items(
    *,
    raw_items: list[dict] | tuple[dict, ...] | None,
    target_list: list[Any],
    existing_ids: set[str],
    id_field: str,
    model_cls: type[BaseModel],
    turn_id: str,
    item_kind: str,
) -> tuple[set[str], bool]:
    """Validate and merge items individually so one invalid row does not drop the turn."""
    dropped_ids: set[str] = set()
    dropped_any = False
    for item in raw_items or ():
        if not isinstance(item, Mapping):
            dropped_any = True
            try:
                model_cls.model_validate(item)
            except ValidationError as error:
                _logger.warning(
                    'research_item_persistence_dropped item_kind=%s metadata=%s',
                    item_kind,
                    safe_exception_diagnostic(error),
                )
            continue
        item_id = item.get(id_field)
        if isinstance(item_id, str) and item_id in existing_ids:
            continue
        try:
            validated = model_cls.model_validate({**item, 'turn_id': turn_id})
        except ValidationError as error:
            dropped_any = True
            _logger.warning(
                'research_item_persistence_dropped item_kind=%s metadata=%s',
                item_kind,
                safe_exception_diagnostic(error),
            )
            if isinstance(item_id, str) and item_id:
                dropped_ids.add(item_id)
            continue
        existing_ids.add(getattr(validated, id_field))
        target_list.append(validated)
    return dropped_ids, dropped_any


def _append_unique_gap(gaps: list[ResearchGap], code: str) -> None:
    if not any(item.code == code for item in gaps):
        gaps.append(ResearchGap(code=code, message=_PERSISTENCE_DROPPED_GAPS[code]))


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
    dropped_calc_ids, dropped_calc_any = _merge_isolated_items(
        raw_items=calculations,
        target_list=content.calculations,
        existing_ids=existing_calcs,
        id_field='calculation_id',
        model_cls=ResearchCalculation,
        turn_id=tid,
        item_kind='calculation',
    )
    dropped_chart_ids, dropped_chart_any = _merge_isolated_items(
        raw_items=charts,
        target_list=content.charts,
        existing_ids=existing_charts,
        id_field='chart_id',
        model_cls=ResearchChart,
        turn_id=tid,
        item_kind='chart',
    )
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
    if dropped_calc_ids:
        turn.calculation_ids = [cid for cid in turn.calculation_ids if cid not in dropped_calc_ids]
    if dropped_chart_ids:
        turn.chart_ids = [cid for cid in turn.chart_ids if cid not in dropped_chart_ids]
    if dropped_calc_any:
        _append_unique_gap(turn.gaps, 'calculation_persistence_dropped')
    if dropped_chart_any:
        _append_unique_gap(turn.gaps, 'chart_persistence_dropped')
    known = {row.get('source_id') for row in content.sources if isinstance(row, dict)}
    content.sources.extend(row for row in sources if row.get('source_id') not in known)
    content.source_candidates = source_candidates[:10]
    content.gaps = [item.model_copy(deep=True) for item in turn.gaps]
    return content


def gap_rows(items: list[dict[str, Any]]) -> list[ResearchGap]:
    return [ResearchGap(code=item['code'], message=item['message']) for item in items]
