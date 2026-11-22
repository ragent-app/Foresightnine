from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main as api
from app.main import app
from app.model_protocol import ModelResponse, ModelToolCall, ModelUsage
from app.research_topics import (
    ResearchCalculation,
    ResearchContent,
    ResearchTopic,
    ResearchTurn,
    ResearchVersion,
    TurnUsage,
    VersionInput,
)
from app.research_calculations import (
    BINARY_FORMULAS,
    MULTI_FACT_FORMULAS,
    CalculationRequest,
    calculate,
    calculation_input_status,
)
from app.research_turns import merge_turn
import pytest


def test_legacy_versions_keep_unknown_creator():
    version = ResearchVersion.model_validate({"version_id": "old", "created_at": "2023-01-01T00:00:00Z",
                                               "content": {}, "review": "draft"})
    assert version.created_by == "unknown"


@pytest.mark.parametrize("claim", [
    {"text": "", "role": "support"},
    {"text": "A", "role": ["support"]},
    {"text": "A", "citations": [{"source_id": ""}]},
    {"text": "A", "citations": "not a list"},
])
def test_user_findings_reject_invalid_shapes(monkeypatch, claim):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    from uuid import uuid4
    registration = client.post("/v1/auth/local/register", json={"email": f"claims-{uuid4().hex}@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Review?"}).json()
    path = "/v2/research/topics/" + topic["topic_id"] + "/versions"
    response = client.post(path, headers=headers, json={"parent_version_id": topic["versions"][0]["version_id"],
        "content": {"claims": [claim]}})
    assert response.status_code == 422


PERMISSIONS = {"store": True, "display": True, "model_input": True, "export": True}


