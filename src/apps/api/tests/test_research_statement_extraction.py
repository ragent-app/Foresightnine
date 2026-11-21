"""Deterministic statement extraction from report text (synthetic fixtures; no real report text)."""
from decimal import Decimal
import json
from types import SimpleNamespace

from app.research_statement_extraction import ExtractStatementsRequest, extract_statements, match_standard_line

CN_INCOME = """\
                        合并利润表
                    2025年度
编制单位：示例股份有限公司                                         单位：元
项目                          附注       本期金额             上期金额
一、营业总收入                           12,345,678,901.23    11,222,333,444.55
其中：营业收入                  五、1    12,345,678,901.23    11,222,333,444.55
二、营业总成本                           10,000,000,000.00     9,500,000,000.00
其中：营业成本                  五、1     8,000,000,000.00     7,600,000,000.00
研发费用                                    500,000,000.00       450,000,000.00
三、营业利润（亏损以“－”号填列）          2,345,678,901.23     1,722,333,444.55
减：所得税费用                              (123,456.00)                   -
四、净利润（净亏损以“－”号填列）          1,900,000,000.00     1,500,000,000.00
归属于母公司所有者的净利润                1,800,000,000.00     1,400,000,000.00
五、每股收益：
（一）基本每股收益                                   0.90                 0.70
（二）稀释每股收益                                   0.89                 0.69
六、综合收益总额                         1,950,000,000.00     1,520,000,000.00
"""
CN_PARENT_INCOME = """\
                        母公司利润表
                    2025年度
                                                                单位：元
项目                          附注       本期金额             上期金额
一、营业收入                              1,000,000.00           900,000.00
减：营业成本                                500,000.00           450,000.00
营业利润                                    300,000.00           200,000.00
净利润                                      250,000.00           150,000.00
六、综合收益总额                            250,000.00           150,000.00
"""
CN_BS1 = """\
                        合并资产负债表
                       2025年12月31日
                                                                单位：元
项目                          附注       期末余额             上年年末余额
流动资产：
货币资金                       五、1      1,000,000,000.00      900,000,000.00
应收账款                       五、2        200,000,000.00      180,000,000.00
存货                                       300,000,000.00      250,000,000.00
流动资产合计                             1,500,000,000.00    1,330,000,000.00
资产总计                                 5,000,000,000.00    4,500,000,000.00
"""
CN_BS2 = """\
                        合并资产负债表（续）
                                                                单位：元
项目                          附注       期末余额             上年年末余额
流动负债合计                             1,200,000,000.00    1,100,000,000.00
负债合计                                 2,000,000,000.00    1,900,000,000.00
归属于母公司所有者权益合计               2,900,000,000.00    2,500,000,000.00
少数股东权益                               100,000,000.00      100,000,000.00
所有者权益合计                           3,000,000,000.00    2,600,000,000.00
负债和所有者权益总计                     5,000,000,000.00    4,500,000,000.00
"""
CN_CF = """\
                        合并现金流量表
                    2025年度                            单位：元
项目                          附注       本期金额             上期金额
经营活动产生的现金流量净额                 800,000,000.00      700,000,000.00
购建固定资产、无形资产和其他长期
  资产支付的现金                          (300,000,000.00)    (250,000,000.00)
投资活动产生的现金流量净额              (500,000,000.00)    (400,000,000.00)
筹资活动产生的现金流量净额                 100,000,000.00       50,000,000.00
现金及现金等价物净增加额                   100,000,000.00      100,000,000.00
期末现金及现金等价物余额                 1,000,000,000.00      900,000,000.00
"""
HK_INCOME = """\
CONSOLIDATED STATEMENT OF PROFIT OR LOSS
For the year ended 31 December 2025

                                      Note        2025          2024
                                                RMB'000       RMB'000

Revenue                                 5     1,234,567     1,100,000
Cost of sales                                  (800,000)     (700,000)
Gross profit                                    434,567       400,000
Profit before income tax                        200,000       150,000
Income tax expense                    8         (50,000)      (40,000)
Profit for the year                             150,000       110,000
Profit for the year attributable to:
  Owners of the Company                         140,000       100,000
  Non-controlling interests                      10,000        10,000
Earnings per share (RMB)
  Basic                                 9          0.52          0.40
  Diluted                               9          0.51          0.39
"""


