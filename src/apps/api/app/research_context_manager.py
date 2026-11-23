"""Context management for the research runtime: L1 history compaction, KV-cache usage tracking, and turn input assembly."""
from __future__ import annotations

from datetime import datetime
import inspect
from hashlib import sha256
import json
import math
import re
from typing import Any, Mapping, Sequence

from . import research_run_insights as insights
from .model_protocol import (
    ModelAssistantContinuation,
    ModelToolDefinition,
    ModelToolResult,
    ModelTurnExchange,
    ModelTurnInput,
    ModelUsage,
)
from .research_tool_presentation import DEFAULT_TOOLS as TOOLS, DEFAULT_METADATA
from .research_library import ResearchLibrary
from .research_source_catalog import canonical_source_url
from .research_source_policy import source_eligibility
from .research_public_query import minimize_identity
from .research_sources import ResearchSource
from .research_tools_registry import (
    BULK_COMPACT_TOOLS,
    PROTECTED_COMPACT_TOOLS,
    SYSTEM,
    clip_text as _clip,
    utc_now_iso as _now_iso,
)

DEFAULT_L1_COMPACT_TRIGGER_CHARS = 90_000
DEFAULT_L1_KEEP_RECENT = 2
MAX_CONTEXT_RECEIPTS = 20
L1_LOW_WATER_RATIO = 0.6
MIN_COMPACTION_SAVINGS_CHARS = 8_000
L1_HARD_LIMIT_FACTOR = 1.5
PRIOR_RESEARCH_TOKEN_BUDGET = 12_000
PRIOR_LIMITS = {'facts': 200, 'calculations': 100, 'claims': 100, 'charts': 50}
_COMPACT_MIN_RESULT_CHARS = 800
_COMPACT_GIANT_PROTECTED_CHARS = 8_000
_COMPACT_RECOVERY_HINT = (
    'Use search_source or targeted read_sources if re-inspection needed; recorded read_ranges remain valid.'
)

_SAFE_TIMEOUT_KIND_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,64}$')
_TIMEOUT_STAGE_TO_KIND = {
    'dns': 'connect_timeout',
    'tcp': 'connect_timeout',
    'tls': 'connect_timeout',
    'request_write': 'connect_timeout',
    'response_headers': 'header_timeout',
    'response_body': 'stream_idle_timeout',
    'deadline': 'stream_deadline_timeout',
}


def _history_serialized_chars(history: Any) -> int:
    if not isinstance(history, list) or not history:
        return 0
    return len(json.dumps(history, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str))


def _extract_assistant_tool_calls(assistant: Any) -> dict[str, tuple[str, dict[str, Any]]]:
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    if not isinstance(assistant, Mapping):
        return calls
    content = assistant.get('content')
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, Mapping) or block.get('type') != 'tool_use':
                continue
            cid, cname = block.get('id'), block.get('name')
            if isinstance(cid, str) and cid and isinstance(cname, str) and cname:
                raw_input = block.get('input')
                calls[cid] = (cname, dict(raw_input) if isinstance(raw_input, Mapping) else {})
    tool_calls = assistant.get('tool_calls')
    if isinstance(assistant.get('output'), list):
        tool_calls = [dict(item, id=item.get('call_id')) for item in assistant['output']
                      if isinstance(item, dict) and item.get('type') == 'function_call']
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, Mapping):
                continue
            cid = tc.get('id')
            fn = tc.get('function') if isinstance(tc.get('function'), Mapping) else {}
            cname = fn.get('name') or tc.get('name')
            if not isinstance(cid, str) or not cid or not isinstance(cname, str) or not cname:
                continue
            raw_args = fn.get('arguments') if 'arguments' in fn else tc.get('arguments')
            parsed_args: dict[str, Any] = {}
            if isinstance(raw_args, Mapping):
                parsed_args = dict(raw_args)
            elif isinstance(raw_args, str) and raw_args.strip().startswith('{'):
                try:
                    loaded = json.loads(raw_args)
                    if isinstance(loaded, dict):
                        parsed_args = loaded
                except (TypeError, ValueError):
                    pass
            calls[cid] = (cname, parsed_args)
    return calls


def _infer_tool_name(parsed: Mapping[str, Any] | None) -> str:
    if not isinstance(parsed, Mapping):
        return 'unknown'
    if 'library_coverage' in parsed:
        return 'search_library'
    if 'statements' in parsed and 'page_basis' in parsed:
        return 'extract_statements'
    if 'hits' in parsed and 'query' in parsed:
        return 'search_source'
    if 'entries' in parsed and 'total_detected_entries' in parsed:
        return 'outline_source'
    if 'leads' in parsed:
        return 'news_search'
    if 'documents' in parsed and 'issuer' in parsed:
        return 'issuer_documents'
    if 'facts' in parsed:
        return 'record_facts'
    if 'calculations' in parsed:
        return 'calculate_financials'
    if 'chart_id' in parsed and 'chart_type' in parsed:
        return 'create_chart'
    if 'table_id' in parsed and 'table_type' in parsed:
        return 'create_table'
    if 'bar_count' in parsed and 'max_drawdown' in parsed:
        return 'analyze_market_series'
    if 'pack_id' in parsed and 'method_pack_text' in parsed:
        return 'activate_method_pack'
    results = parsed.get('results')
    if isinstance(results, list):
        if any(isinstance(r, Mapping) and 'start' in r and 'text' in r for r in results):
            return 'read_sources'
        if any(isinstance(r, Mapping) and 'url' in r for r in results):
            return 'acquire_sources'
    return 'unknown'


