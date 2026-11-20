"""Bounded private diagnostics without model output or arbitrary field names."""
from contextvars import ContextVar
import hashlib
import logging
import math
import os
import re
from pathlib import Path
from typing import AbstractSet, Any
from pydantic import BaseModel, ValidationError
from .transport_diagnostics import VALID_TIMEOUT_KINDS

_finish: ContextVar[str] = ContextVar('research_provider_finish', default='unknown')
_ALLOWED_ERRORS = frozenset({'missing','extra_forbidden','list_type','dict_type','model_type','string_type',
    'int_type','int_parsing','bool_type','bool_parsing','literal_error','too_long','too_short',
    'string_too_long','string_too_short','greater_than_equal','less_than_equal','float_type',
    'decimal_parsing','decimal_type','value_error','union_tag_invalid','union_tag_not_found'})

_FINISH_REASON_MAP = {
    'length': 'provider_length',
    'max_tokens': 'provider_length',
    'model_context_window_exceeded': 'provider_context_limit',
    'content_filter': 'provider_refusal',
    'refusal': 'provider_refusal',
    'stop': 'provider_stop',
    'end_turn': 'provider_stop',
    'stop_sequence': 'provider_stop',
    'tool_use': 'provider_tool_calls',
    'tool_calls': 'provider_tool_calls',
    'function_call': 'provider_tool_calls',
}
_MESSAGES_STOP_REASONS = frozenset({
    'end_turn',
    'max_tokens',
    'stop_sequence',
    'tool_use',
    'pause_turn',
    'refusal',
    'model_context_window_exceeded',
    'stop',
    'length',
    'tool_calls',
    'content_filter',
})
_USAGE_KEYS = (
    'input_tokens',
    'output_tokens',
    'total_tokens',
    'cache_creation_input_tokens',
    'cache_read_input_tokens',
)


def normalize_finish_reason(value: object) -> str:
    """Map provider finish/stop reasons to bounded canonical diagnostic labels."""
    return _FINISH_REASON_MAP.get(value, 'unknown') if isinstance(value, str) else 'unknown'


def set_finish_reason(value: object) -> None:
    _finish.set(normalize_finish_reason(value))


def get_finish_reason() -> str:
    return _finish.get()


def safe_stop_reason(value: object) -> str:
    """Return an allowlisted stop_reason label, never retaining arbitrary provider strings."""
    if value is None:
        return 'absent'
    if isinstance(value, str) and value in _MESSAGES_STOP_REASONS:
        return value
    return 'unrecognized'


def safe_usage_diagnostics(usage: object) -> dict[str, int]:
    """Extract bounded non-negative integer token counts from a usage object or dict."""
    if usage is None:
        return {}
    result: dict[str, int] = {}
    for key in _USAGE_KEYS:
        value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        if type(value) is int and 0 <= value <= 10**9:
            result[key] = value
    return result

def safe_validation_errors(error: ValidationError, schema: type[BaseModel]) -> list[dict[str, Any]]:
    """Only schema-declared property names, array indices and fixed error codes survive."""
    allowed: set[str] = set()
    def collect(node: object) -> None:
        if isinstance(node, dict):
            properties = node.get('properties')
            if isinstance(properties, dict):
                allowed.update(key for key in properties if isinstance(key, str))
            for value in node.values(): collect(value)
        elif isinstance(node, list):
            for value in node: collect(value)
    collect(schema.model_json_schema())
    result = []
    for item in error.errors(include_url=False, include_context=False, include_input=False)[:20]:
        path = [part if isinstance(part, str) and part in allowed else part if type(part) is int and 0 <= part <= 10000 else '<unknown>'
                for part in item.get('loc', ())[:12]]
        code = item.get('type')
        result.append({'path': path, 'type': code if code in _ALLOWED_ERRORS else 'other'})
    return sanitize_diagnostic_errors(result, schema=schema)

_TRANSPORT_STAGES = frozenset({'dns', 'tcp', 'tls', 'request_write', 'response_headers', 'response_body', 'deadline', 'cancelled'})
_TRANSPORT_RESULTS = frozenset({'ok', 'timeout', 'error', 'cancelled'})
_TRANSPORT_SUBMISSION = frozenset({'not_started', 'started_unknown', 'response_received', 'unavailable'})

