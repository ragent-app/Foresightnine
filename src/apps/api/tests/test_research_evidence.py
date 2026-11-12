from app.research_evidence import claim_amount_differences, evidence_differences, suggest_percentage_evidence


def fact(fact_id, source_id, value, *, label="Revenue", period="FY2023", unit="USD million", currency="USD"):
    return {"fact_id": fact_id, "source_id": source_id, "value": value,
            "label": label, "period": period, "unit": unit, "currency": currency}


def test_cross_source_differences_are_candidates_not_confirmed_conflicts():
    a = fact("a", "one", "100")
    b = fact("b", "two", "120", label=" revenue ")
    result = evidence_differences([a, b])
    assert len(result) == 1
    assert result[0]["status"] == "requires_entity_and_method_review"
    assert {f["source_id"] for f in result[0]["facts"]} == {"one", "two"}
    assert evidence_differences([a, dict(b, value="100.00")]) == []
    assert evidence_differences([a, dict(b, source_id="one")]) == []
    assert evidence_differences([a, dict(b, period="FY2022")]) == []
    assert evidence_differences([a, dict(b, currency="EUR")]) == []


def test_invalid_or_ambiguous_facts_do_not_create_candidates():
    a, b = fact("a", "one", "100"), fact("b", "two", "120")
    assert evidence_differences([a, b, dict(b, value="130")]) == []
    assert evidence_differences([a, dict(b, value="NaN")]) == []
    assert evidence_differences([a, dict(b, fact_id=[])]) == []


def test_large_group_is_bounded():
    items = [fact(str(i), f"source{i}", str(i)) for i in range(25)]
    candidate = evidence_differences(items)[0]
    assert len(candidate["facts"]) == 20 and candidate["truncated"]


def test_suggests_unique_numeric_evidence_without_claiming_semantic_support():
    annual = {**fact("annual", "annual-source", "2.5", label="Annual GDP", period="2023", unit="percent", currency=None),
              "quotation": "Annual GDP grew 2.5 percent", "location": {"character_start": 0, "character_end": 27},
              "verification": "model_extraction_not_independently_verified"}
    quarter = {**fact("quarter", "quarter-source", "3.2", label="Quarter GDP", period="Q4 2023", unit="percent", currency=None),
               "quotation": "Quarter GDP grew 3.2 percent", "location": {"character_start": 0, "character_end": 28},
               "verification": "model_extraction_not_independently_verified"}
    claim = {"text": "The annual rate is 2.5 percent and the quarter rate is 3.2 percent.",
             "citations": [{"source_id": "quarter-source", "quotation": quarter["quotation"]}]}
    result = suggest_percentage_evidence([claim], [annual, quarter])
    assert result[0]["citations"] == claim["citations"]
    assert result[0]["suggested_fact_evidence"][0]["fact_id"] == "annual"
    assert result[0]["suggested_fact_evidence"][0]["status"] == "requires_semantic_review"
    assert "suggested_fact_evidence" not in claim


def test_suggestions_fail_closed_for_ambiguous_or_user_edited_facts():
    base = {**fact("a", "one", "2.5", unit="percent", currency=None), "quotation": "Annual 2.5 percent",
            "location": {"character_start": 0, "character_end": 18}, "verification": "model_extraction_not_independently_verified"}
    claim = {"text": "The rate was 2.5%.", "citations": []}
    assert not suggest_percentage_evidence([claim], [base, {**base, "fact_id": "b", "source_id": "two"}])[0].get("suggested_fact_evidence")
    assert not suggest_percentage_evidence([claim], [{**base, "authorship": "user"}])[0].get("suggested_fact_evidence")
    assert not suggest_percentage_evidence([claim], [{**base, "location": {"status": "ambiguous"}}])[0].get("suggested_fact_evidence")
    assert not suggest_percentage_evidence([claim], [{**base, "unit": "percentage points"}])[0].get("suggested_fact_evidence")
    assert not suggest_percentage_evidence([claim], [{**base, "currency": "USD"}])[0].get("suggested_fact_evidence")
    assert not suggest_percentage_evidence([claim], [{**base, "quotation": "Missing number!!!"}])[0].get("suggested_fact_evidence")
    assert not suggest_percentage_evidence([claim], [{**base, "location": {"character_start": 0, "character_end": 10}}])[0].get("suggested_fact_evidence")
    assert not suggest_percentage_evidence([{**claim, "suggested_fact_evidence": [{"fabricated": True}]}], [])[0].get("suggested_fact_evidence")


def test_formula_scale_factor_does_not_suggest_unrelated_percent_fact():
    hundred = {**fact("a", "one", "100", unit="percent", currency=None), "quotation": "Growth 100 percent",
        "location": {"character_start": 0, "character_end": 18}, "verification": "model_extraction_not_independently_verified"}
    formula = {"text": "(120 - 100) / 100 × 100%", "citations": []}
    assert not suggest_percentage_evidence([formula], [hundred])[0].get("suggested_fact_evidence")
    statement = {"text": "Growth was 100%.", "citations": []}
    assert suggest_percentage_evidence([statement], [hundred])[0]["suggested_fact_evidence"][0]["fact_id"] == "a"


def test_unquoted_chinese_percentage_is_flagged_without_claiming_semantic_support():
    claim = {"text": "同比2.6%，不可误读为环比。", "citations": [{"source_id": "one", "quotation": "same month one year Percent 2.0"}]}
    result = suggest_percentage_evidence([claim], [])[0]
    assert result["uncited_percentages"] == ["2.6%"]
    assert "uncited_percentages" not in claim
    assert not suggest_percentage_evidence([{**claim, "citations": [{"quotation": "同比2.6%"}],
                                             "uncited_percentages": ["fabricated"]}], [])[0].get("uncited_percentages")
    assert not suggest_percentage_evidence([{"text": "增长(120-100)/100 × 100%", "citations": []}], [])[0].get("uncited_percentages")


def test_chinese_usd_conversion_flags_only_anchored_unmatched_amounts():
    quote = "2023 revenue 53717, 2024 revenue 57063 million USD"
    def sourced(id, value, year):
        return {**fact(id, "one", value, period=year, unit="USD millions"), "quotation": quote,
                "location": {"character_start": 0, "character_end": len(quote)},
                "verification": "model_extraction_not_independently_verified"}
    facts = [sourced("prior", "53717", "2023"), sourced("current", "57063", "2024")]
    claim = {"text": "2023为53,717百万美元，2024为57,063百万美元，即57,070.63亿美元。",
             "citations": [{"source_id": "one"}]}
    warning = claim_amount_differences([claim], facts)
    assert len(warning) == 1 and warning[0]["claimed_amount"] == "57,070.63亿美元"
    assert warning[0]["status"] == "requires_review" and warning[0]["source_fact_ids"] == ["prior", "current"]
    for amount in ("570.63亿美元", "570.6亿美元", "57063百万美元"):
        assert claim_amount_differences([{**claim, "text": f"2024为57,063百万美元，即{amount}。"}], facts) == []
    assert claim_amount_differences([{**claim, "text": "金额57,070.63亿美元。"}], facts) == []
    assert claim_amount_differences([{**claim, "citations": [{"source_id": "other"}]}], facts) == []
    assert claim_amount_differences([claim], [{**f, "authorship": "user"} for f in facts]) == []
