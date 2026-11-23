from datetime import datetime, timezone
from types import SimpleNamespace

from app.research_run_insights import assess_core_requirements, build_working_memory_anchor


def test_quantitative_question_cannot_be_closed_by_a_proxy_claim():
    question = '分析一下Example的市值变化'
    for quotation, expected in [
        ('Example closing price went from 100 to 120 USD.', 'missing'),
        ('Example market capitalization rose from USD 100 billion to USD 120 billion.', 'missing'),
    ]:
        state = {'read_ranges': [{'source_id': 's', 'start': 0, 'end': len(quotation)}]}
        claims = [{'claim_id': 'c', 'role': 'support', 'text': 'Example市值变化与股价有关。',
                   'citations': [{'source_id': 's', 'quotation': quotation}]}]
        rows = assess_core_requirements(question, state, {'s': source(quotation)}, claims=claims)
        assert rows[0]['status'] == expected


def source(text='100 120 2 3', *, published='2024-01-01'):
    return SimpleNamespace(text=text, is_simulated=False, url=None,
                           permissions=SimpleNamespace(model_input=True, display=True),
                           published_at=datetime.fromisoformat(published).replace(tzinfo=timezone.utc), revised_at=None)


def fact(fid, label, value, period, start, end, unit='USD', entity='ACME'):
    return {'fact_id': fid, 'source_id': 's', 'label': label, 'value': value, 'period': period,
            'unit': unit, 'currency': 'USD', 'entity': entity, 'character_start': start,
            'character_end': end, 'quotation': source().text[start:end]}


def calc(cid, first, second, formula='growth_percent'):
    from app.research_calculations import CalculationRequest, calculate
    keys = ('fact_id', 'value', 'unit', 'currency', 'period')
    payload = {'formula': formula, 'first': {k: first[k] for k in keys}, 'second': {k: second[k] for k in keys}}
    return {**calculate(CalculationRequest.model_validate(payload)), 'calculation_id': cid, 'input_status': 'matched'}


def test_changed_calculation_values_or_bound_inputs_do_not_satisfy_comparison():
    import copy
    f = [fact('r1', 'revenue', '120', 'FY2024', 4, 7), fact('r0', 'revenue', '100', 'FY2023', 0, 3)]
    state = {'facts': f, 'calculations': [calc('c', f[0], f[1])]}
    question = 'Compare revenue growth rate'
    assert assess_core_requirements(question, state, {'s': source()})[0]['status'] == 'satisfied'
    for key, value in (('value', '99'), ('input', '999')):
        altered = copy.deepcopy(state)
        if key == 'input':
            altered['calculations'][0]['inputs'][0]['value'] = value
        else:
            altered['calculations'][0][key] = value
        assert assess_core_requirements(question, altered, {'s': source()})[0]['status'] != 'satisfied'


def test_financial_comparison_requires_each_metric_and_ordered_comparable_inputs():
    from app.research_calculations import CalculationRequest, calculate
    q = 'Compare revenue growth and the change in diluted EPS'
    f = [fact('r0', 'revenue', '100', 'FY2023', 0, 3), fact('r1', 'revenue', '120', 'FY2024', 4, 7),
         fact('e1', 'diluted EPS', '3', 'FY2024', 10, 11)]
    state = {'facts': f, 'calculations': [calc('r', f[1], f[0])]}
    rows = assess_core_requirements(q, state, {'s': source()})
    assert {x['metric']: x['status'] for x in rows} == {'revenue': 'satisfied', 'diluted_eps': 'partial'}
    state['facts'].append(fact('e0', 'diluted EPS', '2', 'FY2023', 8, 9))
    state['calculations'].append(calc('e', f[2], f[3], 'difference'))
    assert all(x['status'] == 'satisfied' for x in assess_core_requirements(q, state, {'s': source()}))
    state['facts'][-1]['label'] = 'basic EPS'
    assert assess_core_requirements(q, state, {'s': source()})[1]['status'] == 'partial'
    state['facts'][-1]['label'] = 'diluted EPS'
    state['facts'][-1]['period'] = 'H1 2023'
    assert assess_core_requirements(q, state, {'s': source()})[1]['status'] == 'partial'
    state['facts'][-1]['period'] = 'FY2023'
    state['facts'][-1]['currency'] = 'EUR'
    assert assess_core_requirements(q, state, {'s': source()})[1]['status'] == 'partial'

    current, prior = f[1], f[0]
    current['unit'], prior['unit'] = 'USD millions', 'USD thousands'
    base = {k: prior[k] for k in ('fact_id', 'value', 'period', 'currency', 'unit')}
    latest = {k: current[k] for k in base}
    aligned = calculate(CalculationRequest.model_validate({'formula': 'growth_percent', 'first': latest, 'second': base}))
    state['calculations'][0] = {**aligned, 'calculation_id': 'r'}
    assert assess_core_requirements(q, state, {'s': source()})[0]['status'] == 'satisfied'
    state['calculations'][0]['scale_adjustments'] = []
    assert assess_core_requirements(q, state, {'s': source()})[0]['status'] == 'partial'


