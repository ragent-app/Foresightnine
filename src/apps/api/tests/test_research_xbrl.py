"""Unit tests for expanded Inline XBRL verification (10-Q quarters, instant balance sheet, negatives, standard labels)."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256

from app.research_sources import ResearchSource, SourcePermissions
from app.research_table_provenance import annual_table_value_alignment, trace_html_table
from app.research_uploads import HtmlUploadRequest, extract_uploaded_html
from app.research_xbrl import match_inline_fact, resolve_inline_annual_facts, resolve_inline_facts


def _source(html: str) -> ResearchSource:
    raw = html.encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    permissions = SourcePermissions(store=True, display=True, model_input=True)
    text, raw_hash = extract_uploaded_html(HtmlUploadRequest(title="10-Q", html_base64=encoded, permissions=permissions))
    return ResearchSource(
        source_id="s-xbrl",
        owner_id="o",
        title="10-Q",
        text=text,
        kind="official",
        obtained_at=datetime.now(timezone.utc),
        content_sha256=sha256(text.encode("utf-8")).hexdigest(),
        retrieval_method="sec_filing_html",
        response_sha256=raw_hash,
        original_html_base64=encoded,
        permissions=permissions,
    )


def _html_shell(*, contexts: str, rows_html: str) -> str:
    return (
        '<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"'
        ' xmlns:xbrli="http://www.xbrl.org/2003/instance"'
        ' xmlns:iso4217="http://www.xbrl.org/2003/iso4217"'
        ' xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12"'
        ' xmlns:us-gaap="http://fasb.org/us-gaap/2024">'
        '<ix:header>'
        f'{contexts}'
        '<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
        '</ix:header>'
        f'<table>{rows_html}</table></html>'
    )


def test_resolve_inline_facts_supports_quarterly_and_instant_while_annual_stays_annual_only():
    contexts = (
        '<xbrli:context id="q1"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier></xbrli:entity>'
        '<xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-03-31</xbrli:endDate></xbrli:period></xbrli:context>'
        '<xbrli:context id="ytd6"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier></xbrli:entity>'
        '<xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-06-30</xbrli:endDate></xbrli:period></xbrli:context>'
        '<xbrli:context id="inst"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier></xbrli:entity>'
        '<xbrli:period><xbrli:instant>2024-03-31</xbrli:instant></xbrli:period></xbrli:context>'
    )
    q_html = _html_shell(
        contexts=contexts,
        rows_html=(
            '<tr><th>Metric</th><th>Q1 2024</th><th>6M 2024</th></tr>'
            '<tr><td>Cost of revenue</td>'
            '<td><ix:nonFraction name="us-gaap:CostOfRevenue" contextRef="q1" unitRef="usd" format="ixt:num-dot-decimal" scale="6" decimals="-6">450</ix:nonFraction></td>'
            '<td><ix:nonFraction name="us-gaap:CostOfRevenue" contextRef="ytd6" unitRef="usd" format="ixt:num-dot-decimal" scale="6" decimals="-6">920</ix:nonFraction></td></tr>'
        ),
    )
    q_src = _source(q_html)
    q_start = q_src.text.index("450")
    q_trace = trace_html_table(q_src, q_start, q_start + 3)
    assert q_trace is not None
    # Annual-only field stays absent for 10-Q rows (full backward compatibility)
    assert "inline_xbrl_annual_facts" not in q_trace
    assert resolve_inline_annual_facts(q_html, q_trace["rows"]) == []
    # 78-105d quarter is resolved; 180d 6M YTD context is excluded
    assert len(q_trace["inline_xbrl_facts"]) == 1
    q_fact = q_trace["inline_xbrl_facts"][0]
    assert q_fact["period_kind"] == "quarter" and q_fact["period_start"] == "2024-01-01" and q_fact["period_end"] == "2024-03-31"
    assert q_fact["value"] == "450" and q_fact["concept"] == "us-gaap:CostOfRevenue"

    q_align = {"status": "matched", "column_date": "2024-03-31", "value_column_start": 1, "value_column_end": 2, "period_kind": "quarter"}
    matched_q = match_inline_fact({"status": "available", **q_trace}, q_align, value=Decimal("450"),
                                  label="Cost of revenue Q1 2024", unit="USD million", currency="USD")
    assert matched_q is not None and matched_q["metric"] == "cost of revenue"
    assert match_inline_fact({"status": "available", **q_trace}, {**q_align, "period_kind": "annual"},
                             value=Decimal("450"), label="Cost of revenue Q1 2024", unit="USD million", currency="USD") is None

    # Instant balance sheet context
    bs_html = _html_shell(
        contexts=contexts,
        rows_html=(
            '<tr><th>Balance Sheet</th><th>March 31, 2024</th></tr>'
            '<tr><td>Cash and cash equivalents</td>'
            '<td><ix:nonFraction name="us-gaap:CashAndCashEquivalentsAtCarryingValue" contextRef="inst" unitRef="usd" format="ixt:num-dot-decimal" scale="6" decimals="-6">1,250</ix:nonFraction></td></tr>'
        ),
    )
    bs_src = _source(bs_html)
    bs_start = bs_src.text.index("1,250")
    bs_trace = trace_html_table(bs_src, bs_start, bs_start + 5)
    assert bs_trace is not None and "inline_xbrl_annual_facts" not in bs_trace
    assert len(bs_trace["inline_xbrl_facts"]) == 1
    bs_fact = bs_trace["inline_xbrl_facts"][0]
    assert bs_fact["period_kind"] == "instant" and bs_fact["period_start"] is None and bs_fact["period_end"] == "2024-03-31"
    assert bs_fact["value"] == "1250"

    bs_align = {"status": "matched", "column_date": "2024-03-31", "value_column_start": 1, "value_column_end": 2, "period_kind": "instant"}
    matched_bs = match_inline_fact({"status": "available", **bs_trace}, bs_align, value=Decimal("1250"),
                                   label="Cash and cash equivalents", unit="USD million", currency="USD")
    assert matched_bs is not None and matched_bs["metric"] == "cash"

    # Mixed instant + startDate fails closed
    bad_ctx = _html_shell(
        contexts=(
            '<xbrli:context id="bad"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier></xbrli:entity>'
            '<xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:instant>2024-03-31</xbrli:instant></xbrli:period></xbrli:context>'
        ),
        rows_html=(
            '<tr><td>Total assets</td>'
            '<td><ix:nonFraction name="us-gaap:Assets" contextRef="bad" unitRef="usd" format="ixt:num-dot-decimal" scale="6" decimals="-6">500</ix:nonFraction></td></tr>'
        ),
    )
    bad_src = _source(bad_ctx)
    bad_pos = bad_src.text.index("500")
    bad_trace = trace_html_table(bad_src, bad_pos, bad_pos + 3)
    assert bad_trace is not None and "inline_xbrl_facts" not in bad_trace


def test_negative_inline_xbrl_values_and_conflict_rejection():
    contexts = (
        '<xbrli:context id="c1"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier></xbrli:entity>'
        '<xbrli:period><xbrli:startDate>2023-01-01</xbrli:startDate><xbrli:endDate>2023-12-31</xbrli:endDate></xbrli:period></xbrli:context>'
        '<xbrli:context id="c2"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier></xbrli:entity>'
        '<xbrli:period><xbrli:startDate>2022-01-01</xbrli:startDate><xbrli:endDate>2022-12-31</xbrli:endDate></xbrli:period></xbrli:context>'
    )
    # Pattern 1: parenthesized negative cell with sign="-" on ix:nonFraction + annual alignment
    html = _html_shell(
        contexts=contexts,
        rows_html=(
            '<tr><th>Years Ended</th><th>December 31, 2023</th><th>December 31, 2022</th></tr>'
            '<tr><td>Net loss</td>'
            '<td>(<ix:nonFraction name="us-gaap:NetIncomeLoss" contextRef="c1" unitRef="usd" format="ixt:num-dot-decimal" scale="6" decimals="-6" sign="-">123.5</ix:nonFraction>)</td>'
            '<td><ix:nonFraction name="us-gaap:NetIncomeLoss" contextRef="c2" unitRef="usd" format="ixt:num-dot-decimal" scale="6" decimals="-6" sign="-">(45)</ix:nonFraction></td></tr>'
        ),
    )
    src = _source(html)
    pos = src.text.index("(123.5)")
    tr = trace_html_table(src, pos, pos + 7)
    assert tr is not None
    # First cell (sign='-' wrapping "(123.5)") resolves to -123.5; second cell (double negation sign='-' AND "(45)") fails closed!
    assert len(tr["inline_xbrl_annual_facts"]) == 1
    assert tr["inline_xbrl_annual_facts"][0]["value"] == "-123.5"

    align = annual_table_value_alignment({"status": "available", **tr}, Decimal("-123.5"), "FY2023 (year ended December 31, 2023)")
    assert align is not None and align["status"] == "matched"
    matched = match_inline_fact({"status": "available", **tr}, align, value=Decimal("-123.5"),
                                label="Net loss FY2023", unit="USD million", currency="USD")
    assert matched is not None and matched["value"] == "-123.5"

    # Pattern 2: parenthesized negative inside ix:nonFraction without sign attribute & sign="-" with leading dash
    html2 = _html_shell(
        contexts=contexts,
        rows_html=(
            '<tr><th>Years Ended</th><th>December 31, 2023</th><th>December 31, 2022</th></tr>'
            '<tr><td>Operating loss</td>'
            '<td><ix:nonFraction name="us-gaap:OperatingIncomeLoss" contextRef="c1" unitRef="usd" format="ixt:num-dot-decimal" scale="6" decimals="-6">(75)</ix:nonFraction></td>'
            '<td>-<ix:nonFraction name="us-gaap:OperatingIncomeLoss" contextRef="c2" unitRef="usd" format="ixt:num-dot-decimal" scale="6" decimals="-6" sign="-">30</ix:nonFraction></td></tr>'
        ),
    )
    src2 = _source(html2)
    pos2 = src2.text.index("(75)")
    tr2 = trace_html_table(src2, pos2, pos2 + 4)
    assert tr2 is not None
    vals = {f["context_ref"]: f["value"] for f in tr2["inline_xbrl_facts"]}
    assert vals == {"c1": "-75", "c2": "-30"}

    # Pattern 3: visual "(123)" WITHOUT sign="-" MUST fail closed (conflict between positive XBRL and negative display)
    conflict_html = _html_shell(
        contexts=contexts,
        rows_html=(
            '<tr><td>Net loss</td>'
            '<td>(<ix:nonFraction name="us-gaap:NetIncomeLoss" contextRef="c1" unitRef="usd" format="ixt:num-dot-decimal" scale="6" decimals="-6">123</ix:nonFraction>)</td></tr>'
        ),
    )
    src3 = _source(conflict_html)
    pos3 = src3.text.index("(123)")
    tr3 = trace_html_table(src3, pos3, pos3 + 5)
    assert tr3 is not None and resolve_inline_facts(conflict_html, tr3["rows"]) == []


def test_expanded_standard_labels_and_strict_decimals_scale():
    contexts = (
        '<xbrli:context id="c1"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier></xbrli:entity>'
        '<xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-03-31</xbrli:endDate></xbrli:period></xbrli:context>'
    )
    html = _html_shell(
        contexts=contexts,
        rows_html=(
            '<tr><th>Line</th><th>Q1 2024</th></tr>'
            '<tr><td>Research and development</td>'
            '<td><ix:nonFraction name="us-gaap:ResearchAndDevelopmentExpense" contextRef="c1" unitRef="usd" format="ixt:num-dot-decimal" scale="6" decimals="-6">210</ix:nonFraction></td></tr>'
        ),
    )
    src = _source(html)
    pos = src.text.index("210")
    tr = trace_html_table(src, pos, pos + 3)
    assert tr is not None
    align = {"status": "matched", "column_date": "2024-03-31", "value_column_start": 1, "value_column_end": 2}
    ok = match_inline_fact({"status": "available", **tr}, align, value=Decimal("210"),
                           label="R&D expense Q1 2024", unit="USD million", currency="USD")
    assert ok is not None and ok["metric"] == "rd expense"
    # Strict decimals == -scale still enforced
    bad_dec = _source(html.replace('decimals="-6"', 'decimals="-3"'))
    pos_b = bad_dec.text.index("210")
    tr_b = trace_html_table(bad_dec, pos_b, pos_b + 3)
    assert match_inline_fact({"status": "available", **tr_b}, align, value=Decimal("210"),
                             label="R&D expense Q1 2024", unit="USD million", currency="USD") is None
