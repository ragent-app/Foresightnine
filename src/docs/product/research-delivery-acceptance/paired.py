"""Run one preregistered pair in isolation. Inputs are authored fixtures; never load topic data."""
import argparse
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

from app.anthropic_messages import AnthropicMessagesProvider
from app.model_protocol import ModelResponse, ModelToolCall, ModelUsage
from app.openai_compatible import OpenAICompatibleSettings
from app.phase3 import ProviderPolicy
from app.research_budget import ResearchExecutionBudget
from app.research_context_manager import _record_failed_attempt
from app.research_coordinator import ResearchExecutionContext
from app.research_recovery import prepare_runtime_continuation
from app.research_runtime import run_research_agent
from app.research_sources import ResearchSource, SourcePermissions
from app.research_tool_executor import TOOL_HANDLERS
from app.research_turn_persister import receipt_breakdown

HERE = Path(__file__).resolve().parent
CASES = {
    'recovery': (
        'Independent training exhibit. Employee shares are locked for 90 days after closing. '
        'No holding period is disclosed for public investors.',
        'Record the employee lock-up in days, cite the exhibit and explain whether it establishes a holding period for public investors.',
    ),
    'facts': (
        'Independent training exhibit. FY2025 revenue was USD 120 million. FY2024 revenue was USD 100 million.',
        'Record both annual revenues, calculate FY2025 year-over-year growth with calculate_financials, and cite the evidence.',
    ),
}
CLOCK = datetime(2026, 11, 22, tzinfo=timezone.utc)
LIMITS = dict(calls=4, tokens=160000, seconds=180, per_request_seconds=90, output=2048)


def digest(data):
    return sha256(data).hexdigest()


def context(calls):
    def search_disabled():
        raise AssertionError('External search is disabled in both arms')
    return ResearchExecutionContext(
        ResearchExecutionBudget(calls, 0, LIMITS['tokens'], time.monotonic() + LIMITS['seconds']),
        False, lambda: None, lambda: None, lambda *_: None, lambda *_: None, search_disabled,
    )


class FixtureTurns:
    """Seed a common checkpoint without sending paid requests."""
    policy = ProviderPolicy(max_output_tokens=2048, timeout_seconds=90)

    def __init__(self, actions):
        self.actions = iter(actions)

    def estimate_turn_input(self, turn):
        return 2000

    def complete_turn(self, turn, **kwargs):
        name, args, cid = next(self.actions)
        block = dict(type='tool_use', id=cid, name=name, input=args)
        return ModelResponse({'role':'assistant', 'content':[block]}, (ModelToolCall(cid, name, args),),
                             ModelUsage(0, 0, 0), 'tool_use', 'tool_calls')


def make_input(case, arm):
    text, question = CASES[case]
    src = ResearchSource(source_id='exhibit', owner_id='isolated', title='Independent exercise',
        text=text, kind='user_supplied', obtained_at=CLOCK, content_sha256=digest(text.encode()),
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))
    actions = [('read_sources', {'reads':[{'source_id':'exhibit'}], 'rationale':'Inspect the fixed exhibit.'}, 'fixture-read')]
    if case == 'facts':
        rows = []
        for period, amount, value in [('FY2025',120,120), ('FY2024',100,125)]:
            rows.append(dict(label='Annual revenue', value=value, unit='USD million', currency='USD',
                             period=period, source_id='exhibit', quotation=f'{period} revenue was USD {amount} million.'))
        actions.append(('record_facts', {'facts':rows, 'rationale':'Record the requested annual amounts.'}, 'fixture-facts'))
    saved = {}
    def capture(state):
        saved.update(deepcopy(state))
    baseline = nullcontext()
    if case == 'facts' and arm == 'A':
        # Replay the pre-change policy, with the same data/schema as the candidate.
        from app.research_tool_executor import _quotation_batch_error
        handler = TOOL_HANDLERS['record_facts']
        def reject_mixed(ctx, inv):
            snapshot = deepcopy(ctx.state.get('facts', []))
            result = handler(ctx, inv)
            failures = result.result.get('failures') or []
            if failures:
                ctx.state['facts'] = snapshot
                raise _quotation_batch_error('facts', failures, len(inv.req.facts))
            return result
        baseline = patch.dict(TOOL_HANDLERS, record_facts=reject_mixed)
    with baseline:
        run_research_agent(question=question, sources=[src], model=FixtureTurns(actions),
                           context=context(len(actions)), clock={'utc_date':'2026-11-22'}, checkpoint=capture)
    saved['failed_attempts'] = []
    if case == 'recovery':
        diagnostics = {'stream_metrics': {'frames_received': 120, 'reassembled_bytes': 4096},
                       'transport': {'submission_state': 'response_received'}}
        if arm == 'B':
            _record_failed_attempt(saved, call_id='fixture-interruption', reason='provider_transport_error', call_diag=diagnostics)
        saved = prepare_runtime_continuation(saved)
    return src, question, saved


