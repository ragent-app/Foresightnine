import json
import re

import pytest
from pydantic import ValidationError

from app.research_methods import (
    KNOWN_TOOLS, MAX_PACK_CHARS, PACKS_DIR, Pack, get_pack, list_available_packs, load_packs,
    pack_fingerprint, packs_fingerprint, render_for_prompt, select_packs,
)

EXPECTED = {'earnings_review', 'company_comparison', 'ipo_or_major_event_day', 'market_trend', 'macro_release'}
ALL_CAPS = {'market_data_available': True, 'sec_financials_available': True, 'filings_available': True,
            'search_available': True, 'acquire_available': True}
NO_MARKET = {**ALL_CAPS, 'market_data_available': False, 'market_data_key_required': True}
NOTHING = {'market_data_available': False, 'market_data_key_required': True, 'sec_financials_available': False,
           'filings_available': False, 'search_available': False, 'acquire_available': False}

Q_TREND = '分析纳斯达克最近走势，并分析预测未来的可能'
Q_IPO = '收集今天anthropic IPO第一天交易的相关信息'


def test_all_packs_load_and_validate():
    packs = load_packs()
    assert set(packs) == EXPECTED
    for pack in packs.values():
        assert pack.counter_evidence.min_search_actions >= 1
        assert any(r.required for r in pack.evidence_requirements)
        assert all(t in KNOWN_TOOLS for r in pack.evidence_requirements for t in r.preferred_tools)
        assert re.fullmatch(r'[a-z_]+@\d+\.\d+\.\d+\+[0-9a-f]{12}', pack_fingerprint(pack))
    assert len(packs_fingerprint()) == 12


def test_schema_rejects_bad_pack():
    data = json.loads((PACKS_DIR / 'market_trend.json').read_text(encoding='utf-8'))
    bad = json.loads(json.dumps(data))
    bad['evidence_requirements'][1]['preferred_tools'] = ['not_a_tool']
    with pytest.raises(ValidationError):
        Pack.model_validate(bad)
    bad = json.loads(json.dumps(data))
    bad['counter_evidence']['min_search_actions'] = 0
    with pytest.raises(ValidationError):
        Pack.model_validate(bad)
    bad = json.loads(json.dumps(data))
    bad['report_structure']['must_state'] = ['something else']
    with pytest.raises(ValidationError):
        Pack.model_validate(bad)
    bad = json.loads(json.dumps(data))
    bad['surprise'] = 1
    with pytest.raises(ValidationError):
        Pack.model_validate(bad)


def test_ipo_and_trend_require_benchmark_and_series():
    ipo = load_packs()['ipo_or_major_event_day']
    bench = next(r for r in ipo.evidence_requirements if r.id == 'benchmark')
    assert bench.required and bench.preferred_tools == ['market_data']
    assert any(r.metric.startswith('open') for r in ipo.reconciliation)


def test_selector_the_two_365_questions():
    assert [r.pack_id for r in select_packs(Q_TREND)] == ['market_trend']
    assert [r.pack_id for r in select_packs(Q_IPO)] == ['ipo_or_major_event_day']


def test_selector_revenue_with_quarter_and_stock_price_question():
    q = '微软最近两个季度的营收和股价表现'
    assert {r.pack_id for r in select_packs(q)} == {'earnings_review', 'market_trend'}
    assert len(select_packs(q)) <= 2 and len(select_packs(q, max_packs=1)) == 1
    assert {r.pack_id for r in select_packs('How did sales and share price performance look over the last two quarters?')} == {
        'earnings_review', 'market_trend'}
    assert 'earnings_review' in {r.pack_id for r in select_packs('Apple revenue in the last quarter')}
    # a single weak word is not enough
    assert select_packs('last quarter') == []
    assert select_packs('季度') == []
    assert select_packs('收入') == []


@pytest.mark.parametrize('question,expected', [
    ('Review the latest quarterly earnings and the 10-Q', 'earnings_review'),
    ('这家公司最新财报怎么样', 'earnings_review'),
    ('Compare two companies on margins', 'company_comparison'),
    ('A 与 B 对比一下盈利能力', 'company_comparison'),
    ('What happened on the IPO debut, first day of trading?', 'ipo_or_major_event_day'),
    ('上市首日表现如何', 'ipo_or_major_event_day'),
    ('How did the index trend this week?', 'market_trend'),
    ('最近大盘行情怎么看', 'market_trend'),
    ('Interpret the latest CPI release', 'macro_release'),
    ('非农数据公布后怎么解读', 'macro_release'),
    ('央行利率决议的影响', 'macro_release'),
])
def test_selector_top_choice(question, expected):
    assert select_packs(question)[0].pack_id == expected


