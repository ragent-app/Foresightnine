from app.research_evidence import evidence_differences


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