def test_cutoff_and_unavailable_source_remove_coverage():
    state = {'facts': [fact('r', 'revenue', '120', 'FY2024', 4, 7)]}
    q = 'What is revenue?'
    assert assess_core_requirements(q, state, {'s': source()})[0]['status'] == 'satisfied'
    assert assess_core_requirements(q, state, {'s': source()}, as_of=datetime(2023, 1, 1, tzinfo=timezone.utc))[0]['status'] == 'missing'
    assert assess_core_requirements(q, state, {})[0]['status'] == 'missing'


def test_benchmark_needs_distinct_instruments_and_common_two_date_window():
    from app.research_return_context import price_return_context
    q = 'Compare ACME performance with SPY as the benchmark'
    quotes = ['ACME | 2024-01-01 | 1d | O 100 H 100 L 100 C 100 V 1', 'ACME | 2024-02-01 | 1d | O 120 H 120 L 120 C 120 V 1',
              'SPY | 2024-01-01 | 1d | O 100 H 100 L 100 C 100 V 1', 'SPY | 2024-02-01 | 1d | O 120 H 120 L 120 C 120 V 1']
    text = '\n'.join(quotes)
    src = source(text)
    src.kind, src.provenance = 'market_data', {'adjustment': 'adjusted'}
    facts = []
    for i, quote in enumerate(quotes):
        start = text.index(quote)
        facts.append({'fact_id': str(i), 'source_id': 's', 'label': 'close price', 'value': '100' if i % 2 == 0 else '120',
                      'period': '2024-01-01' if i % 2 == 0 else '2024-02-01', 'unit': 'USD', 'currency': 'USD',
                      'quotation': quote, 'character_start': start, 'character_end': start + len(quote)})
    by_id = {f['fact_id']: f for f in facts}
    def ret(cid, first, second):
        row = calc(cid, by_id[first], by_id[second])
        row['return_context'] = price_return_context(row, by_id, {'s': src})
        return row
    state = {'facts': facts, 'calculations': [ret('a', '1', '0'), ret('b', '1', '0')]}
    assert assess_core_requirements(q, state, {'s': src})[0]['status'] == 'missing'
    state['calculations'][1] = ret('b', '3', '2')
    assert assess_core_requirements(q, state, {'s': src})[0]['status'] == 'satisfied'
    state['calculations'][1]['return_context']['end_date'] = '2024-03-01'
    assert assess_core_requirements(q, state, {'s': src})[0]['status'] == 'missing'


def test_generic_and_targeted_counterevidence_require_bindings():
    q = 'Explain the terms and provide counter-evidence'
    state = {'events': [{'kind': 'tool', 'name': 'news_search', 'status': 'ok'}]}
    assert assess_core_requirements(q, state, {'s': source()})[0]['status'] == 'missing'
    claim = {'role': 'counterargument', 'claim_id': 'c', 'challenges_claim_ids': ['support'],
             'citations': [{'source_id': 's', 'quotation': '100'}]}
    assert assess_core_requirements(q, state, {'s': source()}, claims=[claim])[0]['status'] == 'missing'
    state['read_ranges'] = [{'source_id': 's', 'start': 0, 'end': 3}]
    assert assess_core_requirements(q, state, {'s': source()}, claims=[claim])[0]['status'] == 'satisfied'
    state = {'user': '{"question":"What is revenue?"}', 'facts': []}
    assert 'Core evidence needed' in build_working_memory_anchor(state, {})


