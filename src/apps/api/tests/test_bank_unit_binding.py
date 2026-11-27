"""Unit binding is derived from the actual HTML cells, not metric names."""
from datetime import date
from decimal import Decimal

import pytest

from app.fin_data.adapters.sec_bank_disclosure import _extract_8k_supplement_metrics
from app.fin_data.adapters.sec_deep import EarningsReleaseExhibit, EarningsReleasePeriod, strip_html_preserving_tables
from app.research_bank_diagnostics import evaluate_bank_valuation
from tests.test_v4a_bank_diagnostics import _snap_fixture


def extract(value, heading="Consolidated results", extra=""):
    html = f'''<html><body><table><tr><th>{heading}</th><th>3Q26</th></tr>
      <tr><td>Tangible book value per share</td><td>{value}</td></tr>
      <tr><td>Standardized CET1 capital ratio</td><td>13.2%</td></tr>{extra}</table></body></html>'''
    text, tables = strip_html_preserving_tables(html)
    exhibit = EarningsReleaseExhibit(exhibit_type="EX-99.1", filename="x", url="https://sec.test/x", text=text, tables=tables, content_sha256="fixture")
    period = EarningsReleasePeriod(period_role="current", accession="fixture", filed_at=date(2026, 10, 15), period_of_report=date(2026, 9, 30), index_json_url="x", ex99_1=exhibit)
    return _extract_8k_supplement_metrics(period, date(2026, 9, 30))


@pytest.mark.parametrize("value, heading", [
    ("12.5%", "Consolidated results"),
    ("12.5", "Consolidated results"),
    ("$12.5", "Consolidated results in CAD"),
    ("$12.5", "Consolidated results in XYZ"),
    ("$12.5", "Consolidated results currency unknown"),
    ("$12.5", "Consolidated results ($ in millions)"),
    ("€12.5", "Consolidated results"),
])
def test_reject_incompatible_or_unknown_units_but_keep_independent_cet1(value, heading):
    result = extract(value, heading)
    assert "tbvps_usd" not in result
    assert result["cet1_std_pct"] == Decimal("13.2")
    # Materialize the same eligibility gate consumed by valuation.
    snap = _snap_fixture(symbol="GS", sic="6211", bank_subtype="investment_bank_bhc", tbvps_rep=None, tbvps_std=None, rotce_q=None)
    val = evaluate_bank_valuation(snap, price_usd="100")
    assert val.p_to_tbv_primary is None
    assert val.implied_rotce_pct is None


@pytest.mark.parametrize("value, heading", [
    ("$12.5", "Consolidated results"),
    ("12.5", "Consolidated results (USD per share)"),
    ("12.5", "Consolidated results ($ in millions except per share data)"),
])
def test_explicit_unit_evidence(value, heading):
    result = extract(value, heading)
    assert result["tbvps_usd"] == Decimal("12.5")
    evidence = result["tbvps_usd_provenance"]
    assert evidence["unit"] == "USD/share"
    assert evidence["unit_evidence"]
    assert evidence["scale_evidence"]


def test_conflicting_disclosures_invalidate_only_that_metric():
    result = extract("$12.5", extra="<tr><td>Tangible book value per share</td><td>$13.5</td></tr>")
    assert "tbvps_usd" not in result
    assert result["cet1_std_pct"] == Decimal("13.2")


def test_unit_scope_does_not_borrow_from_another_column():
    html = '''<table><tr><th>Consolidated results</th><th>3Q26</th><th>2Q26 USD</th></tr>
    <tr><td>Tangible book value per share</td><td>12.5</td><td>$14.0</td></tr>
    <tr><td>Standardized CET1 capital ratio</td><td>13.2%</td><td>13.0%</td></tr></table>'''
    text, tables = strip_html_preserving_tables(html)
    ex = EarningsReleaseExhibit(exhibit_type="EX-99.1", filename="x", url="x", text=text, tables=tables, content_sha256="x")
    period = EarningsReleasePeriod(period_role="current", accession="x", filed_at=date(2026, 10, 15), index_json_url="x", ex99_1=ex)
    result = _extract_8k_supplement_metrics(period, date(2026, 9, 30))
    assert "tbvps_usd" not in result
    assert result["cet1_std_pct"] == Decimal("13.2")


def test_percentage_header_supplies_unit_for_ratio():
    result = extract("$12.5", "Consolidated results (percent)")
    assert "tbvps_usd" not in result
    assert result["cet1_std_pct"] == Decimal("13.2")
    html = '''<table><tr><th>Consolidated results (percent)</th><th>3Q26</th></tr>
    <tr><td>Standardized CET1 capital ratio</td><td>13.2</td></tr></table>'''
    text, tables = strip_html_preserving_tables(html)
    ex = EarningsReleaseExhibit(exhibit_type="EX-99.1", filename="x", url="x", text=text, tables=tables, content_sha256="x")
    period = EarningsReleasePeriod(period_role="current", accession="x", filed_at=date(2026, 10, 15), index_json_url="x", ex99_1=ex)
    result = _extract_8k_supplement_metrics(period, date(2026, 9, 30))
    assert result["cet1_std_pct"] == Decimal("13.2")
    assert "percent" in result["cet1_std_pct_provenance"]["unit_evidence"]
