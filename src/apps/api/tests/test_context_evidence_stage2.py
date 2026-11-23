from copy import deepcopy
import pytest
from app.research_calculations import CalculationError, CalculationRequest, calculate
from app.research_report_grounding import clause_problem
from app.research_partial_answer import merge_checkpoint_parts


def request(current='120', prior='100', contribution='18'):
    return CalculationRequest.model_validate({'formula': 'contribution_to_change_percent', 'facts': [
        {'fact_id': 'driver', 'value': contribution, 'unit': 'USD millions', 'currency': 'USD', 'period': 'FY2025'},
        {'fact_id': 'current', 'value': current, 'unit': 'USD millions', 'currency': 'USD', 'period': 'FY2025'},
        {'fact_id': 'prior', 'value': prior, 'unit': 'USD millions', 'currency': 'USD', 'period': 'FY2024'}]})


def test_contribution_to_change_uses_ordered_three_facts_and_zero_denominator_fails():
    result = calculate(request())
    assert result['value'] == '90.00'
    assert result['expression'] == 'contribution / (current - prior) * 100'
    assert [x['fact_id'] for x in result['inputs']] == ['driver', 'current', 'prior']
    assert calculate(request(current='80', contribution='-10'))['value'] == '50.00'
    with pytest.raises(CalculationError, match='undefined'):
        calculate(request(current='100'))
    mismatch = request().model_dump()
    mismatch['facts'][2]['currency'] = 'EUR'
    with pytest.raises(CalculationError):
        calculate(CalculationRequest.model_validate(mismatch))


def test_period_revenue_does_not_establish_contribution_to_change():
    claim = 'Acquisition contributed 18 million to the increase in revenue.'
    assert clause_problem(claim, ['Acquisition contributed 18 million of current revenue.'], [], [])
    assert clause_problem(claim, ['Acquisition contributed 18 million of FY2025 revenue. Revenue growth from FY2024 to FY2025 was strong.'], [], [])
    assert clause_problem(claim, ['Acquisition contributed 18 million of FY2025 revenue and consolidated revenue growth was strong.'], [], [])
    assert clause_problem(claim, ['Acquisition contributed 18 million to the increase in revenue.'], [], []) is None


def test_contribution_subject_is_local_to_disclosure():
    from app.research_report_grounding import contribution_subject_disclosed
    assert contribution_subject_disclosed('Northport acquisition note. The acquired business contributed 18 million of the increase.', 'Northport')
    assert not contribution_subject_disclosed('Northport financial statements. Otherissuer contributed 18 million of the increase.', 'Northport')


def test_joint_market_requirement_survives_question_split():
    q = 'Compare Gamma and Benchmark adjusted returns.'
    assessed = [{'id': 'r', 'question': q, 'core': True, 'status': 'unresolved', 'answer': '',
                 'requirements': [{'id': 'r', 'core': True, 'status': 'missing'}]}]
    assert any(p.get('id') == 'r' for p in merge_checkpoint_parts(q, {}, set(), deepcopy(assessed)))


def test_statement_rows_return_readable_header_context_and_note_limit():
    from tests.test_research_statement_extraction import src, run, CN_INCOME
    source, pages = src(['Cover', CN_INCOME])
    result, read_ranges = run(source, pages, statements=['income'])
    statement = result['statements'][0]
    for context in statement['context_ranges']:
        assert any(r['start'] <= context['character_start'] < context['character_end'] <= r['end'] for r in read_ranges)
        assert source.text[context['character_start']:context['character_end']]
    assert statement['columns'] and statement['unit']
    note_rows = [row for row in statement['rows'] if row.get('note_ref')]
    assert note_rows and all('referenced_note_not_bound' in row['warnings'] for row in note_rows)