def test_selector_conservative_and_deterministic():
    for q in ['', 'Tell me a joke about cats', '今天天气怎么样', 'recipe for pasta', 'Explain how transistors work']:
        assert select_packs(q) == []
    assert select_packs('compare earnings of two firms') == select_packs('compare earnings of two firms')
    assert len(select_packs('compare earnings of two firms', max_packs=1)) == 1
    assert {r.pack_id for r in select_packs('compare earnings of two firms')} == {'earnings_review', 'company_comparison'}
    assert select_packs(Q_IPO, max_packs=0) == []
    # single weak trigger is not enough; ASCII terms respect word boundaries
    assert select_packs('revenue') == []
    assert select_packs('ipoh scripture') == []
    assert select_packs(Q_IPO)[0].matched_terms == sorted(select_packs(Q_IPO)[0].matched_terms)


@pytest.mark.parametrize('caps', [ALL_CAPS, NO_MARKET, NOTHING, {}])
def test_render_deterministic_and_bounded(caps):
    for pack in load_packs().values():
        a = render_for_prompt(pack, available_capabilities=caps)
        assert a == render_for_prompt(pack, available_capabilities=dict(reversed(list(caps.items()))))
        assert len(a) <= MAX_PACK_CHARS, (pack.id, len(a))
        assert '[truncated]' not in a
        assert pack_fingerprint(pack) in a
        assert not re.search(r'20\d\d-\d\d-\d\d', a)  # no timestamps or dates


def test_render_marks_unavailable_market_data_and_fallback():
    pack = load_packs()['ipo_or_major_event_day']
    text = render_for_prompt(pack, available_capabilities=NO_MARKET)
    assert 'market_data[n/a]' in text
    assert "user's own market-data key" in text
    assert 'NOT market data' in text
    assert 'Without market_data' in text or 'FALLBACK' in text
    ok = render_for_prompt(pack, available_capabilities=ALL_CAPS)
    assert 'market_data[n/a]' not in ok and 'Unavailable now' in ok
    # optional tools missing from the capability dict are shown as unavailable
    assert 'news_search[n/a]' in ok


def test_render_without_key_flag_says_unavailable_in_run():
    pack = load_packs()['market_trend']
    text = render_for_prompt(pack, available_capabilities={'market_data_available': False})
    assert 'market_data is unavailable in this run' in text and 'NOT market data' in text


def test_optional_tools_used_when_available():
    pack = load_packs()['market_trend']
    text = render_for_prompt(pack, available_capabilities={**ALL_CAPS, 'search_library_available': True, 'news_search_available': True})
    assert 'search_library[n/a]' not in text and 'news_search[n/a]' not in text


def test_packs_have_no_sample_specific_strings():
    banned = ['NVDA', 'NVIDIA', 'ANT', 'Anthropic', 'QQQ', 'SPY', 'ONEQ', 'AAPL', 'MSFT', 'TSLA', 'Nasdaq', '纳斯达克', 'OpenAI']
    for path in PACKS_DIR.glob('*.json'):
        raw = path.read_text(encoding='utf-8')
        for word in banned:
            pattern = rf'(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])' if word.isascii() else re.escape(word)
            assert not re.search(pattern, raw, flags=re.I if word.isascii() and len(word) > 4 else 0), (path.name, word)
        assert not re.search(r'20\d\d', raw), path.name  # no concrete years/dates


def test_list_available_packs_and_get_pack():
    listed = list_available_packs()
    assert [item['pack_id'] for item in listed] == sorted(EXPECTED)
    for item in listed:
        assert item['fingerprint'] == pack_fingerprint(get_pack(item['pack_id']))
        assert item['title_en'] and item['title_zh'] and item['question_types']
    pack = get_pack(' earnings_review ')
    assert pack is not None and pack.id == 'earnings_review'
    assert get_pack('nonexistent_pack') is None
    assert get_pack('') is None
