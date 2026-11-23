"""Per-item quotation failure reporting for submit_research and record_facts."""
import json

from app.research_runtime import run_research_agent

from tests.test_research_runtime import Model, context, turn
from tests.test_research_runtime_s0b import STOP, make_source, paged_source

TEXT = ('ETF:SPY | 2025-12-31 | daily close | 681.92 USD\n'
        'ETF:QQQ | 2025-12-31 | daily close | 614.31000 USD\n'
        'ETF:IWM | 2025-12-31 | daily close | 245.10 USD\n')
GOOD1 = 'ETF:SPY | 2025-12-31 | daily close | 681.92 USD'
GOOD2 = 'ETF:QQQ | 2025-12-31 | daily close | 614.31000 USD'
BAD2 = 'ETF:QQQ | 2025-12-31 | daily close | 614.31 USD'
GOOD3 = 'ETF:IWM | 2025-12-31 | daily close | 245.10 USD'
READ = turn('read_sources', {'reads': [{'source_id': 'p1'}], 'rationale': 'r'}, 'a')


def claim(quote, source_id='p1', **extra):
    return {'text': quote, 'source_id': source_id, 'quotation': quote, 'role': 'support', **extra}


def submit(claims, ident='b'):
    return turn('submit_research', {'report': 'r', 'claims': claims, 'limitations': [], 'stopping_reason': 's'}, ident)


def run(model, sources):
    return run_research_agent(question="What was the daily close?", sources=sources, model=model, context=context())


def last_tool(model, index):
    tool_msgs = [m for m in model.inputs[index] if m.get('role') == 'tool']
    return json.loads(tool_msgs[-1]['content'])


def test_only_failing_claim_is_listed_with_hint_and_resubmission_succeeds():
    src = make_source('p1', TEXT)
    m = Model([READ, submit([claim(GOOD1), claim(BAD2), claim(GOOD3)]),
               submit([claim(GOOD1), claim(GOOD2), claim(GOOD3)], 'c')])
    outcome = run(m, [src])
    fb = last_tool(m, 2)
    assert fb['status'] == 'invalid'
    assert [f['item'] for f in fb['failures']] == ['claims[1]']
    f = fb['failures'][0]
    assert f['index'] == 1 and f['source_id'] == 'p1' and f['kind'] == 'not_found_anywhere'
    assert f['quotation_start'] == BAD2[:80]
    assert f['diverges_at'] == BAD2.index(' USD')
    assert f['closest_source_text'].startswith('ETF:QQQ') and len(f['closest_source_text']) <= 300
    assert f['suggested_quotation'] == GOOD2
    assert 'nothing was committed' in fb['reason'] and '1 of 3' in fb['reason']
    assert outcome.status == 'completed' and len(outcome.claims) == 3


def test_curly_apostrophe_dash_and_nbsp_get_exact_source_span_suggestion():
    text = 'Intro. The company’s net margin rose – a lot “strongly” in 2025. End.'
    src = make_source('p1', text)
    typed = 'The company\'s net margin rose - a lot "strongly" in 2025.'
    m = Model([READ, submit([claim(typed)])])
    try:
        run(m, [src])
    except StopIteration:
        pass
    f = last_tool(m, 2)['failures'][0]
    assert f['suggested_quotation'] == 'The company’s net margin rose – a lot “strongly” in 2025.'
    assert f['suggested_quotation'] in text


def test_record_facts_reports_every_bad_fact_and_commits_nothing():
    src = make_source('p1', TEXT)
    def fact(quote, value, label):
        return {'label': label, 'value': value, 'unit': 'USD', 'period': '2025-12-31', 'source_id': 'p1', 'quotation': quote}
    m = Model([READ, turn('record_facts', {'rationale': 'r', 'facts': [
        fact(GOOD1, '688', 'spy'), fact('ETF:QQQ | unknown price', '614.31', 'qqq'), fact('ETF:IWM | nope', '245.10', 'iwm')]}, 'b'),
        submit([claim(GOOD1)], 'c')])
    outcome = run(m, [src])
    fb = last_tool(m, 2)
    assert fb['facts'] == []
    assert [f['item'] for f in fb['failures']] == ['facts[0]', 'facts[1]', 'facts[2]']
    assert fb['failures'][1]['kind'] == 'not_found_anywhere'
    assert 'suggested_quotation' not in fb['failures'][2]
    assert outcome.facts == []


def test_not_unique_reports_index_and_offsets():
    src = make_source('p1', 'Alpha 5 USD. Beta 6 USD. Alpha 5 USD.')
    m = Model([READ, submit([claim('Beta 6 USD'), claim('Alpha 5 USD')])])
    try:
        run(m, [src])
    except StopIteration:
        pass
    fb = last_tool(m, 2)
    assert [f['item'] for f in fb['failures']] == ['claims[1]']
    assert fb['failures'][0]['kind'] == 'not_unique' and fb['failures'][0]['offsets'] == [[0, 11], [25, 36]]


def test_not_in_read_ranges_reports_page_and_index():
    src = paged_source(['First page filler.', 'Second page has Net income of 55 million.'])
    m = Model([turn('read_pages', {'source_id': 'pdf', 'first_page': 1, 'last_page': 1, 'rationale': 'r'}, 'a'),
               submit([claim('First page filler.', 'pdf'), claim('Net income of 55 million', 'pdf')])])
    try:
        run(m, [src])
    except StopIteration:
        pass
    f = last_tool(m, 2)['failures'][0]
    assert f['item'] == 'claims[1]' and f['kind'] == 'not_in_read_ranges' and f['found_at'][0]['page'] == 2


def test_failures_are_capped_at_twenty():
    src = make_source('p1', 'Alpha 5 USD.')
    m = Model([READ, submit([claim(f'Missing {i}') for i in range(25)])])
    try:
        run(m, [src])
    except StopIteration:
        pass
    fb = last_tool(m, 2)
    assert len(fb['failures']) == 20 and '25 of 25' in fb['reason'] and '5 more' in fb['reason']