def src(pages, status=None, ocr=None, text_prefix=''):
    text, entries, off = text_prefix, [], len(text_prefix)
    for i, p in enumerate(pages):
        st = (status or {}).get(i + 1, 'text')
        if p is None or st == 'needs_ocr':
            entries.append([off, off, 'needs_ocr'])
            p = ''
        else:
            entries.append([off, off + len(p), st])
        text += p + '\f'
        off += len(p) + 1
    dp = {'page_count': len(pages), 'pages': entries, 'method': 'pdftotext_layout_paged', 'version': 2}
    if ocr:
        dp['ocr'] = ocr
    return SimpleNamespace(source_id='s1', text=text, provenance={'document_pages': dp}), dp


def run(source, dp, budget=40_000, **req):
    req.setdefault('rationale', 'r')
    ranges = []
    res, _ = extract_statements(ExtractStatementsRequest(source_id='s1', **req), source, document_pages=dp,
                                read_ranges=ranges, read_budget_chars=budget)
    return res, ranges


def row(res, st, line, primary=True):
    s = next(x for x in res['statements'] if x['statement'] == st)
    return next(r for r in s['rows'] if r['standard_line'] == line)


def test_a_share_income_statement():
    s, dp = src(['示例股份有限公司 2025年年度报告\n目录\n合并利润表 ........ 12', CN_PARENT_INCOME, CN_INCOME])
    res, ranges = run(s, dp, statements=['income'])
    st = res['statements'][0]
    assert st['scope'] == 'consolidated' and st['pages'] == [3]
    assert st['alternatives'][0]['scope'] == 'parent_company'
    assert st['unit']['scale'] == 1 and st['unit']['basis'] == 'stated' and st['unit']['currency'] is None
    assert [c['period_end'] for c in st['columns']] == ['2025-12-31', '2024-12-31']
    rev = row(res, 'income', 'revenue')
    assert rev['value'] == ['12345678901.23', '11222333444.55'] and rev['primary'] is True
    dup = [r for r in st['rows'] if r['standard_line'] == 'revenue']
    assert len(dup) == 2 and dup[1]['note_ref'] == '五、1' and 'duplicate_standard_line' in dup[1]['warnings']
    tax = row(res, 'income', 'income_tax')
    assert tax['value_as_stated'] == ['-123456.00', None] and tax['nil_columns'] == [1]
    assert row(res, 'income', 'operating_profit')['value'][0] == '2345678901.23'
    assert row(res, 'income', 'net_profit_attributable_parent')['value'][1] == '1400000000'
    eps = row(res, 'income', 'eps_basic')
    assert eps['value'] == ['0.9', '0.7'] and eps['per_share'] is True
    for r in st['rows']:
        if 'quotation' in r:
            assert s.text[r['character_start']:r['character_end']] == r['quotation']
    assert {'source_id': 's1', 'start': dp['pages'][2][0], 'end': dp['pages'][2][1]} in ranges


def test_parent_only_is_returned_with_warning():
    s, dp = src(['前言', CN_PARENT_INCOME])
    res, _ = run(s, dp, statements=['income'])
    st = res['statements'][0]
    assert st['scope'] == 'parent_company' and 'only_parent_company_statement_found' in st['warnings']


def test_balance_sheet_spans_two_pages_and_cash_flow_multiline_label():
    s, dp = src(['x', CN_BS1, CN_BS2, CN_CF], text_prefix='')
    res, ranges = run(s, dp, statements=['balance', 'cash_flow'])
    bs = next(x for x in res['statements'] if x['statement'] == 'balance')
    assert bs['pages'] == [2, 3] and bs['terminal_reached'] is True
    assert row(res, 'balance', 'total_assets')['value'][1] == '4500000000'
    assert row(res, 'balance', 'total_liabilities_and_equity')['page'] == 3
    assert [c['period_end'] for c in bs['columns']] == ['2025-12-31', '2024-12-31']
    capex = row(res, 'cash_flow', 'capex')
    assert capex['value_as_stated'][0] == '-300000000.00' and 'label_spans_lines' in capex['warnings']
    assert '资产支付的现金' in capex['quotation']
    assert len(ranges) == 1  # pages 2-4 merged into one contiguous range


def test_hk_english_statement_units_and_context_rows():
    s, dp = src([HK_INCOME])
    res, _ = run(s, dp, statements=['income'])
    st = res['statements'][0]
    assert st['unit']['scale'] == 1000 and st['unit']['currency'] == 'CNY'
    assert [c['period_end'] for c in st['columns']] == ['2025-12-31', '2024-12-31']
    assert row(res, 'income', 'revenue')['value'] == ['1234567000', '1100000000']
    assert row(res, 'income', 'revenue')['note_ref'] == '5'
    assert row(res, 'income', 'net_profit_attributable_parent')['value_as_stated'] == ['140000', '100000']
    assert row(res, 'income', 'net_profit_minority')['value'][0] == '10000000'
    assert row(res, 'income', 'eps_basic')['value'] == ['0.52', '0.4']


