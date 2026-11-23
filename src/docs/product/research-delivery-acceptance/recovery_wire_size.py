"""Measure offline recovery request bodies using the configured Messages wire serializer.

No credentials, requests or experiment records are read or written. Run with the
application tree on PYTHONPATH; --fixture can point at the frozen paired.py.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

from app.anthropic_messages import AnthropicMessagesProvider
from app.model_protocol import canonical_json_dumps
from app.openai_compatible import OpenAICompatibleSettings
from app.phase3 import ProviderPolicy
from app.research_context_manager import _record_failed_attempt, build_model_turn_input
from app.research_recovery import prepare_runtime_continuation
from app.research_sources import ResearchSource


def measure(directory: Path, fixture: Path) -> dict:
    spec = importlib.util.spec_from_file_location('recovery_fixture', fixture)
    if spec is None or spec.loader is None:
        raise ValueError('Cannot load fixture')
    paired = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(paired)
    settings = OpenAICompatibleSettings.from_values(model='wire-fixture', base_url='https://example.com', allowlist='example.com', api_key='unused')
    provider = AnthropicMessagesProvider(settings, ProviderPolicy(max_output_tokens=2048, timeout_seconds=90),
        anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=2048, stream_enabled=True)
    def size(state, sources):
        turn = build_model_turn_input(state=state, source_by_id=sources)
        wire = provider._wire(turn, 2048)
        wire['stream'] = True
        return {'wire_bytes':len(canonical_json_dumps(wire).encode()),
                'user_bytes':len(turn.user.encode()), 'tail_bytes':len((turn.tail_note or '').encode()),
                'paired_exchanges':len(turn.history)}
    result = {}
    for arm in ('A', 'B'):
        source, _, state = paired.make_input('recovery', arm)
        result['short_' + arm] = size(state, {source.source_id:source})
    with (directory / 'history.jsonl').open(encoding='utf-8') as file:
        rows = [json.loads(line) for line in file]
    with (directory / 'sources.jsonl').open(encoding='utf-8') as file:
        sources = {source.source_id:source for line in file if (source := ResearchSource.model_validate_json(line))}
    state = deepcopy(rows[3]['checkpoint'])
    result['historical_before_continuation'] = size(state, sources)
    failure = state['events'][-1]
    _record_failed_attempt(state, call_id=failure['call_id'], reason=failure['code'], call_diag=rows[3]['diagnostics'][failure['call_id']])
    result['historical_recovery'] = size(prepare_runtime_continuation(state), sources)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--fixture', type=Path, default=Path(__file__).with_name('paired.py'))
    args = parser.parse_args()
    print(json.dumps(measure(args.directory, args.fixture), ensure_ascii=False, sort_keys=True))