def sanitize_transport_diagnostic(value: object) -> dict[str, Any]:
    """Accept only bounded fixed transport metadata, never arbitrary provider data."""
    if not isinstance(value, dict):
        return {'submission_state': 'unavailable', 'stages': []}
    state = value.get('submission_state')
    state = state if isinstance(state, str) and state in _TRANSPORT_SUBMISSION else 'unavailable'
    stages = []
    items = value.get('stages')
    if isinstance(items, list):
        for item in items[:64]:
            if not isinstance(item, dict):
                continue
            stage, result, elapsed = item.get('stage'), item.get('result'), item.get('elapsed_ms')
            if not isinstance(stage, str) or stage not in _TRANSPORT_STAGES or not isinstance(result, str) or result not in _TRANSPORT_RESULTS or type(elapsed) is not int or not 0 <= elapsed <= 600_000:
                continue
            safe = {'stage': stage, 'result': result, 'elapsed_ms': elapsed}
            for key, lower, upper in (('errno', 0, 65535), ('status_code', 100, 599)):
                number = item.get(key)
                if type(number) is int and lower <= number <= upper:
                    safe[key] = number
            stages.append(safe)
    safe_result: dict[str, Any] = {'submission_state': state, 'stages': stages}
    timeout_kind = value.get('timeout_kind')
    if isinstance(timeout_kind, str) and timeout_kind in VALID_TIMEOUT_KINDS:
        safe_result['timeout_kind'] = timeout_kind
    return safe_result


_STREAM_BLOCK_TYPES = ('thinking', 'text', 'tool_use', 'redacted_thinking')
_SAFE_TOOL_NAME = re.compile(r'^[a-z][a-z0-9_]{0,63}$')


def sanitize_stream_metrics(
    value: object,
    *,
    allowed_tool_names: AbstractSet[str] | None = None,
) -> dict[str, Any]:
    """Retain only bounded, content-free SSE stream metrics."""
    if not isinstance(value, dict):
        return {}

    def bounded_int(raw: object, upper: int) -> int | None:
        return raw if type(raw) is int and 0 <= raw <= upper else None

    frames = bounded_int(value.get('frames_received'), 10**7)
    if frames is None:
        frames = 0
    ttft = bounded_int(value.get('ttft_ms'), 3_600_000)
    last_frame = bounded_int(value.get('last_frame_elapsed_ms'), 3_600_000)
    reassembled = bounded_int(value.get('reassembled_bytes'), 10**8)
    if reassembled is None:
        reassembled = 0

    raw_blocks = value.get('block_bytes_by_type')
    blocks: dict[str, int] = {}
    for btype in _STREAM_BLOCK_TYPES:
        count = bounded_int(raw_blocks.get(btype), 10**8) if isinstance(raw_blocks, dict) else None
        blocks[btype] = count if count is not None else 0

    raw_tools = value.get('active_tool_names')
    tools: list[str] = []
    if isinstance(raw_tools, (list, tuple)):
        for item in raw_tools[:64]:
            if not isinstance(item, str):
                continue
            if item == 'unrecognized':
                clean_name = 'unrecognized'
            elif allowed_tool_names is not None:
                clean_name = item if item in allowed_tool_names else 'unrecognized'
            elif _SAFE_TOOL_NAME.fullmatch(item):
                clean_name = item
            else:
                clean_name = 'unrecognized'
            if clean_name not in tools:
                tools.append(clean_name)
                if len(tools) >= 32:
                    break

    return {
        'frames_received': frames,
        'ttft_ms': ttft,
        'last_frame_elapsed_ms': last_frame,
        'reassembled_bytes': reassembled,
        'block_bytes_by_type': blocks,
        'active_tool_names': tools,
    }