def _extract_compaction_coverage(
    tool_name: str,
    parsed: Mapping[str, Any] | None,
    args: Mapping[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    source_ids: list[str] = []
    source_spans: list[dict[str, Any]] = []

    def add_source_id(val: Any) -> None:
        if isinstance(val, str) and val and val not in source_ids and len(source_ids) < 20:
            source_ids.append(val)

    def add_span(row: Mapping[str, Any]) -> None:
        sid, start, end = row.get('source_id'), row.get('start'), row.get('end')
        add_source_id(sid)
        if (
            tool_name in ('read_sources', 'read_pages', 'extract_statements')
            and isinstance(sid, str) and sid
            and type(start) is int and type(end) is int
            and len(source_spans) < 20
        ):
            span = {'source_id': sid, 'start': start, 'end': end}
            if span not in source_spans:
                source_spans.append(span)

    if isinstance(parsed, Mapping):
        add_source_id(parsed.get('source_id'))
        for list_key in ('results', 'reads', 'hits', 'read_spans'):
            rows = parsed.get(list_key)
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, Mapping):
                        add_span(row)
    if not source_ids and isinstance(args, Mapping):
        add_source_id(args.get('source_id'))
        arg_reads = args.get('reads')
        if isinstance(arg_reads, list):
            for row in arg_reads:
                if isinstance(row, Mapping):
                    add_span(row)
    return source_ids, source_spans


def _strip_consumed_thinking(assistant: Any, *, dry_run: bool = False) -> tuple[int, int]:
    """Strip consumed thinking blocks from a historical assistant message safely.

    Never removes thinking blocks unless at least one non-thinking block (such as
    ``tool_use`` or non-empty ``text``) remains so message validity is preserved.
    """
    if not isinstance(assistant, dict):
        return 0, 0
    content = assistant.get('content')
    if not isinstance(content, list) or not content:
        return 0, 0
    thinking_blocks: list[Any] = []
    remaining: list[Any] = []
    for block in content:
        if isinstance(block, Mapping) and block.get('type') in ('thinking', 'redacted_thinking'):
            thinking_blocks.append(block)
        else:
            remaining.append(block)
    if not thinking_blocks or not remaining:
        return 0, 0
    has_valid_non_thinking = any(
        (isinstance(b, Mapping) and (b.get('type') != 'text' or bool(b.get('text'))))
        or (isinstance(b, str) and bool(b))
        for b in remaining
    )
    if not has_valid_non_thinking:
        return 0, 0
    chars_cleared = sum(
        len(json.dumps(b, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str))
        for b in thinking_blocks
    )
    if not dry_run:
        assistant['content'] = remaining
    return 1, chars_cleared


def _canonical_url_safe(url: str) -> str:
    try:
        return canonical_source_url(url)
    except ValueError:
        return url


def _acquired_url_set(state: Mapping[str, Any]) -> set[str]:
    urls: set[str] = set()
    lead_sources = state.get('acquired_lead_sources')
    if isinstance(lead_sources, Mapping):
        for raw_url in lead_sources:
            if isinstance(raw_url, str) and raw_url:
                urls.add(raw_url)
                urls.add(_canonical_url_safe(raw_url))
    actions = state.get('acquired_actions')
    if isinstance(actions, Mapping):
        for key in actions:
            if isinstance(key, str) and key.startswith('acquire|'):
                raw_url = key[len('acquire|'):]
                if raw_url:
                    urls.add(raw_url)
                    urls.add(_canonical_url_safe(raw_url))
    return urls


def _is_empty_or_low_yield_result(tool_name: str, parsed: Mapping[str, Any] | None, is_error: bool) -> bool:
    if is_error:
        return True
    if not isinstance(parsed, Mapping):
        return False
    status = parsed.get('status')
    if status in ('empty', 'unavailable', 'denied_or_unavailable', 'denied', 'failed', 'restricted', 'not_found', 'duplicate_action'):
        return True
    if tool_name in ('search_source', 'search_library'):
        hits = parsed.get('hits')
        if parsed.get('total_matches') == 0 or parsed.get('returned_hits') == 0 or (isinstance(hits, list) and len(hits) == 0):
            return True
    elif tool_name == 'news_search':
        leads = parsed.get('leads')
        if parsed.get('found_count') == 0 or (isinstance(leads, list) and len(leads) == 0):
            return True
    elif tool_name == 'filings':
        filings = parsed.get('filings')
        if isinstance(filings, list) and len(filings) == 0:
            return True
    elif tool_name == 'issuer_documents':
        docs = parsed.get('documents')
        if isinstance(docs, list) and len(docs) == 0:
            return True
    elif tool_name == 'outline_source':
        entries = parsed.get('entries')
        if parsed.get('entry_count') == 0 or (isinstance(entries, list) and len(entries) == 0):
            return True
    elif tool_name == 'acquire_sources':
        rows = parsed.get('results')
        if isinstance(rows, list) and not any(isinstance(r, Mapping) and r.get('status') == 'acquired' for r in rows):
            return True
    elif tool_name in ('read_sources', 'read_pages'):
        rows = parsed.get('results')
        if isinstance(rows, list):
            if not any(isinstance(r, Mapping) and r.get('status') == 'read' for r in rows):
                return True
            text_chars = sum(len(r.get('text') or '') for r in rows if isinstance(r, Mapping) and isinstance(r.get('text'), str))
            if text_chars < 100:
                return True
    return False


def _result_lead_urls(tool_name: str, parsed: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(parsed, Mapping):
        return []
    urls: list[str] = []
    if tool_name == 'news_search' and isinstance(parsed.get('leads'), list):
        acquirable = [
            row.get('url')
            for row in parsed['leads']
            if isinstance(row, Mapping) and isinstance(row.get('url'), str) and row.get('url') and row.get('acquirable') is not False
        ]
        urls = acquirable or [
            row.get('url')
            for row in parsed['leads']
            if isinstance(row, Mapping) and isinstance(row.get('url'), str) and row.get('url')
        ]
    elif tool_name == 'filings' and isinstance(parsed.get('filings'), list):
        urls = [
            row.get('primary_document_url')
            for row in parsed['filings']
            if isinstance(row, Mapping) and isinstance(row.get('primary_document_url'), str) and row.get('primary_document_url')
        ]
    elif tool_name == 'issuer_documents' and isinstance(parsed.get('documents'), list):
        urls = [
            row.get('url')
            for row in parsed['documents']
            if isinstance(row, Mapping) and isinstance(row.get('url'), str) and row.get('url')
        ]
    return urls


def _compaction_salience_tier(
    tool_name: str,
    parsed: Mapping[str, Any] | None,
    is_error: bool,
    source_ids: Sequence[str],
    *,
    facts_count: int,
    fact_source_ids: set[str],
    acquired_urls: set[str],
) -> int:
    """Assign L1 salience tier: 1=already digested, 2=empty/low-yield, 3=remaining bulk."""
    if _is_empty_or_low_yield_result(tool_name, parsed, is_error):
        return 2
    lead_urls = _result_lead_urls(tool_name, parsed)
    if lead_urls:
        if acquired_urls and all((u in acquired_urls or _canonical_url_safe(u) in acquired_urls) for u in lead_urls):
            return 1
        return 3
    if facts_count > 0 and tool_name in BULK_COMPACT_TOOLS:
        if not fact_source_ids or not source_ids or bool(set(source_ids) & fact_source_ids):
            return 1
    return 3


def _default_cache_usage() -> dict[str, Any]:
    return {
        'input_tokens': 0,
        'output_tokens': 0,
        'cache_read_input_tokens': 0,
        'cache_creation_input_tokens': 0,
        'cache_hit_ratio': None,
        'calls_with_usage': 0,
        'calls_with_cache_telemetry': 0,
    }


def _coerce_model_usage(usage_detail: Any) -> ModelUsage | None:
    if isinstance(usage_detail, ModelUsage):
        return usage_detail
    if isinstance(usage_detail, Mapping):
        def _nonneg_int(val: Any) -> int | None:
            return val if type(val) is int and val >= 0 else None
        return ModelUsage(
            input_tokens=_nonneg_int(usage_detail.get('input_tokens')),
            output_tokens=_nonneg_int(usage_detail.get('output_tokens')),
            total_tokens=_nonneg_int(usage_detail.get('total_tokens')),
            cache_creation_input_tokens=_nonneg_int(usage_detail.get('cache_creation_input_tokens')),
            cache_read_input_tokens=_nonneg_int(usage_detail.get('cache_read_input_tokens')),
        )
    return None


def _record_cache_usage(state: dict[str, Any], *, call_id: str | None, usage_detail: Any) -> dict[str, Any] | None:
    """Idempotently accumulate input & prompt-cache usage into ``state['cache_usage']``."""
    usage_obj = _coerce_model_usage(usage_detail)
    if usage_obj is None:
        return None
    cache_state = state.get('cache_usage')
    if not isinstance(cache_state, dict):
        cache_state = _default_cache_usage()
        state['cache_usage'] = cache_state
    counted = state.get('_cache_counted_call_ids')
    if not isinstance(counted, list):
        counted = []
        state['_cache_counted_call_ids'] = counted
    summary = usage_obj.usage_cache_summary()
    if isinstance(call_id, str) and call_id and call_id in counted:
        return summary
    if isinstance(call_id, str) and call_id:
        counted.append(call_id)
        if len(counted) > 500:
            del counted[:-500]
    norm_input = summary['total_input_tokens']
    out = usage_obj.output_tokens if type(usage_obj.output_tokens) is int and usage_obj.output_tokens >= 0 else 0
    has_any_usage = (
        norm_input is not None
        or (type(usage_obj.output_tokens) is int and usage_obj.output_tokens >= 0)
        or (type(usage_obj.total_tokens) is int and usage_obj.total_tokens >= 0)
    )
    if has_any_usage:
        cache_state['calls_with_usage'] = int(cache_state.get('calls_with_usage') or 0) + 1
    if norm_input is not None:
        cache_state['input_tokens'] = int(cache_state.get('input_tokens') or 0) + norm_input
    cache_state['output_tokens'] = int(cache_state.get('output_tokens') or 0) + out
    read = summary['cache_read_input_tokens']
    creation = summary['cache_creation_input_tokens']
    if read is not None or creation is not None:
        cache_state['calls_with_cache_telemetry'] = int(cache_state.get('calls_with_cache_telemetry') or 0) + 1
        cache_state['cache_read_input_tokens'] = int(cache_state.get('cache_read_input_tokens') or 0) + (read or 0)
        cache_state['cache_creation_input_tokens'] = int(cache_state.get('cache_creation_input_tokens') or 0) + (creation or 0)
        observed = int(state.get('_cache_observed_input_tokens') or 0) + (norm_input or 0)
        state['_cache_observed_input_tokens'] = observed
        if observed > 0:
            cache_state['cache_hit_ratio'] = round(min(1.0, cache_state['cache_read_input_tokens'] / observed), 6)
    return summary


def _compaction_noop(
    reason: str,
    before_chars: int,
    kept_recent: int,
    **extra: Any,
) -> dict[str, Any]:
    return {
        'compacted': False,
        'reason': reason,
        'before_chars': before_chars,
        'after_chars': before_chars,
        'chars_saved': 0,
        'cleared_count': 0,
        'kept_recent': kept_recent,
        'compacted_tools': [],
        'cleared_call_ids': [],
        'cleared_thinking_turns': 0,
        'cleared_thinking_chars': 0,
        'omitted_spans': [],
        'receipt': None,
        **extra,
    }


def _parse_json_dict(raw_content: Any) -> dict[str, Any] | None:
    if isinstance(raw_content, str) and raw_content.lstrip().startswith('{'):
        try:
            loaded = json.loads(raw_content)
            if isinstance(loaded, dict):
                return loaded
        except (TypeError, ValueError):
            return None
    return None


def _event_tool_names(state: Mapping[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for ev in state.get('events') or []:
        if isinstance(ev, Mapping) and ev.get('kind') == 'tool':
            tc_id, t_name = ev.get('tool_call_id'), ev.get('name')
            if isinstance(tc_id, str) and tc_id and isinstance(t_name, str) and t_name:
                names[tc_id] = t_name
    return names


def compact_runtime_history(
    state: dict[str, Any],
    *,
    keep_recent: int = DEFAULT_L1_KEEP_RECENT,
    trigger_chars: int = DEFAULT_L1_COMPACT_TRIGGER_CHARS,
    force: bool = False,
    reason: str | None = None,
    trigger: str | None = None,
    hard_limit_chars: int | None = None,
) -> dict[str, Any]:
    """Deterministically replace bulky older tool results and consumed thinking blocks in ``state['history']``.

    Preserves ``state['history'][-keep_recent:]`` verbatim (and leaves ``state['history']``
    completely untouched below ``trigger_chars`` when ``force=False`` so prompt prefixes
    stay byte-identical for KV-cache reuse). Never modifies ``read_ranges``, ``facts``,
    ``calculations``, or ``events``. Records a bounded receipt into ``state['context_receipts']``
    whenever compaction actually clears results or thinking blocks.

    Non-forced (L1) compaction has hysteresis so the cached prompt prefix is reset rarely: once the
    trigger is crossed it clears the oldest eligible results and thinking blocks in a single pass down
    to a low-water mark of ``L1_LOW_WATER_RATIO`` of the trigger, and does nothing at all (no history
    change, no receipt) when that would save fewer than ``MIN_COMPACTION_SAVINGS_CHARS`` unless the
    history exceeds ``hard_limit_chars`` (default ``L1_HARD_LIMIT_FACTOR`` x the trigger). Forced
    compaction (continuation/recovery) clears everything eligible as before.
    """
    history = state.get('history')
    if not isinstance(history, list) or not history:
        return _compaction_noop('empty_history', 0, 0)

    keep = max(0, int(keep_recent))
    cutoff = len(history) if keep == 0 else max(0, len(history) - keep)
    kept_recent = len(history) - cutoff
    before_chars = _history_serialized_chars(history)

    if not force and before_chars <= trigger_chars:
        return _compaction_noop('below_threshold', before_chars, kept_recent)
    if cutoff == 0:
        return _compaction_noop('within_keep_recent', before_chars, kept_recent)

    effective_reason = reason or ('continuation_compacted' if force else 'context_compacted')
    effective_trigger = trigger or ('l2_continuation' if force else 'l1_in_turn')
    raw_facts = state.get('facts') if isinstance(state.get('facts'), list) else []
    facts_recorded_count = len(raw_facts)
    all_facts_for_tier = [f for f in [*(state.get('prior_facts') or []), *raw_facts] if isinstance(f, Mapping)]
    fact_source_ids = {
        f['source_id'] for f in all_facts_for_tier
        if isinstance(f.get('source_id'), str) and f['source_id']
    }
    acquired_urls = _acquired_url_set(state)
    event_tool_names = _event_tool_names(state)

    cleared_count = 0
    compacted_tools: list[str] = []
    cleared_call_ids: list[str] = []
    cleared_thinking_turns = 0
    cleared_thinking_chars = 0
    omitted_spans: list[dict[str, Any]] = []
    cleared_by_tier = {'tier1': 0, 'tier2': 0, 'tier3': 0}

    # Plan across salience tiers (Tier 1: thinking + digested results; Tier 2: empty/low-yield;
    # Tier 3: remaining oldest bulk results). Nothing is mutated until the plan is accepted.
    planned: list[dict[str, Any]] = []
    for ex_idx, exchange in enumerate(history[:cutoff]):
        if not isinstance(exchange, Mapping):
            continue
        assistant = exchange.get('assistant')
        t_turns, t_chars = _strip_consumed_thinking(assistant, dry_run=True)
        if t_turns:
            planned.append({
                'kind': 'thinking', 'assistant': assistant, 'saved': t_chars, 'chars': t_chars,
                'tier': 1, 'ex_idx': ex_idx, 'item_idx': -1,
            })
        results = exchange.get('results')
        if not isinstance(results, list) or not results:
            continue
        assistant_calls = _extract_assistant_tool_calls(assistant)
        for item_idx, res in enumerate(results):
            if not isinstance(res, dict):
                continue
            raw_content = res.get('content')
            if not isinstance(raw_content, str):
                continue
            parsed_content = _parse_json_dict(raw_content)
            if parsed_content is not None and parsed_content.get('cleared') is True:
                continue

            call_id = res.get('tool_call_id') if isinstance(res.get('tool_call_id'), str) else ''
            tool_name, call_args = assistant_calls.get(call_id, ('', {}))
            if not tool_name:
                tool_name = event_tool_names.get(call_id, '')
            if not tool_name:
                tool_name = _infer_tool_name(parsed_content)

            original_chars = len(raw_content)
            is_error = bool(res.get('is_error', False))
            source_ids, source_spans = _extract_compaction_coverage(tool_name, parsed_content, call_args)

            descriptor: dict[str, Any] = {
                'cleared': True,
                'tool': tool_name,
                'reason': effective_reason,
                'source_ids': source_ids,
                'source_spans': source_spans,
                'original_chars': original_chars,
                'facts_recorded_count': facts_recorded_count,
                'hint': _COMPACT_RECOVERY_HINT,
            }
            if tool_name == 'search_source' and isinstance(parsed_content, Mapping):
                if isinstance(parsed_content.get('query'), str) and parsed_content['query']:
                    descriptor['query'] = _clip(parsed_content['query'], 80)
                if type(parsed_content.get('total_matches')) is int:
                    descriptor['total_matches'] = parsed_content['total_matches']
            elif tool_name == 'outline_source' and isinstance(parsed_content, Mapping):
                if type(parsed_content.get('total_characters')) is int:
                    descriptor['total_characters'] = parsed_content['total_characters']

            descriptor_json = json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            if len(descriptor_json) >= original_chars:
                continue

            if tool_name in PROTECTED_COMPACT_TOOLS:
                should_compact = force and original_chars > _COMPACT_GIANT_PROTECTED_CHARS
            elif tool_name in BULK_COMPACT_TOOLS:
                if is_error:
                    should_compact = original_chars > _COMPACT_MIN_RESULT_CHARS
                else:
                    should_compact = original_chars > _COMPACT_MIN_RESULT_CHARS or original_chars > 400 or force
            else:
                should_compact = original_chars > _COMPACT_MIN_RESULT_CHARS

            if not should_compact:
                continue
            tier = _compaction_salience_tier(
                tool_name,
                parsed_content,
                is_error,
                source_ids,
                facts_count=len(all_facts_for_tier),
                fact_source_ids=fact_source_ids,
                acquired_urls=acquired_urls,
            )
            planned.append({
                'kind': 'result', 'res': res, 'descriptor_json': descriptor_json,
                'saved': original_chars - len(descriptor_json), 'tool': tool_name,
                'call_id': call_id, 'spans': source_spans,
                'tier': tier, 'ex_idx': ex_idx, 'item_idx': item_idx,
            })

    planned.sort(key=lambda item: (item['tier'], item['ex_idx'], item['item_idx']))
    low_water: int | None = None
    selected = planned
    if not force:
        low_water = int(trigger_chars * L1_LOW_WATER_RATIO)
        need = before_chars - low_water
        selected = []
        running = 0
        for action in planned:
            if running >= need:
                break
            selected.append(action)
            running += action['saved']
        hard_limit = hard_limit_chars if hard_limit_chars is not None else int(trigger_chars * L1_HARD_LIMIT_FACTOR)
        if running < MIN_COMPACTION_SAVINGS_CHARS and before_chars <= hard_limit:
            return _compaction_noop(
                'below_min_savings',
                before_chars,
                kept_recent,
                low_water_chars=low_water,
                achievable_saving_chars=running,
            )

    for action in selected:
        tier_key = f"tier{action['tier']}"
        if action['kind'] == 'thinking':
            t_turns, t_chars = _strip_consumed_thinking(action['assistant'])
            cleared_thinking_turns += t_turns
            cleared_thinking_chars += t_chars
            if t_turns:
                cleared_by_tier[tier_key] = cleared_by_tier.get(tier_key, 0) + 1
            continue
        action['res']['content'] = action['descriptor_json']
        cleared_count += 1
        cleared_by_tier[tier_key] = cleared_by_tier.get(tier_key, 0) + 1
        if action['tool'] not in compacted_tools:
            compacted_tools.append(action['tool'])
        if action['call_id']:
            cleared_call_ids.append(action['call_id'])
        for sp in action['spans']:
            if sp not in omitted_spans and len(omitted_spans) < 50:
                omitted_spans.append(sp)

    after_chars = _history_serialized_chars(history)
    chars_saved = max(0, before_chars - after_chars)
    did_compact = cleared_count > 0 or cleared_thinking_turns > 0
    receipt: dict[str, Any] | None = None
    if did_compact:
        prev_count = state.get('compaction_count')
        state['compaction_count'] = (prev_count if type(prev_count) is int and prev_count >= 0 else 0) + 1
        cache_snapshot = dict(state['cache_usage']) if isinstance(state.get('cache_usage'), Mapping) else None
        receipt = {
            'trigger': effective_trigger,
            'reason': effective_reason,
            'before_chars': before_chars,
            'after_chars': after_chars,
            'chars_saved': chars_saved,
            'cleared_tool_call_ids': cleared_call_ids[:50],
            'cleared_thinking_turns': cleared_thinking_turns,
            'cleared_thinking_chars': cleared_thinking_chars,
            'cleared_by_tier': dict(cleared_by_tier),
            'omitted_spans': omitted_spans[:50],
            'preserved_facts_count': facts_recorded_count,
            'prefix_reset': True,
            'low_water_chars': low_water,
            **({'cache_usage': cache_snapshot} if cache_snapshot is not None else {}),
            'at': _now_iso(),
        }
        receipts = state.get('context_receipts')
        if not isinstance(receipts, list):
            receipts = []
            state['context_receipts'] = receipts
        receipts.append(receipt)
        if len(receipts) > MAX_CONTEXT_RECEIPTS:
            del receipts[:-MAX_CONTEXT_RECEIPTS]
    return {
        'compacted': did_compact,
        'reason': effective_reason if did_compact else 'nothing_to_compact',
        'before_chars': before_chars,
        'after_chars': after_chars,
        'chars_saved': chars_saved,
        'cleared_count': cleared_count,
        'kept_recent': kept_recent,
        'compacted_tools': compacted_tools,
        'cleared_call_ids': cleared_call_ids,
        'cleared_thinking_turns': cleared_thinking_turns,
        'cleared_thinking_chars': cleared_thinking_chars,
        'cleared_by_tier': dict(cleared_by_tier),
        'omitted_spans': omitted_spans[:50],
        'receipt': receipt,
    }


def _l1_compaction_trigger_chars(
    state: Mapping[str, Any],
    model: Any,
    context: Any,
    default_trigger: int = DEFAULT_L1_COMPACT_TRIGGER_CHARS,
    *,
    system_chars: int | None = None,
    tools_chars: int | None = None,
) -> int:
    """Compute history compaction watermark honoring tighter provider/budget limits."""
    trigger = default_trigger
    resolved_system_chars = len(SYSTEM) if system_chars is None else system_chars
    resolved_tools_chars = DEFAULT_METADATA[1] if tools_chars is None else tools_chars
    fixed_chars = resolved_system_chars + len(str(state.get('user') or '')) + resolved_tools_chars

    policy = getattr(model, 'policy', None)
    explicit_input = getattr(policy, 'max_input_tokens', None)
    if type(explicit_input) is int and explicit_input > 0:
        available = explicit_input - fixed_chars
        budget_trigger = max(1_000, int(available * 0.65)) if available > 0 else 1_000
        trigger = min(trigger, budget_trigger)

    capacity = getattr(model, 'research_capacity', None)
    profile = capacity() if callable(capacity) else None
    if profile is not None:
        work_limit = getattr(profile, 'work_context_tokens', None)
        context_limit = getattr(profile, 'context_tokens', None)
        input_counter = getattr(profile, 'input_counter', None)
        if type(work_limit) is int and work_limit > 0:
            if input_counter == 'chars_div_3':
                available = int(work_limit * 3 * 0.55) - fixed_chars
            elif input_counter == 'utf8_byte_upper_bound':
                available = int(work_limit * 0.55) - fixed_chars
            else:
                available = int(work_limit * 0.55) - fixed_chars
            trigger = min(trigger, max(1_000, available))
        if type(context_limit) is int and context_limit > 0:
            if input_counter == 'chars_div_3':
                available = int(context_limit * 3 * 0.58) - fixed_chars
                trigger = min(trigger, max(1_000, available))
            elif input_counter == 'utf8_byte_upper_bound':
                available = int(context_limit * 0.58) - fixed_chars
                trigger = min(trigger, max(1_000, available))

    budget = getattr(context, 'budget', None)
    max_total_tokens = getattr(budget, 'max_total_tokens', None)
    accounted_tokens = getattr(context, 'accounted_tokens', None)
    if type(max_total_tokens) is int and max_total_tokens > 0 and type(accounted_tokens) is int:
        remaining = max(0, max_total_tokens - accounted_tokens)
        if remaining < 200_000:
            available = int(remaining * 0.65) - fixed_chars
            trigger = min(trigger, max(1_000, available if available > 0 else 1_000))

    return trigger


def _sanitize_failed_usage(usage: Any) -> int | dict[str, int] | None:
    if type(usage) is int and usage >= 0:
        return usage
    if isinstance(usage, Mapping):
        clean = {
            k: int(v)
            for k, v in usage.items()
            if k in (
                'input_tokens',
                'output_tokens',
                'total_tokens',
                'cache_creation_input_tokens',
                'cache_read_input_tokens',
            )
            and type(v) is int and v >= 0
        }
        return clean or None
    return None


def _sanitize_timeout_kind(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or not _SAFE_TIMEOUT_KIND_RE.fullmatch(cleaned):
        return None
    return cleaned


def _sanitize_bounded_nonneg_int(value: Any, *, max_value: int = 10**9) -> int | None:
    if type(value) is int and 0 <= value <= max_value:
        return value
    if isinstance(value, float) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= max_value:
        return int(value)
    return None


def _extract_failure_metadata(
    *,
    error: Any = None,
    call_diag: Mapping[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {}

    def read_field(src: Any, key: str) -> Any:
        if src is None:
            return None
        if isinstance(src, Mapping):
            return src.get(key)
        return getattr(src, key, None)

    raw_timeout_kind = read_field(error, 'timeout_kind')
    raw_elapsed_ms = read_field(error, 'elapsed_ms')
    raw_wire_bytes = read_field(error, 'wire_bytes')
    raw_streamed_bytes = read_field(error, 'streamed_bytes')

    if isinstance(call_diag, Mapping):
        transport = call_diag.get('transport') if isinstance(call_diag.get('transport'), Mapping) else {}
        ctx = call_diag.get('context') if isinstance(call_diag.get('context'), Mapping) else {}
        stream_metrics = call_diag.get('stream_metrics') if isinstance(call_diag.get('stream_metrics'), Mapping) else {}

        if raw_timeout_kind is None:
            raw_timeout_kind = call_diag.get('timeout_kind') if call_diag.get('timeout_kind') is not None else transport.get('timeout_kind')
        if raw_elapsed_ms is None:
            raw_elapsed_ms = call_diag.get('elapsed_ms') if call_diag.get('elapsed_ms') is not None else transport.get('elapsed_ms')
        if raw_wire_bytes is None:
            raw_wire_bytes = call_diag.get('wire_bytes') if call_diag.get('wire_bytes') is not None else ctx.get('wire_bytes')
        if raw_streamed_bytes is None:
            for candidate in (
                call_diag.get('streamed_bytes'),
                transport.get('streamed_bytes'),
                stream_metrics.get('streamed_bytes'),
                stream_metrics.get('reassembled_bytes'),
            ):
                if candidate is not None:
                    raw_streamed_bytes = candidate
                    break

        stages = transport.get('stages')
        if isinstance(stages, list):
            for stage in reversed(stages):
                if not isinstance(stage, Mapping) or stage.get('result') != 'timeout':
                    continue
                if raw_timeout_kind is None and reason == 'provider_timeout':
                    raw_timeout_kind = _TIMEOUT_STAGE_TO_KIND.get(str(stage.get('stage') or ''))
                if raw_elapsed_ms is None:
                    raw_elapsed_ms = stage.get('elapsed_ms')
                break

    metrics = call_diag.get('stream_metrics', {}) if isinstance(call_diag, Mapping) else {}
    frames = _sanitize_bounded_nonneg_int(metrics.get('frames_received')) if isinstance(metrics, Mapping) else None
    if frames is not None:
        meta['frames_received'] = frames
    if (reason in ('provider_timeout', 'provider_transport_error')
            and ((frames or 0) > 0 or (_sanitize_bounded_nonneg_int(raw_streamed_bytes) or 0) > 0)):
        meta['interruption'] = 'response_body_after_frames'
    elif read_field(error, 'interruption') == 'response_body_after_frames':
        meta['interruption'] = 'response_body_after_frames'

    clean_kind = _sanitize_timeout_kind(raw_timeout_kind)
    if clean_kind is not None:
        meta['timeout_kind'] = clean_kind
    clean_elapsed = _sanitize_bounded_nonneg_int(raw_elapsed_ms)
    if clean_elapsed is not None:
        meta['elapsed_ms'] = clean_elapsed
    clean_wire = _sanitize_bounded_nonneg_int(raw_wire_bytes)
    if clean_wire is not None:
        meta['wire_bytes'] = clean_wire
    clean_streamed = _sanitize_bounded_nonneg_int(raw_streamed_bytes)
    if clean_streamed is not None:
        meta['streamed_bytes'] = clean_streamed
    return meta


def _record_failed_attempt(
    state: dict[str, Any],
    *,
    call_id: str | None,
    reason: str,
    finish_reason: str | None = None,
    usage: Any = None,
    error: Any = None,
    call_diag: Mapping[str, Any] | None = None,
    timeout_kind: str | None = None,
    elapsed_ms: int | None = None,
    wire_bytes: int | None = None,
    streamed_bytes: int | None = None,
) -> None:
    attempts = state.get('failed_attempts')
    if not isinstance(attempts, list):
        attempts = []
        state['failed_attempts'] = attempts
    clean_call_id = _clip(call_id, 128) if isinstance(call_id, str) and call_id else None
    clean_reason = _clip(reason, 100)
    if clean_call_id and any(
        isinstance(item, Mapping) and item.get('call_id') == clean_call_id and item.get('reason') == clean_reason
        for item in attempts
    ):
        return
    meta = _extract_failure_metadata(error=error, call_diag=call_diag, reason=clean_reason)
    explicit_kind = _sanitize_timeout_kind(timeout_kind)
    if explicit_kind is not None:
        meta['timeout_kind'] = explicit_kind
    for key, val in (('elapsed_ms', elapsed_ms), ('wire_bytes', wire_bytes), ('streamed_bytes', streamed_bytes)):
        clean_val = _sanitize_bounded_nonneg_int(val)
        if clean_val is not None:
            meta[key] = clean_val
    evidence_keys = ('facts', 'read_ranges', 'calculations', 'claims', 'charts', 'tables', 'question_parts', 'acquired_source_ids', 'analyzed_market_source_ids')
    evidence = {key: state.get(key) or [] for key in evidence_keys}
    evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    entry: dict[str, Any] = {
        'evidence_progress': {key: len(value) if isinstance(value, (list, dict)) else 0 for key, value in evidence.items()},
        'evidence_fingerprint': sha256(evidence_json.encode('utf-8')).hexdigest(),
        'call_id': clean_call_id,
        'reason': clean_reason,
        'finish_reason': _clip(finish_reason, 100) if isinstance(finish_reason, str) and finish_reason else None,
        'usage': _sanitize_failed_usage(usage),
        'history_chars': _history_serialized_chars(state.get('history')),
        'at': _now_iso(),
        **meta,
    }
    attempts.append(entry)
    if len(attempts) > 20:
        del attempts[:-20]


def _failure_awareness_payload(state: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str] | None:
    raw_attempts = state.get('failed_attempts')
    attempts = [item for item in raw_attempts if isinstance(item, Mapping)] if isinstance(raw_attempts, list) else []
    raw_guidance = state.get('continuation_guidance')
    custom_guidance = _clip(raw_guidance.strip(), 500) if isinstance(raw_guidance, str) and raw_guidance.strip() else None
    if not attempts and not custom_guidance:
        return None
    recent: list[dict[str, Any]] = []
    for item in attempts[-3:]:
        row: dict[str, Any] = {'reason': item.get('reason')}
        if isinstance(item.get('finish_reason'), str) and item['finish_reason']:
            row['finish_reason'] = item['finish_reason']
        if type(item.get('history_chars')) is int:
            row['history_chars'] = item['history_chars']
        if item.get('usage') is not None:
            row['usage'] = item['usage']
        clean_kind = _sanitize_timeout_kind(item.get('timeout_kind'))
        if clean_kind is not None:
            row['timeout_kind'] = clean_kind
        if item.get('interruption') == 'response_body_after_frames':
            row['interruption'] = item['interruption']
        if isinstance(item.get('evidence_progress'), Mapping):
            row['evidence_progress'] = dict(item['evidence_progress'])
        for meta_key in ('elapsed_ms', 'wire_bytes', 'streamed_bytes', 'frames_received'):
            meta_val = _sanitize_bounded_nonneg_int(item.get(meta_key))
            if meta_val is not None:
                row[meta_key] = meta_val
        recent.append(row)
    reasons = {row.get('reason') for row in recent}
    if custom_guidance:
        guidance = custom_guidance
    elif reasons & {'provider_output_truncated', 'provider_context_limit', 'incomplete_model_response'}:
        guidance = (
            'Prior attempt failed due to output/context limit or truncated response. '
            'Do not repeat wide reads or emit overly long drafts; rely on recorded facts and read_ranges, '
            'use search_source and tiny targeted read_sources slices only if essential, and call submit_research concisely.'
        )
    elif any(row.get('interruption') == 'response_body_after_frames' for row in recent):
        guidance = (
            'The response stream was interrupted after partial frames. Use the recorded evidence and reads; '
            'choose one narrow unfinished task, persist facts with record_facts in small batches, and submit a concise partial '
            'result with gaps if necessary. Do not repeat completed work.'
        )
    elif 'provider_timeout' in reasons:
        guidance = (
            'Prior attempt timed out mid-generation. Keep reasoning concise, extract and persist '
            'key facts incrementally via smaller record_facts batches first, and avoid trying '
            'to think and draft everything in one giant turn.'
        )
    else:
        guidance = (
            'Resuming after prior incomplete attempt(s). Review recorded facts, read_ranges, and history, '
            'then proceed toward submit_research.'
        )
    return recent, guidance


def _build_failure_awareness_note(state: Mapping[str, Any]) -> str | None:
    """Build bounded tail recovery/failure context without mutating ``state['user']``."""
    payload_parts = _failure_awareness_payload(state)
    if payload_parts is None:
        return None
    recent, guidance = payload_parts
    note_payload = {'prior_failed_attempts': recent, 'recovery_guidance': guidance} if recent else {'recovery_guidance': guidance}
    return f'[Recovery context: {json.dumps(note_payload, ensure_ascii=False)}]'


def _user_with_failure_awareness(user_text: str, state: Mapping[str, Any]) -> str:
    """Inject bounded failure/continuation awareness transiently without mutating ``state['user']``."""
    payload_parts = _failure_awareness_payload(state)
    if payload_parts is None:
        return user_text
    recent, guidance = payload_parts
    try:
        parsed = json.loads(user_text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        augmented = dict(parsed)
        if recent:
            augmented['prior_failed_attempts'] = recent
        augmented['recovery_guidance'] = guidance
        return json.dumps(augmented, ensure_ascii=False)
    note_payload = {'prior_failed_attempts': recent, 'recovery_guidance': guidance} if recent else {'recovery_guidance': guidance}
    return f'{user_text}\n[Recovery context: {json.dumps(note_payload, ensure_ascii=False)}]'


def _prior_tokens(value: Any) -> int:
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)
    return (len(text) + 2) // 3 if text else 0


def _prior_research_token_budget(model: Any) -> int:
    capacity = getattr(model, 'research_capacity', None)
    profile = capacity() if callable(capacity) else None
    work_limit = getattr(profile, 'work_context_tokens', None) if profile is not None else None
    if type(work_limit) is int and work_limit > 0:
        return min(PRIOR_RESEARCH_TOKEN_BUDGET, max(1_000, int(work_limit * 0.25)))
    return PRIOR_RESEARCH_TOKEN_BUDGET


def _turn_batch_usage(state: Mapping[str, Any]) -> tuple[int, int]:
    """Compute cumulative read_sources characters and bulk tool result characters in the current turn."""
    history = state.get('history')
    if not isinstance(history, list) or not history:
        return 0, 0
    exchange = history[-1]
    if not isinstance(exchange, Mapping):
        return 0, 0
    results = exchange.get('results')
    if not isinstance(results, list) or not results:
        return 0, 0
    assistant_calls = _extract_assistant_tool_calls(exchange.get('assistant'))
    event_tool_names = _event_tool_names(state)
    read_chars = 0
    bulk_result_chars = 0
    for res in results:
        if not isinstance(res, Mapping):
            continue
        raw_content = res.get('content')
        content_len = len(raw_content) if isinstance(raw_content, str) else 0
        res_call_id = res.get('tool_call_id') if isinstance(res.get('tool_call_id'), str) else ''
        tool_name, _ = assistant_calls.get(res_call_id, ('', {}))
        if not tool_name:
            tool_name = event_tool_names.get(res_call_id, '')
        parsed = _parse_json_dict(raw_content)
        if not tool_name:
            tool_name = _infer_tool_name(parsed)
        if tool_name in BULK_COMPACT_TOOLS and not res.get('is_error'):
            bulk_result_chars += content_len
        if tool_name == 'extract_statements' and not res.get('is_error') and isinstance(parsed, Mapping):
            for sp in parsed.get('read_spans') or []:
                if isinstance(sp, Mapping) and type(sp.get('start')) is int and type(sp.get('end')) is int:
                    read_chars += max(0, sp['end'] - sp['start'])
        if tool_name in ('read_sources', 'read_pages') and not res.get('is_error') and isinstance(parsed, Mapping):
            rows = parsed.get('results')
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, Mapping) and isinstance(row.get('text'), str):
                        read_chars += len(row['text'])
            elif parsed.get('cleared') is True and isinstance(parsed.get('source_spans'), list):
                for sp in parsed['source_spans']:
                    if isinstance(sp, Mapping) and type(sp.get('start')) is int and type(sp.get('end')) is int:
                        read_chars += max(0, sp['end'] - sp['start'])
    return read_chars, bulk_result_chars


def _prior_research(
    prior: dict,
    sources: list[ResearchSource],
    as_of: datetime | None,
    *,
    token_budget: int = PRIOR_RESEARCH_TOKEN_BUDGET,
) -> tuple[dict, dict]:
    """Build the priority-bucketed, token-budgeted prior-turn summary and private state.

    Priority tiers (using ``ceil(chars / 3)`` token accounting):
      - P0: note, user_review, open_gaps, failed_source_accesses
      - P1: recorded facts and calculations (most recent first, deduped)
      - P2: recorded claims and charts (most recent first, deduped)
      - P3: eligible unchanged read_ranges index (most recent first)

    Never mid-clips citations and records any omitted items explicitly in
    ``summary['prior_omitted']`` (``I-FIN-CTX-003 NO_SILENT_GAP``).
    """
    facts_all, calcs_all, claims_all, charts_all = (
        list(prior.get(key) or []) for key in ('facts', 'calculations', 'claims', 'charts')
    )
    original_counts = {'facts': len(facts_all), 'calculations': len(calcs_all), 'claims': len(claims_all), 'charts': len(charts_all)}
    eligible = {s.source_id: s for s, code in source_eligibility(sources, as_of) if code == 'eligible'}
    hashes = prior.get('source_hashes') if isinstance(prior.get('source_hashes'), dict) else {}
    unchanged = {sid for sid, source in eligible.items() if hashes.get(sid) == source.content_sha256}
    # A prior observation is only safe to replay when its exact source version
    # remains eligible. Calculations and claims must not outlive their inputs.
    def source_valid(row: Any) -> bool:
        return isinstance(row, Mapping) and row.get('source_id') in unchanged
    def located_prior(row: Any) -> bool:
        if not source_valid(row):
            return False
        source = eligible[row['source_id']]
        quote = row.get('quotation')
        if not isinstance(quote, str) or not quote:
            return False
        location = row.get('location') if isinstance(row.get('location'), Mapping) else row
        start, end = location.get('character_start'), location.get('character_end')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source.text) or source.text[start:end] != quote:
            return False
        return any(isinstance(r, Mapping) and r.get('source_id') == row['source_id']
                   and type(r.get('character_start')) is int and type(r.get('character_end')) is int
                   and r['character_start'] <= start < end <= r['character_end'] for r in (prior.get('read_ranges') or []))
    facts_all = [row for row in facts_all if located_prior(row)]
    valid_fact_ids = {row.get('fact_id') for row in facts_all if row.get('fact_id')}
    calcs_all = [row for row in calcs_all if isinstance(row, Mapping) and row.get('inputs')
                 and all(isinstance(item, Mapping) and item.get('fact_id') in valid_fact_ids for item in row['inputs'])]
    valid_calc_ids = {row.get('calculation_id') for row in calcs_all if row.get('calculation_id')}
    claims_all = [row for row in claims_all if isinstance(row, Mapping) and row.get('citations')
                  and all(located_prior(cit) for cit in row['citations'])
                  and all(fid in valid_fact_ids for fid in (row.get('fact_ids') or []))
                  and all(cid in valid_calc_ids for cid in (row.get('calculation_ids') or []))]
    # Chart and table provenance may have multiple indirect dependencies. Do not
    # replay opaque objects when any previously recorded source changed.
    all_sources_unchanged = (bool(hashes) or (not any(original_counts.values()) and not prior.get('summary'))) and all(sid in unchanged for sid in hashes)
    if not all_sources_unchanged:
        charts_all = []
    facts_pool = facts_all[-PRIOR_LIMITS['facts']:]
    calcs_pool = calcs_all[-PRIOR_LIMITS['calculations']:]
    claims_pool = claims_all[-PRIOR_LIMITS['claims']:]
    charts_pool = charts_all[-PRIOR_LIMITS['charts']:]

    ranges_all: list[dict[str, Any]] = []
    for item in prior.get('read_ranges') or []:
        source = eligible.get(item.get('source_id'))
        start, end = item.get('character_start'), item.get('character_end')
        if (source is not None and hashes.get(source.source_id) == source.content_sha256
                and type(start) is int and type(end) is int and 0 <= start < end <= len(source.text)):
            row = {'source_id': source.source_id, 'character_start': start, 'character_end': end}
            if row not in ranges_all:
                ranges_all.append(row)

    def first_citation(claim: dict) -> dict:
        citations = claim.get('citations') or []
        return citations[0] if citations and isinstance(citations[0], dict) else {}

    open_gaps_all = [
        {'code': g['code'], 'message': g['message']}
        for g in (prior.get('gaps') or [])
        if isinstance(g, Mapping) and isinstance(g.get('code'), str) and isinstance(g.get('message'), str)
    ]
    open_gaps = open_gaps_all[-100:]
    gaps_count_dropped = max(0, len(open_gaps_all) - len(open_gaps))

    raw_failed_accesses = prior.get('failed_source_accesses') or prior.get('failed_sources') or []
    failed_accesses_deduped: list[dict[str, Any]] = []
    if isinstance(raw_failed_accesses, list):
        for item in raw_failed_accesses:
            if not isinstance(item, Mapping):
                continue
            clean_access: dict[str, Any] = {}
            if isinstance(item.get('url'), str) and item['url'].strip():
                clean_access['url'] = _clip(item['url'].strip(), 300)
            if isinstance(item.get('host'), str) and item['host'].strip():
                clean_access['host'] = _clip(item['host'].strip(), 120)
            if isinstance(item.get('status'), str) and item['status'].strip():
                clean_access['status'] = _clip(item['status'].strip(), 100)
            if isinstance(item.get('reason'), str) and item['reason'].strip():
                clean_access['reason'] = _clip(item['reason'].strip(), 100)
            if clean_access and clean_access not in failed_accesses_deduped:
                failed_accesses_deduped.append(clean_access)
    failed_accesses = failed_accesses_deduped[-50:]
    failed_access_count_dropped = max(0, len(failed_accesses_deduped) - len(failed_accesses))
    if not all_sources_unchanged:
        # Old free-form gap messages and URLs can themselves quote or identify
        # a withdrawn source and have no per-source dependency metadata.
        open_gaps = [{'code': row['code'], 'message': 'Prior gap detail requires rechecking.'} for row in open_gaps]
        failed_accesses = []

    summary: dict[str, Any] = {
        'note': 'Prior records from earlier turns; not newly verified evidence.',
        'facts': [],
        'calculations': [],
        'claims': [],
        'charts': [],
        'open_gaps': open_gaps,
        'read_ranges': [],
        'user_review': {'review': prior.get('review'), 'note': _clip(prior.get('note', ''), 1000),
                        'summary': _clip(prior.get('summary', ''), 2000) if all_sources_unchanged else ''},
    }
    if failed_accesses:
        summary['failed_source_accesses'] = failed_accesses

    budget = max(0, int(token_budget))
    p0_tokens = _prior_tokens(summary)
    omission_reserve = 64 if budget > p0_tokens + 64 else 0
    remaining = max(0, budget - p0_tokens - omission_reserve)

    # P1: facts and calculations (most recent first, deduped)
    unique_facts_desc: list[tuple[dict[str, Any], int]] = []
    seen_fact_ids: set[str] = set()
    seen_fact_sigs: set[tuple[Any, ...]] = set()
    for f in reversed(facts_pool):
        if not isinstance(f, Mapping):
            continue
        fid = f.get('fact_id')
        sig = (f.get('label'), str(f.get('value')), f.get('unit'), f.get('currency'), f.get('period'), f.get('source_id'))
        if (isinstance(fid, str) and fid in seen_fact_ids) or sig in seen_fact_sigs:
            continue
        if isinstance(fid, str) and fid:
            seen_fact_ids.add(fid)
        seen_fact_sigs.add(sig)
        row = {
            'fact_id': f['fact_id'], 'label': f['label'], 'value': f['value'], 'unit': f['unit'],
            'currency': f.get('currency'), 'period': f['period'], 'source_id': f['source_id'],
            'quotation': _clip(f.get('quotation', ''), 300),
        }
        unique_facts_desc.append((row, _prior_tokens(row) + 1))

    unique_calcs_desc: list[tuple[dict[str, Any], int]] = []
    seen_calc_ids: set[str] = set()
    seen_calc_sigs: set[tuple[Any, ...]] = set()
    for c in reversed(calcs_pool):
        if not isinstance(c, Mapping):
            continue
        cid = c.get('calculation_id')
        input_ids = tuple(item['fact_id'] for item in c.get('inputs', []) if isinstance(item, Mapping) and 'fact_id' in item)
        sig = (c.get('formula_id'), str(c.get('value')), c.get('unit'), input_ids)
        if (isinstance(cid, str) and cid in seen_calc_ids) or sig in seen_calc_sigs:
            continue
        if isinstance(cid, str) and cid:
            seen_calc_ids.add(cid)
        seen_calc_sigs.add(sig)
        row = {
            'calculation_id': c['calculation_id'], 'formula_id': c['formula_id'], 'value': c['value'],
            'unit': c['unit'], 'input_fact_ids': list(input_ids),
        }
        if c.get('return_context'):
            row['return_context'] = c['return_context']
        unique_calcs_desc.append((row, _prior_tokens(row) + 1))

    selected_calcs_desc: list[dict[str, Any]] = []
    leftover_calcs_desc: list[tuple[dict[str, Any], int]] = []
    calc_cap = remaining if not unique_facts_desc else max(1, remaining // 3)
    calc_spent = 0
    for row, cost in unique_calcs_desc:
        if calc_spent + cost <= calc_cap and cost <= remaining:
            selected_calcs_desc.append(row)
            calc_spent += cost
            remaining -= cost
        else:
            leftover_calcs_desc.append((row, cost))

    selected_facts_desc: list[dict[str, Any]] = []
    facts_budget_dropped = 0
    for row, cost in unique_facts_desc:
        if cost <= remaining:
            selected_facts_desc.append(row)
            remaining -= cost
        else:
            facts_budget_dropped += 1

    calcs_budget_dropped = 0
    for row, cost in leftover_calcs_desc:
        if cost <= remaining:
            selected_calcs_desc.append(row)
            remaining -= cost
        else:
            calcs_budget_dropped += 1
    selected_fact_ids = {row['fact_id'] for row in selected_facts_desc}
    complete_calcs = [row for row in selected_calcs_desc
                      if row['input_fact_ids'] and all(fid in selected_fact_ids for fid in row['input_fact_ids'])]
    incomplete_group_count = len(selected_calcs_desc) - len(complete_calcs)
    selected_calcs_desc = complete_calcs

    # P2: claims and charts (most recent first, deduped; never mid-clip citations)
    unique_claims_desc: list[tuple[dict[str, Any], int]] = []
    seen_claim_ids: set[str] = set()
    for c in reversed(claims_pool):
        if not isinstance(c, Mapping):
            continue
        cid = c.get('claim_id')
        if isinstance(cid, str) and cid in seen_claim_ids:
            continue
        if isinstance(cid, str) and cid:
            seen_claim_ids.add(cid)
        cit = first_citation(c)
        row = {
            'claim_id': c['claim_id'], 'role': c['role'], 'text': _clip(c['text'], 500),
            'source_id': cit.get('source_id'),
            'quotation': _clip(cit.get('quotation', ''), 300),
            'citations': [{'source_id': item['source_id'], 'content_sha256': eligible[item['source_id']].content_sha256,
                           'quotation': item['quotation'], 'location': item.get('location') or {
                               'character_start': item.get('character_start'), 'character_end': item.get('character_end')}}
                          for item in c['citations']],
            'fact_ids': list(c.get('fact_ids') or []),
            'calculation_ids': list(c.get('calculation_ids') or []),
        }
        unique_claims_desc.append((row, _prior_tokens(row) + 1))

    selected_claims_desc: list[dict[str, Any]] = []
    claims_budget_dropped = 0
    claims_group_dropped = 0
    selected_calc_ids = {row['calculation_id'] for row in selected_calcs_desc}
    for row, cost in unique_claims_desc:
        if not (all(fid in selected_fact_ids for fid in row['fact_ids'])
                and all(cid in selected_calc_ids for cid in row['calculation_ids'])):
            claims_group_dropped += 1
        elif cost <= remaining:
            selected_claims_desc.append(row)
            remaining -= cost
        else:
            claims_budget_dropped += 1

    unique_charts_desc: list[tuple[dict[str, Any], int]] = []
    seen_chart_ids: set[str] = set()
    for c in reversed(charts_pool):
        if not isinstance(c, Mapping):
            continue
        cid = c.get('chart_id')
        if isinstance(cid, str) and cid in seen_chart_ids:
            continue
        if isinstance(cid, str) and cid:
            seen_chart_ids.add(cid)
        row = {
            'chart_id': c['chart_id'], 'chart_type': c['chart_type'],
            'title': _clip(c.get('title', ''), 200), 'replaces_chart_id': c.get('replaces_chart_id'),
        }
        unique_charts_desc.append((row, _prior_tokens(row) + 1))

    selected_charts_desc: list[dict[str, Any]] = []
    charts_budget_dropped = 0
    for row, cost in unique_charts_desc:
        if cost <= remaining:
            selected_charts_desc.append(row)
            remaining -= cost
        else:
            charts_budget_dropped += 1

    # P3: read_ranges index (most recent first)
    selected_ranges_desc: list[dict[str, Any]] = []
    ranges_budget_dropped = 0
    for row in reversed(ranges_all):
        cost = _prior_tokens(row) + 1
        if cost <= remaining:
            selected_ranges_desc.append(row)
            remaining -= cost
        else:
            ranges_budget_dropped += 1

    summary['facts'] = list(reversed(selected_facts_desc))
    summary['calculations'] = list(reversed(selected_calcs_desc))
    summary['claims'] = list(reversed(selected_claims_desc))
    summary['charts'] = list(reversed(selected_charts_desc))
    summary['read_ranges'] = list(reversed(selected_ranges_desc))

    truncated = {
        'facts': len(facts_all) - len(facts_pool),
        'calculations': len(calcs_all) - len(calcs_pool),
        'claims': len(claims_all) - len(claims_pool),
        'charts': len(charts_all) - len(charts_pool),
    }
    if any(truncated.values()):
        summary['truncated'] = truncated

    prior_omitted: dict[str, dict[str, Any]] = {}
    for category, count_drop, budget_drop in (
        ('open_gaps', gaps_count_dropped, 0),
        ('failed_source_accesses', failed_access_count_dropped, 0),
        ('facts', truncated['facts'], facts_budget_dropped),
        ('calculations', truncated['calculations'], calcs_budget_dropped),
        ('claims', truncated['claims'], claims_budget_dropped),
        ('charts', truncated['charts'], charts_budget_dropped),
        ('read_ranges', 0, ranges_budget_dropped),
    ):
        total_drop = count_drop + budget_drop
        if total_drop <= 0:
            continue
        if count_drop > 0 and budget_drop > 0:
            reason = 'token_budget_and_count_limit'
        elif budget_drop > 0:
            reason = 'token_budget'
        else:
            reason = 'count_limit'
        prior_omitted[category] = {'count': total_drop, 'reason': reason}
    invalid_counts = {key: original_counts[key] - len(items) for key, items in
                      (('facts', facts_all), ('calculations', calcs_all), ('claims', claims_all), ('charts', charts_all))}
    if any(invalid_counts.values()):
        summary['ineligible_prior_counts'] = {key: count for key, count in invalid_counts.items() if count}
    if incomplete_group_count or claims_group_dropped:
        summary['incomplete_evidence_group_count'] = incomplete_group_count + claims_group_dropped
    if prior_omitted:
        summary['prior_omitted'] = prior_omitted

    # Table metadata is bounded; full cells remain in durable content.
    prior_tables = [t for t in (prior.get('tables') or []) if isinstance(t, Mapping) and isinstance(t.get('table_id'), str)] if all_sources_unchanged else []
    summary['tables'] = prior_tables[-50:]
    prior_state = {
        'prior_table_ids': [t['table_id'] for t in prior_tables],
        'prior_facts': [{key: f.get(key) for key in ('fact_id', 'label', 'value', 'unit', 'currency', 'period', 'source_id',
                                                      'character_start', 'character_end', 'quotation')} for f in facts_pool],
        'prior_calculations': [{key: c.get(key) for key in ('calculation_id', 'formula_id', 'expression', 'inputs',
                                                             'value', 'unit', 'currency', 'return_context')} for c in calcs_pool],
        'prior_claim_ids': [c['claim_id'] for c in claims_all],
        'prior_claims': [dict(c) for c in claims_pool if isinstance(c, Mapping) and isinstance(c.get('claim_id'), str) and c.get('citations') and all(isinstance(cit, Mapping) and cit.get('source_id') in eligible for cit in c['citations'])],
        'prior_claims_meta': [
            {
                'claim_id': c['claim_id'],
                'source_ids': list(dict.fromkeys(
                    cit['source_id']
                    for cit in (c.get('citations') or [])
                    if isinstance(cit, Mapping) and isinstance(cit.get('source_id'), str) and cit['source_id']
                )),
            }
            for c in claims_all
            if isinstance(c, Mapping) and isinstance(c.get('claim_id'), str) and c['claim_id']
        ],
        'prior_chart_ids': [c['chart_id'] for c in charts_all],
        'prior_cited_claims': sum(1 for c in claims_all if c.get('citations')),
        'prior_read_ranges': [{'source_id': r['source_id'], 'start': r['character_start'], 'end': r['character_end']} for r in ranges_all],
    }
    return summary, prior_state


def _narrow_recovery_view(state: Mapping[str, Any], source_by_id: Mapping[str, ResearchSource]) -> tuple[str, list[dict[str, Any]]] | None:
    attempts = state.get('failed_attempts')
    if not (isinstance(attempts, list) and attempts and isinstance(attempts[-1], Mapping)
            and (attempts[-1].get('interruption') == 'response_body_after_frames'
                 or attempts[-1].get('reason') in {'provider_timeout', 'provider_output_truncated',
                                                 'provider_context_limit', 'incomplete_model_response', 'no_submission'})
            and state.get('continuation_guidance')):
        return None
    try:
        original = json.loads(state['user'])
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(original, dict):
        return None
    # A prompt view only: the checkpoint and enforcement state remain authoritative.
    view = {key: original[key] for key in ('question', 'research_as_of', 'research_clock', 'focus_citation') if key in original}
    capabilities = original.get('available_capabilities')
    if isinstance(capabilities, Mapping):
        view['tool_availability'] = {key: value for key, value in capabilities.items()
                                     if key.endswith('_available') and type(value) is bool}
    try:
        as_of = datetime.fromisoformat(original['research_as_of']) if original.get('research_as_of') else None
    except (TypeError, ValueError):
        return None
    eligible = {src.source_id: src for src, code in source_eligibility(list(source_by_id.values()), as_of)
                if code == 'eligible'}
    if (view.get('focus_citation') or {}).get('source_id') not in eligible:
        view.pop('focus_citation', None)
    recorded_hashes = {
        row['source_id']: row['content_sha256'] for row in [*(original.get('sources') or []), *(state.get('_sources') or [])]
        if isinstance(row, Mapping) and row.get('source_id') and row.get('content_sha256')
    }
    recorded_hashes.update(state.get('source_hashes') or {})
    unchanged = {sid for sid, src in eligible.items() if recorded_hashes.get(sid) == src.content_sha256}
    view['sources'] = [{'source_id': src.source_id, 'title': src.title, 'content_sha256': src.content_sha256,
                        'permissions': src.permissions.model_dump()}
                       for src in list(eligible.values())[:50]]
    view['omitted_source_count'] = max(0, len(eligible) - 50)
    # Historical envelopes can contain original text. Retain complete exchanges only
    # while every source previously read is still eligible and fits the recovery window.
    permitted_history = bool(recorded_hashes) and recorded_hashes.keys() <= unchanged and all(row.get('source_id') in unchanged for key in ('prior_read_ranges', 'read_ranges')
                            for row in (state.get(key) or []) if isinstance(row, Mapping))
    history = (state.get('history') or []) if permitted_history else []
    chosen = []
    remaining = 6000
    for exchange in reversed(history):
        if not isinstance(exchange, dict):
            break
        size = len(json.dumps(exchange, ensure_ascii=False, default=str))
        if size > remaining:
            break
        remaining -= size
        chosen.append(exchange)
    chosen.reverse()
    retained_reads: dict[str, list[tuple[int, int]]] = {}
    for exchange in chosen:
        for result in exchange.get('results', []):
            try:
                payload = json.loads(result.get('content', ''))
            except (ValueError, TypeError, AttributeError):
                continue
            for row in payload.get('results', []) if isinstance(payload, Mapping) else []:
                if not isinstance(row, Mapping):
                    continue
                sid, start, end, text = row.get('source_id'), row.get('start'), row.get('end'), row.get('text')
                if (sid in unchanged and type(start) is int and type(end) is int and 0 <= start < end <= len(eligible[sid].text)
                        and isinstance(text, str) and text == eligible[sid].text[start:end]):
                    retained_reads.setdefault(sid, []).append((start, end))
    for key in ('prior_facts', 'facts', 'read_ranges'):
        rows = state.get(key) or []
        selected = []
        for item in reversed(rows):
            if not isinstance(item, Mapping) or item.get('source_id') not in unchanged:
                continue
            candidate = dict(item)
            if key == 'read_ranges':
                start, end = item.get('start'), item.get('end')
                src = eligible[item['source_id']]
                if (type(start) is int and type(end) is int and 0 <= start < end <= len(src.text)
                        and not any(a <= start and end <= b for a, b in retained_reads.get(item['source_id'], []))):
                    excerpt_end = min(end, start + 320)
                    candidate.update(excerpt=src.text[start:excerpt_end], excerpt_end=excerpt_end,
                                     content_sha256=src.content_sha256)
            elif isinstance(candidate.get('quotation'), str):
                start, end = candidate.get('character_start'), candidate.get('character_end')
                if (type(start) is int and type(end) is int
                        and any(a <= start and end <= b for a, b in retained_reads.get(item['source_id'], []))):
                    candidate.pop('quotation', None)
                else:
                    candidate['quotation'] = candidate['quotation'][:320]
            selected.append(candidate)
            if len(selected) >= 12:
                break
        if selected:
            view['recorded_' + key] = list(reversed(selected))
        if len(rows) > len(selected):
            view['omitted_' + key + '_count'] = len(rows) - len(selected)
    from .research_partial_answer import question_parts
    components = question_parts(str(original.get('question', '')), state, set(eligible))
    next_part = next((part['question'] for part in components if part['status'] != 'answered'), None)
    if next_part and next_part != original.get('question'):
        view['next_question_part'] = next_part
    view['recovery_scope'] = ('Work on next_question_part first. Use the supplied verified locations; '
                              'record a supported result or checkpoint a specific gap.')
    return json.dumps(view, ensure_ascii=False), chosen


def _evidence_projection(state: Mapping[str, Any], source_by_id: Mapping[str, ResearchSource], hashes: Mapping[str, Any], as_of: datetime | None) -> tuple[list[dict[str, Any]], int]:
    """Project only located, unchanged, already-read observations as whole groups."""
    eligible = {s.source_id: s for s, code in source_eligibility(list(source_by_id.values()), as_of) if code == 'eligible'}
    ranges = [r for key in ('read_ranges', 'prior_read_ranges') for r in (state.get(key) or []) if isinstance(r, Mapping)]
    def located(row: Mapping[str, Any]) -> dict[str, Any] | None:
        sid = row.get('source_id')
        source = eligible.get(sid)
        if source is None or not hashes.get(sid) or hashes[sid] != source.content_sha256:
            return None
        loc = row.get('location') if isinstance(row.get('location'), Mapping) else row
        start, end = loc.get('character_start'), loc.get('character_end')
        quote = row.get('quotation')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source.text):
            return None
        if not isinstance(quote, str) or source.text[start:end] != quote:
            return None
        if not any(r.get('source_id') == sid and type(r.get('start')) is int and type(r.get('end')) is int
                   and r['start'] <= start < end <= r['end'] for r in ranges):
            return None
        return {'source_id': sid, 'content_sha256': source.content_sha256, 'start': start, 'end': end, 'quotation': quote}
    facts: dict[str, dict[str, Any]] = {}
    for row in [*(state.get('prior_facts') or []), *(state.get('facts') or [])]:
        if not isinstance(row, Mapping) or not isinstance(row.get('fact_id'), str):
            continue
        citation = located(row)
        if citation:
            facts[row['fact_id']] = {key: row.get(key) for key in ('fact_id', 'label', 'value', 'unit', 'currency', 'period')}
            facts[row['fact_id']]['citation'] = citation
    groups: list[dict[str, Any]] = []
    valid_calculations: set[str] = set()
    calculation_groups: dict[str, dict[str, Any]] = {}
    for row in [*(state.get('prior_calculations') or []), *(state.get('calculations') or [])]:
        if not isinstance(row, Mapping) or not isinstance(row.get('calculation_id'), str):
            continue
        inputs = [item.get('fact_id') for item in (row.get('inputs') or []) if isinstance(item, Mapping)]
        if inputs and all(item in facts for item in inputs):
            valid_calculations.add(row['calculation_id'])
            calculation_groups[row['calculation_id']] = {'calculation': {key: row.get(key) for key in ('calculation_id', 'formula_id', 'value', 'unit')},
                                                        'facts': [facts[item] for item in inputs]}

    # A saved question part is atomic: include all of its located references,
    # answer and unresolved limitation together, or omit the whole part.
    for part in state.get('question_parts') or []:
        if not isinstance(part, Mapping) or not isinstance(part.get('question'), str):
            continue
        part_facts, part_citations = [], []
        valid = True
        for ref in part.get('references') or []:
            if not isinstance(ref, Mapping):
                valid = False
                break
            if ref.get('fact_id'):
                fact = facts.get(ref['fact_id'])
                if fact is None:
                    valid = False
                    break
                part_facts.append(fact)
            else:
                src = eligible.get(ref.get('source_id'))
                start, end = ref.get('character_start'), ref.get('character_end')
                if src is None or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(src.text):
                    valid = False
                    break
                citation = located({**ref, 'quotation': src.text[start:end]})
                if citation is None:
                    valid = False
                    break
                part_citations.append(citation)
        if valid and (part_facts or part_citations):
            groups.append({'question_part': {key: part.get(key) for key in ('question', 'status', 'answer', 'reason')},
                           'facts': part_facts, 'citations': part_citations})
        elif valid and not part.get('answer') and part.get('reason') and all(
                sid in eligible and digest == eligible[sid].content_sha256 for sid, digest in hashes.items()):
            groups.append({'question_part': {key: part.get(key) for key in ('question', 'status', 'reason')},
                           'facts': [], 'citations': []})
    claims = [row for row in [*(state.get('prior_claims') or []), *(state.get('claims') or [])] if isinstance(row, Mapping)]
    claims.sort(key=lambda row: (row.get('role') != 'counterargument', str(row.get('claim_id') or '')))
    for row in claims:
        citations = [located(cit) for cit in (row.get('citations') or []) if isinstance(cit, Mapping)]
        if (citations and all(citations) and len(citations) == len(row.get('citations') or [])
                and all(fid in facts for fid in (row.get('fact_ids') or []))
                and all(cid in valid_calculations for cid in (row.get('calculation_ids') or []))):
            groups.append({'claim': {key: row.get(key) for key in ('claim_id', 'role', 'text', 'challenges_claim_ids')},
                           'citations': citations, 'facts': [facts[fid] for fid in (row.get('fact_ids') or [])],
                           'calculations': [calculation_groups[cid] for cid in (row.get('calculation_ids') or [])]})
    groups.extend(calculation_groups.values())
    groups.extend({'fact': fact} for fact in facts.values())
    return groups, 0


def build_model_turn_input(
    *,
    state: Mapping[str, Any],
    source_by_id: Mapping[str, ResearchSource],
    system_prompt: str = SYSTEM,
    tools: Sequence[ModelToolDefinition] = TOOLS,
    library: ResearchLibrary | None = None,
    has_fin_data: bool = False,
) -> ModelTurnInput:
    """Assemble the immutable prompt prefix and transient tail_note for a native turn."""
    # Recovery can reopen an old checkpoint after source permissions or contents
    # changed. Old tool outputs are opaque and may quote those sources, so drop
    # whole paired exchanges in this case rather than leak or break pairing.
    try:
        original = json.loads(state.get('user', ''))
    except (TypeError, ValueError):
        original = None
    # Apply minimization to the wire projection; the account-owned turn keeps
    # its original question and instruction sequence for replay and correction.
    if isinstance(original, dict) and isinstance(original.get('question'), str):
        clean = minimize_identity(original['question'])
        if clean != original['question']:
            original = {**original, 'question': clean}
            state = {**state, 'user': json.dumps(original, ensure_ascii=False)}
    revoked = False
    hashes: dict[str, Any] = {}
    as_of = None
    invalid_as_of = False
    if isinstance(original, dict):
        try:
            as_of = datetime.fromisoformat(original['research_as_of']) if original.get('research_as_of') else None
        except (TypeError, ValueError):
            as_of = None
            invalid_as_of = True
        eligible = {src.source_id: src for src, code in source_eligibility(list(source_by_id.values()), as_of) if code == 'eligible'}
        snapshots = [*(original.get('sources') or []), *(state.get('_sources') or [])]
        hashes = {row['source_id']: row.get('content_sha256') for row in snapshots
                  if isinstance(row, Mapping) and isinstance(row.get('source_id'), str)}
        hashes.update(state.get('source_hashes') or {})
        used_ids = {row.get('source_id') for key in ('read_ranges', 'focus_ranges', 'prior_read_ranges', 'facts', 'prior_facts')
                    for row in (state.get(key) or []) if isinstance(row, Mapping) and isinstance(row.get('source_id'), str)}
        if state.get('history'):
            # Search results can contain snippets before a read range exists.
            # Conservatively invalidate opaque history if any source that was
            # originally eligible is no longer eligible at the same version.
            used_ids.update(state.get('source_hashes') or {})
            used_ids.update(row.get('source_id') for row in snapshots if isinstance(row, Mapping)
                            and row.get('eligibility') == 'eligible' and isinstance(row.get('source_id'), str))
        prior_view = original.get('prior_research')
        if isinstance(prior_view, Mapping):
            used_ids.update(row.get('source_id') for key in ('facts', 'claims', 'read_ranges')
                            for row in (prior_view.get(key) or []) if isinstance(row, Mapping)
                            and isinstance(row.get('source_id'), str))
        revoked = invalid_as_of or any(sid not in eligible or not hashes.get(sid) or hashes[sid] != eligible[sid].content_sha256
                      for sid in used_ids)
        if revoked:
            safe_user = dict(original)
            safe_user['sources'] = [row for row in (original.get('sources') or [])
                                    if isinstance(row, Mapping) and row.get('source_id') in eligible
                                    and hashes.get(row.get('source_id')) == eligible[row['source_id']].content_sha256]
            safe_user.pop('focus_citation', None)
            prior_view = safe_user.get('prior_research')
            if isinstance(prior_view, Mapping):
                review = prior_view.get('user_review') if isinstance(prior_view.get('user_review'), Mapping) else {}
                safe_user['prior_research'] = {'note': 'Some prior evidence is unavailable or changed; recheck before reuse.',
                    'user_review': {'review': review.get('review'), 'note': review.get('note', ''), 'summary': ''}}
            state = {**state, 'user': json.dumps(safe_user, ensure_ascii=False), 'history': []}
    failure_payload = _failure_awareness_payload(state)
    lib_cov = None
    if library is not None:
        try:
            lib_cov = library.coverage()
        except Exception:
            lib_cov = None
    zero_yield = int(state.get('zero_yield_turns') or 0)
    convergence_hint = (
        'Recent turns produced no new facts, calculations, charts, or sources; '
        'synthesize existing evidence or call submit_research (use status="partial" with open gaps if blocked).'
        if zero_yield >= 3
        else None
    )
    include_live = bool(
        state.get('coverage_attention_active')
        or (int(state.get('compaction_count') or 0) > 0)
        or zero_yield >= 3
        or int(state.get('presubmit_nudges') or 0) > 0
    )
    narrow_view = _narrow_recovery_view(state, source_by_id) if not revoked else None
    tail_note = None if revoked else insights.build_tail_note(
        state,
        source_by_id,
        include_live_attention=include_live,
        library_coverage=lib_cov,
        failure_payload=failure_payload,
        convergence_hint=convergence_hint,
        has_fin_data=has_fin_data,
    )
    try:
        accepts_tail = 'tail_note' in inspect.signature(ModelTurnInput).parameters
    except (TypeError, ValueError):
        accepts_tail = False
    if narrow_view is not None and failure_payload is not None:
        recent, guidance = failure_payload
        latest = recent[-1] if recent else {}
        tail_note = '[Recovery context: ' + json.dumps({'last_failure': {k: latest[k] for k in ('reason', 'interruption') if k in latest}, 'recovery_guidance': guidance}, ensure_ascii=False, sort_keys=True) + ']'
    projection, projection_omitted = _evidence_projection(state, source_by_id, hashes, as_of) if not invalid_as_of else ([], 0)
    prompt_user, prompt_history = narrow_view if narrow_view is not None else (state['user'], state['history'])
    if projection:
        # Reuse complete text already retained in the request, while keeping its
        # source/version/range reference in the projected group.
        retained_ranges: list[tuple[str, str, int, int]] = []
        def retain(sid: Any, digest: Any, start: Any, end: Any, text: Any) -> None:
            source = source_by_id.get(sid) if isinstance(sid, str) else None
            if (source is not None and digest == hashes.get(sid) == source.content_sha256
                    and type(start) is int and type(end) is int and 0 <= start < end <= len(source.text)
                    and isinstance(text, str) and text == source.text[start:end]):
                retained_ranges.append((sid, digest, start, end))
        for exchange in prompt_history:
            for result in exchange.get('results', []) if isinstance(exchange, Mapping) else []:
                try:
                    payload = json.loads(result.get('content', ''))
                except (TypeError, ValueError, AttributeError):
                    continue
                for row in payload.get('results', []) if isinstance(payload, Mapping) else []:
                    if isinstance(row, Mapping) and row.get('status') == 'read':
                        retain(row.get('source_id'), row.get('content_sha256'), row.get('start'), row.get('end'), row.get('text'))
        try:
            prompt_object = json.loads(prompt_user)
        except (TypeError, ValueError):
            prompt_object = None
        if isinstance(prompt_object, Mapping):
            for row in prompt_object.get('recorded_read_ranges', []):
                if isinstance(row, Mapping):
                    retain(row.get('source_id'), row.get('content_sha256'), row.get('start'), row.get('excerpt_end'), row.get('excerpt'))
        # Select whole groups only after accounting for text in this request.
        selected = []
        for group in projection:
            candidate = json.loads(json.dumps(group, ensure_ascii=False))
            prior = list(retained_ranges)
            def remove_repeated(node: Any) -> None:
                if isinstance(node, dict):
                    sid, digest, start, end, quote = (node.get('source_id'), node.get('content_sha256'),
                                                       node.get('start'), node.get('end'), node.get('quotation'))
                    if isinstance(quote, str) and type(start) is int and type(end) is int:
                        if any(sid == s and digest == h and a <= start < end <= b for s, h, a, b in prior):
                            node.pop('quotation')
                            node['in_existing_input'] = True
                        else:
                            prior.append((sid, digest, start, end))
                    for child in node.values():
                        remove_repeated(child)
                elif isinstance(node, list):
                    for child in node:
                        remove_repeated(child)
            remove_repeated(candidate)
            if len(json.dumps([*selected, candidate], ensure_ascii=False)) <= 6000:
                selected.append(candidate)
                retained_ranges = prior
            else:
                projection_omitted += 1
        projection = selected
    if (projection or projection_omitted) and not prompt_history:
        try:
            user_object = json.loads(prompt_user)
        except (TypeError, ValueError):
            user_object = None
        if isinstance(user_object, dict):
            user_object['located_evidence'] = projection
            user_object['located_evidence_note'] = 'Locations are checked; semantic support is not verified.'
            if projection_omitted:
                user_object['located_evidence_omitted'] = {'count': projection_omitted, 'reason': 'projection_capacity'}
            prompt_user = json.dumps(user_object, ensure_ascii=False, sort_keys=True)
            projection = []
            projection_omitted = 0
    if projection or projection_omitted:
        tail_note = (tail_note + '\n' if tail_note else '') + '[Located evidence; semantic support is not verified]\n' + json.dumps({'groups': projection, 'omitted_for_capacity': projection_omitted}, ensure_ascii=False, sort_keys=True)
    history_items = tuple(
        ModelTurnExchange(
            ModelAssistantContinuation(item['assistant']),
            tuple(ModelToolResult(**result) for result in item['results']),
        )
        for item in prompt_history
    )
    if accepts_tail:
        return ModelTurnInput(
            system_prompt,
            prompt_user,
            tuple(tools),
            history_items,
            tail_note=tail_note,
            user_instructions=tuple(dict(item, text=minimize_identity(item['text'])) for item in sorted(
                (row for row in (state.get('user_instructions') or [])
                 if isinstance(row, Mapping) and isinstance(row.get('text'), str)),
                key=lambda row: row.get('sequence') if type(row.get('sequence')) is int else 0)),
        )
    user_text = prompt_user if prompt_history else _user_with_failure_awareness(prompt_user, state)
    return ModelTurnInput(
        system_prompt,
        user_text,
        tuple(tools),
        history_items,
    )
