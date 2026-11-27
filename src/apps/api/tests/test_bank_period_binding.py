from datetime import date
from decimal import Decimal

import pytest

from app.fin_data.adapters.sec_bank_disclosure import _extract_8k_supplement_metrics
from app.fin_data.adapters.sec_deep import EarningsReleaseExhibit, EarningsReleasePeriod, strip_html_preserving_tables


def extract(rows: str):
    html = f'<table><tr><th>Consolidated results</th><th>3Q25</th><th>2Q25</th></tr>{rows}</table>'
    _, tables = strip_html_preserving_tables(html)
    exhibit = EarningsReleaseExhibit(exhibit_type='EX-99.1', filename='x', url='x', text='', tables=tables, content_sha256='x')
    period = EarningsReleasePeriod(period_role='current', accession='a', filed_at=date(2025, 10, 16), index_json_url='x', ex99_1=exhibit)
    return _extract_8k_supplement_metrics(period, date(2025, 9, 30))


def test_reordered_spaced_quarter_header_from_html():
    result = extract('''<tr><td></td><td>2Q25</td><td>3Q 25</td></tr>
        <tr><td>Tangible book value per common share</td><td>$29.57</td><td>$30.60</td></tr>''')
    assert result['tbvps_usd'] == Decimal('30.60')
    assert result['tbvps_usd_provenance']['column'] == '3Q 25'


@pytest.mark.parametrize('heading', [
    '<td></td><td>2Q 25</td><td>1Q 25</td>',
    '<td>Year ended</td><td>2025</td><td>2024</td>',
    '<td>Nine months ended</td><td>2025</td><td>2024</td>',
    '<td></td><td>June 30, 2025</td><td>March 31, 2025</td>',
])
def test_unsupported_section_never_inherits_old_column(heading):
    result = extract(f'''<tr>{heading}</tr>
        <tr><td>Tangible book value per common share</td><td>$29.57</td><td>$30.60</td></tr>''')
    assert 'tbvps_usd' not in result


def test_dates_support_instant_but_not_quarter_flow():
    result = extract('''<tr><td></td><td>June 30, 2025</td><td>September 30, 2025</td></tr>
        <tr><td>Tangible book value per common share</td><td>$29.57</td><td>$30.60</td></tr>
        <tr><td>Return on average tangible common equity</td><td>20%</td><td>21%</td></tr>''')
    assert result['tbvps_usd'] == Decimal('30.60')
    assert 'rotce_q_pct' not in result


def test_explicit_ytd_and_duplicate_target_are_ambiguous():
    for heading in ('<td>Year to date</td><td>3Q25</td><td>2Q25</td>',
                    '<td></td><td>3Q25</td><td>3Q 25</td>'):
        result = extract(f'''<tr>{heading}</tr>
            <tr><td>Return on average tangible common equity</td><td>20%</td><td>21%</td></tr>''')
        assert 'rotce_q_pct' not in result


def test_parent_ytd_heading_prevents_quarter_label_from_becoming_quarter_flow():
    result = extract('''<tr><td></td><td>Nine months ended</td><td>Nine months ended</td></tr>
        <tr><td></td><td>3Q25</td><td>3Q24</td></tr>
        <tr><td>Return on average tangible common equity</td><td>20%</td><td>21%</td></tr>''')
    assert 'rotce_q_pct' not in result


@pytest.mark.parametrize('label', ["3Q ' 25", "q 3 ’ 2025", '3 q 2025'])
def test_equivalent_quarter_labels_bind_and_detect(label):
    result = extract(f'''<tr><td></td><td>2Q25</td><td>{label}</td></tr>
        <tr><td>Tangible book value per common share</td><td>$29.57</td><td>$30.60</td></tr>''')
    assert result['tbvps_usd'] == Decimal('30.60')


def test_half_year_and_standalone_section_invalidate_previous_header():
    for heading in ('<td></td><td>H1 2025</td><td>H2 2025</td>',
                    '<td>Other financial measures</td><td></td><td></td>'):
        result = extract(f'''<tr>{heading}</tr>
            <tr><td>Tangible book value per common share</td><td>$29.57</td><td>$30.60</td></tr>''')
        assert 'tbvps_usd' not in result


def test_quarter_section_after_annual_section_rebinds():
    result = extract('''<tr><td>Year ended</td><td>2025</td><td>2024</td></tr>
        <tr><td>Quarter ended</td><td></td><td></td></tr>
        <tr><td></td><td>2Q25</td><td>3Q 25</td></tr>
        <tr><td>Tangible book value per common share</td><td>$29.57</td><td>$30.60</td></tr>''')
    assert result['tbvps_usd'] == Decimal('30.60')


@pytest.mark.parametrize('heading', ['Other financial measures:', 'Segment capital ratios:', 'Annual ratios:'])
def test_unsupported_sibling_headings_do_not_inherit_columns(heading):
    result = extract(f'''<tr><td>{heading}</td><td></td><td></td></tr>
        <tr><td>Tangible book value per common share</td><td>$30.60</td><td>$29.57</td></tr>''')
    assert 'tbvps_usd' not in result


def test_within_table_metric_group_keeps_column_binding():
    result = extract('''<tr><td>Per common share:</td><td></td><td></td></tr>
        <tr><td>Tangible book value per common share</td><td>$30.60</td><td>$29.57</td></tr>''')
    assert result['tbvps_usd'] == Decimal('30.60')