def test_unrecognized_clause_is_not_lost():
    q = 'What is revenue and when should I enter?'
    state = {'facts': [fact('r', 'revenue', '120', 'FY2024', 4, 7)]}
    rows = assess_core_requirements(q, state, {'s': source()})
    assert [r['status'] for r in rows] == ['satisfied', 'missing']
    assert rows[1]['question'] == 'when should I enter'


def test_runtime_financial_comparison_uses_normal_record_and_calculation_tools():
    import json
    from tests.test_research_runtime_attention import _source, _ctx, _tool_response, NativeTurnRecorder
    from app.research_runtime import run_research_agent
    quote = 'Alpha 2025 revenue 120 USD, 2024 revenue 100 USD, 2025 diluted EPS 3 USD, 2024 diluted EPS 2 USD.'
    src = _source(text=quote)
    values = [('revenue', 120, 'FY2025'), ('revenue', 100, 'FY2024'),
              ('diluted EPS', 3, 'FY2025'), ('diluted EPS', 2, 'FY2024')]
    fact_rows = [{'label': label, 'value': value, 'unit': 'USD', 'currency': 'USD', 'period': period,
                  'source_id': 's1', 'quotation': quote} for label, value, period in values]
    submit = {'report': 'Alpha revenue and diluted EPS changed.',
              'claims': [{'text': 'Alpha revenue and diluted EPS changed.', 'source_id': 's1', 'quotation': quote, 'role': 'support'}],
              'limitations': [], 'stopping_reason': 'answered'}
    saved = []
    def calculations(_turn, _index):
        facts = saved[-1]['facts']
        ids = [f['fact_id'] for f in facts]
        return _tool_response([('calculate_financials', {'calculations': [
            {'formula': 'growth_percent', 'first_fact_id': ids[0], 'second_fact_id': ids[1]},
            {'formula': 'difference', 'first_fact_id': ids[2], 'second_fact_id': ids[3]}],
            'rationale': 'Compare the ordered annual inputs.'}, 'calc')])
    recorder = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'read')]),
        _tool_response([('record_facts', {'facts': fact_rows, 'rationale': 'record'}, 'facts')]),
        calculations,
        _tool_response([('submit_research', submit, 'submit')]),
        _tool_response([('submit_research', submit, 'submit-again')]),
    ])
    out = run_research_agent(question='比较 Alpha 2025 与 2024 年收入增速，并说明稀释 EPS 变化。',
                             sources=[src], model=recorder, context=_ctx(),
                             checkpoint=lambda state: saved.append(json.loads(json.dumps(state))))
    assert len(out.calculations) == 2
    assert out.status == 'completed', out
    assert len(out.question_parts) == 2
    assert all(p['status'] == 'answered' and '尚未回答' not in p['reason'] for p in out.question_parts)
    statuses = {r.get('metric'): r['status'] for r in assess_core_requirements(
        '比较 Alpha 2025 与 2024 年收入增速，并说明稀释 EPS 变化。', saved[-1], {'s1': src})}
    assert statuses['revenue'] == statuses['diluted_eps'] == 'satisfied'


