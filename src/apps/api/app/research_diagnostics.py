"""Bounded private diagnostics without model output or arbitrary field names."""
from contextvars import ContextVar
import hashlib
import logging
import math
import os
import re
from pathlib import Path
from typing import Any
from pydantic import BaseModel, ValidationError

_finish: ContextVar[str] = ContextVar('research_provider_finish', default='unknown')
_ALLOWED_ERRORS = frozenset({'missing','extra_forbidden','list_type','dict_type','model_type','string_type',
    'int_type','int_parsing','bool_type','bool_parsing','literal_error','too_long','too_short',
    'string_too_long','string_too_short','greater_than_equal','less_than_equal','float_type',
    'decimal_parsing','decimal_type','value_error','union_tag_invalid','union_tag_not_found'})

def set_finish_reason(value: object) -> None:
    _finish.set('provider_length' if value == 'length' else 'provider_refusal' if value in ('content_filter', 'refusal') else 'provider_stop' if value == 'stop' else 'unknown')

def get_finish_reason() -> str:
    return _finish.get()

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
            if not isinstance(stage, str) or stage not in _TRANSPORT_STAGES or not isinstance(result, str) or result not in _TRANSPORT_RESULTS or type(elapsed) is not int or not 0 <= elapsed <= 180000:
                continue
            safe = {'stage': stage, 'result': result, 'elapsed_ms': elapsed}
            for key, lower, upper in (('errno', 0, 65535), ('status_code', 100, 599)):
                number = item.get(key)
                if type(number) is int and lower <= number <= upper:
                    safe[key] = number
            stages.append(safe)
    return {'submission_state': state, 'stages': stages}


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

def safe_wire_metadata(wire: object, *, contract: object = None) -> dict[str, Any]:
    """Select parameters actually present in a submitted request envelope."""
    if not isinstance(wire, dict): return {}
    params = {key: wire[key] for key in (*_PARAMETER_KEYS, 'reasoning_effort', 'do_sample') if key in wire}
    thinking = wire.get('thinking')
    if isinstance(thinking, dict): params['thinking'] = thinking.get('type')
    mode = 'tool' if isinstance(wire.get('tools'), list) else (
        wire.get('response_format', {}).get('type') if isinstance(wire.get('response_format'), dict) else 'unknown')
    return safe_diagnostic_context(model=wire.get('model'), parameters=params,
        output_mode=mode, contract=getattr(contract, 'version', None) if contract is not None else 'none')


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
