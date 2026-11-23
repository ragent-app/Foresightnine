"""Current tool-loop coverage for source-tag consistency; no remote calls."""
import json

import pytest

from app.model_protocol import ModelResponse
from app.research_calculations import calculation_input_status, xbrl_source_consistent
from app.research_runtime import run_research_agent
from tests.test_research_runtime import Model, context, turn
from tests.test_research_table_provenance import source_for, xbrl_fixture


def recorded(html, *, label='Total revenue', currency='USD', row_only=False, cell_only=False):
    source = source_for(html)
    source.permissions.model_input = True
    row = next(line for line in source.text.splitlines() if '100' in line)
    facts = [{'label': label + ' FY' + year, 'value': value, 'unit': currency + ' million',
              'currency': currency, 'period': 'FY' + year + ' (year ended ' + date + ')',
              'source_id': 's', 'quotation': value if cell_only else row}
             for value, year, date in [('100', '2024', 'July 27, 2024'), ('90', '2023', 'July 29, 2023')]]
    read = {'source_id': 's'}
    if row_only:
        read.update(start=source.text.index(row), end=source.text.index(row) + len(row))
    stop = ModelResponse({'role': 'assistant', 'content': 'Done'}, (), None, 'stop')
    model = Model([turn('read_sources', {'reads': [read], 'rationale': 'read'}, 'a'),
                   turn('record_facts', {'facts': facts, 'rationale': 'record'}, 'b'), stop])
    run_research_agent(question='Compare revenue', sources=[source], model=model, context=context())
    feedback = json.loads([m for m in model.inputs[-1] if m.get('role') == 'tool'][-1]['content'])
    assert not feedback.get('failures'), feedback
    return feedback['facts']


def status(facts):
    return calculation_input_status({'formula_id': 'growth_percent', 'inputs': [
        {k: item[k] for k in ('fact_id', 'value', 'unit', 'currency', 'period')} for item in facts]}, facts)


def test_current_record_facts_checks_xbrl_and_keeps_source_truth_unverified():
    facts = recorded(xbrl_fixture())
    assert all(xbrl_source_consistent(f) for f in facts)
    assert all('not_verified' in f['verification'] for f in facts)
    assert status(facts) == 'matched'
    changed = {**facts[0], 'value': '101'}
    assert not xbrl_source_consistent(changed)


@pytest.mark.parametrize('change', [
    lambda s: s.replace('http://www.xbrl.org/2013/inlineXBRL', 'https://example.test/fake'),
    lambda s: s.replace('iso4217:USD', 'iso4217:CAD'),
    lambda s: s.replace('decimals="-6"', 'decimals="-3"'),
    lambda s: s.replace('</xbrli:entity><xbrli:period>', '<xbrli:segment>dimension</xbrli:segment></xbrli:entity><xbrli:period>', 1),
    lambda s: s.replace('</ix:header>', '<xbrli:context id="c1"></xbrli:context></ix:header>'),
    lambda s: s.replace('<xbrli:endDate>2024-07-27', '<xbrli:endDate>2024-07-26'),
    lambda s: s.replace('us-gaap:Revenues', 'us-gaap:OperatingIncomeLoss'),
    lambda s: s.replace('0000858877</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>2022',
                        '0000000001</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>2022'),
    lambda s: s.replace('2023-07-30</xbrli:startDate>', '2023-07-24</xbrli:startDate>'),
])
def test_current_record_facts_fails_closed_on_xbrl_conflicts(change):
    assert status(recorded(change(xbrl_fixture()))) == 'unverifiable'


def test_current_record_facts_requires_read_headers():
    assert status(recorded(xbrl_fixture(), row_only=True)) == 'unverifiable'


def test_single_cell_quote_uses_read_original_table_and_missing_tags_fail_closed():
    assert status(recorded(xbrl_fixture(), cell_only=True)) == 'matched'
    html = xbrl_fixture().replace('ix:nonFraction', 'span')
    assert status(recorded(html)) == 'unverifiable'


def test_current_record_facts_supports_ifrs_and_rejects_wrong_taxonomy():
    html = (xbrl_fixture().replace('xmlns:us-gaap="http://fasb.org/us-gaap/2024"',
        'xmlns:ifrs-full="https://xbrl.ifrs.org/taxonomy/2024-03-27/ifrs-full"')
        .replace('us-gaap:Revenues', 'ifrs-full:OperatingExpenses')
        .replace('iso4217:USD', 'iso4217:EUR').replace('Total revenue', 'Operating expenses'))
    assert status(recorded(html, label='Operating expenses', currency='EUR')) == 'matched'
    assert status(recorded(html.replace('https://xbrl.ifrs.org/taxonomy/', 'https://example.test/taxonomy/'),
                           label='Operating expenses', currency='EUR')) == 'unverifiable'
