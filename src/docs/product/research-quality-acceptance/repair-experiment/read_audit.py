"""Rebuild the E1 post-read Messages request offline; archive structure only."""
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import sys
from unittest.mock import patch

from app import research_run_insights as insights
from app.model_protocol import ModelResponse, ModelToolCall, ModelUsage
from app.research_runtime import run_research_agent
from app.research_sources import ResearchSource, SourcePermissions

import prior


def canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def leaves(node, path=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from leaves(value, (*path, key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from leaves(value, (*path, str(i)))
    elif isinstance(node, str):
        yield '.'.join(path), node


def wire_counts(wire, spans, source_text):
    """Count literal returned passages across independent wire leaves, including JSON text inside tool results."""
    fields = list(leaves(wire))
    results = []
    for start, end in sorted(set(spans)):
        passage = source_text[start:end]
        if not passage:
            continue
        hits = {path: value.count(passage) for path, value in fields if passage in value}
        results.append(dict(start=start, end=end, utf8_bytes=len(passage.encode()), copies=sum(hits.values()), fields=hits))
    total_bytes = len(canonical(wire))
    duplicate = sum(max(0, row['copies'] - 1) * row['utf8_bytes'] for row in results)
    return dict(passages=results, duplicate_passage_bytes=duplicate, wire_bytes=total_bytes,
                duplicate_fraction=duplicate / total_bytes, wire_sha256=sha256(canonical(wire)).hexdigest())


class Captured(BaseException):
    pass


def audit_run(record):
    events = record['events']
    model_turn = 0
    grouped = {}
    for event in events:
        if event['kind'] in ('model', 'model_failure'):
            model_turn += 1
            grouped[model_turn] = dict(model=event, tools=[])
        elif event['kind'] == 'tool':
            grouped[model_turn]['tools'].append(event)
    read_turn = next((n for n, row in grouped.items() if any(
        t.get('name') == 'read_sources' and (t.get('summary') or {}).get('read_count', 0) for t in row['tools'])), None)
    first_evidence = next((n for n, row in grouped.items() if any(
        (t.get('summary') or {}).get('recorded_count', 0) for t in row['tools'])), None)
    out = dict(read_turn=read_turn, first_saved_fact_turn=first_evidence, saved_facts=len(record['facts']))
    if read_turn is None or read_turn+1 not in grouped:
        return out
    events_after = grouped[read_turn+1]
    out.update(post_read_turn=read_turn+1, truncated=events_after['model'].get('code')=='provider_output_truncated',
               executed_tools=[t['name'] for t in events_after['tools'] if t.get('status') == 'completed'],
               observed_stop_reason=(record.get('decoded_turns') or [{}] * (read_turn+1))[read_turn].get('finish_reason'),
               observed_block_types=[b['type'] for b in (record.get('decoded_turns') or [{}] * (read_turn+1))[read_turn].get('blocks', [])])
    spans = [(item['start'], item['end']) for t in grouped[read_turn]['tools'] if t['name']=='read_sources'
             for item in (t.get('summary') or {}).get('items', [])]
    out['read_ranges'] = spans
    return out


def reconstruct(record, case, out):
    """Only E1 first turns, with original successful tool inputs/IDs and frozen runtime."""
    if out.get('read_turn') != 1 or not out.get('post_read_turn'):
        return
    text = case['snapshot']
    src = ResearchSource(source_id='exhibit', owner_id='isolated', title='Synthetic exhibit', text=text,
        kind='user_supplied', obtained_at=datetime(2026,11,23,tzinfo=timezone.utc), content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))
    first = record['tool_io'][0]
    assert first['tool'] == 'read_sources'
    class Reader:
        def __init__(self):
            self.inner = prior.provider()
            self.index = 0
        def __getattr__(self, key):
            return getattr(self.inner, key)
        def complete_turn(self, turn, **kwargs):
            self.index += 1
            if self.index == 1:
                block = dict(type='tool_use', id=first['id'], name=first['tool'], input=first['input'])
                return ModelResponse({'role':'assistant', 'content':[block]},
                    (ModelToolCall(first['id'],first['tool'],first['input']),),ModelUsage(0,0,0),'tool_use','tool_calls')
            wire = self.inner._wire(turn, cap=2048)
            out['reconstructed'] = wire_counts(wire, out['read_ranges'], text)
            turn_hash=sha256(json.dumps(asdict(turn),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            out['matches_archived_turn_sha256'] = turn_hash == record['prompts'][1]['turn_sha256']
            raise Captured()
    anchor = insights.build_working_memory_anchor
    def count_only(*args, **kwargs):
        with patch.object(insights, 'assess_core_requirements', return_value=[]):
            return anchor(*args, **kwargs)
    try:
        with (patch.object(insights, 'build_working_memory_anchor', count_only) if record['arm']=='A' else nullcontext()):
            run_research_agent(question=case['question'], sources=[src], model=Reader(), context=prior.context(4),
                               clock={'utc_date':'2026-11-23'})
    except Captured:
        return
    raise AssertionError('post-read request was not built')


def main():
    archive, dest = map(Path, sys.argv[1:])
    cases = {c['id']:c for c in json.loads((archive/'cases.json').read_text())['cases']}
    rows = {}
    for batch, sub in [('E1','e1-results'),('fact-batching','fact-batching/results'),('last-call','last-call-submission/results')]:
        for f in sorted((archive/sub).glob('D*.json')):
            r = json.loads(f.read_text())
            row = audit_run(r)
            if batch == 'E1':
                reconstruct(r, cases[r['case']], row)
            rows[batch+'/'+f.stem] = dict(archive_sha256=sha256(f.read_bytes()).hexdigest(), **row)
    dest.write_text(json.dumps(rows, ensure_ascii=False, indent=2)+'\n')


if __name__ == '__main__':
    main()