def warn_if_timeout_output_mismatch(
    max_output_tokens: int | None,
    timeout_seconds: float | None,
    *,
    streaming: bool | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """Log a non-blocking warning when output cap >= 8192 and timeout < 240s."""
    if (
        type(max_output_tokens) is not int
        or max_output_tokens < 8192
        or not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds < 240
    ):
        return False
    target_logger = logger or logging.getLogger(__name__)
    target_logger.warning(
        'llm_timeout_output_cap_mismatch max_output_tokens=%d timeout_seconds=%.1f recommended_min_timeout_seconds=%d streaming=%s',
        max_output_tokens,
        float(timeout_seconds),
        240,
        'unknown' if streaming is None else str(bool(streaming)).lower(),
    )
    return True


_IDENTIFIER = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')
_HASH = re.compile(r'^[a-f0-9]{64}$')
_PARAMETER_KEYS = frozenset({'temperature', 'top_p', 'max_tokens', 'max_completion_tokens', 'seed'})

def fingerprint(value: str | bytes) -> str:
    """Hash caller-supplied canonical input without persisting it."""
    return hashlib.sha256(value.encode('utf-8') if isinstance(value, str) else value).hexdigest()

def safe_diagnostic_context(**kwargs: Any) -> dict[str, Any]:
    """Keep only bounded metadata; never retain prompts or arbitrary parameters."""
    result: dict[str, Any] = {}
    for key in ('request_id', 'operation_id', 'call_id', 'parent_call_id', 'model', 'contract'):
        value = kwargs.get(key)
        if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            result[key] = value
    for key, values in {'provider': {'openai-compatible', 'openai_compatible', 'glm_coding', 'anthropic_messages', 'local', 'unknown'}, 'output_mode': {'json_object', 'json_schema', 'tool', 'tool_call', 'unknown'}}.items():
        value = kwargs.get(key)
        if isinstance(value, str) and value in values:
            result[key] = value
    for key in ('input_hash', 'system_hash', 'source_range_fingerprint'):
        value = kwargs.get(key)
        if isinstance(value, str) and _HASH.fullmatch(value):
            result[key] = value
    wire_bytes = kwargs.get('wire_bytes')
    if type(wire_bytes) is int and 0 <= wire_bytes <= 10**9:
        result['wire_bytes'] = wire_bytes
    stream = kwargs.get('stream')
    if type(stream) is bool:
        result['stream'] = stream
    parameters = kwargs.get('parameters')
    if isinstance(parameters, dict):
        safe = {}
        for key in _PARAMETER_KEYS:
            value = parameters.get(key)
            if type(value) in (int, float) and math.isfinite(value) and abs(value) <= 10**9:
                safe[key] = value
        for key in ('thinking_enabled', 'do_sample'):
            value = parameters.get(key)
            if type(value) is bool:
                safe[key] = value
        for key, values in {'thinking': {'enabled', 'disabled'}, 'reasoning_effort': {'none', 'minimal', 'low', 'medium', 'high', 'xhigh'}}.items():
            value = parameters.get(key)
            if isinstance(value, str) and value in values:
                safe[key] = value
        if safe:
            result['parameters'] = safe
    return result

def safe_exception_diagnostic(error: BaseException) -> dict[str, Any]:
    """Capture a bounded class and local code location, never exception text."""
    name = type(error).__name__
    result: dict[str, Any] = {'exception_class': name if _IDENTIFIER.fullmatch(name) else 'Exception'}
    root = Path(__file__).resolve().parent
    frame = error.__traceback__
    while frame is not None:
        path = Path(frame.tb_frame.f_code.co_filename).resolve()
        if path.parent == root and path.suffix == '.py':
            result['location'] = {'file': path.name, 'line': frame.tb_lineno}
        frame = frame.tb_next
    return result

def report_diagnostic_write_failure(error: BaseException, *, operation_id: str | None = None) -> None:
    """Report a failed diagnostic write without changing the provider outcome."""
    try:
        logging.getLogger(__name__).error('diagnostic_write_failed metadata=%s', {
            **safe_diagnostic_context(operation_id=operation_id), **safe_exception_diagnostic(error)
        })
    except Exception:
        # A broken logging handler must not alter a settled provider call.
        # The fallback contains no exception text or other external input.
        try:
            os.write(2, b'diagnostic_write_failed logging_unavailable\n')
        except OSError:
            pass


_ENVELOPE_ERRORS = frozenset({'envelope_type', 'missing_version', 'version_type', 'version_mismatch',
    'missing_fields', 'extra_fields', 'collection_type', 'collection_limit', 'invalid_limitations', 'slot_mismatch'})
_JSON_TYPES = frozenset({'null', 'boolean', 'number', 'string', 'array', 'object', 'other'})

def sanitize_diagnostic_errors(value: object, *, schema: type[BaseModel] | None = None) -> list[dict[str, Any]]:
    """Share one schema-bounded cleaner between private storage and export."""
    from .research_output_contract import ResearchDraft
    schemas = [ResearchDraft] + ([schema] if schema is not None else [])
    allowed_paths: set[str] = set()
    def collect(node: object) -> None:
        if isinstance(node, dict):
            properties = node.get('properties')
            if isinstance(properties, dict): allowed_paths.update(k for k in properties if isinstance(k, str))
            for child in node.values(): collect(child)
        elif isinstance(node, list):
            for child in node: collect(child)
    for item in schemas: collect(item.model_json_schema())
    top = set(ResearchDraft.model_fields)
    result: list[dict[str, Any]] = []
    if not isinstance(value, list): return result
    for item in value[:20]:
        if not isinstance(item, dict): continue
        kind = item.get('type')
        if not isinstance(kind, str) or kind not in _ALLOWED_ERRORS | _ENVELOPE_ERRORS | {'other'}: continue
        safe: dict[str, Any] = {'type': kind}
        path = item.get('path')
        if isinstance(path, (list, tuple)):
            safe['path'] = [part if isinstance(part, str) and part in allowed_paths else part if type(part) is int and 0 <= part <= 10000 else '<unknown>' for part in path[:12]]
        field = item.get('field')
        if isinstance(field, str) and field in top: safe['field'] = field
        fields = item.get('fields')
        if isinstance(fields, list): safe['fields'] = [x for x in fields[:20] if isinstance(x, str) and x in top]
        count = item.get('count')
        if type(count) is int and 0 <= count <= 1000000: safe['count'] = count
        actual = item.get('actual_type')
        if isinstance(actual, str) and actual in _JSON_TYPES: safe['actual_type'] = actual
        result.append(safe)
    return result

_PROVIDER_ERROR_CODES = frozenset({'invalid_request_error', 'authentication_error', 'permission_error',
    'not_found_error', 'rate_limit_error', 'server_error', 'invalid_api_key',
    'context_length_exceeded', 'rate_limit_exceeded', 'insufficient_quota', 'invalid_request'})

def safe_provider_error(body: str | bytes) -> dict[str, str]:
    """Extract fixed standard error labels; never return a provider message."""
    import json
    try:
        raw = json.loads(body)
    except (ValueError, UnicodeError, TypeError):
        return {'shape': 'non_json', 'code': 'unknown', 'category': 'unknown'}
    if not isinstance(raw, dict):
        return {'shape': 'non_object', 'code': 'unknown', 'category': 'unknown'}
    error = raw.get('error')
    if not isinstance(error, dict):
        return {'shape': 'object_without_error_object', 'code': 'unknown', 'category': 'unknown'}
    result = {'shape': 'error_object', 'code': 'unknown', 'category': 'unknown'}
    for target, key in (('code', 'code'), ('category', 'type')):
        value = error.get(key)
        if isinstance(value, str) and value in _PROVIDER_ERROR_CODES:
            result[target] = value
    for target, key in (('messages_type', 'type'), ('messages_code', 'code'), ('messages_parameter', 'param')):
        value = error.get(key)
        if key not in error or value is None:
            result[target] = 'absent'
        elif isinstance(value, str) and value in (_PROVIDER_ERROR_CODES if key != 'param' else {'model', 'max_tokens', 'messages', 'tools', 'system', 'thinking', 'stream'}):
            result[target] = value
        else:
            result[target] = 'unrecognized'
    return result

def safe_wire_metadata(wire: object, *, contract: object = None, wire_bytes: int | None = None) -> dict[str, Any]:
    """Select parameters actually present in a submitted request envelope."""
    if not isinstance(wire, dict): return {}
    params = {key: wire[key] for key in (*_PARAMETER_KEYS, 'reasoning_effort', 'do_sample') if key in wire}
    thinking = wire.get('thinking')
    if isinstance(thinking, dict): params['thinking'] = thinking.get('type')
    mode = 'tool' if isinstance(wire.get('tools'), list) else (
        wire.get('response_format', {}).get('type') if isinstance(wire.get('response_format'), dict) else 'unknown')
    extra: dict[str, Any] = {}
    if type(wire_bytes) is int and 0 <= wire_bytes <= 10**9:
        extra['wire_bytes'] = wire_bytes
    if type(wire.get('stream')) is bool:
        extra['stream'] = wire['stream']
    return safe_diagnostic_context(model=wire.get('model'), parameters=params,
        output_mode=mode, contract=getattr(contract, 'version', None) if contract is not None else 'none', **extra)


def sanitize_provider_error(value: object) -> dict[str, str]:
    if not isinstance(value, dict): return {'shape': 'unknown', 'code': 'unknown', 'category': 'unknown'}
    result = {}
    for key, allowed in (('shape', {'non_json', 'non_object', 'object_without_error_object', 'error_object'}),
                         ('code', _PROVIDER_ERROR_CODES), ('category', _PROVIDER_ERROR_CODES)):
        item = value.get(key)
        result[key] = item if isinstance(item, str) and item in allowed else 'unknown'
    return result


def safe_messages_error(value: object, status: object, headers: object) -> dict[str, Any]:
    """Keep fixed labels and bounded request IDs, never provider text."""
    result: dict[str, Any] = {}
    if type(status) is int and 100 <= status <= 599:
        result['status_code'] = status
    if isinstance(value, dict):
        for key in ('messages_type', 'messages_code', 'messages_parameter'):
            item = value.get(key)
            allowed = _PROVIDER_ERROR_CODES | {'absent', 'unrecognized'} if key != 'messages_parameter' else {'absent', 'unrecognized', 'model', 'max_tokens', 'messages', 'tools', 'system', 'thinking', 'stream'}
            if isinstance(item, str) and item in allowed:
                result[key] = item
    if isinstance(headers, dict):
        request_id = headers.get('request-id') or headers.get('x-request-id')
        if isinstance(request_id, str) and _IDENTIFIER.fullmatch(request_id):
            result['request_id'] = request_id
    return result
