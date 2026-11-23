"""Frozen E1 B native runner, with a single prompt intervention and structural diagnostics."""
import argparse
from dataclasses import asdict, replace
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

from app import anthropic_messages as messages, research_runtime as runtime
from app import research_tool_executor as executor
from app.research_sources import ResearchSource, SourcePermissions
from app.research_turn_persister import receipt_breakdown

HERE = Path(__file__).resolve().parent
prior_spec = importlib.util.spec_from_file_location('prior_paired', HERE / 'prior.py')
prior = importlib.util.module_from_spec(prior_spec)
prior_spec.loader.exec_module(prior)
case_bytes = (HERE / 'cases.json').read_bytes()
assert sha256(case_bytes).hexdigest() == '7e62e0cd385e9923ea1030b1fd9825b2fba41ade816d6601e8e57e288cdcd316'
CASES = json.loads(case_bytes)
INSTRUCTION = 'This is your last available model call. Prioritize calling submit_research now with the evidence you already have, and explicitly state any remaining gaps.'


def summarize_input(obj):
    # Key names are limited to the model-facing tool schema; never retain values.
    allowed = set()
    def scan(x):
        if isinstance(x, dict):
            if isinstance(x.get('properties'), dict): allowed.update(x['properties'])
            for v in x.values(): scan(v)
        elif isinstance(x, list):
            for v in x: scan(v)
    for tool in runtime.TOOLS: scan(tool.input_schema)
    def visit(x):
        if isinstance(x, dict):
            return {k: visit(v) for k, v in x.items() if k in allowed}
        if isinstance(x, list): return {'items': len(x), 'item_shapes': [visit(v) for v in x[:100]]}
        if isinstance(x, str): return {'chars': len(x)}
        return {'type': type(x).__name__}
    return visit(obj)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('case', choices=['D1','D2','D3','D4'])
    parser.add_argument('arm', choices=['A','B'])
    parser.add_argument('output', type=Path)
    a = parser.parse_args()
    case = next(c for c in CASES['cases'] if c['id'] == a.case)
    source = ResearchSource(source_id='exhibit', owner_id='isolated', title='Synthetic exhibit', text=case['snapshot'],
        kind='user_supplied', obtained_at=datetime(2026,11,23,tzinfo=timezone.utc),
        content_sha256=sha256(case['snapshot'].encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))
    context, prompts, checkpoints, wire_turns = prior.context(4), [], [], []
    build_observations, submit_receipts = [], []
    original_build = runtime.build_model_turn_input
    original_submit = executor.TOOL_HANDLERS['submit_research']
    def digest_turn(turn):
        return sha256(json.dumps(asdict(turn), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    def build_turn(**kwargs):
        turn = original_build(**kwargs)
        eligible = context.budget.max_model_calls is not None and context.budget.max_model_calls - context.calls == 1
        modified = replace(turn, tail_note=((turn.tail_note + "\n") if turn.tail_note else "") + INSTRUCTION) if a.arm == "B" and eligible else turn
        build_observations.append({"call_candidate": context.calls + 1, "eligible_last_call": eligible, "injected": modified is not turn, "before_sha256": digest_turn(turn), "after_sha256": digest_turn(modified)})
        return modified
    def observe_submit(ctx, inv):
        state_before = deepcopy({k: ctx.state.get(k) for k in ("facts", "calculations", "claims", "draft_claims", "question_parts", "gaps", "submission", "market_analyses", "read_ranges")})
        item = {"id": inv.call.id, "index": inv.index, "input": deepcopy(inv.args), "before": state_before, "history_turn": len(ctx.state.get("history", [])), "prior_tool_receipts": deepcopy([ex.get("results", []) for ex in ctx.state.get("history", [])]), "handler_returned": False}
        submit_receipts.append(item)
        try:
            result = original_submit(ctx, inv)
        except Exception as exc:
            item["handler_exception"] = type(exc).__name__
            raise
        item["handler_returned"] = True
        item["result"] = deepcopy(result.result)
        item["early_outcome"] = ({"status": result.early_outcome.status, "reason": result.early_outcome.reason} if result.early_outcome is not None else None)
        item["state_submission"] = deepcopy(ctx.state.get("submission"))
        return result
    original_transport = messages._default_stream_transport
    original_build_response = messages.AnthropicMessagesProvider._build_response
    decoded_turns = []
    def observing_build_response(blocks, reason, usage):
        rows = []
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                rows.append({'type':'invalid'}); continue
            typ = block.get('type')
            row = {'type': typ if typ in ('tool_use','text','thinking','redacted_thinking') else 'other'}
            if typ == 'tool_use':
                name = block.get('name')
                row['name'] = name if name in allowed_names else '<unrecognized>'
                if isinstance(block.get('input'), dict): row['arguments_shape'] = summarize_input(block['input'])
            elif typ == 'text': row['chars'] = len(block.get('text','')) if isinstance(block.get('text'),str) else None
            elif typ == 'thinking': row['chars'] = len(block.get('thinking','')) if isinstance(block.get('thinking'),str) else None
            elif typ == 'redacted_thinking': row['chars'] = len(block.get('data','')) if isinstance(block.get('data'),str) else None
            rows.append(row)
        decoded_turns.append({'finish_reason':reason if reason in ('end_turn','tool_use','max_tokens','stop_sequence','refusal','model_context_window_exceeded') else 'unknown', 'blocks':rows, 'raw_output_saved':False})
        return original_build_response(blocks, reason, usage)
    allowed_names = {t.name for t in runtime.TOOLS}
    def observing_transport(*args, **kwargs):
        forward = kwargs['on_frame']
        record = {'blocks': [], 'finish_reason': None, 'raw_output_saved': False}
        wire_turns.append(record)
        by_index = {}
        def frame(event, data):
            try:
                obj = json.loads(data)
                kind = obj.get('type') or event
                if kind == 'content_block_start':
                    block = obj.get('content_block') or {}; idx = obj.get('index')
                    typ = block.get('type'); name = block.get('name')
                    row = {'type': typ if typ in ('tool_use','text','thinking','redacted_thinking') else 'other', 'chars': 0, 'complete': False}
                    if typ == 'tool_use':
                        row['name'] = name if name in allowed_names else '<unrecognized>'
                        row['_parts'] = []
                        if isinstance(block.get('input'),dict): row['_initial'] = block['input']
                    by_index[idx] = row; record['blocks'].append(row)
                elif kind == 'content_block_delta':
                    row = by_index.get(obj.get('index')); delta = obj.get('delta') or {}
                    if row is not None:
                        val = delta.get('partial_json') if delta.get('type') == 'input_json_delta' else delta.get('text') if delta.get('type') == 'text_delta' else delta.get('thinking') if delta.get('type') == 'thinking_delta' else None
                        if isinstance(val,str):
                            row['chars'] += len(val)
                            if row['type'] == 'tool_use': row['_parts'].append(val)
                elif kind == 'content_block_stop':
                    row = by_index.get(obj.get('index'))
                    if row is not None: row['complete'] = True
                elif kind == 'message_delta':
                    reason = (obj.get('delta') or {}).get('stop_reason')
                    if reason in ('end_turn','tool_use','max_tokens','stop_sequence','refusal','model_context_window_exceeded'): record['finish_reason'] = reason
            except (ValueError, TypeError, AttributeError):
                record['observation_parse_error'] = True
            return forward(event, data)
        kwargs['on_frame'] = frame
        try: return original_transport(*args, **kwargs)
        finally:
            for row in record['blocks']:
                parts = row.pop('_parts', None); initial = row.pop('_initial', {})
                if parts is not None:
                    try:
                        obj = json.loads(''.join(parts)) if parts else initial
                        row['arguments_shape'] = summarize_input(obj)
                        row['arguments_json_complete'] = True
                    except (ValueError, TypeError): row['arguments_json_complete'] = False
            record['truncation_position'] = next((i for i, row in reversed(list(enumerate(record['blocks']))) if not row['complete']), None)
    class TracingProvider:
        def __init__(self): self.inner = prior.provider()
        def __getattr__(self, name): return getattr(self.inner, name)
        def complete_turn(self, turn, **kwargs):
            prompts.append({'turn_sha256': digest_turn(turn), 'tail_note': turn.tail_note, 'injected': bool(a.arm == 'B' and turn.tail_note and turn.tail_note.endswith(INSTRUCTION))})
            return self.inner.complete_turn(turn, **kwargs)
    os.environ['FORESIGHTNINE_RESEARCH_PRESEND_RETRIES'] = '0'
    start = time.monotonic()
    output = {'case':a.case, 'arm':a.arm, 'source': {'source_id':'exhibit', 'content_sha256':source.content_sha256, 'permissions':source.permissions.model_dump()}, 'actual_cost':None}
    try:
        with patch.object(runtime, 'build_model_turn_input', build_turn), patch.dict(executor.TOOL_HANDLERS, {'submit_research': observe_submit}), patch.object(messages, '_default_stream_transport', observing_transport), patch.object(messages.AnthropicMessagesProvider, '_build_response', staticmethod(observing_build_response)):
            result = runtime.run_research_agent(question=case['question'], sources=[source], model=TracingProvider(), context=context,
                clock={'utc_date':'2026-11-23'}, checkpoint=lambda s: checkpoints.append(deepcopy(s)))
        latest = checkpoints[-1] if checkpoints else {}
        history = latest.get('history', [])
        tool_io = [{'turn': i+1, 'id': b['id'], 'tool': b['name'], 'input': b['input'],
                    'result': next((r['content'] for r in ex['results'] if r['tool_call_id'] == b['id']),None)}
                   for i,ex in enumerate(history) for b in ex['assistant'].get('content',[]) if b.get('type') == 'tool_use']
        submitted = [x for x in tool_io if x['tool'] == 'submit_research']
        output.update({'status':result.status,'reason':result.reason,'report':result.report,'claims':result.claims,'facts':result.facts,
            'question_parts':result.question_parts,'gaps':result.gaps,'calculations':result.calculations,'events':result.events,'tool_io':tool_io,
            'answered_parts':[p.get('id') or p.get('question') for p in result.question_parts if p.get('status')=='answered'],
            'all_core_answered':bool([p for p in result.question_parts if p.get('core',True)]) and all(p.get('status')=='answered' for p in result.question_parts if p.get('core',True)),
            'submit_research_observed':bool(submitted), 'submit_research_calls':submitted, 'submit_handler_receipts':submit_receipts, 'state_submission':latest.get('submission'), 'partial_notes':result.partial_notes,
            'final_state_projection':{k:latest.get(k) for k in ('facts','calculations','claims','draft_claims','question_parts','gaps','submission','market_analyses','read_ranges')},
            'submission_acceptance': 'accepted' if result.status in ('completed','partial') and result.reason == 'submitted' and latest.get('submission') else 'rejected_or_unexecuted' if submitted else 'not_observed',
            'record_facts_parallel_turns':[i for i in sorted({x['turn'] for x in tool_io}) if sum(x['tool']=='record_facts' and x['turn']==i for x in tool_io)>1],
            'usage':receipt_breakdown(context.model_call_receipts,result.events,context.accounted_tokens), 'search_calls':context.search_calls})
    except Exception as e:
        output['error_type'] = type(e).__name__
    output.update({'calls':context.calls,'budget_accounted_tokens':context.accounted_tokens,'prompts':prompts,'wire_turns':wire_turns,'decoded_turns':decoded_turns,
        'diagnostics': [{'transport':d.get('transport'), 'stream_metrics':d.get('stream_metrics'), 'finish_reason':d.get('finish_reason'), 'stop_reason':d.get('stop_reason'), 'usage':d.get('usage')} for d in context.private_diagnostics.values()],
        'build_observations':build_observations, 'prompt_exposure': 'provider_entered' if any(p['injected'] for p in prompts) else ('not_applicable_control' if a.arm == 'A' else 'not_sent'), 'exposure_reason': None if any(p['injected'] for p in prompts) else (output.get('reason') or output.get('error_type') or 'no_last_call'), 'submit_handler_receipts': submit_receipts, 'elapsed_seconds':round(time.monotonic()-start,3)})
    a.output.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')

if __name__ == '__main__': main()
