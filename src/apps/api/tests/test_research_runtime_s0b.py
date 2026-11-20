"""S0-B runtime behaviours: read_pages, outline pages, duplicate handling, quotation hints, compaction hysteresis,
currency autofill and calculation field descriptions."""
import json
from datetime import datetime, timezone

from app.model_protocol import ModelResponse, ModelToolCall, ModelUsage
from app.research_runtime import (MIN_COMPACTION_SAVINGS_CHARS, TOOLS, SYSTEM, compact_runtime_history,
                                  run_research_agent)
from app.research_sources import ResearchSource, SourcePermissions

from tests.fin_runtime_helpers import FakeTransport, json_reply, make_toolbox, memory_persist
from tests.test_research_runtime import Model, context, turn

STOP = ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def make_source(source_id='p1', text='Revenue was 100 USD.', provenance=None, kind='user_supplied', retrieval_method='user_supplied'):
    return ResearchSource(source_id=source_id, owner_id='owner', title='Doc ' + source_id, text=text, kind=kind,
                          retrieval_method=retrieval_method, obtained_at=NOW, content_sha256='h' + source_id,
                          provenance=provenance or {},
                          permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))


def multi(calls):
    """One model response with several tool calls: calls = [(name, args, id), ...]."""
    objs = tuple(ModelToolCall(i, n, json.dumps(a)) for n, a, i in calls)
    msg = {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': o.id, 'type': 'function', 'function': {'name': o.name, 'arguments': o.arguments}} for o in objs]}
    return ModelResponse(msg, objs, ModelUsage(1, 1, 2), 'tool_calls')


def tool_messages(model, index):
    return [json.loads(m['content']) for m in model.inputs[index] if m.get('role') == 'tool']


def paged_source(pages, source_id='pdf'):
    """pages: list of (text|None) - None means a needs_ocr page. Pages are joined with form feeds."""
    text, entries, offset = '', [], 0
    for number, piece in enumerate(pages):
        if piece is None:
            entries.append([offset, offset, 'needs_ocr'])
            piece = ''
        else:
            entries.append([offset, offset + len(piece), 'text'])
        text += piece + ('\f' if number < len(pages) - 1 else '')
        offset += len(piece) + 1
    page_map = {'page_count': len(pages), 'pages': entries, 'text_limit_reached': False, 'original_retained': True,
                'method': 'pdftotext_layout', 'version': 1}
    return make_source(source_id, text, {'document_pages': page_map})


def run(model, sources, **kw):
    return run_research_agent(question='Q', sources=sources, model=model, context=context(), **kw)


def test_read_pages_tool_schema_and_order():
    names = [t.name for t in TOOLS]
    assert 'read_pages' in names and names[-1] == 'submit_research'
    schema = next(t for t in TOOLS if t.name == 'read_pages').input_schema
    assert set(schema['required']) == {'source_id', 'first_page', 'last_page', 'rationale'}
    assert 'read_pages' in SYSTEM and '2026' not in SYSTEM


def test_read_pages_reads_text_pages_reports_ocr_and_records_ranges():
    src = paged_source(['Page one revenue 10.', None, 'Page three margin 20.'])
    m = Model([turn('read_pages', {'source_id': 'pdf', 'first_page': 1, 'last_page': 3, 'rationale': 'r'}, 'a'), STOP])
    out = run(m, [src])
    fb = json.loads(m.inputs[1][-1]['content'])
    assert fb['results'][0]['text'] == src.text[0:len(src.text)]
    assert [r['status'] for r in fb['page_statuses']] == ['text', 'needs_ocr', 'text']
    assert 'OCR' in fb['message'] and 'next_page' not in fb
    assert out.source_ids == ['pdf']


def test_read_pages_only_ocr_pages_returns_needs_ocr_status():
    src = paged_source(['Text page.', None])
    m = Model([turn('read_pages', {'source_id': 'pdf', 'first_page': 2, 'last_page': 2, 'rationale': 'r'}, 'a'), STOP])
    run(m, [src])
    fb = json.loads(m.inputs[1][-1]['content'])
    assert fb['status'] == 'needs_ocr' and fb['page_statuses'] == [{'page': 2, 'status': 'needs_ocr'}]
    assert 'not available' in fb['message']


def test_read_pages_limit_returns_next_page_and_continues():
    piece = 'x' * 9000
    src = paged_source([piece, piece, piece, piece])
    m = Model([turn('read_pages', {'source_id': 'pdf', 'first_page': 1, 'last_page': 4, 'rationale': 'r'}, 'a'),
               turn('read_pages', {'source_id': 'pdf', 'first_page': 3, 'last_page': 4, 'rationale': 'more'}, 'b'), STOP])
    run(m, [src])
    first = json.loads(m.inputs[1][-1]['content'])
    assert first['pages_read'] == [1, 2] and first['next_page'] == 3 and first['truncated_by_limit'] is True
    assert len(first['results'][0]['text']) <= 25_000
    second = json.loads(m.inputs[2][-1]['content'])
    assert second['pages_read'] == [3, 4] and 'next_page' not in second


def test_read_pages_without_page_map_is_invalid_and_points_to_read_sources():
    m = Model([turn('read_pages', {'source_id': 'p1', 'first_page': 1, 'last_page': 2, 'rationale': 'r'}, 'a'), STOP])
    run(m, [make_source()])
    fb = json.loads(m.inputs[1][-1]['content'])
    assert fb['status'] == 'invalid' and 'read_sources' in fb['instruction']


def test_read_pages_validates_page_order():
    m = Model([turn('read_pages', {'source_id': 'pdf', 'first_page': 3, 'last_page': 1, 'rationale': 'r'}, 'a'), STOP])
    run(m, [paged_source(['a', 'b', 'c'])])
    assert json.loads(m.inputs[1][-1]['content'])['status'] == 'invalid'


def test_outline_includes_page_map_summary_and_entry_pages():
    pages = ['Intro text', 'Item 1. Business\nsome text', None, 'Item 7. Management Discussion\nmore']
    src = paged_source(pages)
    m = Model([turn('outline_source', {'source_id': 'pdf'}, 'a'), STOP])
    run(m, [src])
    fb = json.loads(m.inputs[1][-1]['content'])
    assert fb['page_count'] == 4
    assert fb['page_status_summary'] == {'counts': {'text': 3, 'needs_ocr': 1}, 'needs_ocr_pages': [3]}
    by_title = {e['title']: e.get('page') for e in fb['entries']}
    assert by_title['Item 1. Business'] == 2 and by_title['Item 7. Management Discussion'] == 4


def price_toolbox(fail_first=False, persist=None):
    replies = ([OSError('down')] if fail_first else []) + [json_reply('td_time_series_ipo.json')]
    transport = FakeTransport(*replies) if len(replies) > 1 else FakeTransport(replies[0])
    return make_toolbox(twelve=transport, persist=persist), transport


PRICE = {'entity_id': 'SSE:ANT', 'lookback': '1m', 'rationale': 'p'}


def test_successful_acquisition_repeat_in_next_turn_is_reused_without_a_second_fetch():
    box, transport = price_toolbox()
    m = Model([turn('market_data', PRICE, 'a'),
               multi([('market_data', {**PRICE, 'rationale': 'again'}, 'b'),
                      ('read_sources', {'reads': [{'source_id': 'src_1'}], 'rationale': 'read'}, 'e')]),
               multi([('market_data', PRICE, 'c'),
                      ('read_sources', {'reads': [{'source_id': 'src_1', 'end': 5}], 'rationale': 'read'}, 'd')]),
               STOP])
    out = run(m, [], fin_data=box, now=lambda: NOW)
    assert len(transport.calls) == 1
    second = tool_messages(m, 2)[-2]
    assert second['status'] == 'duplicate_action' and second['reused'] is True and second['source_id'] == 'src_1'
    third = tool_messages(m, 3)[-2:]
    dup = next(r for r in third if r.get('status') == 'duplicate_action')
    assert dup['reused'] is True and dup['source_ids'] == ['src_1'] and any('results' in r for r in third)
    assert out.reason != 'no_progress'
    assert any(e.get('duplicate') == 'action' and e['name'] == 'market_data' for e in out.events)


def test_successful_acquisition_repeat_in_same_batch_is_reused():
    box, transport = price_toolbox()
    m = Model([multi([('market_data', PRICE, 'a'), ('market_data', {**PRICE, 'rationale': 'x'}, 'b')]), STOP])
    run(m, [], fin_data=box)
    first, second = tool_messages(m, 1)[-2:]
    assert len(transport.calls) == 1 and first['status'] == 'ok'
    assert second['status'] == 'duplicate_action' and second['reused'] is True and second['source_id'] == 'src_1'


def test_lookback_and_explicit_dates_with_same_resolved_range_are_duplicates():
    persist = memory_persist()
    box, transport = price_toolbox(persist=persist)
    m = Model([turn('market_data', PRICE, 'a'), STOP])
    state = []
    run(m, [], fin_data=box, checkpoint=lambda st: state.append(json.loads(json.dumps(st))))
    url = transport.calls[0][0]
    start = url.split('start_date=')[1].split('&')[0]
    explicit = {'entity_id': 'SSE:ANT', 'start_date': start, 'end_date': '2026-11-19', 'rationale': 'explicit'}
    assert state[-1]['acquired_actions']  # persisted in checkpointed state
    # Resume from the persisted state: the explicit-date call resolves to the same range and is reused.
    saved = state[-1]
    saved['history'] = []
    m2 = Model([multi([('market_data', explicit, 'z'),
                       ('read_sources', {'reads': [{'source_id': 'src_1'}], 'rationale': 'r'}, 'y')]), STOP])
    box2, transport2 = price_toolbox()
    run(m2, list(persist.store), fin_data=box2, initial_state=saved)
    fb = tool_messages(m2, 1)[-2]
    assert not transport2.calls and fb['status'] == 'duplicate_action' and fb['reused'] is True and fb['source_id'] == 'src_1'


def test_failed_acquisition_is_not_cached_and_repeat_follows_general_rules():
    box, transport = price_toolbox(fail_first=True)
    other = {'entity_id': 'SSE:ANT', 'lookback': '3m', 'rationale': 'p'}
    m = Model([turn('market_data', PRICE, 'a'),
               multi([('market_data', PRICE, 'b'), ('market_data', other, 'c')]), STOP])
    out = run(m, [], fin_data=box)
    assert tool_messages(m, 1)[-1]['status'] == 'unknown'
    rep, new = tool_messages(m, 2)[-2:]
    assert rep['status'] == 'duplicate_action' and 'reused' not in rep  # general signature rule, not the success cache
    assert new['status'] == 'ok' and len(transport.calls) == 2
    assert out.reason != 'no_progress'


def test_lone_all_duplicate_response_gets_results_and_warning_then_two_in_a_row_stop():
    m = Model([turn('discover_sources', {'query': 'Q', 'rationale': 'first'}, 'a'),
               multi([('discover_sources', {'query': 'Q', 'rationale': 'again'}, 'b'),
                      ('discover_sources', {'query': 'Q', 'rationale': 'once more'}, 'c')]),
               multi([('discover_sources', {'query': 'Q', 'rationale': 'and again'}, 'd')])])
    out = run(m, [])
    assert out.reason == 'no_progress'
    assert [e.get('duplicate') for e in out.events if e.get('kind') == 'tool'] == [None, 'action', 'action', 'action']
    first_dups = tool_messages(m, 2)[-2:]
    assert all(r['status'] == 'duplicate_action' for r in first_dups)
    assert 'will stop' in first_dups[-1]['run_notice'] and 'run_notice' not in first_dups[0]


def test_new_action_between_duplicate_responses_resets_the_streak():
    m = Model([turn('discover_sources', {'query': 'Q', 'rationale': 'first'}, 'a'),
               turn('discover_sources', {'query': 'Q', 'rationale': 'again'}, 'b'),
               turn('discover_sources', {'query': 'Q2', 'rationale': 'new move'}, 'c'),
               turn('discover_sources', {'query': 'Q2', 'rationale': 'repeat'}, 'd'), STOP])
    out = run(m, [])
    assert out.reason == 'no_submission'
    assert [e.get('duplicate') for e in out.events if e.get('kind') == 'tool'] == [None, 'action', None, 'action']


def test_repeated_invalid_in_mixed_batch_does_not_stop():
    bad = {'reads': [], 'rationale': 'x'}
    m = Model([turn('read_sources', bad, 'a'),
               multi([('read_sources', bad, 'b'),
                      ('read_sources', {'reads': [{'source_id': 'p1'}], 'rationale': 'ok'}, 'c')]), STOP])
    out = run(m, [make_source()])
    assert out.reason != 'no_progress'
    results = tool_messages(m, 2)[-2:]
    assert results[0]['status'] == 'invalid' and 'results' in results[1]


def quotation_error(source, quotation, **claim_extra):
    claim = {'text': 't', 'source_id': source.source_id, 'quotation': quotation, 'role': 'support', **claim_extra}
    sub = {'report': 'r', 'claims': [claim], 'limitations': [], 'stopping_reason': 's'}
    return sub


def test_locate_reports_quotation_outside_read_ranges_with_page():
    src = paged_source(['First page filler text.', 'Second page has Net income of 55 million.'])
    start = src.text.index('Net income')
    m = Model([turn('read_pages', {'source_id': 'pdf', 'first_page': 1, 'last_page': 1, 'rationale': 'r'}, 'a'),
               turn('submit_research', quotation_error(src, 'Net income of 55 million'), 'b'), STOP])
    run(m, [src])
    fb = json.loads(m.inputs[2][-1]['content'])
    assert fb['status'] == 'invalid'
    reason = fb['reason']
    assert f'[{start}, {start + len("Net income of 55 million")})' in reason and 'page 2' in reason
    assert 'OUTSIDE' in reason and 'read_pages' in reason


def test_locate_not_found_anywhere_suggests_checking_whitespace_and_escapes():
    src = make_source('p1', 'Revenue was 100 USD.')
    m = Model([turn('read_sources', {'reads': [{'source_id': 'p1'}], 'rationale': 'r'}, 'a'),
               turn('submit_research', quotation_error(src, 'Revenue was 999 USD.'), 'b'), STOP])
    run(m, [src])
    reason = json.loads(m.inputs[2][-1]['content'])['reason']
    assert 'does not occur verbatim anywhere' in reason and 'whitespace' in reason


def history_state(n_results, size):
    history = []
    for i in range(n_results):
        history.append({'assistant': {'role': 'assistant', 'content': None, 'tool_calls': [
                            {'id': f'c{i}', 'type': 'function', 'function': {'name': 'read_sources', 'arguments': '{}'}}]},
                        'results': [{'tool_call_id': f'c{i}', 'content': json.dumps({'results': [
                            {'source_id': 's', 'start': 0, 'end': size, 'text': 'x' * size}]}), 'is_error': False}]})
    return {'history': history, 'facts': [], 'events': []}


def test_compaction_reaches_low_water_in_one_pass_and_records_it():
    state = history_state(14, 10_000)
    trigger = 100_000
    report = compact_runtime_history(state, keep_recent=2, trigger_chars=trigger, force=False)
    assert report['compacted'] is True
    assert report['after_chars'] <= int(trigger * 0.6)
    assert report['receipt']['low_water_chars'] == int(trigger * 0.6)
    # Only as many oldest results as needed were cleared: the newest eligible ones stay intact.
    cleared = [json.loads(h['results'][0]['content']).get('cleared') for h in state['history']]
    assert cleared[0] is True and cleared[-1] is None and cleared[-3] is None
    again = compact_runtime_history(state, keep_recent=2, trigger_chars=trigger, force=False)
    assert again['compacted'] is False  # below trigger: history untouched
    assert len(state['context_receipts']) == 1


def test_compaction_with_tiny_saving_is_skipped_without_receipt():
    state = history_state(4, 1_000)
    state['history'][0]['results'][0]['content'] = json.dumps({'results': [{'text': 'y' * 2_000}]})
    before = json.dumps(state['history'])
    trigger = 4_500  # crossed, but the achievable saving is far below the minimum
    report = compact_runtime_history(state, keep_recent=2, trigger_chars=trigger, force=False)
    assert report['compacted'] is False and report['reason'] == 'below_min_savings'
    assert json.dumps(state['history']) == before and 'context_receipts' not in state
    assert MIN_COMPACTION_SAVINGS_CHARS == 8_000
    # Beyond the hard limit the compaction proceeds even for a small saving.
    forced = compact_runtime_history(state, keep_recent=2, trigger_chars=trigger, force=False, hard_limit_chars=3_000)
    assert forced['compacted'] is True


def test_record_facts_fills_currency_from_unit_for_structured_sources_only():
    px = make_source('px', 'Close on 2026-11-19 was 100 USD.', kind='market_data', retrieval_method='market_data')
    plain = make_source('doc', 'Revenue was 100 USD.')
    def fact(sid, text):
        return {'label': 'close', 'value': 100, 'unit': 'USD', 'period': '2026-11-19', 'source_id': sid, 'quotation': text}
    m = Model([turn('read_sources', {'reads': [{'source_id': 'px'}, {'source_id': 'doc'}], 'rationale': 'r'}, 'a'),
               turn('record_facts', {'facts': [fact('px', 'Close on 2026-11-19 was 100 USD.')], 'rationale': 'f'}, 'b'),
               turn('record_facts', {'facts': [fact('doc', 'Revenue was 100 USD.')], 'rationale': 'g'}, 'c'), STOP])
    out = run(m, [px, plain])
    by_source = {f['source_id']: f for f in out.facts}
    assert by_source['px']['currency'] == 'USD'
    assert by_source['doc']['currency'] is None


def test_calculation_plan_fields_describe_operand_order():
    schema = next(t for t in TOOLS if t.name == 'calculate_financials').input_schema
    plan = schema['$defs']['CalculationPlan']['properties']
    assert 'current' in plan['first_fact_id']['description'] and 'numerator' in plan['first_fact_id']['description']
    assert 'baseline' in plan['second_fact_id']['description'] and '(first/second - 1) * 100' in plan['second_fact_id']['description']
