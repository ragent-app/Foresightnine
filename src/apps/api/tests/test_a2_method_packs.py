"""Tests for Phase A2 L1 + L2 industry KPI method packs, SIC matching, and composition."""
from __future__ import annotations

import json
import re

import pytest
from pydantic import ValidationError

from app.research_methods import (
    INDUSTRY_PACK_IDS,
    KNOWN_TOOLS,
    MAX_COMPOSED_PACK_CHARS,
    MAX_INDUSTRY_PACK_CHARS,
    MAX_PACK_CHARS,
    PACKS_DIR,
    SIC_INDUSTRY_RANGES,
    ComposedEarningsMethodPack,
    IndustryKpiPack,
    classify_sic_sector,
    compose_earnings_method_pack,
    get_industry_pack,
    get_pack,
    industry_pack_fingerprint,
    list_industry_packs,
    load_industry_packs,
    load_packs,
    match_industry_pack_by_sic,
    pack_fingerprint,
    render_for_prompt,
    render_industry_pack_for_prompt,
    tool_available_in_run,
)

A1_TOOLS = {
    'sec_earnings_release',
    'sec_filing_tables',
    'sec_filing_sections',
    'sec_redline_diff',
    'sec_fulltext_search',
    'earnings_consensus',
    'earnings_transcript',
    'earnings_quality_diagnostics',
    'guidance_and_nongaap_reconciler',
    'valuation_and_implied_expectations',
}

ALL_CAPS = {
    'market_data_available': True,
    'sec_financials_available': True,
    'filings_available': True,
    'search_available': True,
    'acquire_available': True,
}
FULL_CAPS = {
    **ALL_CAPS,
    'earnings_consensus_available': True,
    'earnings_transcript_available': True,
    'search_library_available': True,
    'news_search_available': True,
}
NO_MARKET = {**ALL_CAPS, 'market_data_available': False, 'market_data_key_required': True}
NOTHING = {
    'market_data_available': False,
    'market_data_key_required': True,
    'sec_financials_available': False,
    'filings_available': False,
    'search_available': False,
    'acquire_available': False,
}


def test_earnings_review_v2_references_all_a1_tools_blocks_and_bear_rules():
    pack = get_pack('earnings_review')
    assert pack is not None
    assert pack.version == '2.0.0'

    referenced_tools = {t for req in pack.evidence_requirements for t in req.preferred_tools}
    assert A1_TOOLS <= referenced_tools
    assert A1_TOOLS <= KNOWN_TOOLS

    # 6 required buy-side report blocks
    sections = pack.report_structure.sections
    assert len(sections) == 6
    joined_sections = ' | '.join(sections).lower()
    for keyword in ('summary', 'earnings quality', 'segments', 'redline', 'valuation', 'bear case'):
        assert keyword in joined_sections

    # Evidence-anchored bear-case rules (no generic macro platitudes; anchor + next-Q trigger)
    bear_text = ' '.join([
        *pack.counter_evidence.look_for,
        *pack.report_structure.must_state,
        *pack.limits,
    ]).lower()
    assert 'no generic macro' in bear_text
    assert 'redline' in bear_text
    assert 'reverse-dcf' in bear_text
    assert 'next-q' in bear_text or 'next-quarter' in bear_text
    assert 'r/y' in bear_text or 'flag' in bear_text


@pytest.mark.parametrize('caps', [ALL_CAPS, FULL_CAPS, NO_MARKET, NOTHING, {}])
def test_earnings_review_v2_renders_within_budget_and_marks_fmp_capabilities(caps):
    pack = get_pack('earnings_review')
    assert pack is not None
    rendered = render_for_prompt(pack, available_capabilities=caps)
    assert rendered == render_for_prompt(pack, available_capabilities=dict(reversed(list(caps.items()))))
    assert len(rendered) <= MAX_PACK_CHARS
    assert '[truncated]' not in rendered
    assert pack_fingerprint(pack) in rendered

    if caps.get('earnings_consensus_available'):
        assert 'earnings_consensus[n/a]' not in rendered
    else:
        assert 'earnings_consensus[n/a]' in rendered
    if caps.get('earnings_transcript_available'):
        assert 'earnings_transcript[n/a]' not in rendered
    else:
        assert 'earnings_transcript[n/a]' in rendered


