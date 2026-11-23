#!/usr/bin/env python3
"""Read only archived E1 JSON; never import the app or make model/tool calls."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).with_name('trace-results.json')

def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    runs = []
    for p in sorted((ROOT / 'e1-results').glob('*.json')):
        d = json.loads(p.read_text())
        events = d['events']
        model = [e for e in events if e.get('kind') in ('model', 'model_failure')]
        successes = [e for e in events if e.get('kind') == 'tool' and e.get('status') == 'completed']
        last = successes[-1] if successes else None
        tool_io = d.get('tool_io', [])
        last_io = next((t for t in tool_io if last and t.get('id') == last.get('tool_call_id')), None)
        runs.append({
            'id': p.stem, 'input_path': str(p.relative_to(ROOT)), 'input_sha256': digest(p),
            'status': d['status'], 'reason': d['reason'], 'calls': d['calls'],
            'prompt_count': len(d.get('prompts', [])), 'prompts': d.get('prompts', []),
            'model_events_in_order': model,
            'last_completed_tool_event': last, 'last_completed_tool_io': last_io,
            'all_tool_events': [e for e in events if e.get('kind') == 'tool'],
            'tool_io': tool_io, 'facts': d.get('facts', []), 'calculations': d.get('calculations', []),
            'claims': d.get('claims', []), 'report': d.get('report'),
            'budget_accounted_tokens': d.get('budget_accounted_tokens'),
            'model_usage_sum': sum(e.get('usage', 0) or 0 for e in model),
            'failed_response_raw_content': 'not retained in E1 JSON',
            'failed_response_raw_stop_reason': 'not retained in E1 JSON',
        })
    result = {'description': 'Archived isolated in-memory E1 run results; not production DB records.',
              'source_count': len(runs), 'truncated_count': sum(x['reason']=='provider_output_truncated' for x in runs),
              'budget_exhausted_count': sum(x['reason']=='provider_budget_exhausted' for x in runs),
              'runs': runs}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(f"{OUT}: {len(runs)} runs, {result['truncated_count']} truncated, {result['budget_exhausted_count']} budget-exhausted")

if __name__ == '__main__': main()