def test_runtime_market_comparison_uses_persisted_analytics_and_selection_reason():
    import json
    from tests.test_research_runtime_attention import _ctx, _tool_response, NativeTurnRecorder
    from tests.test_research_market_analytics import _market_source
    from app.research_runtime import run_research_agent
    def series(symbol, first, second):
        return '\n'.join([f'{symbol}@NASDAQ | 2026-01-05 | 1d | O {first} H {first} L {first} C {first} V 100 USD ADJF 1',
                          f'{symbol}@NASDAQ | 2026-01-06 | 1d | O {second} H {second} L {second} C {second} V 100 USD ADJF 1'])
    a, b = _market_source('a', series('GAMMA', 10, 12)), _market_source('b', series('SPY', 100, 110))
    req = {'source_id': 'a', 'benchmark_source_id': 'b', 'price_basis': 'adjusted',
           'benchmark_selection_reason': 'SPY tracks a broad US market index.'}
    submit = {'report': 'Gamma and its benchmark had different returns.',
              'claims': [{'text': 'Gamma closing price was 12 USD.', 'source_id': 'a',
                          'quotation': a.text.splitlines()[1], 'role': 'support'}],
              'limitations': [], 'stopping_reason': 'answered'}
    saved = []
    recorder = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 'a'}], 'rationale': 'read'}, 'read')]),
        _tool_response([('analyze_market_series', req, 'market')]),
        _tool_response([('submit_research', submit, 'submit')]),
        _tool_response([('submit_research', submit, 'submit-again')]),
    ])
    out = run_research_agent(question='比较 Gamma 与基准在两个共同交易日的调整收盘收益。', sources=[a, b],
                       model=recorder, context=_ctx(), checkpoint=lambda state: saved.append(json.loads(json.dumps(state))))
    rows = assess_core_requirements('比较 Gamma 与基准在两个共同交易日的调整收盘收益。', saved[-1], {'a': a, 'b': b})
    assert rows[0]['status'] == 'satisfied'
    assert rows[0]['references'][0].get('analysis_id')
    assert out.status == 'completed', out


def test_runtime_qualitative_question_and_complete_announcement_absence():
    import json
    from tests.test_research_runtime_attention import _source, _ctx, _tool_response, NativeTurnRecorder
    from app.research_runtime import run_research_agent
    cases = [
        ('公告的赎回截止日及其条件是什么？', '赎回截止日是 2026-06-30，条件是获得监管批准。', '赎回截止日是 2026-06-30，条件是获得监管批准。'),
        ('完整公告是否披露赎回截止日？', '本公告允许赎回，条件是获得监管批准。', '完整公告未披露赎回截止日。'),
    ]
    for index, (question, text, answer) in enumerate(cases):
        from hashlib import sha256
        src = _source(text=text)
        src.content_sha256 = sha256(text.encode()).hexdigest()
        submit = {'report': answer, 'claims': [{'text': answer, 'source_id': 's1', 'quotation': text, 'role': 'support'}],
                  'limitations': [], 'stopping_reason': 'answered'}
        recorder = NativeTurnRecorder([
            _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read full announcement'}, 'read')]),
            _tool_response([('submit_research', submit, 'submit')]),
            _tool_response([('submit_research', submit, 'submit-again')]),
        ], review_verdict='supported' if index == 0 else 'insufficient')
        saved = []
        out = run_research_agent(question=question, sources=[src], model=recorder, context=_ctx(),
                                 checkpoint=lambda state: saved.append(json.loads(json.dumps(state))))
        assert out.status == ('completed' if index == 0 else 'partial'), out