def test_lines_filter_and_unknown_lines():
    s, dp = src([HK_INCOME])
    res, _ = run(s, dp, lines=['revenue', 'total_assets', 'bogus'])
    assert [r['standard_line'] for r in res['statements'][0]['rows']] == ['revenue']
    assert res['unknown_lines'] == ['bogus'] and res['lines_not_found'] == ['total_assets']
    assert [x['statement'] for x in res['statements']] == ['income']


def test_ocr_page_low_confidence_flagged():
    s, dp = src([CN_INCOME], status={1: 'ocr'}, ocr={'1': {'engine': 'tess', 'mean_confidence': 55.0, 'low_confidence_count': 3}})
    res, _ = run(s, dp, statements=['income'])
    r = row(res, 'income', 'revenue')
    assert r['extraction_method'] == 'ocr' and r['ocr_needs_check'] is True and 'ocr_low_confidence' in r['warnings']


def test_needs_ocr_pages_suggest_request_ocr():
    s, dp = src(['前言', None, None])
    res, _ = run(s, dp)
    assert res['status'] == 'needs_ocr' and res['suggested_actions'][0]['tool'] == 'request_ocr'
    assert res['suggested_actions'][0]['arguments']['pages'] == [2, 3]
    s, dp = src([CN_BS1, None])
    res, _ = run(s, dp, statements=['balance'])
    assert res['needs_ocr_pages'] == [2] and res['status'] == 'partial'


def test_not_found_and_read_limit_partial():
    s, dp = src(['nothing here'])
    res, _ = run(s, dp)
    assert res['status'] == 'unavailable' and res['reason'] == 'statements_not_found'
    s, dp = src([CN_INCOME, CN_BS1, CN_BS2, CN_CF])
    res, ranges = run(s, dp, budget=len(CN_INCOME) + 10)
    assert res['truncated_by_limit'] and res['status'] == 'partial'
    assert [x['statement'] for x in res['statements']] == ['income']
    assert res['next_pages'] == [2, 3, 4]
    assert len(ranges) == 1


def test_standard_line_dictionary():
    assert match_standard_line('cash_flow', 'Net cash (used in)/generated from operating activities').key == 'operating_cash_flow'
    assert match_standard_line('income', '归属于母公司股东的净利润').key == 'net_profit_attributable_parent'
    assert match_standard_line('balance', 'Total equity and liabilities').key == 'total_liabilities_and_equity'
    assert match_standard_line('income', 'Purchase of property, plant and equipment') is None
    assert match_standard_line('cash_flow', 'Purchase of property, plant and equipment').key == 'capex'


# ---- runtime integration ------------------------------------------------------------------------------------
from tests.test_research_runtime import Model, turn  # noqa: E402
from tests.test_research_runtime_s0b import STOP, paged_source, run as run_agent  # noqa: E402
from app.research_runtime import SYSTEM, TOOLS  # noqa: E402


def test_tool_registered_before_submit_and_described():
    names = [t.name for t in TOOLS]
    assert names.index('extract_statements') == len(names) - 2 and names[-1] == 'submit_research'
    schema = next(t for t in TOOLS if t.name == 'extract_statements').input_schema
    assert set(schema['required']) == {'source_id', 'rationale'} and 'statements' in schema['properties']
    assert 'extract_statements' in SYSTEM


def test_runtime_extract_then_record_facts_with_returned_quotation():
    source = paged_source(['示例股份有限公司 2025年年度报告', CN_INCOME])
    m = Model([turn('extract_statements', {'source_id': 'pdf', 'statements': ['income'], 'rationale': 'r'}, 'a'), STOP])
    run_agent(m, [source])
    fb = json.loads(m.inputs[1][-1]['content'])
    assert fb['status'] == 'completed'
    rev = next(r for r in fb['statements'][0]['rows'] if r['standard_line'] == 'revenue')
    fact = {'label': '营业总收入', 'value': rev['value_as_stated'][0], 'unit': 'CNY', 'currency': 'CNY', 'period': 'FY2025',
            'source_id': 'pdf', 'quotation': rev['quotation'], 'character_start': rev['character_start'],
            'character_end': rev['character_end']}
    m2 = Model([turn('extract_statements', {'source_id': 'pdf', 'statements': ['income'], 'rationale': 'r'}, 'a'),
                turn('record_facts', {'facts': [fact], 'rationale': 'r'}, 'b'), STOP])
    run_agent(m2, [source])
    rec = json.loads(m2.inputs[2][-1]['content'])
    assert rec['facts'][0]['character_start'] == rev['character_start'] and 'fact_id' in rec['facts'][0]
