from copy import deepcopy
from fastapi.testclient import TestClient

from app.main import app
from app.research_topics import ResearchVersion
from app.research_calculations import CalculationRequest, calculate
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
