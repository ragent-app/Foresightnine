"""One isolated E1 run; the launcher supplies a hard wall-clock deadline."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

from app import research_run_insights as insights
from app.research_runtime import run_research_agent
from app.research_sources import ResearchSource, SourcePermissions

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('prior_paired', HERE.parent / 'research-delivery-acceptance' / 'paired.py')
prior = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prior)
CASES = json.loads((HERE / 'cases.json').read_text())
assert sha256((HERE / 'cases.json').read_bytes()).hexdigest() == '7e62e0cd385e9923ea1030b1fd9825b2fba41ade816d6601e8e57e288cdcd316'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('case', choices=['D1', 'D2', 'D3', 'D4'])
    p.add_argument('arm', choices=['A', 'B'])
    p.add_argument('output', type=Path)
    a = p.parse_args()
    case = next(c for c in CASES['cases'] if c['id'] == a.case)
    text = case['snapshot']
    src = ResearchSource(source_id='exhibit', owner_id='isolated', title='Synthetic exhibit', text=text,
        kind='user_supplied', obtained_at=datetime(2026, 11, 23, tzinfo=timezone.utc),
        content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))
    original_anchor = insights.build_working_memory_anchor
    def count_only(*args, **kwargs):
        # Suppress only the newly introduced core-requirement feedback line.
        # The submission evaluator remains identical in both arms.
        with patch.object(insights, 'assess_core_requirements', return_value=[]):
            return original_anchor(*args, **kwargs)
    context = prior.context(4)
    prompts = []
    class TracingProvider:
        def __init__(self): self.inner = prior.provider()
        def __getattr__(self, name): return getattr(self.inner, name)
        def complete_turn(self, turn, **kwargs):
            raw = json.dumps(asdict(turn), sort_keys=True, ensure_ascii=False).encode()
            prompts.append({'turn_sha256': sha256(raw).hexdigest(), 'tail_note': turn.tail_note})
            return self.inner.complete_turn(turn, **kwargs)
    checkpoints = []
    import time
    start = time.monotonic()
    try:
        with patch.object(insights, 'build_working_memory_anchor', count_only if a.arm == 'A' else original_anchor):
            result = run_research_agent(question=case['question'], sources=[src], model=TracingProvider(),
                context=context, clock={'utc_date': '2026-11-23'}, checkpoint=lambda s: checkpoints.append(s))
        latest = checkpoints[-1] if checkpoints else {}
        output = {'case': a.case, 'arm': a.arm, 'status': result.status, 'reason': result.reason,
                  'report': result.report, 'claims': result.claims, 'facts': result.facts,
                  'question_parts': result.question_parts, 'gaps': result.gaps,
                  'answered_parts': [p.get('id') or p.get('question') for p in result.question_parts if p.get('status') == 'answered'],
                  'all_core_answered': bool([p for p in result.question_parts if p.get('core', True)]) and all(p.get('status') == 'answered' for p in result.question_parts if p.get('core', True)),
                  'calculations': result.calculations, 'events': result.events,
                  'tool_io': [{'id': b['id'], 'tool': b['name'], 'input': b['input'],
                      'result': next((r['content'] for r in ex['results'] if r['tool_call_id'] == b['id']), None)}
                     for ex in latest.get('history', []) for b in ex['assistant'].get('content', [])
                     if b.get('type') == 'tool_use'], 'calls': context.calls,
                  'budget_accounted_tokens': context.accounted_tokens,
                  'prompts': prompts, 'elapsed_seconds': round(time.monotonic() - start, 3), 'actual_cost': None}
    except Exception as error:
        output = {'case': a.case, 'arm': a.arm, 'error_type': type(error).__name__,
                  'calls': context.calls, 'budget_accounted_tokens': context.accounted_tokens,
                  'prompts': prompts, 'elapsed_seconds': round(time.monotonic() - start, 3), 'actual_cost': None}
    a.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
