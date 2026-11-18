"""Sanitized, read-only view of a research run's private runtime checkpoint.

The checkpoint holds call ids, tool arguments, source text and provider messages.
The view built here is a strict whitelist: only fields named below ever leave the
server, and events of an unknown kind are dropped.
"""
from __future__ import annotations

from typing import Any, Mapping

RATIONALE_LIMIT = 300
NAME_LIMIT = 100
_STATUSES = ('pending', 'running', 'completed', 'failed', 'cancelled')
_CURRENT_BY_PENDING = {'model': {'kind': 'model'}, 'model_returned': {'kind': 'model'}}
_TOOL_BY_PENDING = {'search': 'discover_sources', 'acquire': 'acquire_sources', 'historical_prices': 'historical_prices'}


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def sanitize_event(event: Any) -> dict[str, Any] | None:
    """Return the public fields of one run event, or None for an unknown event kind."""
    if not isinstance(event, Mapping):
        return None
    kind = event.get('kind')
    if kind == 'model':
        detail = event.get('usage_detail') if isinstance(event.get('usage_detail'), Mapping) else {}
        return {'kind': 'model', 'finish_reason': _text(event.get('finish_reason'), NAME_LIMIT),
                'usage': _int(event.get('usage')), 'input_tokens': _int(detail.get('input_tokens')),
                'output_tokens': _int(detail.get('output_tokens')), 'settlement': _text(event.get('settlement'), NAME_LIMIT)}
    if kind == 'model_failure':
        return {'kind': 'model_failure', 'code': _text(event.get('code'), NAME_LIMIT)}
    if kind == 'tool':
        return {'kind': 'tool', 'name': _text(event.get('name'), NAME_LIMIT), 'status': _text(event.get('status'), NAME_LIMIT),
                'reason': _text(event.get('reason'), NAME_LIMIT), 'rationale': _text(event.get('rationale'), RATIONALE_LIMIT),
                'denied_count': _int(event.get('denied_count'))}
    if kind == 'submission':
        return {'kind': 'submission', 'status': _text(event.get('status'), NAME_LIMIT)}
    return None


def sanitize_events(events: Any, after: int = 0) -> list[dict[str, Any]]:
    """Sanitize events at positions >= after; each keeps its index in the full list."""
    result: list[dict[str, Any]] = []
    for index, event in enumerate(events if isinstance(events, list) else []):
        clean = sanitize_event(event)
        if index >= after and clean is not None:
            result.append({'index': index, **clean})
    return result


def current_activity(checkpoint: Mapping[str, Any] | None) -> dict[str, str] | None:
    pending = (checkpoint or {}).get('pending')
    if not isinstance(pending, Mapping):
        return None
    kind = pending.get('kind')
    if kind in _CURRENT_BY_PENDING:
        return dict(_CURRENT_BY_PENDING[kind])
    if kind in _TOOL_BY_PENDING:
        return {'kind': 'tool', 'name': _TOOL_BY_PENDING[kind]}
    if kind == 'tool_batch':
        calls, cursor = pending.get('calls'), pending.get('cursor')
        if isinstance(calls, list) and type(cursor) is int and 0 <= cursor < len(calls) and isinstance(calls[cursor], Mapping):
            name = _text(calls[cursor].get('name'), NAME_LIMIT)
            if name:
                return {'kind': 'tool', 'name': name}
    return None


def build_progress(*, operation_id: str, operation_status: str, inputs: Mapping[str, Any], checkpoint: Mapping[str, Any] | None,
                   saved_turn: Any, version_id: str | None, after: int) -> dict[str, Any]:
    """Assemble the public progress document from private inputs, checkpoint and saved turn."""
    turn_meta = inputs.get('turn') if isinstance(inputs.get('turn'), Mapping) else {}
    if checkpoint:
        accounting = checkpoint.get('_accounting') if isinstance(checkpoint.get('_accounting'), Mapping) else {}
        usage = {'model_calls': _int(accounting.get('calls')) or 0, 'search_calls': _int(accounting.get('search_calls')) or 0,
                 'total_tokens': _int(accounting.get('usage')) or 0}
        events = checkpoint.get('events')
        updated_at = checkpoint.get('_saved_at') if isinstance(checkpoint.get('_saved_at'), str) else None
    elif saved_turn is not None:
        usage = saved_turn.usage.model_dump()
        events = saved_turn.events
        updated_at = saved_turn.finished_at.isoformat()
    else:
        usage, events, updated_at = {'model_calls': 0, 'search_calls': 0, 'total_tokens': 0}, [], None
    events = events if isinstance(events, list) else []
    started = inputs.get('runtime_origin_captured_at') or inputs.get('captured_at')
    return {
        'operation_id': operation_id,
        'operation_status': operation_status if operation_status in _STATUSES else 'failed',
        'turn': {'index': _int(turn_meta.get('index')) or 0, 'question': str(turn_meta.get('question') or inputs.get('question') or '')},
        'usage': usage,
        'current': current_activity(checkpoint) if operation_status == 'running' else None,
        'events': sanitize_events(events, after),
        'event_count': len(events),
        'started_at': started if isinstance(started, str) else None,
        'updated_at': updated_at,
        'version_id': version_id,
    }
