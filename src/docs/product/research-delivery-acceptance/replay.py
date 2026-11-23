"""Read only the topic-scoped private snapshot, and emit aggregate results."""
import argparse
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

from app.research_context_manager import _record_failed_attempt, build_model_turn_input
from app.research_recovery import prepare_runtime_continuation, runtime_interruption_no_progress_blocked
from app.research_sources import ResearchSource
from app.research_topics import ResearchContent, ResearchTurn
from app.research_turns import merge_turn


def inputs_size(cp, sources):
    msg = build_model_turn_input(state=cp, source_by_id=sources)
    return len(json.dumps(asdict(msg), ensure_ascii=False, separators=(',', ':')).encode())


def replay_facts(cp, sources):
    from app.research_tool_executor import ToolExecutionContext, _CallInvocation, _exec_record_facts
    from app.research_tools_registry import FactsRequest
    block = next(b for b in cp['history'][3]['assistant']['content'] if b.get('name') == 'record_facts')
    original_reads = json.loads(cp['history'][2]['results'][0]['content'])['source_spans']
    state = {'read_ranges':original_reads, 'facts':[], 'prior_facts':[], 'events':[]}
    ctx = ToolExecutionContext(question='', state=state, sources=list(sources.values()), source_by_id=sources,
                               context=None, as_of=None, seen=set())
    req = FactsRequest.model_validate(block['input'])
    inv = _CallInvocation(call=SimpleNamespace(id='offline'), req=req, args=block['input'],
                          index=0, started_at='', signature='', turn_read_chars=0, turn_bulk_result_chars=0)
    result = _exec_record_facts(ctx, inv).result
    facts = result['facts']
    assert not result['failures'] and len(facts) == 26
    assert all(sources[f['source_id']].text[f['character_start']:f['character_end']] == f['quotation'] for f in facts)
    before = [f['fact_id'] for f in facts]
    assert before == [f['fact_id'] for f in _exec_record_facts(ctx, inv).result['facts']]
    assert len(state['facts']) == 26
    return {'accepted':len(facts), 'failed':len(result['failures']),
            'canonical_original_quotes':sum(f['quotation'] != raw['quotation'] for f,raw in zip(facts, block['input']['facts'])),
            'idempotent':True}


def replay(directory):
    rows = [json.loads(line) for line in (directory/'history.jsonl').read_text().splitlines()]
    sources = {s.source_id:s for line in (directory/'sources.jsonl').read_text().splitlines()
               if (s := ResearchSource.model_validate_json(line))}
    versions = rows[0]['versions']
    first = rows[2]['checkpoint']
    follow = deepcopy(rows[3]['checkpoint'])
    hashes = {name:sha256((directory/name).read_bytes()).hexdigest() for name in ('history.jsonl','sources.jsonl')}
    initial = inputs_size(follow, sources)
    for index in (3,4):
        failure = rows[index]['checkpoint']['events'][-1]
        diag = rows[index]['diagnostics'][failure['call_id']]
        _record_failed_attempt(follow, call_id=failure['call_id'], reason=failure['code'], call_diag=diag)
        if index == 3:
            follow = prepare_runtime_continuation(follow)
            smaller = inputs_size(follow, sources)
    assert runtime_interruption_no_progress_blocked(follow)
    assert smaller < initial
    # Reconstruct the actual checked submission using the same version merger as the persister.
    content = ResearchContent.model_validate(versions[2]['content'])
    turn = ResearchTurn.model_validate(content.turns[-1])
    table_id = first['tables'][0]['table_id']
    turn.table_ids = [table_id]
    merged = merge_turn(content, turn, facts=[], calculations=[], claims=[], charts=[], sources=[], source_candidates=[],
                        tables=first['tables'])
    reopened = ResearchContent.model_validate_json(merged.model_dump_json())
    assert reopened.tables[0].table_id == table_id
    assert table_id in reopened.turns[-1].report
    assert all(sid in sources for sid in reopened.tables[0].source_ids)
    registry = {item.get('fact_id') or item['calculation_id']:item
                for group in ('facts', 'calculations') for item in first[group]}
    numeric_cells = 0
    for row in reopened.tables[0].rows:
        for cell in row.cells.values():
            if cell.provenance is None:
                continue
            prov = cell.provenance
            stored = registry[prov.fact_id or prov.calculation_id]
            assert Decimal(cell.value) == Decimal(stored['value'])
            assert prov.source_id in sources and all(fid in registry for fid in prov.input_fact_ids)
            if prov.fact_id:
                assert sources[prov.source_id].text[prov.character_start:prov.character_end] == stored['quotation']
            numeric_cells += 1
    from app.research_delivery import assemble_parts, assess
    from app.research_partial_answer import merge_checkpoint_parts
    from app.research_source_policy import source_eligibility
    last = rows[-1]['checkpoint']
    question = versions[-1]['content']['turns'][-1]['question']
    eligible = {s.source_id for s, reason in source_eligibility(list(sources.values()), None) if reason == 'eligible'}
    requirements = assess(question, last, sources, claims=last.get('claims', []))
    parts = merge_checkpoint_parts(question, last, eligible, assemble_parts(question, last, sources, eligible, requirements, last.get('claims', [])))
    assert len(parts) == 3 and all(any(word in p['question'] for p in parts) for word in ('风险', '入手', '持有'))
    assert len({p['question'] for p in parts}) == 3 and all(p['reason'] or p['answer'] for p in parts)
    from app.research_turn_persister import receipt_breakdown
    usage = []
    for run in (rows[2], rows[5]):
        cp = run['checkpoint']
        ledger = cp['_accounting']
        breakdown = receipt_breakdown(ledger['model_call_receipts'], cp['events'], ledger['accounted_tokens'])
        usage.append({**breakdown, 'model_calls':ledger['calls'], 'external_search':ledger['search_calls']})
    totals = {key:sum(u[key] for u in usage) for key in usage[0]}
    assert totals == {'reported_tokens':1092587, 'reserved_estimate_tokens':791988, 'model_calls':24,
                      'external_search':3, 'unknown_usage_calls':4}
    return dict(input_sha256=hashes, receipt_totals=totals, fact_batch=replay_facts(first, sources), initial_model_input_bytes=initial, resumed_model_input_bytes=smaller,
                repeated_interruption_stopped=True, question_parts=[{'question':p['question'], 'status':p['status']} for p in parts], numeric_cells=numeric_cells, table_rows=len(reopened.tables[0].rows),
                table_columns=len(reopened.tables[0].columns), full_export_permitted=all(
                    sources[sid].permissions.export for sid in reopened.tables[0].source_ids))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('directory', type=Path)
    print(json.dumps(replay(p.parse_args().directory), indent=2))