def test_runtime_cutoff_and_question_about_conflicting_revenue():
    import json
    from tests.test_research_runtime_attention import _source, _ctx, _tool_response, NativeTurnRecorder
    from app.research_runtime import run_research_agent
    cutoff = datetime(2026, 3, 31, tzinfo=timezone.utc)
    from hashlib import sha256
    early = _source('early', 'G 2025 年指标已公布值为 50。')
    early.content_sha256 = sha256(early.text.encode()).hexdigest()
    early.published_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
    late = _source('late', 'G 2025 年指标修订值为 55。')
    late.published_at = datetime(2026, 4, 10, tzinfo=timezone.utc)
    submit = {'report': early.text, 'claims': [{'text': early.text, 'source_id': 'early', 'quotation': early.text, 'role': 'support'}],
              'limitations': [], 'stopping_reason': 'answered'}
    recorder = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 'early'}], 'rationale': 'read'}, 'read')]),
        _tool_response([('submit_research', submit, 'submit')]),
        _tool_response([('submit_research', submit, 'submit-again')]),
    ])
    out = run_research_agent(question='截至 2026-03-31，G 指标 2025 年的已公布值是多少？', sources=[early, late],
                             model=recorder, context=_ctx(), as_of=cutoff)
    assert out.status == 'completed', out
    assert all(c['citations'][0]['source_id'] == 'early' for c in out.claims)

    a, b = _source('a', 'H 2025 revenue 50 USD.'), _source('b', 'H 2025 revenue 80 USD.')
    # Comparable issuer identity is explicit on both sources.
    a.provenance = {'symbol': 'H'}
    b.provenance = {'symbol': 'H'}
    claim_rows = [{'text': source.text, 'source_id': source.source_id, 'quotation': source.text, 'role': 'support'} for source in (a, b)]
    facts = [{'label': 'H revenue', 'value': value, 'unit': 'USD', 'currency': 'USD', 'period': '2025',
              'source_id': sid, 'quotation': text} for sid, value, text in [('a', 50, a.text), ('b', 80, b.text)]]
    submit = {'report': 'H revenue was 50 USD. H revenue was 80 USD. Sources conflict.', 'claims': claim_rows,
              'limitations': ['The conflicting values remain unexplained.'], 'stopping_reason': 'answered'}
    recorder = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 'a'}, {'source_id': 'b'}], 'rationale': 'read'}, 'read')]),
        _tool_response([('record_facts', {'facts': facts, 'rationale': 'compare disclosures'}, 'facts')]),
        _tool_response([('submit_research', submit, 'submit')]),
        _tool_response([('submit_research', submit, 'submit-again')]),
    ])
    saved = []
    out = run_research_agent(question='H 两份同日资料是否对 2025 年收入存在无法解释的冲突？', sources=[a, b],
                             model=recorder, context=_ctx(), checkpoint=lambda state: saved.append(json.loads(json.dumps(state))))
    rows = assess_core_requirements('H 两份同日资料是否对 2025 年收入存在无法解释的冲突？', saved[-1], {'a': a, 'b': b})
    assert rows[0]['status'] == 'satisfied'
    assert out.status == 'completed', out


def test_requested_issuer_and_period_must_match_recorded_fact():
    q = 'What was Alpha revenue in 2020?'
    src = source('Beta revenue 120 in 2021. Alpha revenue 100 in 2020.')
    wrong = {'fact_id': 'f', 'source_id': 's', 'label': 'Beta revenue', 'period': 'FY2021', 'value': '120',
             'unit': 'USD', 'currency': 'USD', 'quotation': 'Beta revenue 120 in 2021.',
             'character_start': 0, 'character_end': 25}
    assert assess_core_requirements(q, {'facts': [wrong]}, {'s': src})[0]['status'] == 'missing'
    wrong['period'] = 'FY2020'
    assert assess_core_requirements(q, {'facts': [wrong]}, {'s': src})[0]['status'] == 'missing'


def test_targeted_negative_search_is_scoped_to_eligible_document():
    q = 'Discuss demand and counter-evidence'
    src = source('The source discusses demand but no price competition.')
    intent = {'scope': 'document', 'status': 'completed', 'matches': 0, 'target': q,
              'hypothesis': 'price competition', 'query': 'price war', 'source_id': 's'}
    row = assess_core_requirements(q, {'counter_searches': [intent]}, {'s': src})[0]
    assert row['status'] == 'satisfied'
    assert 'does not exclude evidence elsewhere' in row['reason']
    intent['scope'] = 'external'
    assert assess_core_requirements(q, {'counter_searches': [intent]}, {'s': src})[0]['status'] == 'missing'
    intent['scope'], intent['target'] = 'document', 'unrelated'
    assert assess_core_requirements(q, {'counter_searches': [intent]}, {'s': src})[0]['status'] == 'missing'