def provider():
    prefix = 'FORESIGHTNINE_RESEARCH_LLM_'
    settings = OpenAICompatibleSettings.from_values(
        model=os.environ[prefix+'MODEL'], base_url=os.environ[prefix+'BASE_URL'],
        allowlist=os.environ[prefix+'ALLOWED_HOSTS'], api_key=os.environ[prefix+'API_KEY'])
    assert settings.api_key and os.environ['FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL'] == 'anthropic_messages'
    assert os.environ['FORESIGHTNINE_MESSAGES_CAPABILITY_BASE_URL'].rstrip('/') == settings.base_url
    assert os.environ['FORESIGHTNINE_MESSAGES_CAPABILITY_MODEL'] == settings.model
    assert os.environ['FORESIGHTNINE_MESSAGES_NATIVE_TOOLS'] == os.environ['FORESIGHTNINE_MESSAGES_CONTINUATION'] == 'true'
    return AnthropicMessagesProvider(settings, ProviderPolicy(max_output_tokens=LIMITS['output'], timeout_seconds=LIMITS['per_request_seconds']),
        anthropic_version=os.environ['FORESIGHTNINE_MESSAGES_VERSION'], auth_scheme=os.environ['FORESIGHTNINE_MESSAGES_AUTH_SCHEME'],
        max_tokens=LIMITS['output'], stream_enabled=True)


def run_one(case, arm, model):
    src, question, state = make_input(case, arm)
    n_before, event_count = len(state['facts']), len(state['events'])
    if case == 'facts' and n_before != (0 if arm == 'A' else 1):
        raise RuntimeError(f'Fixture failed to establish the {arm} recording policy')
    c = context(LIMITS['calls'])
    t = time.monotonic()
    latest = {}
    def observe(snapshot):
        latest['history'] = snapshot.get('history', [])
    result = run_research_agent(question=question, sources=[src], model=model, context=c, initial_state=state, checkpoint=observe)
    tool_io = []
    for exchange in latest.get('history', []):
        blocks = [b for b in exchange['assistant'].get('content', []) if b.get('type') == 'tool_use']
        tool_io.extend({'id':b['id'], 'tool':b['name'], 'input':b['input'],
                        'result':next((r['content'] for r in exchange['results'] if r['tool_call_id'] == b['id']), None)}
                       for b in blocks)
    # No chain-of-thought, credential, or private wire responses enter the evidence file.
    events = [{key: e[key] for key in ('kind','name','status','usage','call_id','code','settlement','finish_reason','usage_detail','summary') if key in e}
              for e in result.events[event_count:]]
    calls = []
    for cid, d in c.private_diagnostics.items():
        calls.append({'id':cid,'status_code': (d.get('provider_error') or {}).get('status_code'),
                      'error_code':(d.get('provider_error') or {}).get('messages_code'),
                      'error_type':(d.get('provider_error') or {}).get('messages_type'),
                      'transport':d.get('transport'), 'wire_bytes':(d.get('context') or {}).get('wire_bytes')})
    return {'case':case, 'arm':arm, 'status':result.status,'reason':result.reason,'seed_facts':n_before,
            'facts':result.facts,'calculations':result.calculations,'report':result.report,'claims':result.claims,
            'gaps':result.gaps,'events':events,'tool_io':tool_io,'diagnostics':calls,'actual_calls':c.calls,
            'elapsed_seconds':round(time.monotonic()-t,2),'budget_accounted_tokens':c.accounted_tokens,
            'usage':receipt_breakdown(c.model_call_receipts, events, c.accounted_tokens),
            'question_parts':result.question_parts, 'search_calls':c.search_calls,
            'actual_cost':None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('case', choices=tuple(CASES))
    parser.add_argument('arm', choices=('A','B'))
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    model = provider()
    # Exclusive marker guards ambiguous calls against a second external submission.
    args.output.with_suffix('.started').touch(exist_ok=False)
    row = run_one(args.case, args.arm, model)
    args.output.write_text(json.dumps(row, ensure_ascii=False, indent=2)+'\n')


if __name__ == '__main__':
    main()
