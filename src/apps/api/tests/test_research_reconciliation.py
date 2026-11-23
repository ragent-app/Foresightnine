"""Tests for multi-source fact reconciliation (I1). Fixtures are minimal excerpts of the 365 real-model exports."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.research_reconciliation import (
    METRIC_RULES, TOLERANCES, classify_source, compare_values, derive_metric, detect_qualifiers, entity_hint, detect_metrics,
    model_summary, normalize_period, normalize_value, reconcile, suggested_gaps, ui_view,
)

# -- fixtures --------------------------------------------------------------------------------------------------------

SRC = {
    "stocktitan": {"source_id": "s_stocktitan", "url": "https://www.stocktitan.net/articles/anthropic-ipo-live-ant", "kind": "official",
                   "retrieval_method": "web_html", "title": "Anthropic IPO Live", "published_at": None},
    "cnbc": {"source_id": "s_cnbc", "url": "https://www.cnbc.com/2026/11/19/anthropic-ipo-ant-live-updates.html",
             "kind": "official", "retrieval_method": "web_html", "published_at": "2026-11-19T22:00:00Z"},
    "cbs": {"source_id": "s_cbs", "url": "https://www.cbsnews.com/news/anthropic-ipo-ant", "kind": "official",
            "retrieval_method": "web_html", "published_at": "2026-11-19T23:00:00Z"},
    "yahoo": {"source_id": "s_yahoo", "url": "https://finance.yahoo.com/technology/live/anthropic-ipo-live", "kind": "official",
              "retrieval_method": "web_html"},
    "anthropic": {"source_id": "s_anthropic", "url": "https://www.anthropic.com/news/anthropic-prices-initial-public-offering",
                  "kind": "official", "retrieval_method": "official_html"},
    "sec": {"source_id": "s_sec", "url": "https://www.sec.gov/Archives/edgar/data/1/s1.htm", "kind": "official",
            "retrieval_method": "sec_filing_html"},
    "bar": {"source_id": "s_bar", "title": "ANT@NASDAQ 1day bars (2026-11-19 to 2026-11-19)", "kind": "market_data",
            "retrieval_method": "market_data", "permissions": {"store": True, "display": True, "model_input": True, "export": False},
            "provenance": {"provider": "twelve_data", "symbol": "ANT", "exchange": "NASDAQ", "currency": "USD",
                           "actual_first": "2026-11-19", "actual_last": "2026-11-19"}},
    "upload": {"source_id": "s_upload", "title": "my notes", "kind": "user_supplied", "retrieval_method": "user_supplied"},
}


def fact(fid, label, value, unit, source, *, period="2026-11-19", currency=None, quotation="", **extra):
    return {"fact_id": fid, "label": label, "value": value, "unit": unit, "currency": currency, "period": period,
            "source_id": SRC[source]["source_id"], "quotation": quotation or f"{label} {value}", **extra}


def ant_facts():
    return [
        fact("f_cbs", "ANT intraday high", "114.99", "USD", "cbs", quotation="hit a high of $114.99"),
        fact("f_cnbc", "ANT首日盘中最高价", "114.99", "USD per share", "cnbc", quotation="peaked at $114.99"),
        fact("f_bar", "ANT最高价", "115.099998", "USD", "bar", currency="USD", quotation="ANT@NASDAQ | 2026-11-19 | 1day | H 115.099998"),
        fact("f_close_news", "ANT首日收盘价 (4:00 p.m. ET)", "97.17", "USD per share", "stocktitan"),
        fact("f_close_bar", "ANT收盘价", "97.17000", "USD", "bar", currency="USD"),
    ]


def sources(*names):
    return [SRC[n] for n in names]


def row_for(report, metric, entity="ANT"):
    rows = [r for r in report["rows"] if r["metric"] == metric and r["entity_hint"] == entity]
    assert len(rows) == 1, [r["metric"] for r in report["rows"]]
    return rows[0]


# -- normalisation ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("2026-11-19", "2026-11-19"), ("2026/11/19", "2026-11-19"), ("Nov. 19, 2026", "2026-11-19"),
    ("19 November 2026", "2026-11-19"), ("2026年11月19日", "2026-11-19"), ("2026-11-19 close", "2026-11-19"),
    ("Nov 19, 2026 open", "2026-11-19"), ("FY2026", "FY2026"), ("fiscal 2026", "FY2026"), ("2026财年", "FY2026"),
    ("Q3 FY2026", "FY2026Q3"), ("2026Q3", "2026Q3"), ("Q3 2026", "2026Q3"), ("2026年第三季度", "2026Q3"),
    ("H1 2026", "2026H1"), ("2026年上半年", "2026H1"), ("前9个月 2026", "2026-9M"), ("2026-09", "2026-09"),
    ("September 2026", "2026-09"), ("2026", "Y2026"), ("2026-11-02当周", "W2026-11-02"),
    ("2025-11-19 to 2026-11-19", "2025-11-19..2026-11-19"), ("9M ended Sept 30, 2026", "2026-09-30"),
    ("Q3", None), ("", None), (None, None), ("recent", None), ("2026-02-30", None),
])
def test_normalize_period(text, expected):
    assert normalize_period(text) == expected


@pytest.mark.parametrize("value,unit,kind,expected,currency", [
    ("114.99", "USD per share", "price", Decimal("114.99"), "USD"),
    ("832", "million shares", "count", Decimal("832000000"), None),
    ("846M", "shares", "count", Decimal("846000000"), None),
    ("8.3亿", "股", "count", Decimal("830000000"), None),
    ("1.8", "trillions USD", "money", Decimal("1800000000000"), "USD"),
    ("81.3", "billions USD", "money", Decimal("81300000000"), "USD"),
    ("33.24", "亿元", "money", Decimal("3324000000"), "CNY"),
    ("1,234.5", "USD thousand", "money", Decimal("1234500"), "USD"),
    ("12.5", "%", "percent", Decimal("12.5"), None),
    ("12.5%", "", "percent", Decimal("12.5"), None),
    ("0.125", "ratio", "percent", Decimal("12.500"), None),
    ("50", "bps", "percent", Decimal("0.5"), None),
])
def test_normalize_value(value, unit, kind, expected, currency):
    got = normalize_value(value, unit, kind)
    assert got is not None and got.value == expected and got.currency == currency


def test_normalize_value_rejects_conflicting_scales_and_text():
    assert normalize_value("8亿", "million shares", "count") is None
    assert normalize_value("n/a", "USD", "price") is None


def test_currency_never_inferred_from_conflicting_tokens():
    assert normalize_value("5", "USD", "price", currency="EUR").currency is None


@pytest.mark.parametrize("label,unit,metric", [
    ("ANT首日盘中最高价", "USD", "high"), ("QQQ收盘价（最新，2026-11-19）", "USD", "close"), ("ANT IPO发行价", "USD", "ipo_price"),
    ("ANT首日首笔成交价 (11:48 a.m. ET)", "USD", "open"), ("First-day trading volume", "million shares", "volume"),
    ("Previous close", "USD", "prev_close"), ("Net income", "USD", "net_income"), ("营业收入", "亿元", "revenue"),
    ("Revenue growth YoY, nine months", "percent", "revenue_pct"), ("Gross margin", "percent", "gross_margin"),
    ("Fully diluted market cap at close", "trillions USD", "market_cap"),
])
def test_derive_metric(label, unit, metric):
    assert derive_metric(label, unit)[0] == metric


@pytest.mark.parametrize("label,unit,reason", [
    ("收盘价较发行价涨幅", "percent", "ambiguous_metric"), ("52-week high", "USD", "range_scope"),
    ("Anthropic gain", "USD", "no_metric"), ("Revenue share", "percent", "percent_semantics_unknown"),
    ("Trading volume", "USD", "count_with_currency_unit"), ("Gross margin", "USD", "margin_without_percent_unit"),
])
def test_derive_metric_refuses_ambiguity(label, unit, reason):
    assert derive_metric(label, unit) == (None, reason)


def test_entity_hint_rules():
    spans = lambda label: detect_metrics(label)[1]
    assert entity_hint("ANT首日盘中最高价", metric_spans=spans("ANT首日盘中最高价")) == "ANT"
    assert entity_hint("ANT IPO发行价") == "ANT"
    assert entity_hint("QQQ与SPY收盘价") is None
    assert entity_hint("Anthropic IPO offer price per share") == "ANTHROPIC"
    assert entity_hint("宁德时代营收", metric_spans=spans("宁德时代营收")) == "宁德时代"
    assert entity_hint("首日成交量", metric_spans=spans("首日成交量")) is None
    assert entity_hint("NASDAQ:ANT close") == "ANT"
    # provenance symbol of a market data source wins over label words
    assert entity_hint("close", source={"provenance": {"instrument_id": "ETF:QQQ"}}) == "QQQ"


def test_qualifiers():
    assert detect_qualifiers("ANT盘后收盘价") == ("after_hours",)
    assert detect_qualifiers("全稀释市值") == ("diluted",)
    assert set(detect_qualifiers("Run-rate revenue estimate")) == {"run_rate", "estimate"}


def test_source_classes():
    assert classify_source(SRC["bar"]) == "market_data"
    assert classify_source(SRC["sec"]) == "filing"
    assert classify_source({"url": "https://www.federalreserve.gov/x"}) == "filing"
    assert classify_source(SRC["anthropic"]) == "issuer"
    assert classify_source(SRC["upload"]) == "user_upload"
    assert classify_source(SRC["cnbc"]) == "news"            # runtime kind=official covers every fetched web page
    assert classify_source({"url": "https://www.cnbc.com/a", "retrieval_method": "web_html"}) == "news"
    assert classify_source({"url": "https://www.cnbc.com/a", "kind": "official"}, issuer_hosts=["cnbc.com"]) == "issuer"
    assert classify_source({"source_id": "x"}) == "other"
    # older run exports carry no kind: a url-less "daily close" series is market data
    assert classify_source({"title": "ETF:QQQ daily close (2025-11-19 to 2026-11-19)"}) == "market_data"


# -- tolerance and rounding ------------------------------------------------------------------------------------------

def n(value, unit="USD", kind="price"):
    return normalize_value(value, unit, kind)


@pytest.mark.parametrize("a,b,status,severity", [
    ("97.17", "97.17000", "consistent", None),
    ("97.17", "97.17", "consistent", None),
    ("645.04", "645.039978", "rounding_only", None),
    ("114.99", "115.099998", "differs", "minor"),        # 0.1% apart but not explained by rounding: a REAL difference
    ("114.99", "115.10", "differs", "minor"),
    ("83", "83.4", "rounding_only", None),
    ("100", "103", "differs", "material"),
])
def test_price_rules(a, b, status, severity):
    cmp = compare_values(n(a), n(b), "price")
    assert cmp["status"] == status and cmp["severity"] == severity


def test_scaled_rounding_and_counts():
    assert compare_values(n("1.8", "trillion USD", "money"), n("1.77", "trillion USD", "money"), "money")["status"] == "rounding_only"
    assert compare_values(n("846", "million shares", "count"), n("832", "million shares", "count"), "count") ["severity"] == "material"
    assert compare_values(n("832", "million shares", "count"), n("832413000", "shares", "count"), "count")["status"] == "rounding_only"
    # 1% volume tolerance: 0.6% apart, not rounding -> differs but minor
    assert compare_values(n("832000000", "shares", "count"), n("837000000", "shares", "count"), "count")["severity"] == "minor"


def test_percent_rules_use_percentage_points():
    assert compare_values(n("12.5", "%", "percent"), n("12.54", "%", "percent"), "percent")["status"] == "rounding_only"
    assert compare_values(n("12.5", "%", "percent"), n("12.55", "%", "percent"), "percent")["severity"] == "minor"
    assert compare_values(n("12.5", "%", "percent"), n("12.7", "%", "percent"), "percent")["severity"] == "material"
    assert compare_values(n("0.125", "ratio", "percent"), n("12.5", "%", "percent"), "percent")["status"] == "consistent"
    assert TOLERANCES["percent"]["abs"] == Decimal("0.1")


# -- the ANT example -------------------------------------------------------------------------------------------------

def test_ant_ipo_day_high_differs_with_both_sources():
    report = reconcile(ant_facts(), sources("cbs", "cnbc", "bar", "stocktitan"))
    high = row_for(report, "high")
    assert high["status"] == "differs" and high["period"] == "2026-11-19"
    assert {v["source_class"] for v in high["values"]} == {"market_data", "news"}
    ref = high["reference_value"]
    assert ref["value"] == "115.099998" and ref["source_class"] == "market_data" and ref["source_id"] == "s_bar"
    assert high["difference"]["abs"] == "0.109998" and high["severity"] == "minor"
    assert high["independent_source_count"] == 3 and high["distinct_hosts"] == ["cbsnews.com", "cnbc.com"]
    bar_value = next(v for v in high["values"] if v["is_reference"])
    assert bar_value["export_allowed"] is False and bar_value["fact_id"] == "f_bar"
    other = next(v for v in high["values"] if v["fact_id"] == "f_cbs")
    assert other["vs_reference"]["status"] == "differs" and other["value_as_stated"] == "114.99"
    assert "114.99" in other["quotation_excerpt"]


def test_ant_close_97_17_consistent_and_source_independent():
    report = reconcile(ant_facts(), sources("cbs", "cnbc", "bar", "stocktitan"))
    close = row_for(report, "close")
    assert close["status"] == "consistent" and close["independent_source_count"] == 2
    assert report["stats"]["by_status"] == {"consistent": 1, "rounding_only": 0, "differs": 1}


def test_cluster_hook_counts_syndicated_copies_once():
    clusters = {"s_cbs": "c1", "s_cnbc": "c1"}
    report = reconcile(ant_facts(), sources("cbs", "cnbc", "bar", "stocktitan"),
                       cluster_of=lambda s: clusters.get(s["source_id"]))
    high = row_for(report, "high")
    assert high["independent_source_count"] == 2           # CBS + CNBC copy collapse into one cluster
    assert high["distinct_hosts"] == ["cbsnews.com", "cnbc.com"]
    assert [v["fact_id"] for v in high["values"]].count("f_cbs") == 1
    deduped = next(v for v in high["values"] if v["fact_id"] == "f_cbs")
    assert deduped["duplicate_fact_ids"] == ["f_cnbc"] and deduped["cluster_id"] == "c1"


def test_two_copies_alone_are_not_a_reconciliation():
    clusters = {"s_cbs": "c1", "s_cnbc": "c1"}
    facts = [f for f in ant_facts() if f["fact_id"] in ("f_cbs", "f_cnbc")]
    report = reconcile(facts, sources("cbs", "cnbc"), cluster_of=lambda s: clusters.get(s["source_id"]))
    assert report["rows"] == [] and report["stats"]["single_source_groups_skipped"] == 1


def test_exchange_preferred_over_filing_for_prices_and_filing_for_statements():
    facts = [fact("a", "ANT收盘价", "97.17", "USD", "bar", currency="USD"),
             fact("b", "ANT close", "97.20", "USD", "sec", currency="USD"),
             fact("c", "ANT revenue", "33.24", "billions USD", "sec", period="9M 2026"),
             fact("d", "ANT revenue", "30.0", "billions USD", "stocktitan", period="9M 2026")]
    report = reconcile(facts, sources("bar", "sec", "stocktitan"))
    assert row_for(report, "close")["reference_value"]["source_class"] == "market_data"
    revenue = row_for(report, "revenue")
    assert revenue["reference_value"]["source_class"] == "filing" and revenue["period"] == "2026-9M"
    assert revenue["status"] == "differs" and revenue["severity"] == "material"


def test_all_news_has_no_reference_and_never_auto_corrects():
    facts = [fact("a", "ANT intraday high", "114.99", "USD", "cnbc"), fact("b", "ANT intraday high", "115.10", "USD", "yahoo")]
    report = reconcile(facts, sources("cnbc", "yahoo"))
    row = row_for(report, "high")
    assert row["reference_value"] == {"value": None, "reason": "no_primary_source"}
    assert "no_primary_source" in row["flags"] and not any(v["is_reference"] for v in row["values"])
    assert {v["value_as_stated"] for v in row["values"]} == {"114.99", "115.10"}


# -- never merge when unsure -----------------------------------------------------------------------------------------

def test_different_currencies_are_never_merged_or_converted():
    facts = [fact("a", "ANT close", "97.17", "USD", "bar", currency="USD"), fact("b", "ANT close", "88.0", "EUR", "cnbc")]
    report = reconcile(facts, sources("bar", "cnbc"))
    assert report["rows"] == []
    assert reconcile([fact("a", "ANT close", "97.17", "", "bar")], sources("bar"))["stats"]["unmerged"] == {"currency_unknown": 1}


def test_qualifiers_keep_groups_apart():
    facts = [fact("a", "ANT收盘价", "97.17", "USD", "bar", currency="USD"),
             fact("b", "ANT盘后收盘价", "92.60", "USD", "cnbc"),
             fact("c", "ANT全稀释市值", "1.8", "trillions USD", "cnbc"), fact("d", "ANT市值", "1.27", "trillions USD", "yahoo")]
    report = reconcile(facts, sources("bar", "cnbc", "yahoo"))
    assert report["rows"] == []


def test_missing_entity_period_or_metric_is_reported_not_merged():
    facts = [fact("a", "首日成交量", "846", "million shares", "cnbc"), fact("b", "首日成交量", "832", "million shares", "stocktitan"),
             fact("c", "ANT close", "97.17", "USD", "cnbc", period=""), fact("d", "ANT close", "97.17", "USD", "yahoo", period="recent"),
             fact("e", "ANT gain", "17", "percent", "cnbc"), fact("f", "ANT close", "NaN", "USD", "cnbc")]
    report = reconcile(facts, sources("cnbc", "stocktitan", "yahoo"))
    assert report["rows"] == []
    assert report["stats"]["unmerged"] == {"entity_unknown": 2, "no_metric": 1, "period_unknown": 2, "unparseable_value": 1}


def test_default_entity_and_aliases_enable_merging_when_caller_knows_the_topic():
    facts = [fact("a", "首日成交量", "846", "million shares", "cnbc"), fact("b", "首日常规时段成交量", "832", "million shares", "stocktitan"),
             fact("c", "Anthropic IPO offer price per share", "83.00", "USD", "anthropic", currency="USD", period="IPO priced before Nov 19, 2026"),
             fact("d", "ANT IPO发行价", "83", "USD per share", "stocktitan", period="2026-11-18 (定价日)")]
    srcs = sources("cnbc", "stocktitan", "anthropic")
    assert reconcile(facts, srcs)["rows"] == []
    report = reconcile(facts, srcs, default_entity="ant", entity_aliases={"Anthropic": "ANT"})
    volume = row_for(report, "volume")
    assert volume["status"] == "differs" and volume["severity"] == "material"
    assert volume["difference"]["abs"] == "14000000"
    ipo = row_for(report, "ipo_price")
    assert ipo["status"] == "consistent" and ipo["period"] is None      # IPO price is timeless: dates ignored
    assert ipo["reference_value"]["source_class"] == "issuer"


def test_single_source_internal_conflict_is_flagged():
    facts = [fact("a", "ANT close", "97.17", "USD", "cnbc"), fact("b", "ANT收盘价", "92.60", "USD", "cnbc")]
    row = row_for(reconcile(facts, sources("cnbc")), "close")
    assert row["status"] == "differs" and "single_source_internal_conflict" in row["flags"] and row["independent_source_count"] == 1


def test_net_loss_sign_and_percent_change_metrics():
    facts = [fact("a", "Net loss, nine months", "104", "billions USD", "cnbc", period="9M 2026", currency="USD"),
             fact("b", "ANT net income", "-104.0", "billions USD", "sec", period="9M 2026", currency="USD"),
             fact("c", "ANT revenue growth YoY", "1200", "percent", "cnbc", period="9M 2026"),
             fact("d", "ANT revenue growth yoy", "1.2e3", "percent", "sec", period="9M 2026")]
    report = reconcile(facts, sources("cnbc", "sec"), default_entity="ANT")
    assert row_for(report, "net_income")["status"] in ("consistent", "rounding_only")
    assert "revenue_pct" not in [r["metric"] for r in report["rows"]]       # "1.2e3" is unparseable on purpose
    assert report["stats"]["unmerged"]["unparseable_value"] == 1


def test_low_ocr_confidence_flag_and_location_passthrough():
    facts = [fact("a", "ANT revenue", "33.24", "billions USD", "sec", period="FY2026", currency="USD", extraction_method="ocr",
                  ocr_mean_confidence=55.0, location={"page": 12, "character_start": 3, "character_end": 9}),
             fact("b", "ANT revenue", "33.0", "billions USD", "cnbc", period="FY2026", currency="USD")]
    row = row_for(reconcile(facts, sources("sec", "cnbc")), "revenue")
    assert "low_ocr_confidence" in row["flags"]
    ocr_value = next(v for v in row["values"] if v["fact_id"] == "a")
    assert ocr_value["low_ocr_confidence"] and ocr_value["location"]["page"] == 12 and ocr_value["extraction_method"] == "ocr"


# -- consumers -------------------------------------------------------------------------------------------------------

def test_calculations_mark_affected_rows():
    calc = {"calculation_id": "calc-1", "inputs": [{"fact_id": "f_cbs", "value": "114.99"}]}
    row = row_for(reconcile(ant_facts(), sources("cbs", "cnbc", "bar", "stocktitan"), calculations=[calc]), "high")
    assert row["affected_calculations"] == ["calc-1"]


def test_model_summary_and_ui_view_shapes():
    report = reconcile(ant_facts(), sources("cbs", "cnbc", "bar", "stocktitan"))
    summary = model_summary(report, top_n=3)["reconciliation"]
    assert summary["by_status"]["differs"] == 1 and len(summary["discrepancies"]) == 1
    d = summary["discrepancies"][0]
    assert d["entity"] == "ANT" and d["metric"] == "high" and d["reference"] == "115.099998"
    assert {v["fact_id"] for v in d["values"]} == {"f_cbs", "f_cnbc", "f_bar"}
    assert len(repr(model_summary(report, max_chars=300))) < 900
    gaps = suggested_gaps(report)
    assert gaps[0]["code"] == "source_discrepancy_minor" and "115.099998" in gaps[0]["message"]
    ui = ui_view(report)
    assert ui["counts"]["differs"] == 1 and ui["rows"][0]["badge"] == "differs_minor"
    assert ui["rows"][0]["title"]["en"].startswith("ANT High") and ui["rows"][0]["cells"][0]["is_reference"] is True


def test_rows_sorted_material_first_and_deterministic():
    facts = ant_facts() + [fact("v1", "ANT volume", "846", "million shares", "cnbc"), fact("v2", "ANT volume", "832", "million shares", "stocktitan")]
    srcs = sources("cbs", "cnbc", "bar", "stocktitan")
    a = reconcile(facts, srcs)
    assert [r["metric"] for r in a["rows"]][:2] == ["volume", "high"]
    assert reconcile(facts, srcs) == a


def test_works_with_objects_and_missing_source_metadata():
    from app.research_sources import ResearchSource, SourcePermissions
    from datetime import datetime, timezone
    src = ResearchSource(source_id="s_obj", owner_id="o", title="t", text="x", kind="official", obtained_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
                         content_sha256="0" * 64, url="https://www.reuters.com/a", retrieval_method="web_html",
                         permissions=SourcePermissions(store=True, model_input=True))
    facts = [{"fact_id": "x", "label": "ANT close", "value": "97.17", "unit": "USD", "period": "2026-11-19", "source_id": "s_obj"},
             {"fact_id": "y", "label": "ANT close", "value": "97.00", "unit": "USD", "period": "2026-11-19", "source_id": "unknown_src"}]
    report = reconcile(facts, [src])
    row = row_for(report, "close")
    assert {v["source_class"] for v in row["values"]} == {"news", "other"} and row["status"] == "differs"


# -- real run excerpts (365 export Q-B1 + Q-B2, two independent runs over the same question) ----------------------------

REAL_SOURCES = [
    {"source_id": "b1_stocktitan", "url": "https://www.stocktitan.net/articles/anthropic-ipo-live-ant-first-trading-day", "kind": "official", "retrieval_method": "web_html"},
    {"source_id": "b1_cnbc", "url": "https://www.cnbc.com/2026/11/19/anthropic-ipo-ant-live-updates.html", "kind": "official", "retrieval_method": "web_html"},
    {"source_id": "b1_yahoo", "url": "https://finance.yahoo.com/technology/live/anthropic-ipo-live-anthropic", "kind": "official", "retrieval_method": "web_html"},
    {"source_id": "b2_anthropic", "url": "https://www.anthropic.com/news/anthropic-prices-initial-public-offering", "kind": "official", "retrieval_method": "official_html"},
    {"source_id": "b2_cnbc", "url": "https://www.cnbc.com/2026/11/19/anthropic-ipo-ant-live-updates.html", "kind": "official", "retrieval_method": "web_html"},
]
# (fact_id, label, value, unit, currency, period, source_id)
REAL_FACTS = [
    ("b1-ipo", "ANT IPO发行价", "83", "USD per share", None, "2026-11-18 (定价日)", "b1_stocktitan"),
    ("b1-open", "ANT首日首笔成交价 (11:48 a.m. ET)", "111.5", "USD per share", None, "2026-11-19", "b1_stocktitan"),
    ("b1-high", "ANT首日盘中最高价", "114.99", "USD per share", None, "2026-11-19", "b1_stocktitan"),
    ("b1-close", "ANT首日收盘价 (4:00 p.m. ET)", "97.17", "USD per share", None, "2026-11-19", "b1_stocktitan"),
    ("b1-after", "ANT首日盘后最后一笔成交价 (7:59 p.m. ET)", "92.6", "USD per share", None, "2026-11-19", "b1_stocktitan"),
    ("b1-proceeds", "IPO募资总额", "81.3", "billions USD", None, "2026-11 (IPO)", "b1_yahoo"),
    ("b1-mcap", "首日收盘全稀释市值", "1.8", "trillions USD", None, "2026-11-19 收盘", "b1_cnbc"),
    ("b1-vol-cnbc", "首日成交量", "846", "million shares", None, "2026-11-19", "b1_cnbc"),
    ("b1-vol-st", "首日常规时段成交量 (StockTitan口径)", "832", "million shares", None, "2026-11-19", "b1_stocktitan"),
    ("b1-shares", "IPO发行股数", "980", "million shares", None, "2026-11-18 (定价)", "b1_cnbc"),
    ("b2-ipo", "Anthropic IPO offer price per share", "83.00", "USD per share", "USD", "IPO priced before Nov 19, 2026 Nasdaq debut", "b2_anthropic"),
    ("b2-shares", "Total shares offered in IPO", "980000000", "shares", None, "Nov 2026 IPO", "b2_anthropic"),
    ("b2-open", "ANT first-day open price", "111.5", "USD per share", "USD", "Nov 19, 2026 open", "b2_cnbc"),
    ("b2-close", "ANT first-day close price", "97.17", "USD per share", "USD", "Nov 19, 2026 close", "b2_cnbc"),
    ("b2-proceeds", "Proceeds raised in the IPO", "81.3", "billions USD", "USD", "Nov 2026 IPO", "b2_cnbc"),
    ("b2-vol", "First-day trading volume", "846", "million shares", None, "Nov 19, 2026", "b2_cnbc"),
    ("b2-mcap", "Fully diluted market cap at first-day close", "1.8", "trillions USD", "USD", "Nov 19, 2026 close", "b2_cnbc"),
    ("b2-mcap-open", "Market capitalization at first-day open", "2.01", "trillions USD", "USD", "Nov 19, 2026 open", "b2_cnbc"),
    ("b2-gain", "First-day gain vs IPO price", "17", "percent", None, "Nov 19, 2026 close vs $83.00 IPO price", "b2_cnbc"),
]


def real_facts():
    return [{"fact_id": f, "label": l, "value": v, "unit": u, "currency": c, "period": p, "source_id": s, "quotation": l}
            for f, l, v, u, c, p, s in REAL_FACTS]


def test_real_anthropic_ipo_runs_reconcile_conservatively():
    # Without topic context only facts that name the ticker merge.
    bare = reconcile(real_facts(), REAL_SOURCES)
    assert {r["metric"] for r in bare["rows"]} == {"close", "open"}
    assert bare["stats"]["unmerged"]["entity_unknown"] >= 5

    report = reconcile(real_facts(), REAL_SOURCES, default_entity="ANT", entity_aliases={"ANTHROPIC": "ANT"})
    by_metric = {r["metric"]: r for r in report["rows"]}
    assert by_metric["volume"]["status"] == "differs" and by_metric["volume"]["severity"] == "material"
    assert {v["value_as_stated"] for v in by_metric["volume"]["values"]} == {"846", "832"}
    assert by_metric["volume"]["independent_source_count"] == 2          # the CNBC page fetched in both runs is one source (same canonical URL)
    for metric in ("close", "open", "proceeds", "shares_offered", "ipo_price"):
        assert by_metric[metric]["status"] == "consistent", metric
    assert by_metric["ipo_price"]["reference_value"]["source_class"] == "issuer"
    assert by_metric["shares_offered"]["independent_source_count"] == 2   # 980 million shares == 980000000 shares
    # the after-hours print, the open-time market cap and the gain percentage are never compared with other figures
    # the diluted market cap comes twice from the same CNBC page: one source, nothing to reconcile; the open-time
    # market cap, the after-hours print and the gain percentage never meet another figure
    assert "high" not in by_metric and "market_cap" not in by_metric
    assert report["stats"]["by_status"]["differs"] == 1


def test_expanded_metric_rules_cover_40_plus_metrics_without_collisions():
    assert len(METRIC_RULES) >= 40
    cases = [
        ("AAPL gross profit", "USD", "gross_profit"),
        ("AAPL cost of revenue", "USD", "cost_of_revenue"),
        ("AAPL营业成本", "CNY", "cost_of_revenue"),
        ("AAPL rd expense", "USD", "rd_expense"),
        ("AAPL研发费用", "CNY", "rd_expense"),
        ("AAPL sga expense", "USD", "sga_expense"),
        ("AAPL ebitda", "USD", "ebitda"),
        ("AAPL free cash flow", "USD", "free_cash_flow"),
        ("AAPL自由现金流", "USD", "free_cash_flow"),
        ("AAPL operating cash flow", "USD", "operating_cash_flow"),
        ("AAPL capex", "USD", "capex"),
        ("AAPL total debt", "USD", "total_debt"),
        ("AAPL short term debt", "USD", "short_term_debt"),
        ("AAPL long term debt", "USD", "long_term_debt"),
        ("AAPL pe ratio", "x", "pe_ratio"),
        ("AAPL 市盈率", "倍", "pe_ratio"),
        ("AAPL pb ratio", "x", "pb_ratio"),
        ("AAPL ev/ebitda", "x", "ev_ebitda"),
        ("AAPL ROE", "%", "roe"),
        ("AAPL ROIC", "%", "roic"),
        ("AAPL dividend yield", "%", "dividend_yield"),
        ("AAPL shares diluted", "shares", "shares_diluted"),
        ("AAPL cash", "USD", "cash"),
        ("AAPL 货币资金", "CNY", "cash"),
    ]
    for label, unit, expected in cases:
        assert derive_metric(label, unit) == (expected, None), (label, unit)


def test_expanded_metrics_reconcile_multiples_and_return_percentages():
    facts = [
        fact("pe1", "AAPL P/E ratio", "28.5", "x", "sec", period="FY2025"),
        fact("pe2", "AAPL市盈率", "28.5x", "", "cnbc", period="FY2025"),
        fact("roe1", "AAPL ROE", "145.0", "%", "sec", period="FY2025"),
        fact("roe2", "AAPL净资产收益率", "1.450", "ratio", "yahoo", period="FY2025"),
        fact("fcf1", "AAPL free cash flow", "100", "billions USD", "sec", period="FY2025", currency="USD"),
        fact("fcf2", "AAPL自由现金流", "90", "billions USD", "cnbc", period="FY2025", currency="USD"),
    ]
    report = reconcile(facts, sources("sec", "cnbc", "yahoo"))
    pe = row_for(report, "pe_ratio", entity="AAPL")
    assert pe["status"] == "consistent" and pe["kind"] == "ratio"
    roe = row_for(report, "roe", entity="AAPL")
    assert roe["status"] == "consistent" and roe["kind"] == "percent"
    fcf = row_for(report, "free_cash_flow", entity="AAPL")
    assert fcf["status"] == "differs" and fcf["severity"] == "material"


def test_fiscal_alignment_hints_surface_fy_vs_year_end_date_without_merging():
    facts = [
        fact("fy_sec", "AAPL revenue", "391.0", "billions USD", "sec", period="FY2025", currency="USD"),
        fact("dt_cnbc", "AAPL revenue", "391.0", "billions USD", "cnbc", period="2025-09-27", currency="USD"),
        # Different entity, metric, currency, or year must NOT produce false alignment hints
        fact("other_cur", "AAPL revenue", "360.0", "billions EUR", "yahoo", period="2025-12-31", currency="EUR"),
        fact("other_yr", "AAPL revenue", "380.0", "billions USD", "yahoo", period="2024-09-28", currency="USD"),
    ]
    report = reconcile(facts, sources("sec", "cnbc", "yahoo"))
    assert report["rows"] == []  # never silently merged across FY2025 vs 2025-09-27
    hints = report["fiscal_alignment_hints"]
    assert len(hints) == 1
    hint = hints[0]
    assert hint["entity"] == "AAPL"
    assert hint["metric"] == "revenue"
    assert hint["currency"] == "USD"
    assert hint["fiscal_period"] == "FY2025"
    assert hint["date_period"] == "2025-09-27"
    assert hint["fiscal_fact_ids"] == ["fy_sec"]
    assert hint["date_fact_ids"] == ["dt_cnbc"]
    assert hint["merged"] is False
    assert "not merged" in hint["message"]

    summary = model_summary(report)["reconciliation"]
    assert summary["fiscal_alignment_hints"] == hints
    ui = ui_view(report)
    assert ui["fiscal_alignment_hints"] == hints


def test_single_source_is_exposed_separately_without_claiming_comparison():
    report = reconcile([fact('machine', 'ANT close', '97.17', 'USD', 'bar', currency='USD')], sources('bar'))
    assert report['rows'] == []
    assert len(report['single_source_rows']) == 1
    view = ui_view(report)
    assert view['rows'][0]['status'] == 'single_source'
    assert view['rows'][0]['independent_source_count'] == 1
    assert view['rows'][0]['cells'][0]['source_id'] == 's_bar'