def test_l2_industry_packs_load_validate_and_match_expected_kpis():
    l1_packs = load_packs()
    assert not any(pid.startswith('kpi_') for pid in l1_packs)

    packs = load_industry_packs()
    assert set(packs) == set(INDUSTRY_PACK_IDS)
    assert len(packs) == 5

    expected_kpis = {
        'kpi_saas_cloud': {'rpo_crpo', 'deferred_revenue_billings', 'nrr_expansion', 'rule_of_40_fcf_ex_sbc'},
        'kpi_semis_hardware': {'segment_gm_cycle', 'dio_inventory_cycle', 'capex_rd_intensity', 'customer_concentration_export'},
        'kpi_internet_ads': {'users_arpu_take_rate', 'ad_volume_vs_price', 'ai_capex_depreciation', 'tac_margin'},
        'kpi_consumer_retail': {'sssg_traffic_ticket', 'store_count_unit_econ', 'gm_pricing_vs_promo', 'inventory_turnover_dio'},
        'kpi_pharma_biotech': {'pipeline_clinical_milestones', 'product_concentration_loe', 'rd_burn_cash_runway', 'gross_to_net_payer_mix'},
    }

    listed = list_industry_packs()
    assert [item['pack_id'] for item in listed] == sorted(INDUSTRY_PACK_IDS)
    for item in listed:
        pid = item['pack_id']
        pack = get_industry_pack(pid)
        assert pack is not None
        assert item['fingerprint'] == industry_pack_fingerprint(pack)
        assert set(item['kpi_ids']) == expected_kpis[pid]
        assert pack.layer == 'l2_industry_kpi'
        assert pack.parent_pack_id == 'earnings_review'
        assert any(k.required for k in pack.kpis)
        assert all(
            a.angle and a.evidence_anchor and a.next_quarter_trigger
            for a in pack.counter_evidence_angles
        )

    assert get_industry_pack(' kpi_saas_cloud ') is not None
    assert get_industry_pack('kpi_missing') is None
    assert get_industry_pack('') is None


def test_l2_industry_pack_schema_rejects_invalid_definitions():
    raw = json.loads((PACKS_DIR / 'kpi_saas_cloud.json').read_text(encoding='utf-8'))

    bad = json.loads(json.dumps(raw))
    bad['kpis'][0]['preferred_tools'] = ['not_a_real_tool']
    with pytest.raises(ValidationError):
        IndustryKpiPack.model_validate(bad)

    bad = json.loads(json.dumps(raw))
    bad['kpis'][1]['id'] = bad['kpis'][0]['id']
    with pytest.raises(ValidationError):
        IndustryKpiPack.model_validate(bad)

    bad = json.loads(json.dumps(raw))
    for kpi in bad['kpis']:
        kpi['required'] = False
    with pytest.raises(ValidationError):
        IndustryKpiPack.model_validate(bad)

    bad = json.loads(json.dumps(raw))
    bad['sic_major_groups'] = ['7372']
    with pytest.raises(ValidationError):
        IndustryKpiPack.model_validate(bad)

    bad = json.loads(json.dumps(raw))
    bad['unexpected_field'] = True
    with pytest.raises(ValidationError):
        IndustryKpiPack.model_validate(bad)


