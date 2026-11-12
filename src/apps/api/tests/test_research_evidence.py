from app.research_evidence import evidence_differences, suggest_percentage_evidence


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