def test_overlapping_subwindow_is_not_presented_as_requested_endpoints():
    from app.research_market_analytics import AnalyzeMarketSeriesRequest, analyze_market_series
    from app.research_evidence_requirements import assess
    from tests.test_research_market_analytics import _market_source
    def bars(symbol, dates):
        return '\n'.join(f'{symbol}@X | {day} | 1d | O {v} H {v} L {v} C {v} V 100 USD ADJF 1' for day, v in dates)
    a = _market_source('a', bars('AAA', [('2026-02-02',10),('2026-02-03',11),('2026-02-04',12)]))
    b = _market_source('b', bars('BBB', [('2026-02-03',100),('2026-02-04',110),('2026-02-05',120)]))
    sources={'a':a,'b':b}
    req=AnalyzeMarketSeriesRequest(source_id='a',benchmark_source_id='b',price_basis='adjusted')
    output=analyze_market_series(req,sources=sources)
    assert (output['benchmark']['aligned_start_date'], output['benchmark']['aligned_end_date']) == ('2026-02-03','2026-02-04')
    state={'market_analyses':[{'analysis_id':'market','request':req.model_dump(mode='json'),'result':output}]}
    rows=assess('Compare AAA and BBB benchmark adjusted returns from 2026-02-02 to 2026-02-05.',state,sources)
    assert next(r for r in rows if r['kind']=='market:benchmark')['status']=='missing'
    rows=assess('Compare AAA and BBB benchmark adjusted returns from 2026-02-03 to 2026-02-04.',state,sources)
    assert next(r for r in rows if r['kind']=='market:benchmark')['status']=='satisfied'


def test_cross_page_headers_are_located_conflicts_are_not_bound_and_note_is_read_separately():
    from hashlib import sha256
    from app.research_tool_executor import _read_pages
    from app.research_tools_registry import ReadPagesRequest
    from tests.test_research_runtime_attention import _source
    from tests.test_research_statement_extraction import src, run, CN_BS1, CN_BS2
    note = '五、1 货币资金：本期现金及银行存款。'
    synthetic, pages = src([CN_BS1, CN_BS2, note])
    result, ranges = run(synthetic, pages, statements=['balance'])
    statement = result['statements'][0]
    assert statement['pages'] == [1, 2]
    assert [c['period_end'] for c in statement['columns']] == ['2025-12-31', '2024-12-31']
    assert [c['page'] for c in statement['context_ranges']] == [1, 2]
    note_row = next(r for r in statement['rows'] if r.get('note_ref') == '五、1')
    assert 'referenced_note_not_bound' in note_row['warnings']
    assert not any(r['start'] <= pages['pages'][2][0] < r['end'] for r in ranges)
    source = _source('s1', synthetic.text, method='user_pdf')
    source.provenance = {'document_pages': pages}
    source.content_sha256 = sha256(source.text.encode()).hexdigest()
    out, _ = _read_pages(ReadPagesRequest(source_id='s1', first_page=3, last_page=3, rationale='Read the cited note'), source_by_id={'s1':source}, as_of=None, read_ranges=ranges)
    assert note in str(out)
    assert any(r['start'] <= pages['pages'][2][0] < r['end'] for r in ranges)
    reversed_header = CN_BS2.replace('期末余额             上年年末余额', '上年年末余额             期末余额')
    conflict, dp = src([CN_BS1, reversed_header, note])
    changed, _ = run(conflict, dp, statements=['balance'])
    table = changed['statements'][0]
    assert 'continuation_column_header_conflict' in table['warnings']
    row = next(r for r in table['rows'] if r.get('standard_line') == 'total_liabilities_and_equity')
    assert row['page'] == 2 and row['value'] == [None, None]
    assert conflict.text[row['character_start']:row['character_end']] == row['quotation']


def test_manual_calculation_api_rejects_attribution_formula(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as api
    monkeypatch.setattr(api, '_authenticated_account', lambda _: object())
    payload = request().model_dump(mode='json')
    response = TestClient(api.app).post('/v2/research/calculations', json=payload, headers={'Authorization':'Bearer test'})
    assert response.status_code == 422
    public = api.research_openapi()['components']['schemas']['PublicCalculationRequest']['properties']['formula']['enum']
    assert 'contribution_to_change_percent' not in public


def test_manual_recalculate_api_rejects_attribution_formula(monkeypatch):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    import app.main as api
    monkeypatch.setattr(api, '_authenticated_account', lambda _: SimpleNamespace(account_id='test'))
    old = SimpleNamespace(formula_id='contribution_to_change_percent')
    version = SimpleNamespace(version_id='parent', content=SimpleNamespace(calculations=[old]))
    monkeypatch.setattr(api.research_topics, 'get', lambda *_: SimpleNamespace(versions=[version]))
    response=TestClient(api.app).post('/v2/research/topics/topic/calculations/recalculate', json={'parent_version_id':'parent','calculation_index':0}, headers={'Authorization':'Bearer test'})
    assert response.status_code == 422