def start(monkeypatch, text, label):
    """Register an account, attach one source and open a topic; returns (client, headers, source_id, topic, path)."""
    from uuid import uuid4
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    auth = client.post("/v1/auth/local/register", json={"email": f"{label}-{uuid4().hex}@example.test",
        "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": text,
        "permissions": PERMISSIONS}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Compare", "source_ids": [source_id]}).json()
    return client, headers, source_id, topic, "/v2/research/topics/" + topic["topic_id"]


def fact(source_id, text, quote, value, period, *, fact_id, unit="USD", currency="USD", label="Revenue"):
    start = text.index(quote)
    return {"fact_id": fact_id, "label": label, "value": value, "unit": unit, "currency": currency, "period": period,
            "source_id": source_id, "quotation": quote, "character_start": start, "character_end": start + len(quote),
            "location": {"character_start": start, "character_end": start + len(quote)},
            "verification": "numeric_and_location_match_semantics_not_verified"}


def numeric(item):
    return {key: item[key] for key in ("fact_id", "value", "unit", "currency", "period")}


def save(client, headers, path, parent, content, **extra):
    return client.post(path + "/versions", headers=headers, json={"parent_version_id": parent["version_id"], "content": content, **extra})


TEXT = "Revenue was 100 USD in 2023. Revenue was 80 USD in 2022. Revenue was 0 USD in 2021. Costs were 5 EUR."


def test_recalculate_current_facts_creates_auditable_draft(monkeypatch):
    client, headers, sid, topic, path = start(monkeypatch, TEXT, "recalculate")
    first = fact(sid, TEXT, "Revenue was 100 USD", "100", "2023", fact_id="a")
    second = fact(sid, TEXT, "Revenue was 80 USD", "80", "2022", fact_id="b")
    old = calculate(CalculationRequest(formula="growth_percent", first=numeric(first), second=numeric(second)))
    old["calculation_id"] = "calc-1"
    saved = save(client, headers, path, topic["versions"][0], {"summary": "My view", "facts": [first, second],
        "calculations": [old]}, review="confirmed", note="Review me").json()
    assert saved["versions"][-1]["content"]["calculations"][0]["input_status"] == "matched"
    edited = deepcopy(saved["versions"][-1]["content"])
    edited["facts"][0].update(value="200", quotation="Revenue was 100 USD", verification="x")
    original = save(client, headers, path, saved["versions"][-1], edited).json()["versions"][-1]
    assert original["content"]["calculations"][0]["input_status"] == "changed"
    assert original["content"]["facts"][0]["verification"] == "user_edited_value_not_in_quotation"
    # A value the quotation does not contain cannot feed a calculation until it is corrected.
    payload = {"parent_version_id": original["version_id"], "calculation_index": 0}
    assert client.post(path + "/calculations/recalculate", headers=headers, json=payload).status_code == 422
    fixed = deepcopy(original["content"])
    fixed["facts"][0].update(value="100")
    current = save(client, headers, path, original, fixed, note="Review me").json()["versions"][-1]
    assert current["content"]["calculations"][0]["input_status"] == "matched"
    response = client.post(path + "/calculations/recalculate", headers=headers,
                           json={"parent_version_id": current["version_id"], "calculation_index": 0})
    assert response.status_code == 201, response.text
    latest = response.json()["versions"][-1]
    calculation = latest["content"]["calculations"][0]
    assert calculation["value"] == "25.00" and calculation["authorship"] == "calculation_engine"
    assert calculation["input_status"] == "matched" and calculation["calculation_id"] == "calc-1"
    assert latest["review"] == "draft" and latest["note"] == "Review me" and latest["content"]["summary"] == "My view"
    assert client.post(path + "/calculations/recalculate", headers=headers,
                       json={"parent_version_id": current["version_id"], "calculation_index": 0}).status_code == 409
    zero = deepcopy(latest["content"])
    zero["facts"][1].update(fact(sid, TEXT, "Revenue was 0 USD", "0", "2022", fact_id="b"))
    broken = save(client, headers, path, latest, zero)
    assert broken.status_code == 201, broken.text
    parent = broken.json()["versions"][-1]["version_id"]
    failed = client.post(path + "/calculations/recalculate", headers=headers, json={"parent_version_id": parent, "calculation_index": 0})
    assert failed.status_code == 422
    assert client.get(path, headers=headers).json()["versions"][-1]["version_id"] == parent


def test_legacy_percent_difference_recalculates_as_percentage_points(monkeypatch):
    text = "The rate was 2.8 percent in 2024 and 2.9 percent in 2023."
    client, headers, sid, topic, path = start(monkeypatch, text, "percentage-recalculate")
    first = fact(sid, text, "2.8 percent", "2.8", "2024", fact_id="a", unit="percent", currency=None, label="Rate")
    second = fact(sid, text, "2.9 percent", "2.9", "2023", fact_id="b", unit="percent", currency=None, label="Rate")
    legacy = {**calculate(CalculationRequest(formula="difference", first=numeric(first), second=numeric(second))),
              "formula_version": "1", "unit": "percent", "calculation_id": "calc-legacy"}
    saved = save(client, headers, path, topic["versions"][0], {"facts": [first, second], "calculations": [legacy]}).json()["versions"][-1]
    shown = client.get(path, headers=headers).json()["versions"][-1]
    assert shown["content"]["calculations"][0]["legacy_unit_warning"] == "percentage_difference_requires_recalculation"
    assert saved["content"]["calculations"][0].get("legacy_unit_warning") is None  # Annotated on read, never stored.
    response = client.post(path + "/calculations/recalculate", headers=headers,
        json={"parent_version_id": saved["version_id"], "calculation_index": 0})
    assert response.status_code == 201, response.text
    latest = response.json()["versions"][-1]
    assert latest["content"]["calculations"][0]["unit"] == "percentage points"
    assert latest["content"]["calculations"][0]["formula_version"] == "2"
    assert response.json()["versions"][-2]["content"]["calculations"][0]["unit"] == "percent"
    repeated = client.post(path + "/calculations/recalculate", headers=headers,
        json={"parent_version_id": latest["version_id"], "calculation_index": 0})
    assert repeated.status_code == 201 and repeated.json()["versions"][-1]["content"]["calculations"][0]["formula_version"] == "2"


def test_edited_facts_reject_invalid_values_and_duplicate_identifiers(monkeypatch):
    client, headers, sid, topic, path = start(monkeypatch, TEXT, "fact-validation")
    base = fact(sid, TEXT, "Revenue was 100 USD", "100", "2023", fact_id="a")
    parent = topic["versions"][0]
    for invalid in (dict(base, value="NaN"), dict(base, value="1e31"), dict(base, unit=" "), dict(base, source_id=[]),
                    dict(base, source_id="source_missing"), dict(base, quotation="Revenue was 101 USD"),
                    dict(base, location={"character_start": 0, "character_end": 5}), dict(base, source_id=None, quotation=None)):
        assert save(client, headers, path, parent, {"facts": [invalid]}).status_code == 422
    assert save(client, headers, path, parent, {"facts": [base, dict(base, fact_id=" a ")]}).status_code == 422
    valid = save(client, headers, path, parent, {"facts": [base]})
    assert valid.status_code == 201, valid.text
    saved = valid.json()["versions"][-1]["content"]["facts"][0]
    assert saved["value"] == "100" and saved["verification"] == "user_edited_not_independently_verified" and saved["turn_id"] is None


def test_account_scoped_research_version_history_and_conflicts(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)

    def account(email):
        response = client.post("/v1/auth/local/register", json={
            "email": email, "password": "correct horse battery staple!"})
        assert response.status_code == 201, response.text
        return {"Authorization": "Bearer " + response.json()["access_token"]}

    first = account("first@example.test")
    second = account("second@example.test")
    assert client.post("/v2/research/topics", json={"question": "Research?"}).status_code == 401
    assert client.post("/v2/research/topics", headers=first, json={}).status_code == 422
    created = client.post("/v2/research/topics", headers=first,
                          json={"question": "Compare revenue"})
    assert created.status_code == 201, created.text
    topic = created.json()
    path = "/v2/research/topics/" + topic["topic_id"]
    assert client.get(path, headers=second).status_code == 404
    assert client.get("/v2/research/topics", headers=second).json() == []
    parent = topic["versions"][0]["version_id"]
    payload = {"parent_version_id": parent, "content": {"summary": "Reviewed"},
               "review": "confirmed"}
    assert client.post(path + "/versions", headers=second, json=payload).status_code == 404
    saved = client.post(path + "/versions", headers=first, json=payload)
    assert saved.status_code == 201, saved.text
    assert len(saved.json()["versions"]) == 2
    assert saved.json()["versions"][0]["content"]["summary"] == ""
    assert client.post(path + "/versions", headers=first, json=payload).status_code == 409
    assert client.get(path, headers=first).json()["versions"][-1]["content"]["summary"] == "Reviewed"
    assert client.delete("/v1/auth/local/account", headers=first).status_code == 200


def test_full_export_discloses_input_precision_for_existing_versions(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    client = TestClient(app)
    auth = client.post('/v1/auth/local/register', json={'email': f'precision-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Compare figures', 'scope': '2024', 'as_of': '2025-01-01T00:00:00Z'}).json()
    path = '/v2/research/topics/' + topic['topic_id'] + '/versions/' + topic['versions'][0]['version_id']
    result = client.get(path + '/export', headers=headers)
    assert result.status_code == 200
    exported = result.json()
    assert exported['request']['scope'] == '2024'
    assert exported['calculation_disclosure'] == 'Calculations use the recorded inputs; their source precision and prior rounding are not established.'
    assert 'calculation_disclosure' not in client.get(path + '/export/metadata', headers=headers).json()


def test_edited_fact_reports_value_missing_from_quotation_and_rejects_untrusted_location(monkeypatch):
    text = "Revenue 100. Revenue 200."
    client, headers, sid, topic, path = start(monkeypatch, text, "fact-source")
    first = save(client, headers, path, topic["versions"][0], {"facts": [fact(sid, text, "Revenue 100", "100", "2024", fact_id="revenue")]})
    assert first.status_code == 201, first.text
    old = first.json()["versions"][-1]
    changed = deepcopy(old["content"])
    changed["facts"][0]["value"] = "200"
    second = save(client, headers, path, old, changed)
    assert second.status_code == 201, second.text
    current = second.json()["versions"][-1]
    assert current["content"]["facts"][0]["verification"] == "user_edited_value_not_in_quotation"
    assert second.json()["versions"][-2]["content"]["facts"][0]["verification"] == "user_edited_not_independently_verified"
    corrected = deepcopy(current["content"])
    corrected["facts"][0].update(fact(sid, text, "Revenue 200", "200", "2024", fact_id="revenue"))
    third = save(client, headers, path, current, corrected)
    assert third.status_code == 201, third.text
    saved = third.json()["versions"][-1]["content"]["facts"][0]
    assert saved["verification"] == "user_edited_not_independently_verified" and saved["location"] == {"character_start": 13, "character_end": 24}
    inconsistent = deepcopy(third.json()["versions"][-1]["content"])
    inconsistent["facts"][0]["location"] = {"character_start": 0, "character_end": 11}
    assert save(client, headers, path, third.json()["versions"][-1], inconsistent).status_code == 422


def test_edited_monetary_fact_keeps_currency_cue_computed_from_its_quotation(monkeypatch):
    text = "| Revenue | $ | 100 |\n| Revenue | USD 200 |"
    client, headers, sid, topic, path = start(monkeypatch, text, "currency-fragment")
    original = fact(sid, text, "Revenue | $ | 100", "100", "2024", fact_id="revenue", unit="million")
    first = save(client, headers, path, topic["versions"][0], {"facts": [original]})
    assert first.status_code == 201, first.text
    saved = first.json()["versions"][-1]
    assert saved["content"]["facts"][0]["currency_quote_missing"] is True
    edited = deepcopy(saved["content"])
    edited["facts"][0].update(period="2023", currency_quote_missing=False)
    second = save(client, headers, path, saved, edited)
    assert second.status_code == 201, second.text
    current = second.json()["versions"][-1]
    assert current["content"]["facts"][0]["currency_quote_missing"] is True
    assert client.get(path, headers=headers).json()["versions"][-1]["content"]["facts"][0]["currency_quote_missing"] is True
    corrected = deepcopy(current["content"])
    corrected["facts"][0].update(fact(sid, text, "Revenue | USD 200", "200", "2023", fact_id="revenue", unit="million"))
    third = save(client, headers, path, current, corrected)
    assert third.status_code == 201, third.text
    assert not third.json()["versions"][-1]["content"]["facts"][0].get("currency_quote_missing")


def test_create_calculation_from_saved_facts_validates_inputs_and_preserves_history(monkeypatch):
    text = "Old revenue 100 USD million. New revenue 125 USD million. Zero 0 USD million. Other 5 EUR million."
    client, headers, sid, topic, path = start(monkeypatch, text, "create-calc")
    facts = [fact(sid, text, "Old revenue 100 USD million", "100", "2023", fact_id="old", unit="USD million", label="Old revenue"),
             fact(sid, text, "New revenue 125 USD million", "125", "2024", fact_id="new", unit="USD million", label="New revenue"),
             fact(sid, text, "Zero 0 USD million", "0", "2022", fact_id="zero", unit="USD million", label="Zero"),
             fact(sid, text, "Other 5 EUR million", "5", "2024", fact_id="other", unit="EUR million", currency="EUR", label="Other")]
    saved = save(client, headers, path, topic["versions"][0], {"facts": facts})
    assert saved.status_code == 201, saved.text
    parent = saved.json()["versions"][-1]["version_id"]
    spec = {"parent_version_id": parent, "formula": "growth_percent", "first_fact_id": "new", "second_fact_id": "old"}
    for invalid in ({**spec, "first_fact_id": "missing"}, {**spec, "second_fact_id": "new"}, {**spec, "second_fact_id": "zero"},
                    {**spec, "second_fact_id": "other"}, {**spec, "formula": "unknown"}):
        assert client.post(path + "/calculations", headers=headers, json=invalid).status_code == 422
    assert client.get(path, headers=headers).json()["versions"][-1]["version_id"] == parent
    result = client.post(path + "/calculations", headers=headers, json=spec)
    assert result.status_code == 201, result.text
    previous, current = result.json()["versions"][-2:]
    assert not previous["content"]["calculations"]
    calc = current["content"]["calculations"][0]
    assert calc["value"] == "25.00" and calc["unit"] == "percent" and calc["input_status"] == "matched"
    assert calc["calculation_id"].startswith("calc-") and calc["turn_id"] is None and calc["result_status"] == "matched"
    assert [x["fact_id"] for x in calc["inputs"]] == ["new", "old"]
    assert calc["authorship"] == "calculation_engine" and current["review"] == "draft"
    assert client.post(path + "/calculations", headers=headers, json=spec).status_code == 409


def test_fact_with_unmatched_value_blocks_calculation_until_corrected(monkeypatch):
    text = "Revenue USD 100. Revenue USD 200. Base 50 USD."
    client, headers, sid, topic, path = start(monkeypatch, text, "retarget")
    revenue = fact(sid, text, "Revenue USD 100", "200", "2024", fact_id="revenue")
    base = fact(sid, text, "Base 50 USD", "50", "2024", fact_id="base", label="Base")
    saved = save(client, headers, path, topic["versions"][0], {"facts": [revenue, base]})
    assert saved.status_code == 201, saved.text
    old = saved.json()["versions"][-1]
    assert old["content"]["facts"][0]["verification"] == "user_edited_value_not_in_quotation"
    request = {"parent_version_id": old["version_id"], "formula": "difference", "first_fact_id": "revenue", "second_fact_id": "base"}
    assert client.post(path + "/calculations", headers=headers, json=request).status_code == 422
    corrected = deepcopy(old["content"])
    corrected["facts"][0].update(fact(sid, text, "Revenue USD 200", "200", "2024", fact_id="revenue"))
    updated = save(client, headers, path, old, corrected)
    assert updated.status_code == 201, updated.text
    current = updated.json()["versions"][-1]
    assert current["content"]["facts"][0]["location"] == {"character_start": 17, "character_end": 32}
    request["parent_version_id"] = current["version_id"]
    calculation = client.post(path + "/calculations", headers=headers, json=request)
    assert calculation.status_code == 201, calculation.text
    latest = calculation.json()["versions"][-1]
    assert latest["content"]["calculations"][0]["value"] == "150.00"
    # Spoiling the quotation again makes the saved calculation unverifiable and unrepeatable.
    spoiled = deepcopy(latest["content"])
    spoiled["facts"][0].update(fact(sid, text, "Revenue USD 100", "200", "2024", fact_id="revenue"))
    uncertain = save(client, headers, path, latest, spoiled).json()["versions"][-1]
    assert uncertain["content"]["calculations"][0]["input_status"] == "unverifiable"
    assert client.post(path + "/calculations/recalculate", headers=headers,
                       json={"parent_version_id": uncertain["version_id"], "calculation_index": 0}).status_code == 422


def test_topic_list_queries_only_current_owner_and_ignores_other_owners_records():
    from app.research_topics import ResearchInput, ResearchTopics

    class OwnerScopedRepository:
        def __init__(self):
            self.items_called = False
            self.owners_queried: list[str] = []
            self.store = {}

        def put(self, key, value, *, expected_version=None):
            self.store[key] = value
            return 1

        def items(self):
            self.items_called = True
            raise AssertionError("ResearchTopics.list must not scan all owners when items_for_owner is available")

        def items_for_owner(self, owner_id: str):
            self.owners_queried.append(owner_id)
            return ((key, value) for key, value in self.store.items() if value.owner_id == owner_id)

    repo = OwnerScopedRepository()
    topics = ResearchTopics(repo)
    topic_a = topics.create("owner-a", ResearchInput(question="Owner A question"))
    repo.store["corrupted-b"] = type("BadTopic", (), {"owner_id": "owner-b"})()

    listed = topics.list("owner-a")
    assert [item.topic_id for item in listed] == [topic_a.topic_id]
    assert repo.items_called is False
    assert repo.owners_queried == ["owner-a"]


def test_research_calculation_schema_enforces_formula_arity_and_optional_fields():
    sample_inputs = [
        {"fact_id": f"f{i}", "value": str(10 + i), "unit": "USD millions", "currency": "USD", "period": f"2025-{i}"}
        for i in range(51)
    ]
    base_payload = {
        "calculation_id": "calc-schema",
        "formula_version": "1",
        "expression": "x",
        "input_verification": "not_verified_by_calculator",
        "input_precision_status": "not_established",
        "value": "10.00",
        "unit": "USD millions",
        "currency": "USD",
        "rounding": "half_up",
        "decimal_places": 2,
    }
    for formula in sorted(BINARY_FORMULAS):
        model = ResearchCalculation.model_validate({**base_payload, "formula_id": formula, "inputs": sample_inputs[:2]})
        assert len(model.inputs) == 2
        with pytest.raises(ValidationError):
            ResearchCalculation.model_validate({**base_payload, "formula_id": formula, "inputs": sample_inputs[:3]})

    for formula in sorted(MULTI_FACT_FORMULAS):
        model_two = ResearchCalculation.model_validate({**base_payload, "formula_id": formula, "inputs": sample_inputs[:2]})
        assert len(model_two.inputs) == 2
        model_fifty = ResearchCalculation.model_validate({**base_payload, "formula_id": formula, "inputs": sample_inputs[:50]})
        assert len(model_fifty.inputs) == 50
        with pytest.raises(ValidationError):
            ResearchCalculation.model_validate({**base_payload, "formula_id": formula, "inputs": sample_inputs[:1]})
        with pytest.raises(ValidationError):
            ResearchCalculation.model_validate({**base_payload, "formula_id": formula, "inputs": sample_inputs})

    with pytest.raises(ValidationError):
        ResearchCalculation.model_validate({**base_payload, "formula_id": "sum", "inputs": sample_inputs[:2], "extra_key": 1})

    with pytest.raises(ValidationError):
        ResearchCalculation.model_validate({
            **base_payload,
            "formula_id": "sum",
            "inputs": sample_inputs[:2],
            "scale_adjustments": [{
                "fact_id": "f0",
                "from_unit": "USD billions",
                "to_unit": "USD millions",
                "from_scale": "billions",
                "to_scale": "millions",
                "multiplier": "1000",
                "original_value": "2",
                "aligned_value": "2000",
                "unexpected": "nope",
            }],
        })


ALL_NINE_TEXT = (
    "FY2022 revenue was USD 100 million. "
    "FY2025 revenue was USD 133.1 million. "
    "FY2025 updated revenue was USD 172.8 million. "
    "FY2025 operating income was USD 40 million. "
    "FY2025 updated operating income was USD 66.55 million. "
    "FY2025 segment A revenue was USD 2 billion. "
    "FY2025 segment B revenue was USD 500 million. "
    "FY2025 segment C revenue was USD 300 million. "
    "FY2025 segment C updated revenue was USD 700 million. "
    "FY2025 EPS was USD 35 per share. "
    "FY2025 updated EPS was USD 42 per share. "
    "FY2025 denominator was USD 2.5 per share. "
    "2025-Q1 revenue was USD 30 billion. "
    "2025-Q2 revenue was USD 31 billion. "
    "2025-Q3 revenue was USD 32 billion. "
    "2025-Q4 revenue was USD 40100 million. "
    "2025-Q4 updated revenue was USD 50100 million. "
    "2025年中国业务收入为CNY 2亿元。"
    "2024年中国业务收入为CNY 10000万元。"
    "2025年更新中国业务收入为CNY 3亿元。"
)


def _roundtrip_case(case_name: str):
    if case_name == "growth_percent":
        return (
            "growth_percent",
            [
                ("f_curr", "FY2025 revenue was USD 133.1 million", "133.1", "FY2025", "USD millions", "USD", "Revenue"),
                ("f_base", "FY2022 revenue was USD 100 million", "100", "FY2022", "USD millions", "USD", "Revenue"),
            ],
            {},
            "33.10",
            ("f_curr", "FY2025 updated revenue was USD 172.8 million", "172.8"),
            "72.80",
            None,
        )
    if case_name == "growth_percent_scale_adjusted":
        return (
            "growth_percent",
            [
                ("f_zh_curr", "2025年中国业务收入为CNY 2亿元", "2", "FY2025", "亿元", "CNY", "营业收入"),
                ("f_zh_base", "2024年中国业务收入为CNY 10000万元", "10000", "FY2024", "万元", "CNY", "营业收入"),
            ],
            {},
            "100.00",
            ("f_zh_curr", "2025年更新中国业务收入为CNY 3亿元", "3"),
            "200.00",
            1,
        )
    if case_name == "margin_percent":
        return (
            "margin_percent",
            [
                ("f_opinc", "FY2025 operating income was USD 40 million", "40", "FY2025", "USD millions", "USD", "Operating income"),
                ("f_rev", "FY2025 revenue was USD 133.1 million", "133.1", "FY2025", "USD millions", "USD", "Revenue"),
            ],
            {},
            "30.05",
            ("f_opinc", "FY2025 updated operating income was USD 66.55 million", "66.55"),
            "50.00",
            None,
        )
    if case_name == "difference":
        return (
            "difference",
            [
                ("f_curr", "FY2025 revenue was USD 133.1 million", "133.1", "FY2025", "USD millions", "USD", "Revenue"),
                ("f_base", "FY2022 revenue was USD 100 million", "100", "FY2022", "USD millions", "USD", "Revenue"),
            ],
            {},
            "33.10",
            ("f_curr", "FY2025 updated revenue was USD 172.8 million", "172.8"),
            "72.80",
            None,
        )
    if case_name == "ratio":
        return (
            "ratio",
            [
                ("f_eps", "FY2025 EPS was USD 35 per share", "35", "FY2025", "USD per share", "USD", "EPS"),
                ("f_den", "FY2025 denominator was USD 2.5 per share", "2.5", "FY2025", "USD per share", "USD", "Denominator"),
            ],
            {},
            "14.00",
            ("f_eps", "FY2025 updated EPS was USD 42 per share", "42"),
            "16.80",
            None,
        )
    if case_name == "cagr":
        return (
            "cagr",
            [
                ("f_curr", "FY2025 revenue was USD 133.1 million", "133.1", "FY2025", "USD millions", "USD", "Revenue"),
                ("f_base", "FY2022 revenue was USD 100 million", "100", "FY2022", "USD millions", "USD", "Revenue"),
            ],
            {"periods": Decimal("3")},
            "10.00",
            ("f_curr", "FY2025 updated revenue was USD 172.8 million", "172.8"),
            "20.00",
            None,
        )
    if case_name == "sum_scale_adjusted":
        return (
            "sum",
            [
                ("f_a", "FY2025 segment A revenue was USD 2 billion", "2", "FY2025", "USD billions", "USD", "Segment A"),
                ("f_b", "FY2025 segment B revenue was USD 500 million", "500", "FY2025", "USD millions", "USD", "Segment B"),
                ("f_c", "FY2025 segment C revenue was USD 300 million", "300", "FY2025", "USD millions", "USD", "Segment C"),
            ],
            {},
            "2800.00",
            ("f_c", "FY2025 segment C updated revenue was USD 700 million", "700"),
            "3200.00",
            1,
        )
    if case_name == "subtract":
        return (
            "subtract",
            [
                ("f_b", "FY2025 segment B revenue was USD 500 million", "500", "FY2025", "USD millions", "USD", "Segment B"),
                ("f_c", "FY2025 segment C revenue was USD 300 million", "300", "FY2025", "USD millions", "USD", "Segment C"),
                ("f_base", "FY2022 revenue was USD 100 million", "100", "FY2022", "USD millions", "USD", "Revenue"),
            ],
            {},
            "100.00",
            ("f_c", "FY2025 segment C updated revenue was USD 700 million", "700"),
            "-300.00",
            None,
        )
    if case_name == "weighted_average":
        return (
            "weighted_average",
            [
                ("f_b", "FY2025 segment B revenue was USD 500 million", "500", "FY2025", "USD millions", "USD", "Segment B"),
                ("f_c", "FY2025 segment C revenue was USD 300 million", "300", "FY2025", "USD millions", "USD", "Segment C"),
                ("f_base", "FY2022 revenue was USD 100 million", "100", "FY2022", "USD millions", "USD", "Revenue"),
            ],
            {"weights": [Decimal("1"), Decimal("2"), Decimal("3")]},
            "233.33",
            ("f_c", "FY2025 segment C updated revenue was USD 700 million", "700"),
            "366.67",
            None,
        )
    if case_name == "ttm_sum_scale_adjusted":
        return (
            "ttm_sum",
            [
                ("q1", "2025-Q1 revenue was USD 30 billion", "30", "2025-Q1", "USD billions", "USD", "Q1 Revenue"),
                ("q2", "2025-Q2 revenue was USD 31 billion", "31", "2025-Q2", "USD billions", "USD", "Q2 Revenue"),
                ("q3", "2025-Q3 revenue was USD 32 billion", "32", "2025-Q3", "USD billions", "USD", "Q3 Revenue"),
                ("q4", "2025-Q4 revenue was USD 40100 million", "40100", "2025-Q4", "USD millions", "USD", "Q4 Revenue"),
            ],
            {},
            "133100.00",
            ("q4", "2025-Q4 updated revenue was USD 50100 million", "50100"),
            "143100.00",
            3,
        )
    raise AssertionError(f"Unknown case {case_name}")


@pytest.mark.parametrize("case_name", [
    "growth_percent",
    "growth_percent_scale_adjusted",
    "margin_percent",
    "difference",
    "ratio",
    "cagr",
    "sum_scale_adjusted",
    "subtract",
    "weighted_average",
    "ttm_sum_scale_adjusted",
])
def test_all_nine_formulas_roundtrip_calculate_merge_turn_validate_and_recalculate(monkeypatch, case_name):
    formula, specs, extra_params, expected_initial, update_spec, expected_recalc, expected_adj_count = _roundtrip_case(case_name)
    client, headers, sid, topic, path = start(monkeypatch, ALL_NINE_TEXT, f"roundtrip-{case_name}")

    fact_dicts = [
        fact(sid, ALL_NINE_TEXT, quote, value, period, fact_id=fid, unit=unit, currency=currency, label=label)
        for fid, quote, value, period, unit, currency, label in specs
    ]
    numeric_inputs = [numeric(item) for item in fact_dicts]
    req_payload: dict = {"formula": formula, **extra_params}
    if formula in BINARY_FORMULAS:
        req_payload["first"] = numeric_inputs[0]
        req_payload["second"] = numeric_inputs[1]
    else:
        req_payload["facts"] = numeric_inputs
    calc = calculate(CalculationRequest.model_validate(req_payload))
    assert calc["value"] == expected_initial
    calc["calculation_id"] = f"calc-{case_name}"
    calc["input_status"] = calculation_input_status(calc, fact_dicts)
    assert calc["input_status"] == "matched"

    now = datetime.now(timezone.utc)
    turn = ResearchTurn(
        turn_id="turn-1",
        index=1,
        question=topic["request"]["question"],
        question_origin="user",
        operation_ids=["op-1"],
        parent_version_id=topic["versions"][0]["version_id"],
        started_at=now,
        finished_at=now,
        status="completed",
        reason="answered",
        report="Calculated.",
        fact_ids=[item["fact_id"] for item in fact_dicts],
        calculation_ids=[calc["calculation_id"]],
        usage=TurnUsage(model_calls=1, search_calls=0, total_tokens=10, usage_status="complete"),
        search_status="not_requested",
        official_fetch_status="not_requested",
    )
    merged = merge_turn(
        ResearchContent(),
        turn,
        claims=[],
        facts=fact_dicts,
        calculations=[calc],
        sources=[],
        source_candidates=[],
    )
    assert len(merged.calculations) == 1
    assert merged.calculations[0].formula_id == formula
    if expected_adj_count is None:
        assert merged.calculations[0].scale_adjustments is None
    else:
        assert merged.calculations[0].scale_adjustments is not None
        assert len(merged.calculations[0].scale_adjustments) == expected_adj_count

    stored_topic = api.research_topics.add_version(
        topic["owner_id"],
        topic["topic_id"],
        VersionInput(parent_version_id=topic["versions"][0]["version_id"], content=merged),
        created_by="agent",
    )
    fetched = client.get(path, headers=headers)
    assert fetched.status_code == 200, fetched.text
    validated = ResearchTopic.model_validate(fetched.json())
    saved_calc = validated.versions[-1].content.calculations[0]
    assert saved_calc.result_status == "matched"
    assert saved_calc.input_status == "matched"
    assert saved_calc.periods == calc.get("periods")
    assert saved_calc.weights == calc.get("weights")

    # Update one input fact with a new source-backed quotation and value, then recalculate.
    target_fid, new_quote, new_value = update_spec
    edited_content = deepcopy(fetched.json()["versions"][-1]["content"])
    for entry in edited_content["facts"]:
        if entry["fact_id"] == target_fid:
            pos = ALL_NINE_TEXT.index(new_quote)
            entry.update(
                value=new_value,
                quotation=new_quote,
                character_start=pos,
                character_end=pos + len(new_quote),
                location={"character_start": pos, "character_end": pos + len(new_quote)},
            )
    edited_resp = save(client, headers, path, stored_topic.versions[-1].model_dump(mode="json"), edited_content)
    assert edited_resp.status_code == 201, edited_resp.text
    edited_version = edited_resp.json()["versions"][-1]
    assert edited_version["content"]["calculations"][0]["input_status"] == "changed"

    recalc_resp = client.post(
        path + "/calculations/recalculate",
        headers=headers,
        json={"parent_version_id": edited_version["version_id"], "calculation_index": 0},
    )
    assert recalc_resp.status_code == 201, recalc_resp.text
    recalc_topic = ResearchTopic.model_validate(recalc_resp.json())
    recalc_calc = recalc_topic.versions[-1].content.calculations[0]
    assert recalc_calc.value == expected_recalc
    assert recalc_calc.calculation_id == calc["calculation_id"]
    assert recalc_calc.turn_id == "turn-1"
    assert recalc_calc.authorship == "calculation_engine"
    assert recalc_calc.input_status == "matched"
    assert recalc_calc.result_status == "matched"
    assert recalc_calc.periods == calc.get("periods")
    assert recalc_calc.weights == calc.get("weights")
    if expected_adj_count is None:
        assert recalc_calc.scale_adjustments is None
    else:
        assert recalc_calc.scale_adjustments is not None
        assert len(recalc_calc.scale_adjustments) == expected_adj_count


def test_recalculate_rejects_missing_cagr_periods_or_weighted_average_weights(monkeypatch):
    client, headers, sid, topic, path = start(monkeypatch, ALL_NINE_TEXT, "recalc-missing-params")
    f_curr = fact(sid, ALL_NINE_TEXT, "FY2025 revenue was USD 133.1 million", "133.1", "FY2025", fact_id="f_curr", unit="USD millions", currency="USD")
    f_base = fact(sid, ALL_NINE_TEXT, "FY2022 revenue was USD 100 million", "100", "FY2022", fact_id="f_base", unit="USD millions", currency="USD")
    cagr = calculate(CalculationRequest(formula="cagr", first=numeric(f_curr), second=numeric(f_base), periods=Decimal("3")))
    cagr["calculation_id"] = "calc-cagr-missing"
    cagr_no_periods = {**cagr, "periods": None}
    wavg = calculate(CalculationRequest(formula="weighted_average", facts=[numeric(f_curr), numeric(f_base)], weights=[Decimal("1"), Decimal("3")]))
    wavg["calculation_id"] = "calc-wavg-missing"
    wavg_no_weights = {**wavg, "weights": None}

    saved = save(client, headers, path, topic["versions"][0], {
        "facts": [f_curr, f_base],
        "calculations": [cagr_no_periods, wavg_no_weights],
    })
    assert saved.status_code == 201, saved.text
    version = saved.json()["versions"][-1]
    assert version["content"]["calculations"][0]["result_status"] == "unverifiable"
    assert version["content"]["calculations"][1]["result_status"] == "unverifiable"
    for idx in (0, 1):
        resp = client.post(
            path + "/calculations/recalculate",
            headers=headers,
            json={"parent_version_id": version["version_id"], "calculation_index": idx},
        )
        assert resp.status_code == 422


def test_native_run_persists_cagr_weighted_average_ttm_and_scale_adjustments_through_recalculate(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")

    def _tool_turn(name: str, args: dict, ident: str) -> ModelResponse:
        call = ModelToolCall(ident, name, json.dumps(args))
        return ModelResponse(
            {"role": "assistant", "content": None, "tool_calls": [{"id": ident, "type": "function", "function": {"name": name, "arguments": call.arguments}}]},
            (call,),
            ModelUsage(1, 1, 2),
            "tool_calls",
        )

    class Provider:
        name = "p0-roundtrip-native"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=2000)

        def __init__(self):
            self.calls = 0
            self.fact_ids: list[str] = []

        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _tool_turn("read_sources", {"reads": [{"source_id": sid}], "rationale": "read"}, "c1")
            if self.calls == 2:
                return _tool_turn(
                    "record_facts",
                    {
                        "rationale": "record facts",
                        "facts": [
                            {"label": "FY2022 Revenue", "value": 100, "unit": "USD millions", "currency": "USD", "period": "FY2022", "source_id": sid, "quotation": "FY2022 revenue was USD 100 million"},
                            {"label": "FY2025 Revenue", "value": 133.1, "unit": "USD millions", "currency": "USD", "period": "FY2025", "source_id": sid, "quotation": "FY2025 revenue was USD 133.1 million"},
                            {"label": "Q1 Revenue", "value": 30, "unit": "USD billions", "currency": "USD", "period": "2025-Q1", "source_id": sid, "quotation": "2025-Q1 revenue was USD 30 billion"},
                            {"label": "Q2 Revenue", "value": 31, "unit": "USD billions", "currency": "USD", "period": "2025-Q2", "source_id": sid, "quotation": "2025-Q2 revenue was USD 31 billion"},
                            {"label": "Q3 Revenue", "value": 32, "unit": "USD billions", "currency": "USD", "period": "2025-Q3", "source_id": sid, "quotation": "2025-Q3 revenue was USD 32 billion"},
                            {"label": "Q4 Revenue", "value": 40100, "unit": "USD millions", "currency": "USD", "period": "2025-Q4", "source_id": sid, "quotation": "2025-Q4 revenue was USD 40100 million"},
                        ],
                    },
                    "c2",
                )
            if self.calls == 3:
                tool_msgs = [m for m in messages if m.get("role") == "tool"]
                rec = json.loads(tool_msgs[-1]["content"])
                self.fact_ids = [item["fact_id"] for item in rec["facts"]]
                return _tool_turn(
                    "calculate_financials",
                    {
                        "rationale": "compute cagr, weighted average, and ttm_sum",
                        "calculations": [
                            {"formula": "cagr", "first_fact_id": self.fact_ids[1], "second_fact_id": self.fact_ids[0], "periods": 3},
                            {"formula": "weighted_average", "fact_ids": self.fact_ids[2:4], "weights": [1, 3]},
                            {"formula": "ttm_sum", "fact_ids": self.fact_ids[2:6]},
                        ],
                    },
                    "c3",
                )
            return _tool_turn(
                "submit_research",
                {
                    "report": "Computed 3Y CAGR, weighted average, and TTM revenue.",
                    "claims": [
                        {
                            "text": "FY2025 revenue was USD 133.1 million.",
                            "source_id": sid,
                            "quotation": "FY2025 revenue was USD 133.1 million",
                            "role": "support",
                        }
                    ],
                    "limitations": [],
                    "stopping_reason": "answered",
                },
                "c4",
            )

    monkeypatch.setattr(api, "challenge_provider", Provider())
    client, headers, sid, topic, path = start(monkeypatch, ALL_NINE_TEXT, "native-run-p0")
    run_resp = client.post(
        path + "/runs",
        headers={**headers, "Idempotency-Key": uuid4().hex},
        json={"parent_version_id": topic["versions"][0]["version_id"], "discover_sources": False},
    )
    assert run_resp.status_code == 202, run_resp.text
    op_id = run_resp.json()["operation"]["operation_id"]
    import time
    status = None
    for _ in range(200):
        status = client.get(path + "/runs/" + op_id, headers=headers).json()["operation"]["status"]
        if status in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.01)
    assert status == "completed"

    topic_payload = client.get(path, headers=headers).json()
    validated = ResearchTopic.model_validate(topic_payload)
    calcs = validated.versions[-1].content.calculations
    assert [c.formula_id for c in calcs] == ["cagr", "weighted_average", "ttm_sum"]
    assert calcs[0].periods == "3" and calcs[0].value == "10.00" and calcs[0].result_status == "matched"
    assert calcs[1].weights == ["1", "3"] and calcs[1].value == "30.75" and calcs[1].result_status == "matched"
    assert calcs[2].value == "133100.00" and calcs[2].result_status == "matched"
    assert calcs[2].scale_adjustments is not None and len(calcs[2].scale_adjustments) == 3

    # Recalculate each calculation through /calculations/recalculate.
    current_vid = validated.versions[-1].version_id
    for idx in range(3):
        recalc = client.post(
            path + "/calculations/recalculate",
            headers=headers,
            json={"parent_version_id": current_vid, "calculation_index": idx},
        )
        assert recalc.status_code == 201, recalc.text
        recalc_topic = ResearchTopic.model_validate(recalc.json())
        current_vid = recalc_topic.versions[-1].version_id
        assert recalc_topic.versions[-1].content.calculations[idx].result_status == "matched"