def test_sic_ranges_are_disjoint_and_never_overlap_financials():
    covered: dict[int, str] = {}
    for low, high, pack_id in SIC_INDUSTRY_RANGES:
        assert low <= high
        assert pack_id in INDUSTRY_PACK_IDS
        for sic in range(low, high + 1):
            assert not (6000 <= sic <= 6799), f'Financial SIC {sic} leaked into {pack_id}'
            assert sic not in covered, f'Overlapping SIC {sic}: {covered[sic]} vs {pack_id}'
            covered[sic] = pack_id


@pytest.mark.parametrize('sic,expected', [
    ('7372', 'kpi_saas_cloud'),
    (7371, 'kpi_saas_cloud'),
    (' 7374 ', 'kpi_saas_cloud'),
    ('3674', 'kpi_semis_hardware'),
    (3571, 'kpi_semis_hardware'),
    ('3576', 'kpi_semis_hardware'),
    ('3559', 'kpi_semis_hardware'),
    ('7370', 'kpi_internet_ads'),
    ('7375', 'kpi_internet_ads'),
    (7311, 'kpi_internet_ads'),
    ('7841', 'kpi_internet_ads'),
    ('5331', 'kpi_consumer_retail'),
    (5211, 'kpi_consumer_retail'),
    ('5812', 'kpi_consumer_retail'),
    ('5961', 'kpi_consumer_retail'),
    ('2086', 'kpi_consumer_retail'),
    ('2840', 'kpi_consumer_retail'),
    ('3021', 'kpi_consumer_retail'),
    ('2834', 'kpi_pharma_biotech'),
    ('2836', 'kpi_pharma_biotech'),
    (3841, 'kpi_pharma_biotech'),
    ('8731', 'kpi_pharma_biotech'),
    # Financial / gated sectors MUST return None
    ('6021', None),
    (6022, None),
    ('6199', None),
    ('6211', None),
    ('6331', None),
    (6411, None),
    ('6798', None),
    # Unmatched industrial/energy/utilities & invalid inputs
    ('2911', None),
    ('4911', None),
    (None, None),
    ('', None),
    ('   ', None),
    ('not-numeric', None),
    (True, None),
    (False, None),
    (0, None),
    (10000, None),
])
def test_match_industry_pack_by_sic_deterministic(sic, expected):
    assert match_industry_pack_by_sic(sic) == expected
    assert match_industry_pack_by_sic(sic) == match_industry_pack_by_sic(sic)


def test_classify_sic_sector_matches_diagnostics_gate():
    assert classify_sic_sector('6021') == ('6021', 'bank', True)
    assert classify_sic_sector(6331) == ('6331', 'insurance', True)
    assert classify_sic_sector('6798') == ('6798', 'reit', True)
    assert classify_sic_sector('6211') == ('6211', 'financial_other', True)
    assert classify_sic_sector('7372') == ('7372', 'non_financial', False)
    assert classify_sic_sector(None) == (None, 'non_financial', False)


