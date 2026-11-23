"""Compare native request envelopes and replay a returned batch with a changed profile."""
import json
import os
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.anthropic_messages import AnthropicMessagesProvider
from app.model_protocol import canonical_json_dumps
from app.openai_compatible import HttpResponse
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext
from app.research_runtime import run_research_agent

HERE = Path(__file__).resolve().parent
TABLE = {'title': 'Evidence', 'columns': [{'key': 'status', 'label': 'Status'}],
         'rows': [{'label': 'Coverage', 'cells': {'status': {'kind': 'text', 'text': 'Open'}}}],
         'rationale': 'Summarize review status.'}


def tool(name, args, cid):
    return {'type': 'tool_use', 'id': cid, 'name': name, 'input': args}


def run(dynamic, resume):
    wires = []
    saved = {}
    def transport(method, url, headers, body, timeout):
        wire = json.loads(body)
        wires.append(wire)
        if len(wires) == 1 and dynamic:
            blocks = [tool('activate_tool_group', {'group': 'presentation'}, 'enable'),
                      tool('create_table', TABLE, 'early'),
                      tool('activate_tool_group', {'group': 'external_sources'}, 'unavailable')]
        elif (len(wires) == 2 and dynamic) or (len(wires) == 1 and not dynamic):
            blocks = [tool('create_table', TABLE, 'allowed')]
        else:
            blocks = [{'type': 'text', 'text': 'done'}]
        stop = 'tool_use' if blocks[0]['type'] == 'tool_use' else 'end_turn'
        return HttpResponse(200, json.dumps({'role': 'assistant', 'content': blocks, 'stop_reason': stop,
                                             'usage': {'input_tokens': 0, 'output_tokens': 0}}))
    settings = SimpleNamespace(base_url='https://example.com', model='fixture', api_key='unused', allow_private_network=False)
    policy = SimpleNamespace(timeout_seconds=10, max_input_tokens=None, max_output_tokens=256)
    model = AnthropicMessagesProvider(settings, policy, anthropic_version='2023-06-01', auth_scheme='bearer',
                                      max_tokens=256, transport=transport)
    ctx = ResearchExecutionContext(ResearchExecutionBudget(4, 0, None, time.monotonic() + 30), False,
                                  lambda: None, lambda: None, lambda *_: None, lambda *_: None, lambda: None)
    def capture(state):
        saved.update(deepcopy(state))
    def fail_settlement(*args):
        raise RuntimeError('Offline settlement interruption')
    profile = {'endpoint': 'https://example.com/v1/messages', 'model': 'fixture', 'version': '1',
               'mode': 'json_object', 'native_tools': True, 'dynamic_tool_definitions': dynamic}
    configure = {'FORESIGHTNINE_LLM_CAPABILITY_PROFILE': json.dumps(profile), 'FORESIGHTNINE_LLM_ENDPOINT_VERSION': '1'}
    kwargs = dict(question='Summarize review status in a table.', sources=[], model=model, context=ctx, checkpoint=capture)
    with patch.dict(os.environ, configure):
        if resume:
            with patch.object(ctx, 'settle', fail_settlement):
                assert run_research_agent(**kwargs).reason == 'settlement_failed'
            assert len(wires) == 1 and saved['pending']['kind'] == 'model_returned'
            with patch.dict(os.environ, {'FORESIGHTNINE_LLM_CAPABILITY_PROFILE': ''}):
                outcome = run_research_agent(**kwargs, initial_state=deepcopy(saved))
        else:
            outcome = run_research_agent(**kwargs)
    names = [[d['name'] for d in wire['tools']] for wire in wires]
    assert ('create_table' not in names[0]) == dynamic
    if dynamic:
        assert 'create_table' in names[1]
        catalog = next(d['description'] for d in wires[0]['tools'] if d['name'] == 'activate_tool_group')
        assert 'external_sources' not in catalog
    receipts = {r['tool_use_id']: json.loads(r['content']) for w in wires[1:] for m in w['messages']
                if isinstance(m['content'], list) for r in m['content'] if r.get('type') == 'tool_result'}
    if dynamic:
        assert receipts['early']['status'] == 'invalid' and receipts['unavailable']['status'] == 'invalid'
        assert receipts['enable']['status'] == 'active_next_call'
    assert len(outcome.tables) == 1 and len(wires) == (3 if dynamic else 2), (len(outcome.tables), receipts)
    return {'definitions': names, 'wire_tools_bytes': [len(canonical_json_dumps(w['tools']).encode()) for w in wires],
            'recorded_tables': len(outcome.tables), 'submitted_tools': list(receipts), 'transport_requests': len(wires),
            'real_model_requests': 0, 'replayed_saved_batch': resume}


if __name__ == '__main__':
    result = {name: run(dynamic, resume) for name, dynamic, resume in
              [('full', False, False), ('dynamic', True, False), ('resume_full', True, True)]}
    print(json.dumps(result, indent=2))
