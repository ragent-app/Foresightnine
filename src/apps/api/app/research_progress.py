"""Sanitized, read-only view of a research run's private runtime checkpoint.

The checkpoint holds call ids, tool arguments, source text and provider messages.
The view built here is a strict whitelist: only fields named below ever leave the
server, and events of an unknown kind are dropped.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

RATIONALE_LIMIT = 300
TARGET_LIMIT = 200
NOTE_LIMIT = 600
NAME_LIMIT = 100
TITLE_LIMIT = 160
HOST_LIMIT = 120
LABEL_LIMIT = 120
VALUE_LIMIT = 64
UNIT_LIMIT = 64
CURRENCY_LIMIT = 16
PERIOD_LIMIT = 64
EXPRESSION_LIMIT = 200
TIMESTAMP_LIMIT = 64
SUMMARY_MAX_BYTES = 2048

_STATUSES = ('pending', 'running', 'completed', 'failed', 'cancelled')
_MODEL_PHASES = ('connecting', 'thinking', 'responding', 'retrying')
_CURRENT_BY_PENDING = {'model': {'kind': 'model'}, 'model_returned': {'kind': 'model'}}
_TOOL_BY_PENDING = {
    'search': 'discover_sources',
    'acquire': 'acquire_sources',
    'historical_prices': 'historical_prices',
    'sec_company_concept': 'sec_company_concept',
    'resolve_entity': 'resolve_entity',
    'market_data': 'market_data',
    'company_financials': 'company_financials',
    'filings': 'filings',
}


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def _nonneg_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _fit_summary_bytes(summary: dict[str, Any]) -> dict[str, Any] | None:
    def encoded_len(obj: dict[str, Any]) -> int:
        return len(json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))

    if encoded_len(summary) < SUMMARY_MAX_BYTES:
        return summary
    for list_key in ('leads_preview', 'items', 'facts_preview', 'calcs_preview'):
        if isinstance(summary.get(list_key), list):
            items = list(summary[list_key])
            while items and encoded_len({**summary, list_key: items}) >= SUMMARY_MAX_BYTES:
                items.pop()
            summary = {**summary, list_key: items}
            if encoded_len(summary) < SUMMARY_MAX_BYTES:
                return summary
    for str_key in ('title', 'expression', 'entity', 'instrument_id'):
        if isinstance(summary.get(str_key), str) and len(summary[str_key]) > 16:
            value = summary[str_key]
            while len(value) > 16 and encoded_len({**summary, str_key: value}) >= SUMMARY_MAX_BYTES:
                value = value[: len(value) // 2]
            summary = {**summary, str_key: value}
            if encoded_len(summary) < SUMMARY_MAX_BYTES:
                return summary
    return summary if encoded_len(summary) < SUMMARY_MAX_BYTES else None


def _sanitize_summary(tool_name: str | None, raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    kind = raw.get('kind')
    if not isinstance(kind, str) or not tool_name:
        return None
    if kind == 'data_tool':
        if raw.get('tool') != tool_name:
            return None
    elif kind != tool_name:
        return None

    clean: dict[str, Any] | None = None
    if kind == 'discover_sources':
        found = _nonneg_int(raw.get('found_count'))
        eligible = _nonneg_int(raw.get('eligible_count'))
        denied = _nonneg_int(raw.get('denied_count'))
        leads_raw = raw.get('leads_preview')
        if found is None or eligible is None or denied is None or not isinstance(leads_raw, list):
            return None
        leads_preview = []
        for item in leads_raw[:5]:
            if not isinstance(item, Mapping):
                continue
            title = _text(item.get('title'), TITLE_LIMIT) or ''
            host = _text(item.get('host'), HOST_LIMIT) or ''
            if not title and not host:
                continue
            leads_preview.append({
                'title': title,
                'host': host,
                'published_at': _text(item.get('published_at'), TIMESTAMP_LIMIT),
            })
        clean = {
            'kind': 'discover_sources',
            'found_count': found,
            'eligible_count': eligible,
            'denied_count': denied,
            'leads_preview': leads_preview,
        }
    elif kind == 'acquire_sources':
        acquired = _nonneg_int(raw.get('acquired_count'))
        failed = _nonneg_int(raw.get('failed_count'))
        items_raw = raw.get('items')
        if acquired is None or failed is None or not isinstance(items_raw, list):
            return None
        items = []
        for item in items_raw[:5]:
            if not isinstance(item, Mapping):
                continue
            status = _text(item.get('status'), NAME_LIMIT)
            if not status:
                continue
            items.append({
                'title': _text(item.get('title'), TITLE_LIMIT) or '',
                'host': _text(item.get('host'), HOST_LIMIT) or '',
                'status': status,
                'reason': _text(item.get('reason'), NAME_LIMIT),
                'total_characters': _nonneg_int(item.get('total_characters')),
            })
        clean = {
            'kind': 'acquire_sources',
            'acquired_count': acquired,
            'failed_count': failed,
            'items': items,
        }
    elif kind == 'sec_company_concept':
        entity = _text(raw.get('entity'), 100)
        taxonomy = _text(raw.get('taxonomy'), 40)
        tag = _text(raw.get('tag'), 128)
        unit = _text(raw.get('unit'), 64)
        status = _text(raw.get('status'), NAME_LIMIT)
        if not (entity and taxonomy and tag and unit and status):
            return None
        clean = {
            'kind': 'sec_company_concept',
            'entity': entity,
            'taxonomy': taxonomy,
            'tag': tag,
            'unit': unit,
            'status': status,
            'title': _text(raw.get('title'), TITLE_LIMIT),
            'total_characters': _nonneg_int(raw.get('total_characters')),
        }
    elif kind == 'data_tool':
        tool = _text(raw.get('tool'), NAME_LIMIT)
        status = _text(raw.get('status'), NAME_LIMIT)
        if not (tool and status):
            return None
        clean = {
            'kind': 'data_tool',
            'tool': tool,
            'target': _text(raw.get('target'), 200),
            'status': status,
            'title': _text(raw.get('title'), TITLE_LIMIT),
            'total_characters': _nonneg_int(raw.get('total_characters')),
        }
    elif kind == 'historical_prices':
        instrument_id = _text(raw.get('instrument_id'), 128)
        start_date = _text(raw.get('start_date'), 32)
        end_date = _text(raw.get('end_date'), 32)
        status = _text(raw.get('status'), NAME_LIMIT)
        if not (instrument_id and start_date and end_date and status):
            return None
        clean = {
            'kind': 'historical_prices',
            'instrument_id': instrument_id,
            'start_date': start_date,
            'end_date': end_date,
            'status': status,
            'title': _text(raw.get('title'), TITLE_LIMIT),
        }
    elif kind == 'read_sources':
        read_count = _nonneg_int(raw.get('read_count'))
        items_raw = raw.get('items')
        if read_count is None or not isinstance(items_raw, list):
            return None
        items = []
        for item in items_raw[:5]:
            if not isinstance(item, Mapping):
                continue
            source_id = _text(item.get('source_id'), NAME_LIMIT)
            start = _nonneg_int(item.get('start'))
            end = _nonneg_int(item.get('end'))
            if not source_id or start is None or end is None:
                continue
            items.append({
                'source_id': source_id,
                'title': _text(item.get('title'), TITLE_LIMIT) or '',
                'start': start,
                'end': end,
                'total_characters': _nonneg_int(item.get('total_characters')),
            })
        clean = {
            'kind': 'read_sources',
            'read_count': read_count,
            'items': items,
        }
    elif kind == 'record_facts':
        recorded_count = _nonneg_int(raw.get('recorded_count'))
        facts_raw = raw.get('facts_preview')
        if recorded_count is None or not isinstance(facts_raw, list):
            return None
        facts_preview = []
        for item in facts_raw[:5]:
            if not isinstance(item, Mapping):
                continue
            label = _text(item.get('label'), LABEL_LIMIT)
            value = _text(item.get('value'), VALUE_LIMIT)
            unit = _text(item.get('unit'), UNIT_LIMIT)
            period = _text(item.get('period'), PERIOD_LIMIT)
            if not (label and value and unit and period):
                continue
            facts_preview.append({
                'label': label,
                'value': value,
                'unit': unit,
                'currency': _text(item.get('currency'), CURRENCY_LIMIT),
                'period': period,
            })
        clean = {
            'kind': 'record_facts',
            'recorded_count': recorded_count,
            'facts_preview': facts_preview,
        }
    elif kind == 'calculate_financials':
        calculated_count = _nonneg_int(raw.get('calculated_count'))
        calcs_raw = raw.get('calcs_preview')
        if calculated_count is None or not isinstance(calcs_raw, list):
            return None
        calcs_preview = []
        for item in calcs_raw[:5]:
            if not isinstance(item, Mapping):
                continue
            formula_id = _text(item.get('formula_id'), NAME_LIMIT)
            value = _text(item.get('value'), VALUE_LIMIT)
            unit = _text(item.get('unit'), UNIT_LIMIT)
            expression = _text(item.get('expression'), EXPRESSION_LIMIT)
            if not (formula_id and value and unit and expression):
                continue
            calcs_preview.append({
                'formula_id': formula_id,
                'value': value,
                'unit': unit,
                'currency': _text(item.get('currency'), CURRENCY_LIMIT),
                'expression': expression,
            })
        clean = {
            'kind': 'calculate_financials',
            'calculated_count': calculated_count,
            'calcs_preview': calcs_preview,
        }
    elif kind == 'create_chart':
        chart_id = _text(raw.get('chart_id'), NAME_LIMIT)
        chart_type = _text(raw.get('chart_type'), 40)
        title = _text(raw.get('title'), TITLE_LIMIT)
        series_count = _nonneg_int(raw.get('series_count'))
        point_count = _nonneg_int(raw.get('point_count'))
        if not (chart_id and chart_type and title) or series_count is None or point_count is None:
            return None
        clean = {
            'kind': 'create_chart',
            'chart_id': chart_id,
            'chart_type': chart_type,
            'title': title,
            'series_count': series_count,
            'point_count': point_count,
        }
    return _fit_summary_bytes(clean) if clean is not None else None


def sanitize_event(event: Any) -> dict[str, Any] | None:
    """Return the public fields of one run event, or None for an unknown event kind."""
    if not isinstance(event, Mapping):
        return None
    kind = event.get('kind')
    if kind == 'model':
        detail = event.get('usage_detail') if isinstance(event.get('usage_detail'), Mapping) else {}
        return {
            'kind': 'model',
            'finish_reason': _text(event.get('finish_reason'), NAME_LIMIT),
            'usage': _int(event.get('usage')),
            'input_tokens': _int(detail.get('input_tokens')),
            'output_tokens': _int(detail.get('output_tokens')),
            'settlement': _text(event.get('settlement'), NAME_LIMIT),
            'note': _text(event.get('note'), NOTE_LIMIT),
        }
    if kind == 'model_failure':
        return {'kind': 'model_failure', 'code': _text(event.get('code'), NAME_LIMIT)}
    if kind == 'tool':
        name = _text(event.get('name'), NAME_LIMIT)
        return {
            'kind': 'tool',
            'name': name,
            'status': _text(event.get('status'), NAME_LIMIT),
            'reason': _text(event.get('reason'), NAME_LIMIT),
            'rationale': _text(event.get('rationale'), RATIONALE_LIMIT),
            'denied_count': _int(event.get('denied_count')),
            'summary': _sanitize_summary(name, event.get('summary')),
        }
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


def current_activity(checkpoint: Mapping[str, Any] | None) -> dict[str, Any] | None:
    pending = (checkpoint or {}).get('pending')
    if not isinstance(pending, Mapping):
        return None
    kind = pending.get('kind')
    if kind in _CURRENT_BY_PENDING:
        phase = pending.get('phase')
        retry_attempt = _nonneg_int(pending.get('retry_attempt'))
        return {
            'kind': 'model',
            'phase': phase if isinstance(phase, str) and phase in _MODEL_PHASES else None,
            'retry_attempt': retry_attempt if retry_attempt is not None and retry_attempt > 0 else None,
            'started_at': _text(pending.get('started_at'), TIMESTAMP_LIMIT),
        }
    tool_name: str | None = None
    if kind in _TOOL_BY_PENDING:
        tool_name = _TOOL_BY_PENDING[kind]
    elif kind == 'tool_batch':
        calls, cursor = pending.get('calls'), pending.get('cursor')
        if isinstance(calls, list) and type(cursor) is int and 0 <= cursor < len(calls) and isinstance(calls[cursor], Mapping):
            tool_name = _text(calls[cursor].get('name'), NAME_LIMIT)
    if tool_name:
        return {
            'kind': 'tool',
            'name': tool_name,
            'rationale': _text(pending.get('rationale'), RATIONALE_LIMIT),
            'target': _text(pending.get('target'), TARGET_LIMIT),
            'started_at': _text(pending.get('started_at'), TIMESTAMP_LIMIT),
        }
    return None


def _interim_counts(checkpoint: Mapping[str, Any] | None, saved_turn: Any) -> dict[str, int]:
    if checkpoint:
        leads = checkpoint.get('leads')
        read_ranges = checkpoint.get('read_ranges')
        facts = checkpoint.get('facts')
        calculations = checkpoint.get('calculations')
        acquired_ids = checkpoint.get('acquired_source_ids')
        if isinstance(acquired_ids, list):
            acquired_count = len({item for item in acquired_ids if isinstance(item, str) and item})
        else:
            lead_sources = checkpoint.get('acquired_lead_sources')
            acquired_set: set[str] = (
                {v for v in lead_sources.values() if isinstance(v, str) and v}
                if isinstance(lead_sources, Mapping) else set()
            )
            events = checkpoint.get('events')
            if isinstance(events, list):
                for ev in events:
                    if isinstance(ev, Mapping) and ev.get('kind') == 'tool' and ev.get('name') in ('sec_company_concept', 'historical_prices'):
                        summary = ev.get('summary')
                        if isinstance(summary, Mapping) and summary.get('status') == 'acquired':
                            acquired_set.add(f"ev:{ev.get('tool_call_id') or len(acquired_set)}")
            acquired_count = len(acquired_set)
        read_ids = (
            {item.get('source_id') for item in read_ranges if isinstance(item, Mapping) and isinstance(item.get('source_id'), str)}
            if isinstance(read_ranges, list) else set()
        )
        return {
            'leads_count': len(leads) if isinstance(leads, list) else 0,
            'acquired_sources_count': acquired_count,
            'read_sources_count': len(read_ids),
            'facts_count': len(facts) if isinstance(facts, list) else 0,
            'calculations_count': len(calculations) if isinstance(calculations, list) else 0,
        }
    if saved_turn is not None:
        events = getattr(saved_turn, 'events', None)
        lead_keys: set[tuple[Any, ...]] = set()
        leads_overflow = 0
        acquired_keys: set[tuple[Any, ...]] = set()
        acquired_overflow = 0
        for ev in events if isinstance(events, list) else []:
            if not isinstance(ev, Mapping) or ev.get('kind') != 'tool':
                continue
            summary = ev.get('summary')
            if not isinstance(summary, Mapping):
                continue
            s_kind = summary.get('kind')
            if s_kind == 'discover_sources':
                previews = summary.get('leads_preview') if isinstance(summary.get('leads_preview'), list) else []
                for item in previews:
                    if isinstance(item, Mapping):
                        lead_keys.add((item.get('host'), item.get('title'), item.get('published_at')))
                eligible = _nonneg_int(summary.get('eligible_count')) or 0
                if eligible > len(previews):
                    leads_overflow += eligible - len(previews)
            elif s_kind == 'acquire_sources':
                items = summary.get('items') if isinstance(summary.get('items'), list) else []
                preview_acquired = 0
                for item in items:
                    if isinstance(item, Mapping) and item.get('status') == 'acquired':
                        preview_acquired += 1
                        acquired_keys.add(('acquire', item.get('host'), item.get('title')))
                acq_count = _nonneg_int(summary.get('acquired_count')) or 0
                if acq_count > preview_acquired:
                    acquired_overflow += acq_count - preview_acquired
            elif s_kind == 'sec_company_concept' and summary.get('status') == 'acquired':
                acquired_keys.add(('sec', summary.get('entity'), summary.get('taxonomy'), summary.get('tag'), summary.get('unit'), summary.get('title')))
            elif s_kind == 'data_tool' and summary.get('status') in ('ok', 'partial') and summary.get('title'):
                acquired_keys.add(('data', summary.get('tool'), summary.get('target'), summary.get('title')))
            elif s_kind == 'historical_prices' and summary.get('status') == 'acquired':
                acquired_keys.add(('price', summary.get('instrument_id'), summary.get('start_date'), summary.get('end_date')))
        read_ranges = getattr(saved_turn, 'read_ranges', None)
        read_ids = {
            getattr(item, 'source_id', None) if not isinstance(item, Mapping) else item.get('source_id')
            for item in (read_ranges if isinstance(read_ranges, list) else [])
        }
        read_ids.discard(None)
        fact_ids = getattr(saved_turn, 'fact_ids', None)
        calc_ids = getattr(saved_turn, 'calculation_ids', None)
        return {
            'leads_count': len(lead_keys) + leads_overflow,
            'acquired_sources_count': len(acquired_keys) + acquired_overflow,
            'read_sources_count': len(read_ids),
            'facts_count': len(fact_ids) if isinstance(fact_ids, list) else 0,
            'calculations_count': len(calc_ids) if isinstance(calc_ids, list) else 0,
        }
    return {
        'leads_count': 0,
        'acquired_sources_count': 0,
        'read_sources_count': 0,
        'facts_count': 0,
        'calculations_count': 0,
    }


_CONTEXT_TRIGGERS = frozenset({'l1_in_turn', 'l2_continuation'})


def _sanitize_context_receipts(checkpoint: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return whitelisted context compaction summary without raw prompt text or call IDs."""
    raw = (checkpoint or {}).get('context_receipts')
    if not isinstance(raw, list):
        return {'count': 0, 'total_chars_saved': 0, 'recent': []}
    sanitized: list[dict[str, Any]] = []
    total_saved = 0
    for item in raw[-20:]:
        if not isinstance(item, Mapping):
            continue
        trigger = _text(item.get('trigger'), NAME_LIMIT)
        if not trigger or trigger not in _CONTEXT_TRIGGERS:
            trigger = 'compacted'
        before_chars = _nonneg_int(item.get('before_chars')) or 0
        after_chars = _nonneg_int(item.get('after_chars')) or 0
        chars_saved = _nonneg_int(item.get('chars_saved')) or 0
        total_saved += chars_saved
        cleared_ids = item.get('cleared_tool_call_ids')
        omitted_spans = item.get('omitted_spans')
        sanitized.append({
            'trigger': trigger,
            'reason': _text(item.get('reason'), NAME_LIMIT),
            'before_chars': before_chars,
            'after_chars': after_chars,
            'chars_saved': chars_saved,
            'cleared_results_count': len(cleared_ids) if isinstance(cleared_ids, list) else 0,
            'cleared_thinking_turns': _nonneg_int(item.get('cleared_thinking_turns')) or 0,
            'cleared_thinking_chars': _nonneg_int(item.get('cleared_thinking_chars')) or 0,
            'omitted_spans_count': len(omitted_spans) if isinstance(omitted_spans, list) else 0,
            'preserved_facts_count': _nonneg_int(item.get('preserved_facts_count')) or 0,
            'prefix_reset': bool(item.get('prefix_reset')) if type(item.get('prefix_reset')) is bool else False,
            'at': _text(item.get('at'), TIMESTAMP_LIMIT),
        })
    return {
        'count': len(sanitized),
        'total_chars_saved': total_saved,
        'recent': sanitized[-5:],
    }


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
        'interim': _interim_counts(checkpoint, saved_turn),
        'context_receipts': _sanitize_context_receipts(checkpoint),
        'events': sanitize_events(events, after),
        'event_count': len(events),
        'started_at': started if isinstance(started, str) else None,
        'updated_at': updated_at,
        'version_id': version_id,
    }