def test_runtime_missing_prior_eps_and_duplicate_market_data_stay_partial():
    import json
    from tests.test_research_runtime_attention import _source, _ctx, _tool_response, NativeTurnRecorder
    from tests.test_research_market_analytics import _market_source
    from app.research_runtime import run_research_agent
    text = 'Beta 2025 revenue 120 USD, 2024 revenue 100 USD, 2025 diluted EPS 3 USD.'
    src = _source(text=text)
    rows = [{'label': label, 'value': value, 'unit': 'USD', 'currency': 'USD', 'period': period,
             'source_id': 's1', 'quotation': text} for label, value, period in
            [('revenue', 120, 'FY2025'), ('revenue', 100, 'FY2024'), ('diluted EPS', 3, 'FY2025')]]
    submit = {'report': 'Beta revenue and diluted EPS are disclosed.',
              'claims': [{'text': text, 'source_id': 's1', 'quotation': text, 'role': 'support'}],
              'limitations': ['Prior diluted EPS is missing.'], 'stopping_reason': 'answered'}
    saved = []
    def calculate(_turn, _index):
        ids = [f['fact_id'] for f in saved[-1]['facts']]
        return _tool_response([('calculate_financials', {'calculations': [{'formula': 'growth_percent', 'first_fact_id': ids[0], 'second_fact_id': ids[1]}], 'rationale': 'growth'}, 'calc')])
    recorder = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'read')]),
        _tool_response([('record_facts', {'facts': rows, 'rationale': 'record'}, 'facts')]), calculate,
        _tool_response([('submit_research', submit, 'submit')]),
        _tool_response([('submit_research', submit, 'submit-again')]),
    ])
    out = run_research_agent(question='比较 Beta 2025 与 2024 年收入增速，并说明稀释 EPS 变化。', sources=[src],
                             model=recorder, context=_ctx(), checkpoint=lambda state: saved.append(json.loads(json.dumps(state))))
    assert out.status == 'partial'
    assert {r.get('metric'): r['status'] for r in assess_core_requirements(
        '比较 Beta 2025 与 2024 年收入增速，并说明稀释 EPS 变化。', saved[-1], {'s1': src})} == {'revenue': 'satisfied', 'diluted_eps': 'partial'}
    assert any('EPS' in p['question'] and '2024' in p['reason'] for p in out.question_parts)

    bars = '\n'.join(f'DELTA@NASDAQ | 2026-01-0{day} | 1d | O {value} H {value} L {value} C {value} V 100 USD ADJF 1'
                     for day, value in [(5, 10), (6, 12)])
    src = _market_source('delta', bars)
    submit = {'report': 'Delta closing price was 12 USD.',
              'claims': [{'text': 'Delta closing price was 12 USD.', 'source_id': 'delta',
                          'quotation': bars.splitlines()[1], 'role': 'support'}],
              'limitations': ['Benchmark data is missing.'], 'stopping_reason': 'answered'}
    recorder = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 'delta'}], 'rationale': 'read'}, 'read')]),
        _tool_response([('analyze_market_series', {'source_id': 'delta'}, 'market1')]),
        _tool_response([('analyze_market_series', {'source_id': 'delta'}, 'market2')]),
        _tool_response([('submit_research', submit, 'submit')]),
        _tool_response([('submit_research', submit, 'submit-again')]),
    ])
    out = run_research_agent(question='比较 Delta 与基准在两个共同交易日的调整收盘收益。', sources=[src],
                             model=recorder, context=_ctx())
    assert out.status == 'partial'
    assert any(part['status'] == 'answered' and '20' in part['answer'] and
               any(ref.get('analysis_id') for ref in part['references']) for part in out.question_parts)
    assert any(part['status'] == 'unresolved' for part in out.question_parts)


def test_chart_only_task_requires_matching_kind_units_source_and_replacement():
    question = 'Convert chart to line in billions'
    chart = {'chart_id': 'c', 'chart_type': 'line', 'axes': [{'scale': 'billions'}], 'source_ids': ['s'], 'replaces_chart_id': 'old'}
    state = {'charts': [chart]}
    assert assess_core_requirements(question, state, {'s': source()})[0]['status'] == 'satisfied'
    for change in ({'chart_type': 'bar'}, {'axes': [{'scale': 'millions'}]}, {'source_ids': ['unavailable']}, {'replaces_chart_id': None}):
        state['charts'] = [{**chart, **change}]
        assert assess_core_requirements(question, state, {'s': source()})[0]['status'] == 'missing'
