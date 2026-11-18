"""Bounded private diagnostics without model output or arbitrary field names."""
from contextvars import ContextVar
from typing import Any
from pydantic import BaseModel, ValidationError

_finish: ContextVar[str] = ContextVar('research_provider_finish', default='unknown')
_ALLOWED_ERRORS = frozenset({'missing','extra_forbidden','list_type','dict_type','model_type','string_type',
    'int_type','int_parsing','bool_type','bool_parsing','literal_error','too_long','too_short',
    'string_too_long','string_too_short','greater_than_equal','less_than_equal','float_type',
    'decimal_parsing','decimal_type','value_error','union_tag_invalid','union_tag_not_found'})

def set_finish_reason(value: object) -> None:
    _finish.set('provider_length' if value == 'length' else 'provider_stop' if value == 'stop' else 'unknown')

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
    return result