def test_compose_earnings_method_pack_auto_override_and_sector_gate():
    # 1. Default L1 only
    base_only = compose_earnings_method_pack()
    assert isinstance(base_only, ComposedEarningsMethodPack)
    assert base_only.base_pack.id == 'earnings_review'
    assert base_only.base_pack.version == '2.0.0'
    assert base_only.industry_pack is None
    assert base_only.selection_source == 'none'
    assert base_only.is_sector_gated is False
    assert len(base_only.pack_records) == 1
    assert len(base_only.rendered_prompt) <= MAX_PACK_CHARS
    assert '[truncated]' not in base_only.rendered_prompt

    # 2. Auto SIC match -> L1 + L2 merged deterministically
    saas = compose_earnings_method_pack(sic='7372', available_capabilities=FULL_CAPS)
    assert saas.sic_code == '7372'
    assert saas.matched_industry_pack_id == 'kpi_saas_cloud'
    assert saas.industry_pack_id == 'kpi_saas_cloud'
    assert saas.industry_pack is not None
    assert saas.selection_source == 'sic_auto'
    assert len(saas.pack_records) == 2
    assert '+' in saas.fingerprint
    assert 'rpo_crpo' in saas.rendered_prompt
    assert 'METHOD PACK earnings_review@2.0.0' in saas.rendered_prompt
    assert 'L2 INDUSTRY KPI PACK kpi_saas_cloud@' in saas.rendered_prompt
    assert len(saas.rendered_prompt) <= MAX_COMPOSED_PACK_CHARS
    assert '[truncated]' not in saas.rendered_prompt
    assert render_for_prompt(saas, available_capabilities=FULL_CAPS) == saas.rendered_prompt

    # 3. Explicit override wins for non-financial SIC while preserving matched_industry_pack_id
    override = compose_earnings_method_pack(sic='3571', industry_pack_id='kpi_saas_cloud')
    assert override.matched_industry_pack_id == 'kpi_semis_hardware'
    assert override.industry_pack_id == 'kpi_saas_cloud'
    assert override.selection_source == 'explicit_override'

    # 4. Financial sector gate blocks L2 non-financial packs & marks boundary
    for fin_sic, expected_cat in (('6021', 'bank'), ('6331', 'insurance'), ('6798', 'reit')):
        gated = compose_earnings_method_pack(sic=fin_sic)
        assert gated.is_sector_gated is True
        assert gated.sector_category == expected_cat
        assert gated.industry_pack is None
        assert gated.selection_source == 'sector_gated'
        assert 'SECTOR GATE' in gated.rendered_prompt
        with pytest.raises(ValueError):
            compose_earnings_method_pack(sic=fin_sic, industry_pack_id='kpi_saas_cloud')

    # 5. Unknown explicit industry_pack_id fails closed
    with pytest.raises(ValueError):
        compose_earnings_method_pack(industry_pack_id='kpi_nonexistent')


@pytest.mark.parametrize('caps', [ALL_CAPS, FULL_CAPS, NO_MARKET, NOTHING, {}])
def test_all_l2_and_composed_packs_render_deterministically_without_truncation(caps):
    for pid, pack in load_industry_packs().items():
        l2_a = render_industry_pack_for_prompt(pack, available_capabilities=caps)
        l2_b = render_for_prompt(pack, available_capabilities=dict(reversed(list(caps.items()))))
        assert l2_a == l2_b
        assert len(l2_a) <= MAX_INDUSTRY_PACK_CHARS, (pid, len(l2_a))
        assert '[truncated]' not in l2_a
        assert not re.search(r'20\d\d-\d\d-\d\d', l2_a)

        composed = compose_earnings_method_pack(industry_pack_id=pid, available_capabilities=caps)
        assert len(composed.rendered_prompt) <= MAX_COMPOSED_PACK_CHARS, (pid, len(composed.rendered_prompt))
        assert '[truncated]' not in composed.rendered_prompt


def test_tool_available_in_run_handles_a1_tools():
    assert tool_available_in_run('sec_earnings_release', {'filings_available': True}) is True
    assert tool_available_in_run('sec_earnings_release', {'filings_available': False}) is False
    assert tool_available_in_run('sec_filing_tables', {'filings_available': True}) is True
    assert tool_available_in_run('sec_fulltext_search', {'filings_available': False}) is False
    assert tool_available_in_run('earnings_consensus', {'earnings_consensus_available': True}) is True
    assert tool_available_in_run('earnings_consensus', {}) is False
    assert tool_available_in_run('earnings_transcript', {'earnings_transcript_available': True}) is True
    assert tool_available_in_run('earnings_transcript', {}) is False
    assert tool_available_in_run('sec_filing_sections', {}, has_eligible_sources=True) is True
    assert tool_available_in_run('sec_redline_diff', {}, has_eligible_sources=False) is False
    assert tool_available_in_run('earnings_quality_diagnostics', {}) is True
    assert tool_available_in_run('guidance_and_nongaap_reconciler', {}) is True
    assert tool_available_in_run('valuation_and_implied_expectations', {}) is True
