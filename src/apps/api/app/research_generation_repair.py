"""Safe, serializable continuation for an invalid complete response.

Only a response known to have arrived may be retried. No model output is kept
in this state. A repeated error with no retained progress terminates the work.
"""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

VERSION = 'generation-repair-v1'
_ALLOWED_ERRORS = frozenset({'contract_validation', 'provider_parse', 'decision_validation'})


def fingerprint(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), default=str).encode('utf-8')).hexdigest()


def normalized_errors(error: str, diagnostics: object, schema=None) -> list[dict]:
    """Keep only declared schema paths and fixed error types; ignore values/indices."""
    from .research_output_contract import ResearchDraft
    fields = set()
    def collect(node):
        if isinstance(node, dict):
            properties = node.get('properties')
            if isinstance(properties, dict): fields.update(properties)
            for value in node.values(): collect(value)
        elif isinstance(node, list):
            for value in node: collect(value)
    collect(ResearchDraft.model_json_schema())
    if schema is not None:
        collect(schema.model_json_schema())
    allowed_types = {'missing','extra_forbidden','list_type','dict_type','model_type','string_type',
        'int_type','int_parsing','bool_type','literal_error','too_long','too_short','string_too_long',
        'string_too_short','greater_than_equal','less_than_equal','value_error','other',
        'missing_version','version_type','version_mismatch','missing_fields','extra_fields',
        'collection_type','collection_limit','invalid_limitations','envelope_type','slot_mismatch',
        'invalid_reference','unauthorized_action','invalid_range','invalid_action'}
    result = []
    if isinstance(diagnostics, list):
        for item in diagnostics:
            if not isinstance(item, dict): continue
            typ = item.get('type')
            typ = typ if typ in allowed_types else 'other'
            path = item.get('path', [])
            if not isinstance(path, list): path = []
            path = [part if isinstance(part, str) and part in fields else '*' for part in path[:12]]
            field = item.get('field')
            if not path and isinstance(field, str) and field in fields: path = [field]
            named = item.get('fields')
            paths = [[name] for name in named if isinstance(name, str) and name in fields] if isinstance(named, list) else []
            for safe_path in paths or [path]:
                record = {'type': typ, 'path': safe_path}
                if record not in result: result.append(record)
    return sorted(result, key=lambda x: (x['type'], x['path'])) or [{'type': error, 'path': []}]


def make_continuation(*, binding: str, parent_call_id: str, error: str,
                      previous: dict | None = None, diagnostics: object = None,
                      progress: object = None, schema=None,
                      version: str = VERSION, scope: str = 'complete_response') -> dict | None:
    if error not in _ALLOWED_ERRORS: return None
    errors = normalized_errors(error, diagnostics, schema=schema)
    mark = fingerprint({'binding': binding, 'scope': scope, 'errors': errors, 'progress': progress or []})
    seen = list(previous.get('seen', [])) if isinstance(previous, dict) else []
    if mark in seen: return None
    return {'version': version, 'binding': binding, 'parent_call_id': parent_call_id,
            'pending': [{'kind': 'regenerate', 'scope': scope, 'errors': errors}],
            'seen': [*seen, mark], 'retained_refs': {}}


def validate_continuation(value: object, binding: str, *, version: str = VERSION,
                          scope: str = 'complete_response') -> bool:
    if not isinstance(value, dict) or value.get('version') != version or value.get('binding') != binding: return False
    pending = value.get('pending')
    if not isinstance(pending, list) or len(pending) != 1 or not isinstance(pending[0], dict): return False
    item = pending[0]
    errors = item.get('errors')
    return (item.get('kind') == 'regenerate' and item.get('scope') == scope
            and isinstance(errors, list) and all(isinstance(x, dict) and set(x) == {'type', 'path'}
            and isinstance(x['type'], str) and isinstance(x['path'], list) for x in errors)
            and isinstance(value.get('seen'), list))


def split_work_item(item: dict) -> list[dict]:
    """Bisect finite slots or complete evidence units; never split a singleton."""
    scope = item.get('scope')
    if not isinstance(scope, dict):
        return []
    for key in ('targets', 'evidence_units'):
        values = scope.get(key)
        if not isinstance(values, list) or len(values) < 2:
            continue
        middle = len(values) // 2
        return [{**item, 'scope': {**scope, key: part}} for part in (values[:middle], values[middle:])]
    return []


def inject_slot_dependencies(raw: object, targets: list[dict], dependencies: list[dict]) -> dict | None:
    """Accept only server-addressed targets; inject immutable verified DTO dependencies."""
    from copy import deepcopy
    from .research_output_contract import ResearchDraft
    if not isinstance(raw, dict): return None
    fields = set(ResearchDraft.model_fields)
    if set(raw) != fields or raw.get('version') != 'research-draft-v1' or not isinstance(raw.get('limitations'), list): return None
    collections = fields - {'version', 'limitations'}
    expected = set()
    for slot in targets:
        if not isinstance(slot, dict): return None
        key = (slot.get('collection'), slot.get('slot_id'))
        if key[0] not in collections or not isinstance(key[1], str) or key in expected: return None
        expected.add(key)
    actual = set()
    for collection in collections:
        values = raw.get(collection)
        if not isinstance(values, list): return None
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get('id'), str): return None
            key = (collection, value['id'])
            if key not in expected or key in actual: return None
            actual.add(key)
    if actual != expected: return None
    result = deepcopy(raw)
    all_keys = set(expected)
    for slot in dependencies:
        if not isinstance(slot, dict): return None
        key = (slot.get('collection'), slot.get('slot_id'))
        dto = slot.get('dto')
        if (key[0] not in collections or not isinstance(key[1], str) or key in all_keys
                or not isinstance(dto, dict) or dto.get('id') != key[1]
                or not isinstance(slot.get('dto_digest'), str) or fingerprint(dto) != slot['dto_digest']): return None
        all_keys.add(key)
        result[key[0]].append(deepcopy(dto))
    return result


def validate_slot_response(raw: object, targets: list[dict], dependencies: list[dict]) -> dict | None:
    return inject_slot_dependencies(raw, targets, dependencies)


def split_complete_segments(segments: list[dict]) -> list[list[dict]]:
    """Partition atomic context groups without separating their linked passages."""
    groups: list[tuple[set[str], list[dict]]] = []
    for segment in segments:
        if not isinstance(segment, dict) or segment.get('complete_context') is not True:
            return []
        sid, start, end = segment.get('source_id'), segment.get('character_start'), segment.get('character_end')
        if not isinstance(sid, str) or type(start) is not int or type(end) is not int or not 0 <= start < end:
            return []
        ids = segment.get('context_unit_ids')
        if not isinstance(ids, list) or not ids or any(not isinstance(x, str) for x in ids):
            return []
        ids = {sid + ':' + x for x in ids}
        ranges = [{'source_id': sid, 'character_start': start, 'character_end': end}]
        matching = [group for group in groups if group[0] & ids]
        for group in matching:
            ids.update(group[0]); ranges.extend(group[1]); groups.remove(group)
        groups.append((ids, ranges))
    if len(groups) < 2: return []
    mid = len(groups) // 2
    return [[item for _, group in part for item in group] for part in (groups[:mid], groups[mid:])]
