"""Version-local source metadata; no semantic or independence assertions."""
from collections import defaultdict
import re
from typing import Any


def summarize_source_quality(sources: list[dict]) -> dict[str, Any]:
    groups: dict[str, list[str]] = defaultdict(list)
    for source in sources[:200]:
        if not isinstance(source, dict):
            continue
        sid, digest = source.get('source_id'), source.get('content_sha256')
        if isinstance(sid, str) and isinstance(digest, str) and re.fullmatch(r'[0-9a-fA-F]{64}', digest):
            groups[digest.casefold()].append(sid)
    rows = []
    for source in sources[:200]:
        if not isinstance(source, dict) or not isinstance(source.get('source_id'), str):
            continue
        sid, digest = source['source_id'], source.get('content_sha256')
        matching = groups.get(digest.casefold(), []) if isinstance(digest, str) else []
        other = list(dict.fromkeys(value for value in matching if value != sid))
        rows.append({'source_id': sid,
                     'content_match': 'same_hash_as_another' if other else 'no_same_hash_recorded' if matching else 'unknown',
                     'same_hash_source_ids': other[:100],
                     'time_fields_recorded': {field: bool(source.get(field)) for field in ('event_at', 'published_at', 'obtained_at', 'revised_at')},
                     'republication_independence': 'not_established',
                     'identity_and_time_verification': 'not_independently_verified'})
    return {'schema_version': '1', 'scope': 'recorded_analyzed_sources_only', 'sources': rows,
            'truncated': len(sources) > 200, 'meaning': 'same_hash_is_not_independence_or_semantic_support'}
