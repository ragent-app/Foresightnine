"""Version-local, high-level view of recorded research execution.

This view summarizes saved state, not live progress or the truth of findings.
Missing fields from historical versions remain unknown.
"""
from __future__ import annotations

from typing import Any


def _status(value: object, *, requested: object = True) -> str:
    if requested is False:
        return 'not_requested'
    if not isinstance(value, str):
        return 'unknown'
    if value in {'completed', 'complete', 'not_needed', 'no_gaps'}:
        return 'completed'
    if value in {'not_requested'}:
        return 'not_requested'
    if value in {'not_started', 'pending', 'in_progress'}:
        return 'pending'
    return 'attention'


def summarize_research_stages(run: dict[str, Any]) -> dict[str, Any]:
    if not run.get('operation_id'):
        return {'schema_version': '1', 'meaning': 'saved_execution_not_quality_verdict', 'stages': []}
    planning = _status(run.get('planning_status'), requested=run.get('planning_requested'))
    sources = _status(run.get('search_status'))
    analysis = _status(run.get('generation_status'))
    source_processing = run.get('source_processing')
    if run.get('checkpoint') == 'planning':
        sources = 'pending'
    elif isinstance(source_processing, list) and source_processing:
        if sources != 'attention':
            sources = ('completed' if all(isinstance(item, dict) and
                item.get('eligibility') == 'eligible' and item.get('initial_selection') == 'complete'
                for item in source_processing) else 'attention')
    optional = [
        _status(run.get(status), requested=run.get(requested))
        for requested, status in (('counterpoint_requested', 'counterpoint_status'),
                                  ('gap_search_requested', 'gap_search_status'),
                                  ('gap_followup_requested', 'gap_followup_status'),
                                  ('support_review_requested', 'support_review_status'))
        if run.get(requested) is True
    ]
    if not any(key in run for key in ('counterpoint_requested', 'gap_search_requested',
                                      'gap_followup_requested', 'support_review_requested')):
        review = 'unknown'
    elif not optional:
        review = 'not_requested'
    elif 'attention' in optional:
        review = 'attention'
    elif 'pending' in optional:
        review = 'pending'
    elif 'unknown' in optional:
        review = 'unknown'
    else:
        review = 'completed'
    delivery = 'pending' if run.get('checkpoint') else _status(run.get('status'))
    return {'schema_version': '1', 'meaning': 'saved_execution_not_quality_verdict',
            'stages': [{'stage': name, 'status': status} for name, status in (
                ('planning', planning), ('sources', sources), ('analysis', analysis),
                ('review', review), ('delivery', delivery))]}
