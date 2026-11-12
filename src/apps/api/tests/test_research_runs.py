import time
import threading
from copy import deepcopy
from threading import Event
from datetime import date, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import app.main as api
from app.glm_mcp_search import SearchCandidate
from app.research_topics import ResearchContent
from app.research_analysis import AnalysisInterrupted
from app.research_verification import REVIEW_SYSTEM_PROMPT


@pytest.mark.parametrize("required,bad_citation,expected", [
    (True, False, "partial"),
    (False, False, "completed"),
    (False, True, "partial"),
])
def test_counterargument_requirement_can_be_disabled_for_fact_checks(monkeypatch, required, bad_citation, expected):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""

    class Provider:
        name = "fact-check-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            quotation = "invented" if bad_citation else "Revenue was 100"
            return {"claims": [{"text": "Revenue was 100", "citations": [
                {"source_id": source_id, "quotation": quotation}]}], "counterarguments": []}, 10

    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    account = client.post("/v1/auth/local/register", json={"email": f"facts-{required}-{bad_citation}@example.test",
        "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + account["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report",
        "text": "Revenue was 100.", "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    payload = {"parent_version_id": topic["versions"][0]["version_id"], "require_counterarguments": required}
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "fact-check-requirement"}, json=payload)
    assert receipt.status_code == 202
    op = receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(0.02)
    assert state == "completed"
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert content["run"]["status"] == expected
    assert content["run"]["require_counterarguments"] is required
    assert any("No counterarguments were provided" in item for item in content["limitations"])
    snapshot = client.get(path + "/runs/" + op + "/inputs", headers=headers).json()
    assert snapshot["budget"].get("require_counterarguments", True) is required
    # Old requests and explicit default true have identical idempotency payloads.
    if required:
        replay = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "fact-check-requirement"},
                             json={"parent_version_id": payload["parent_version_id"]})
        assert replay.status_code == 202 and replay.headers["Idempotency-Replayed"] == "true"


def test_research_run_commits_evidence_once_and_replays(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    calls = []

    class Provider:
        name = "test-provider"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)

        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            calls.append(prompt)
            assert "untrusted data" in system
            assert 0 < timeout_seconds <= 5
            return {"claims": [{"text": "Revenue is reported", "citations": [
                {"source_id": source_id, "quotation": "Revenue was 100"}]}],
                "counterarguments": [{"text": "Revenue alone does not establish profit growth", "citations": [
                    {"source_id": source_id, "quotation": "Revenue was 100"}]}],
                "limitations": [], "facts": [
                    {"fact_id": "current", "label": "Revenue", "value": "100", "unit": "USD million", "currency": "USD", "period": "2023", "citation": {"source_id": source_id, "quotation": "Revenue was 100 million in 2023."}},
                    {"fact_id": "prior", "label": "Revenue", "value": "80", "unit": "USD million", "currency": "USD", "period": "2022", "citation": {"source_id": source_id, "quotation": "Revenue was 80 million in 2022."}}],
                "calculations": [{"formula": "growth_percent", "first_fact_id": "current", "second_fact_id": "prior"}]}, 55

    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email": "a@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report",
        "text": "Revenue was 100 million in 2023. Revenue was 80 million in 2022.",
        "event_at": "2023-12-31T00:00:00Z", "published_at": "2024-01-01T00:00:00Z",
        "revised_at": "2024-02-01T00:00:00Z", "permissions": {"store": True,
        "model_input": True, "display": True}}).json()
    source_id = source["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={
        "question": "What is revenue?"}).json()
    topic_path = "/v2/research/topics/" + topic["topic_id"]
    attached = client.post(topic_path + "/sources", headers=headers, json={
        "parent_version_id": topic["versions"][-1]["version_id"], "source_ids": [source_id]})
    assert attached.status_code == 201
    topic = attached.json()
    run_path = topic_path + "/runs"
    payload = {"parent_version_id": topic["versions"][-1]["version_id"]}
    request_headers = {**headers, "Idempotency-Key": "research-request-0001"}
    started = client.post(run_path, headers=request_headers, json=payload)
    assert started.status_code == 202, started.text
    operation_id = started.json()["operation"]["operation_id"]
    inputs_path = run_path + "/" + operation_id + "/inputs"
    inputs = client.get(inputs_path, headers=headers)
    assert inputs.status_code == 200
    snapshot = inputs.json()
    assert snapshot["parent_version_id"] == payload["parent_version_id"]
    assert snapshot["sources"][0]["source_id"] == source_id
    assert snapshot["sources"][0]["permissions"]["model_input"] is True
    assert snapshot["sources"][0]["event_at"].startswith("2023-12-31")
    assert snapshot["sources"][0]["published_at"].startswith("2024-01-01")
    assert snapshot["sources"][0]["revised_at"].startswith("2024-02-01")
    assert snapshot["sources"][0]["obtained_at"]
    assert snapshot["budget"]["max_model_calls"] == 1
    assert snapshot["selection_method"] == "ranked_nonoverlapping_windows_v34"
    assert "Revenue was 100" not in inputs.text and "owner_id" not in inputs.json()
    other = client.post("/v1/auth/local/register", json={"email": "other@example.test",
        "password": "correct horse battery staple!"}).json()["access_token"]
    assert client.get(inputs_path, headers={"Authorization": "Bearer " + other}).status_code == 404
    for _ in range(100):
        state = client.get(run_path + "/" + operation_id, headers=headers)
        if state.json()["operation"]["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert state.json()["operation"]["status"] == "completed", state.text
    updated = client.get(topic_path, headers=headers).json()
    assert len(updated["versions"]) == 3
    assert [v["created_by"] for v in updated["versions"]] == ["user", "user", "agent"]
    result = updated["versions"][-1]["content"]
    assert result["run"]["status"] == "completed"
    assert result["run"]["cost_status"] == "unknown"
    assert result["run"]["selection_method"] == snapshot["selection_method"]
    assert result["sources"][0]["event_at"] == snapshot["sources"][0]["event_at"]
    assert result["sources"][0]["revised_at"] == snapshot["sources"][0]["revised_at"]
    assert result["claims"][0]["citations"][0]["quotation"] == "Revenue was 100"
    assert result["calculations"][0]["value"] == "25.00"
    assert result["facts"][0]["verification"] == "model_extraction_not_independently_verified"
    assert updated["versions"][-1]["source_ids"] == [source_id]
    replay = client.post(run_path, headers=request_headers, json=payload)
    assert replay.status_code == 202, replay.text
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert len(calls) == 1
    assert client.get(inputs_path, headers=headers).json() == snapshot
    runs = client.get(run_path, headers=headers)
    assert runs.status_code == 200
    assert runs.json()[0]["operation"]["operation_id"] == operation_id

    # A later user edit cannot erase source permissions from version lineage.
    edited = deepcopy(result)
    edited["summary"] = "Edited"
    edited["sources"] = []
    edited["run"] = {"status": "fabricated"}
    edited["claims"][0]["text"] = "User changed the finding"
    edited["claims"][0]["support_status"] = "verified"
    edited["facts"][0]["value"] = "999"
    edited["calculations"][0]["value"] = "999"
    scrubbed = client.post(topic_path + "/versions", headers=headers, json={
        "parent_version_id": updated["versions"][-1]["version_id"], "content": edited,
        "review": "confirmed", "note": "My retained reservation"})
    assert scrubbed.status_code == 201
    version = scrubbed.json()["versions"][-1]
    assert version["created_by"] == "user"
    assert version["content"]["run"] == result["run"]
    assert version["content"]["sources"] == result["sources"]
    assert version["content"]["claims"][0]["authorship"] == "user"
    assert version["content"]["claims"][0]["support_status"] == "not_independently_verified"
    assert version["content"]["facts"][0]["verification"] == "user_edited_not_independently_verified"
    assert version["content"]["calculations"][0]["authorship"] == "user"
    assert version["content"]["calculations"][0]["input_status"] == "changed"
    assert "authorship" not in version["content"]["claims"][1]
    assert version["source_ids"] == [source_id]
    export_path = topic_path + "/versions/" + version["version_id"] + "/export"
    assert client.get(export_path, headers=headers).status_code == 403

    # When pricing is unknown, an explicit currency cap prevents the call.
    new_parent = version["version_id"]
    capped = client.post(run_path, headers={**headers, "Idempotency-Key": "research-request-0002"},
                         json={"parent_version_id": new_parent, "max_cost_usd": "0.10"})
    assert capped.status_code == 202
    second_id = capped.json()["operation"]["operation_id"]
    for _ in range(100):
        second = client.get(run_path + "/" + second_id, headers=headers)
        if second.json()["operation"]["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert second.json()["operation"]["status"] == "completed"
    capped_result = client.get(topic_path, headers=headers).json()["versions"][-1]["content"]
    capped_version = client.get(topic_path, headers=headers).json()["versions"][-1]
    assert capped_version["review"] == "draft"
    assert capped_result["summary"] == "Edited"
    assert capped_version["note"] == "My retained reservation"
    assert capped_version["parent_version_id"] == version["version_id"]
    assert capped_result["run"]["status"] == "partial"
    assert capped_result["run"]["error"] == "provider_budget_unknown"
    assert capped_result["run"]["model_calls"] == 0
    assert len(calls) == 1

    latest = client.get(topic_path, headers=headers).json()["versions"][-1]["version_id"]
    token_limited = client.post(run_path, headers={**headers, "Idempotency-Key": "research-token-budget"},
        json={"parent_version_id": latest, "max_total_tokens": 100})
    token_id = token_limited.json()["operation"]["operation_id"]
    for _ in range(100):
        token_status = client.get(run_path + "/" + token_id, headers=headers).json()["operation"]["status"]
        if token_status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert token_status == "completed"
    limited_result = client.get(topic_path, headers=headers).json()["versions"][-1]["content"]
    assert limited_result["run"]["error"] == "provider_budget_exhausted"
    assert limited_result["run"]["model_calls"] == 0 and len(calls) == 1
    assert client.delete("/v1/auth/local/account", headers=headers).status_code == 200
    assert api.RESEARCH_RUN_INPUTS.get(operation_id) is None


def test_cancelled_run_does_not_save_late_model_output(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    entered, release = Event(), Event()

    class SlowProvider:
        name = "slow-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            entered.set()
            assert release.wait(5)
            return {"claims": [], "counterarguments": [], "limitations": []}, 10

    monkeypatch.setattr(api, "challenge_provider", SlowProvider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email": "cancel@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report",
        "text": "Revenue was 100.", "permissions": {"store": True, "model_input": True, "display": True}}).json()
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?",
        "source_ids": [source["source_id"]]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    started = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "research-cancel-1"},
                          json={"parent_version_id": topic["versions"][0]["version_id"]}).json()["operation"]
    assert entered.wait(5)
    restored = client.get(path + "/runs", headers=headers).json()
    assert restored[0]["operation"]["operation_id"] == started["operation_id"]
    stopped = client.post("/v1/operations/" + started["operation_id"] + "/cancel",
        headers={**headers, "Idempotency-Key": "cancel-research-1"}, json={"cancel_token": started["cancel_token"]})
    assert stopped.status_code == 200
    assert stopped.json()["operation"]["status"] == "cancelled"
    release.set()
    time.sleep(0.1)
    assert len(client.get(path, headers=headers).json()["versions"]) == 1


def test_cancelling_support_review_keeps_completed_generation_as_draft(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    entered, release = Event(), Event()
    source_id = ""
    class Provider:
        name = "review-cancellation-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            if system == REVIEW_SYSTEM_PROMPT:
                entered.set()
                assert release.wait(5)
                return {"reviews": [{"claim_index": 0, "verdict": "supports"}]}, 10
            citation = {"source_id": source_id, "quotation": "Revenue was 100"}
            return {"claims": [{"text": "Revenue was 100", "citations": [citation]}],
                    "counterarguments": [], "facts": [], "calculations": [], "limitations": []}, 55
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email": "review-cancel@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()
    source_id = source["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    started = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "review-cancel-run"}, json={
        "parent_version_id": topic["versions"][0]["version_id"], "support_review": True,
        "max_model_calls": 2, "max_total_tokens": 9000}).json()["operation"]
    assert entered.wait(5)
    versions = client.get(path, headers=headers).json()["versions"]
    assert len(versions) == 2 and versions[-1]["review"] == "draft"
    checkpoint = versions[-1]["content"]
    assert checkpoint["run"]["checkpoint"] == "generation" and checkpoint["run"]["status"] == "partial"
    assert checkpoint["run"]["cost_status"] == "unknown" and checkpoint["run"]["generation_tokens"] == 55
    assert checkpoint["run"]["max_model_calls"] == 2 and checkpoint["run"]["source_hashes"][source_id] == source["content_sha256"]
    assert checkpoint["claims"][0]["citations"][0]["quotation"] == "Revenue was 100"
    stopped = client.post("/v1/operations/" + started["operation_id"] + "/cancel",
        headers={**headers, "Idempotency-Key": "cancel-review-run"}, json={"cancel_token": started["cancel_token"]})
    assert stopped.status_code == 200 and stopped.json()["operation"]["status"] == "cancelled"
    release.set()
    time.sleep(0.1)
    assert client.get(path, headers=headers).json()["versions"] == versions
    assert client.delete("/v1/auth/local/account", headers=headers).status_code == 200


def test_selection_timeout_produces_partial_result_without_calling_model(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.setattr(api, "analyze", lambda *args, **kwargs: (_ for _ in ()).throw(AnalysisInterrupted()))
    client = TestClient(api.app)
    registration = client.post("/v1/auth/local/register", json={"email": "selection-timeout@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Data",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Data?", "source_ids": [source["source_id"]]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    started = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "selection-timeout-1"},
        json={"parent_version_id": topic["versions"][0]["version_id"], "max_elapsed_seconds": 1}).json()
    operation_id = started["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + operation_id, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert content["run"]["status"] == "partial" and content["run"]["model_calls"] == 0
    assert content["run"]["error"] == "provider_budget_exhausted"
    assert content["claims"] == []


@pytest.mark.parametrize("budget,second_verdict,first_usage,output_limit,expected_calls,expected_review", [
    (9000, "contradicts", 55, 1500, 2, "complete"),
    (9000, "supports", 55, 1500, 2, "complete"),
    (4500, "supports", 55, 1500, 1, "budget_exhausted"),
    (5000, "supports", 55, 1500, 2, "complete"),
    (5000, "supports", 1000, 1500, 1, "budget_exhausted"),
    (5000, "supports", None, 1500, 1, "budget_exhausted"),
    (5000, "supports", -1, 1500, 1, "budget_exhausted"),
    (9000, "supports", None, 1500, 2, "complete"),
    (9000, "invalid", 55, 1500, 2, "failed"),
    (9000, "provider_failure", 55, 1500, 2, "failed"),
    (10000, "supports", 3000, 4000, 2, "complete"),
    (10000, "supports", 3001, 4000, 1, "budget_exhausted"),
])
def test_optional_support_review_is_bounded_and_preserves_initial_research(monkeypatch, budget, second_verdict, first_usage, output_limit, expected_calls, expected_review):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    calls = []
    source_id = ""
    class Provider:
        name = "review-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=output_limit)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            calls.append(system)
            if system == REVIEW_SYSTEM_PROMPT:
                assert "Revenue was 100" in prompt
                if second_verdict == "provider_failure":
                    from app.phase3 import ProviderFailure
                    raise ProviderFailure("invalid_provider_output", "Invalid output.", retryable=False, reported_tokens=12)
                return {"reviews": [{"claim_index": 0, "verdict": "supports"},
                                    {"claim_index": 1, "verdict": second_verdict}]}, 12
            citation = {"source_id": source_id, "quotation": "Revenue was 100"}
            return {"claims": [{"text": "Revenue was 100", "citations": [citation]}],
                    "counterarguments": [{"text": "Revenue does not establish profit", "citations": [citation]}],
                    "limitations": [], "facts": [], "calculations": []}, first_usage
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    registration = client.post("/v1/auth/local/register", json={"email": f"review-{budget}-{second_verdict}-{first_usage}-{output_limit}@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()
    source_id = source["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    response = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "support-review-test-1"},
        json={"parent_version_id": topic["versions"][0]["version_id"], "max_model_calls": 2,
              "max_total_tokens": budget, "support_review": True})
    assert response.status_code == 202
    op = response.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    versions = client.get(path, headers=headers).json()["versions"]
    current = versions[-1]
    content, run = current["content"], current["content"]["run"]
    assert len(calls) == expected_calls == run["model_calls"]
    assert run["support_review_status"] == expected_review
    assert len(content["claims"]) == 2
    if expected_review == "budget_exhausted":
        assert run["support_review_omissions"] == [{"claim_index": 0, "reason": "token_budget"}, {"claim_index": 1, "reason": "token_budget"}]
    else:
        assert run["support_review_omissions"] == []
    assert run["support_review_failed_indexes"] == ([0, 1] if expected_review == "failed" else [])
    first_entry = api.billing_ledger.usage.get("model:" + op)
    accounted_first = first_usage if type(first_usage) is int and first_usage >= 0 else None
    assert first_entry.status == "settled" and first_entry.actual_tokens == (accounted_first if accounted_first is not None else 3000 + output_limit)
    second_entry = api.billing_ledger.usage.get("model:" + op + ":support-review")
    if expected_calls == 2:
        assert len(versions) == 3 and versions[-2]["content"]["run"]["checkpoint"] == "generation"
        assert current["parent_version_id"] == versions[-2]["version_id"]
        assert second_entry.status == "settled" and second_entry.actual_tokens == 12
        assert run["total_tokens"] == (accounted_first + 12 if accounted_first is not None else None)
        assert len(run["support_review_prompt_sha256"]) == 64
    else:
        assert len(versions) == 2
        assert second_entry is None and run["total_tokens"] == accounted_first
    if expected_review == "complete":
        assert content["claims"][1]["support_review"]["verdict"] == second_verdict
        assert content["claims"][1]["support_review"]["independently_verified"] is False
        edited = deepcopy(content)
        edited["claims"][0]["text"] = "Modified conclusion"
        saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": edited})
        assert saved.status_code == 201
        assert "support_review" not in saved.json()["versions"][-1]["content"]["claims"][0]
    else:
        assert all("support_review" not in claim for claim in content["claims"])


def test_invalid_model_output_retains_prompt_provenance_without_output(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    class Provider:
        name = "invalid-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            return {"claims": "sensitive model output"}, 25
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email": "invalid-provenance@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()
    excluded = client.post("/v2/research/sources", headers=headers, json={"title": "Not authorized", "text": "Private source.",
        "permissions": {"store": True, "model_input": False, "display": True}}).json()
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source["source_id"], excluded["source_id"]]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    response = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "invalid-provenance-test"},
                           json={"parent_version_id": topic["versions"][0]["version_id"]})
    operation = response.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + operation, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    run = content["run"]
    assert run["error"] == "invalid_model_output"
    assert run["analysis_error_detail"] == "Model output did not match the research schema."
    assert len(run["prompt_sha256"]) == 64 and run["selected_segments"][0]["source_id"] == source["source_id"]
    assert run["status"] == "partial" and run["model_calls"] == 1
    assert [entry["source_id"] for entry in content["sources"]] == [source["source_id"]]
    assert content["sources"][0]["content_sha256"] == source["content_sha256"]
    assert any("processing permission is not granted" in note for note in content["limitations"])
    assert any("selected for analysis" in note for note in content["limitations"])
    assert "sensitive model output" not in str(content)


def test_unlocatable_quote_preserves_valid_work_as_partial(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "partial-citation-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def cite(quote): return {"source_id": source_id, "quotation": quote}
            return {"claims": [{"text": "Revenue is 100", "citations": [cite("Revenue 100")]},
                               {"text": "Unsupported assertion", "citations": [cite("invented quotation")]}],
                    "counterarguments": [{"text": "Revenue is not profit", "citations": [cite("Revenue 100")]}],
                    "facts": [{"fact_id": "r", "label": "Revenue", "value": "100", "unit": "USD", "period": "2023",
                               "citation": cite("Revenue 100")}], "calculations": [], "limitations": []}, 20
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email": "partial-quotation@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue 100",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "partial-citation-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"]}).json()
    operation_id = receipt["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(path + "/runs/" + operation_id, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(0.02)
    assert state == "completed"
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert content["run"]["status"] == "partial" and len(content["facts"]) == 1
    assert len(content["claims"]) == 2 and "Unsupported assertion" not in str(content)
    assert "invented quotation" not in str(content)
    assert any("quotation" in item and "omitted" in item for item in content["limitations"])


def test_research_marks_cross_source_differences_for_review_and_recomputes_them(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    ids = []
    class Provider:
        name = "conflict-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def cite(index, quote): return {"source_id": ids[index], "quotation": quote}
            return {"claims": [{"text": "Reported revenue varies", "citations": [cite(0, "Revenue 100")]}],
                    "counterarguments": [{"text": "Sources may measure different things", "citations": [cite(1, "Revenue 120")]}],
                    "facts": [{"fact_id": "a", "label": "Revenue", "value": "100", "unit": "USD million",
                               "currency": "USD", "period": "2023", "citation": cite(0, "Revenue 100")},
                              {"fact_id": "b", "label": "Revenue", "value": "120", "unit": "USD million",
                               "currency": "USD", "period": "2023", "citation": cite(1, "Revenue 120")}],
                    "calculations": [], "limitations": []}, 20
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    registration = client.post("/v1/auth/local/register", json={"email": "differences@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    for text in ("Revenue 100", "Revenue 120"):
        response = client.post("/v2/research/sources", headers=headers, json={"title": text, "text": text,
            "permissions": {"store": True, "model_input": True, "display": True}})
        ids.append(response.json()["source_id"])
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": ids}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "difference-run-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"]}).json()
    operation_id = receipt["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(path + "/runs/" + operation_id, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(0.02)
    assert state == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    assert current["content"]["run"]["status"] == "partial"
    assert len(current["content"]["evidence_differences"]) == 1
    edited = deepcopy(current["content"])
    edited["facts"][1]["value"] = "100"
    edited["evidence_differences"] = [{"fabricated": True}]
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": edited})
    assert saved.status_code == 201, saved.text
    assert saved.json()["versions"][-1]["content"]["evidence_differences"] == []
    assert len(saved.json()["versions"][-2]["content"]["evidence_differences"]) == 1


def test_claim_calculation_difference_is_saved_and_rechecked_after_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "calculation-conflict-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def cite(quote): return {"source_id": source_id, "quotation": quote}
            return {"claims": [{"text": "On sales of 642,637, the margin is 4.21%.", "citations": [cite("sales 642637")]}],
                    "counterarguments": [{"text": "Revenue alone is not enough", "citations": [cite("income 27012")]}],
                    "facts": [{"fact_id": "profit", "label": "Profit", "value": "27012", "unit": "USD million", "currency": "USD", "period": "2024", "citation": cite("income 27012")},
                              {"fact_id": "sales", "label": "Sales", "value": "642637", "unit": "USD million", "currency": "USD", "period": "2024", "citation": cite("sales 642637")}],
                    "calculations": [{"formula": "margin_percent", "first_fact_id": "profit", "second_fact_id": "sales"}], "limitations": []}, 20
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email": "claim-calculation-conflict@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "income 27012; sales 642637",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()
    source_id = source["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Margin?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "calculation-conflict-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"]}).json()
    operation_id = receipt["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(path + "/runs/" + operation_id, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(0.02)
    assert state == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    assert current["content"]["run"]["status"] == "partial"
    assert current["content"]["calculation_differences"][0]["calculated_value"] == "4.20"
    edited = deepcopy(current["content"])
    edited["claims"][0]["text"] = "On sales of 642,637, the margin is 4.20%."
    edited["calculation_differences"] = [{"fabricated": True}]
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": edited})
    assert saved.status_code == 201, saved.text
    assert saved.json()["versions"][-1]["content"]["calculation_differences"] == []
    assert len(saved.json()["versions"][-2]["content"]["calculation_differences"]) == 1


def test_candidate_numeric_evidence_is_separate_and_recomputed_after_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "candidate-evidence-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def cite(quote): return {"source_id": source_id, "quotation": quote}
            return {"claims": [{"text": "Annual growth was 2.5 percent and quarter growth was 3.2 percent.",
                                "citations": [cite("Quarter growth 3.2 percent")]}],
                    "counterarguments": [{"text": "Periods differ", "citations": [cite("Annual growth 2.5 percent")]}],
                    "facts": [{"fact_id": "annual", "label": "Annual", "value": "2.5", "unit": "percent", "period": "2023",
                               "citation": cite("Annual growth 2.5 percent")},
                              {"fact_id": "quarter", "label": "Quarter", "value": "3.2", "unit": "percent", "period": "Q4 2023",
                               "citation": cite("Quarter growth 3.2 percent")}], "calculations": [], "limitations": []}, 20
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email": "candidate-fact-evidence@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Annual growth 2.5 percent. Quarter growth 3.2 percent.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Growth?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "candidate-evidence-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"]}).json()
    op = receipt["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(0.02)
    assert state == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    claim = current["content"]["claims"][0]
    assert current["content"]["run"]["status"] == "partial"
    assert claim["uncited_percentages"] == ["2.5 percent"]
    assert any("claim percentage is absent" in note for note in current["content"]["limitations"])
    assert len(claim["citations"]) == 1 and claim["suggested_fact_evidence"][0]["fact_id"] == "annual"
    assert claim["suggested_fact_evidence"][0]["status"] == "requires_semantic_review"
    edited = deepcopy(current["content"])
    edited["facts"][0]["value"] = "2.6"
    edited["claims"][0]["suggested_fact_evidence"] = [{"fabricated": True}]
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": edited})
    assert saved.status_code == 201, saved.text
    assert not saved.json()["versions"][-1]["content"]["claims"][0].get("suggested_fact_evidence")
    assert current["content"]["claims"][0]["suggested_fact_evidence"][0]["fact_id"] == "annual"


def test_research_lease_recovery_keeps_ambiguous_charge_and_does_not_replay():
    api.reset_state()
    entered, release, finished = Event(), Event(), Event()
    committed = []
    calls = []
    endpoint = "/v1/research/topics/topic_recovery/runs"
    payload = {"parent_version_id": "version_one"}
    def build(operation_id):
        calls.append(operation_id)
        entered.set()
        try:
            assert release.wait(5)
            return ResearchContent(summary="late result"), "topic_recovery"
        finally:
            finished.set()
    try:
        receipt, replayed = api.operations.submit_async(endpoint=endpoint, idempotency_key="research-lease-recovery",
            owner_subject="account:account_test_123", request_payload=payload, result_field="research",
            build_result=build, session_id="topic_recovery", commit_result=lambda *_: committed.append(True))
        assert not replayed and entered.wait(5)
        operation = receipt.operation
        worker = next(thread for thread in threading.enumerate() if thread.name == f"provider-{operation.operation_id}")
        reservation = f"model:{operation.operation_id}"
        now = api.lifecycle_clock.now()
        api.billing_ledger.ensure_state("account_test_123", now=now)
        api.billing_ledger.reserve("account_test_123", reservation, 4500, now=now)
        assert api.operations.recover_expired_leases(now=now + timedelta(seconds=301)) == 1
        assert api.operations._cache_operation(operation.operation_id).status == "failed"
        api.cleanup_expired_state()
        assert api.billing_ledger.usage.get(reservation).status == "reserved"
        replay, replayed = api.operations.submit_async(endpoint=endpoint, idempotency_key="research-lease-recovery",
            owner_subject="account:account_test_123", request_payload=payload, result_field="research",
            build_result=build, session_id="topic_recovery")
        assert replayed and replay.operation.status == "failed" and len(calls) == 1
        release.set()
        assert finished.wait(5)
        # The terminal transition must not admit a result from an expired worker.
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert committed == []
    finally:
        release.set()
        api.reset_state()


def test_question_only_discovers_unverified_leads_without_creating_evidence(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    calls = []
    class Search:
        name = "test-search"
        supports_project_search = True
        def search(self, query, *, limit, deadline, cancel_event):
            assert deadline > time.monotonic()
            calls.append((query, limit))
            return (SearchCandidate(title="Official report", url="https://www.bea.gov/news/report?tracking=secret",
                excerpt="Search-only text", publisher="BEA", published_at=date(2023, 1, 1), independently_reviewable=True),
                SearchCandidate(title="Future report", url="https://www.bea.gov/news/future",
                excerpt="Future", publisher="BEA", published_at=date(2024, 1, 1), independently_reviewable=True))
    monkeypatch.setattr(api, "evidence_provider", Search())
    monkeypatch.setattr(api, "_reserve_search_call", lambda request: None)
    client = TestClient(api.app)
    registration = client.post("/v1/auth/local/register", json={"email": "search@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Find economic evidence",
        "as_of": "2023-02-01T00:00:00Z"}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    payload = {"parent_version_id": topic["versions"][0]["version_id"], "max_search_calls": 1}
    req_headers = {**headers, "Idempotency-Key": "discovery-request-1"}
    receipt = client.post(path + "/runs", headers=req_headers, json=payload)
    assert receipt.status_code == 202
    operation_id = receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + operation_id, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    result = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert result["run"]["status"] == "partial"
    assert result["run"]["search_calls"] == 1
    assert len(result["source_candidates"]) == 1
    assert result["source_candidates"][0]["status"] == "unverified"
    assert result["source_candidates"][0]["url"] == "https://www.bea.gov/news/report"
    assert "Search-only text" not in str(result)
    assert result["facts"] == [] and result["claims"] == []
    assert client.post(path + "/runs", headers=req_headers, json=payload).headers["Idempotency-Replayed"] == "true"
    assert len(calls) == 1
    latest = client.get(path, headers=headers).json()["versions"][-1]["version_id"]
    disabled = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "discovery-disabled-1"},
                           json={"parent_version_id": latest, "max_search_calls": 0})
    assert disabled.status_code == 202
    disabled_id = disabled.json()["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(path + "/runs/" + disabled_id, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(0.02)
    assert state == "completed"
    disabled_content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert disabled_content["run"]["search_status"] == "not_requested"
    assert len(calls) == 1


def test_review_completion_does_not_overwrite_edits_to_generation_checkpoint(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    entered, release = Event(), Event()
    source_id = ""
    class Provider:
        name = "checkpoint-edit-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            if system == REVIEW_SYSTEM_PROMPT:
                entered.set()
                assert release.wait(5)
                return {"reviews": [{"claim_index": 0, "verdict": "supports"}]}, 10
            citation = {"source_id": source_id, "quotation": "Revenue was 100"}
            return {"claims": [{"text": "Revenue was 100", "citations": [citation]}]}, 55
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    reg = client.post("/v1/auth/local/register", json={"email": "checkpoint-edit@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + reg.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()
    source_id = source["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "checkpoint-edit-run"}, json={
        "parent_version_id": topic["versions"][0]["version_id"], "support_review": True,
        "max_model_calls": 2, "max_total_tokens": 9000}).json()["operation"]
    assert entered.wait(5)
    checkpoint = client.get(path, headers=headers).json()["versions"][-1]
    edited = deepcopy(checkpoint["content"]); edited["summary"] = "User revision"
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": checkpoint["version_id"], "content": edited})
    assert saved.status_code == 201
    release.set()
    for _ in range(100):
        status = client.get(path + "/runs/" + receipt["operation_id"], headers=headers).json()["operation"]["status"]
        if status in {"failed", "completed"}: break
        time.sleep(.02)
    assert status == "failed"
    versions = client.get(path, headers=headers).json()["versions"]
    assert len(versions) == 3 and versions[-1]["content"]["summary"] == "User revision"
    assert client.delete("/v1/auth/local/account", headers=headers).status_code == 200


def test_discovery_keeps_safe_leads_when_another_candidate_is_malformed(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    calls = []
    class Search:
        name = "mixed-search"
        supports_project_search = True
        valid = True
        def search(self, query, *, limit, deadline, cancel_event):
            calls.append(query)
            invalid = SimpleNamespace(title="SECRET_INVALID", url="not-a-url", excerpt="invalid", publisher=None,
                                      published_at=None, independently_reviewable=True)
            if not self.valid:
                return (invalid,)
            return (invalid, SearchCandidate(title="BEA report", url="https://www.bea.gov/news/report?private=secret",
                        excerpt="Unverified result", publisher="BEA", published_at=date(2023, 1, 1), independently_reviewable=True),
                    SearchCandidate(title="Unsafe local", url="http://127.0.0.1/private", excerpt="invalid",
                                    publisher=None, published_at=None, independently_reviewable=True))
    search = Search()
    monkeypatch.setattr(api, "evidence_provider", search)
    monkeypatch.setattr(api, "_reserve_search_call", lambda request: None)
    client = TestClient(api.app)
    reg = client.post("/v1/auth/local/register", json={"email": "mixed-search@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + reg.json()["access_token"]}
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Find a report"}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    payload = {"parent_version_id": topic["versions"][0]["version_id"], "max_search_calls": 1}
    req_headers = {**headers, "Idempotency-Key": "mixed-search-test"}
    receipt = client.post(path + "/runs", headers=req_headers, json=payload)
    assert receipt.status_code == 202
    op = receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(.02)
    assert status == "completed"
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert content["run"]["status"] == "partial" and content["run"]["search_status"] == "partial"
    assert len(content["source_candidates"]) == 1 and content["source_candidates"][0]["url"] == "https://www.bea.gov/news/report"
    assert "Some search candidates could not be validated and were omitted." in content["limitations"]
    assert "SECRET_INVALID" not in str(content) and "private=secret" not in str(content)
    replay = client.post(path + "/runs", headers=req_headers, json=payload)
    assert replay.headers["Idempotency-Replayed"] == "true" and len(calls) == 1
    search.valid = False
    other = client.post("/v2/research/topics", headers=headers, json={"question": "Another report"}).json()
    other_path = "/v2/research/topics/" + other["topic_id"]
    second = client.post(other_path + "/runs", headers={**headers, "Idempotency-Key": "invalid-search-test"},
        json={"parent_version_id": other["versions"][0]["version_id"], "max_search_calls": 1}).json()["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(other_path + "/runs/" + second, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(.02)
    assert state == "completed"
    no_safe = client.get(other_path, headers=headers).json()["versions"][-1]["content"]
    assert no_safe["run"]["search_status"] == "failed" and no_safe["source_candidates"] == []
    assert "No eligible search candidates remained after validation." in no_safe["limitations"]
    assert "SECRET_INVALID" not in str(no_safe) and len(calls) == 2
    assert client.delete("/v1/auth/local/account", headers=headers).status_code == 200


def test_simulated_source_is_not_sent_to_real_research_or_saved_as_evidence(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    seen = []
    class Provider:
        name = "simulation-boundary-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            seen.append(prompt)
            import json
            data = json.loads(prompt)
            assert len(data["sources"]) == 1 and "SIMULATED_VALUE" not in prompt
            citation = {"source_id": data["sources"][0]["source_id"], "quotation": "Revenue was 100"}
            return {"claims": [{"text": "Revenue was 100", "citations": [citation]}],
                    "counterarguments": [{"text": "Revenue is not profit", "citations": [citation]}]}, 55
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    reg = client.post("/v1/auth/local/register", json={"email": "simulation-boundary@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + reg.json()["access_token"]}
    def create(title, text, simulated):
        response = client.post("/v2/research/sources", headers=headers, json={"title": title, "text": text,
            "is_simulated": simulated, "permissions": {"store": True, "display": True, "model_input": True, "export": True}})
        assert response.status_code == 201
        return response.json()
    real = create("Report", "Revenue was 100.", False)
    simulated = create("Scenario", "SIMULATED_VALUE 900", True)
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?",
        "source_ids": [real["source_id"], simulated["source_id"]]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    # Marked simulated material cannot be promoted by manual citations or facts either.
    for content in ({"claims": [{"text": "A finding", "citations": [{"source_id": simulated["source_id"], "quotation": "SIMULATED_VALUE 900"}]}]},
                    {"facts": [{"fact_id": "sim", "label": "Revenue", "value": "900", "unit": "USD", "period": "2024", "source_id": simulated["source_id"]}]}):
        assert client.post(path + "/versions", headers=headers, json={"parent_version_id": topic["versions"][0]["version_id"], "content": content}).status_code == 422
    started = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "simulation-boundary-run"},
        json={"parent_version_id": topic["versions"][0]["version_id"]}).json()["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(path + "/runs/" + started, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(.02)
    assert state == "completed" and len(seen) == 1
    version = client.get(path, headers=headers).json()["versions"][-1]
    content = version["content"]
    assert content["run"]["status"] == "partial"
    assert [item["source_id"] for item in content["sources"]] == [real["source_id"]]
    assert any("simulated material is excluded" in item for item in content["limitations"])
    assert "SIMULATED_VALUE" not in str(content)
    snapshot = client.get(path + "/runs/" + started + "/inputs", headers=headers).json()
    assert next(item for item in snapshot["sources"] if item["source_id"] == simulated["source_id"])["is_simulated"] is True
    metadata = client.get(path + "/versions/" + version["version_id"] + "/sources", headers=headers).json()
    assert next(item for item in metadata if item["source_id"] == simulated["source_id"])["is_simulated"] is True
    export = client.get(path + "/versions/" + version["version_id"] + "/export", headers=headers).json()
    assert next(item for item in export["source_provenance"] if item["source_id"] == simulated["source_id"])["is_simulated"] is True
    assert "SIMULATED_VALUE" not in str(export)
    only_sim = client.post("/v2/research/topics", headers=headers, json={"question": "Scenario?",
        "source_ids": [simulated["source_id"]]}).json()
    sim_path = "/v2/research/topics/" + only_sim["topic_id"]
    sim_op = client.post(sim_path + "/runs", headers={**headers, "Idempotency-Key": "only-simulation-run"},
        json={"parent_version_id": only_sim["versions"][0]["version_id"]}).json()["operation"]["operation_id"]
    for _ in range(100):
        sim_status = client.get(sim_path + "/runs/" + sim_op, headers=headers).json()["operation"]["status"]
        if sim_status in {"completed", "failed"}: break
        time.sleep(.02)
    assert sim_status == "completed" and len(seen) == 1
    sim_content = client.get(sim_path, headers=headers).json()["versions"][-1]["content"]
    assert sim_content["run"]["model_calls"] == 0 and sim_content["run"]["status"] == "partial"
    assert any("simulated material is excluded" in item for item in sim_content["limitations"])
    assert client.delete("/v1/auth/local/account", headers=headers).status_code == 200


def test_mismatched_currency_conversion_is_reviewable_and_recomputed_on_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    class Provider:
        name = "amount-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            import json
            sid = json.loads(prompt)["sources"][0]["source_id"]
            quote = "2023 revenue 53717, 2024 revenue 57063 million USD"
            cite = {"source_id": sid, "quotation": quote}
            facts = [{"fact_id": id, "label": "Revenue", "value": value, "unit": "USD millions", "currency": "USD",
                      "period": year, "citation": cite} for id, value, year in (("prior", "53717", "2023"), ("current", "57063", "2024"))]
            return {"claims": [{"text": "2023为53,717百万美元，2024为57,063百万美元，即57,070.63亿美元。", "citations": [cite]}],
                    "counterarguments": [{"text": "Revenue alone does not establish profit.", "citations": [cite]}], "facts": facts}, 55
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    reg = client.post("/v1/auth/local/register", json={"email": "amount-warning@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + reg.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report",
        "text": "2023 revenue 53717, 2024 revenue 57063 million USD", "permissions": {"store": True, "display": True, "model_input": True}}).json()
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source["source_id"]]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    op = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "amount-warning-run"},
        json={"parent_version_id": topic["versions"][0]["version_id"]}).json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(.02)
    assert status == "completed"
    original = client.get(path, headers=headers).json()["versions"][-1]
    assert original["content"]["run"]["status"] == "partial"
    assert original["content"]["amount_differences"][0]["claimed_amount"] == "57,070.63亿美元"
    edited = deepcopy(original["content"])
    edited["claims"][0]["text"] = "2023为53,717百万美元，2024为57,063百万美元，即570.63亿美元。"
    edited["amount_differences"] = [{"fabricated": True}]
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": original["version_id"], "content": edited})
    assert saved.status_code == 201
    assert saved.json()["versions"][-1]["content"]["amount_differences"] == []
    assert saved.json()["versions"][-2]["content"]["amount_differences"] == original["content"]["amount_differences"]
    assert client.delete("/v1/auth/local/account", headers=headers).status_code == 200


def test_publication_date_candidate_persists_and_is_recomputed_after_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    class Provider:
        name = "publication-date-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            citation = {"source_id": source_id, "quotation": "Revenue was 100"}
            return {"claims": [{"text": "2023年9月发布的公告报告收入。", "citations": [citation]}],
                    "counterarguments": [{"text": "收入不能证明利润。", "citations": [citation]}]}, 30
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    auth = client.post("/v1/auth/local/register", json={"email": "date-warning@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100",
        "published_at": "2023-10-27T12:30:00Z", "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Report?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "date-warning-run"},
                          json={"parent_version_id": topic["versions"][0]["version_id"]})
    assert receipt.status_code == 202
    op = receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(.02)
    assert status == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    assert current["content"]["run"]["status"] == "partial"
    assert current["content"]["claims"][0]["publication_date_warnings"][0]["claimed_text"] == "2023年9月发布"
    changed = deepcopy(current["content"])
    changed["claims"][0]["text"] = "2023年10月发布的公告报告收入。"
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": changed})
    assert saved.status_code == 201, saved.text
    versions = saved.json()["versions"]
    assert not versions[-1]["content"]["claims"][0].get("publication_date_warnings")
    assert versions[-2]["content"]["claims"][0]["publication_date_warnings"]


def test_rate_unit_candidate_makes_run_partial_and_recomputes_on_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    class Provider:
        name = "rate-unit-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            citation = {"source_id": source_id, "quotation": "target range at 4-1/4 to 4-1/2 percent"}
            return {"claims": [{"text": "目标区间维持在4-1/4至4-1/2个百分点。", "citations": [citation]}],
                    "counterarguments": [{"text": "未来政策存在不确定性。", "citations": [citation]}]}, 30
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    auth = client.post("/v1/auth/local/register", json={"email": "rate-unit@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Statement", "text": "target range at 4-1/4 to 4-1/2 percent",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Rate?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "rate-unit-run"},
                          json={"parent_version_id": topic["versions"][0]["version_id"]})
    assert receipt.status_code == 202
    op = receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(.02)
    assert status == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    assert current["content"]["run"]["status"] == "partial"
    assert current["content"]["claims"][0]["rate_unit_warnings"]
    changed = deepcopy(current["content"])
    changed["claims"][0]["text"] = "目标区间维持在4.25%至4.50%。"
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": changed})
    assert saved.status_code == 201, saved.text
    versions = saved.json()["versions"]
    assert not versions[-1]["content"]["claims"][0].get("rate_unit_warnings")
    assert versions[-2]["content"]["claims"][0]["rate_unit_warnings"]


def test_unverified_rate_currency_is_visible_and_rechecked_on_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    class Provider:
        name = "rate-currency-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            citation = {"source_id": source_id, "quotation": "4-1/4 to 4-1/2 percent"}
            return {"claims": [{"text": "The rate target is 4.25% to 4.5%.", "citations": [citation]}],
                    "facts": [{"fact_id": "range", "label": "Target", "value": "4-1/4 to 4-1/2",
                               "unit": "percent", "currency": "USD", "period": "2025", "citation": citation}]}, 30
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    auth = client.post("/v1/auth/local/register", json={"email": "rate-currency@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Statement", "text": "4-1/4 to 4-1/2 percent",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Rate?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "rate-currency-run"},
                          json={"parent_version_id": topic["versions"][0]["version_id"], "require_counterarguments": False})
    assert receipt.status_code == 202
    op = receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(.02)
    assert status == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    assert current["content"]["run"]["status"] == "partial"
    assert len(current["content"]["facts"]) == 2
    assert all(item["currency_context_unverified"] for item in current["content"]["facts"])
    with_calculation = deepcopy(current["content"])
    inputs = [{key: fact[key] for key in ("fact_id", "value", "unit", "currency", "period")}
              for fact in with_calculation["facts"]]
    with_calculation["calculations"] = [{"formula_id": "difference", "formula_version": "1", "inputs": inputs,
                                          "expression": "first - second", "value": "0", "unit": "percent"}]
    saved_calc = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": with_calculation})
    assert saved_calc.status_code == 201, saved_calc.text
    current = saved_calc.json()["versions"][-1]
    retry = client.post(path + "/calculations/recalculate", headers=headers,
                        json={"parent_version_id": current["version_id"], "calculation_index": 0})
    assert retry.status_code == 422
    changed = deepcopy(current["content"])
    for item in changed["facts"]:
        item["currency"] = None
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": changed})
    assert saved.status_code == 201, saved.text
    versions = saved.json()["versions"]
    assert all(not item.get("currency_context_unverified") for item in versions[-1]["content"]["facts"])
    assert all(item["currency_context_unverified"] for item in versions[-2]["content"]["facts"])


def test_uncited_amounts_are_candidates_and_rechecked_after_adding_quotes(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    class Provider:
        name = "amount-candidate-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def item(id, value, quote):
                return {"fact_id": id, "label": id, "value": value, "unit": "millions", "currency": "USD", "period": "2025",
                        "citation": {"source_id": source_id, "quotation": quote}}
            return {"claims": [{"text": "收入200,966百万美元，营业利润83,276百万美元。", "citations": [
                        {"source_id": source_id, "quotation": "unit USD millions"}]}],
                    "facts": [item("revenue", "200966", "revenue 200966"), item("income", "83276", "income 83276")]}, 30
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    auth = client.post("/v1/auth/local/register", json={"email": "amount-candidate@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report",
        "text": "unit USD millions; revenue 200966; income 83276.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue and income?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "amount-candidate-run"},
                          json={"parent_version_id": topic["versions"][0]["version_id"], "require_counterarguments": False})
    assert receipt.status_code == 202
    op = receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(.02)
    assert status == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    claim = current["content"]["claims"][0]
    assert current["content"]["run"]["status"] == "partial"
    assert len(claim["uncited_amounts"]) == 2 and len(claim["suggested_fact_evidence"]) == 2
    assert len(claim["uncited_fact_values"]) == 2
    changed = deepcopy(current["content"])
    changed["claims"][0]["citations"].extend({"source_id": item["source_id"], "quotation": item["quotation"]}
                                                for item in claim["suggested_fact_evidence"])
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": changed})
    assert saved.status_code == 201, saved.text
    versions = saved.json()["versions"]
    assert not versions[-1]["content"]["claims"][0].get("uncited_amounts")
    assert not versions[-1]["content"]["claims"][0].get("uncited_fact_values")
    assert len(versions[-1]["content"]["claims"][0]["citations"]) == 3
    assert versions[-2]["content"]["claims"][0]["uncited_amounts"]
    assert versions[-1]["content"]["claims"][0]["support_status"] == "not_independently_verified"


def test_budget_exhaustion_keeps_selected_source_without_claiming_a_call(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    class Provider:
        name = "no-call-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            raise AssertionError("budget disallows this call")
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email": "no-call-source@example.test",
        "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + registered["access_token"]}
    src = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [src["source_id"]]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    response = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "no-call-source-test"},
                           json={"parent_version_id": topic["versions"][0]["version_id"], "max_model_calls": 0})
    operation = response.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + operation, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert content["run"]["status"] == "partial" and content["run"]["model_calls"] == 0
    assert content["run"]["error"] == "provider_budget_exhausted"
    assert content["run"]["prompt_sha256"] is None
    assert [item["source_id"] for item in content["sources"]] == [src["source_id"]]
    assert any("selected for analysis" in note for note in content["limitations"])


def test_discarded_model_annotations_keep_research_partial(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "extra-metadata-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            return {"claims": [{"text": "Revenue was 100", "status": "evidence", "citations": [
                {"source_id": source_id, "quotation": "Revenue was 100"}]}], "counterarguments": []}, 10
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    account = client.post("/v1/auth/local/register", json={"email": "extra-model-fields@example.test",
        "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + account["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "extra-metadata-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"], "require_counterarguments": False}).json()
    op = receipt["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert content["run"]["status"] == "partial" and content["run"]["error"] is None
    assert content["claims"][0]["text"] == "Revenue was 100"
    assert any("annotations were omitted" in note for note in content["limitations"])


def test_omitted_citation_subject_keeps_run_partial_and_recomputes_on_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    text = "Personal saving rate was 2.7 percent in the third quarter, a downward revision of 0.1 percentage point."
    quote = "quarter, a downward revision of 0.1 percentage point."
    class Provider:
        name = "subject-context-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            return {"claims": [{"text": "GDP 向下修订 0.1 个百分点。", "citations": [
                {"source_id": source_id, "quotation": quote}]}], "counterarguments": []}, 10
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    account = client.post("/v1/auth/local/register", json={"email": "context-subject@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + account["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": text,
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "GDP?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "subject-context-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"], "require_counterarguments": False}).json()
    op = receipt["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(0.02)
    assert state == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    content = current["content"]
    assert content["run"]["status"] == "partial"
    assert content["claims"][0]["citation_context_warnings"][0]["context_metric"] == "personal saving rate"
    revised = deepcopy(content)
    revised["claims"][0]["citations"] = [{"source_id": source_id, "quotation": text}]
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": revised})
    assert saved.status_code == 201
    assert not saved.json()["versions"][-1]["content"]["claims"][0].get("citation_context_warnings")
    assert current["content"]["claims"][0]["citation_context_warnings"]


def test_inferred_negative_percentage_cannot_be_silently_recalculated(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "direction-context-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def item(key, value, period, quotation):
                return {"fact_id": key, "label": "GDP change", "value": value, "unit": "percent", "period": period,
                        "citation": {"source_id": source_id, "quotation": quotation}}
            return {"facts": [item("old", "-0.6", "2022", "decreased 0.6 percent"),
                               item("new", "3.2", "2023", "increased 3.2 percent")],
                    "calculations": [{"formula": "difference", "first_fact_id": "new", "second_fact_id": "old"}]}, 10
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    account = client.post("/v1/auth/local/register", json={"email": "direction-context@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + account["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report",
        "text": "Real GDP decreased 0.6 percent. It increased 3.2 percent later.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "GDP?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "direction-context-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"]}).json()
    op = receipt["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    content = deepcopy(current["content"])
    assert content["run"]["status"] == "partial" and not content["calculations"]
    negative = next(item for item in content["facts"] if item["fact_id"] == "old")
    assert negative.pop("direction_context_unverified") is True
    content["calculations"] = [{"formula_id": "difference", "formula_version": "1",
        "inputs": [{"fact_id": "new"}, {"fact_id": "old"}]}]
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": content})
    assert saved.status_code == 201
    latest = saved.json()["versions"][-1]
    assert next(item for item in latest["content"]["facts"] if item["fact_id"] == "old")["direction_context_unverified"] is True
    response = client.post(path + "/calculations/recalculate", headers=headers,
        json={"parent_version_id": latest["version_id"], "calculation_index": 0})
    assert response.status_code == 422
    assert "require review" in response.json()["message"]


def test_recalculation_refreshes_claim_calculation_candidates_without_changing_history(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "recalculation-candidate-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def cite(quotation): return {"source_id": source_id, "quotation": quotation}
            current, prior = "Revenue was 100 in 2023", "Revenue was 80 in 2022"
            return {"claims": [{"text": "Revenue grew 25%", "citations": [cite(current), cite(prior)]}],
                    "facts": [{"fact_id": "current", "label": "Revenue", "value": "100", "unit": "USD million", "currency": "USD", "period": "2023", "citation": cite(current)},
                              {"fact_id": "prior", "label": "Revenue", "value": "80", "unit": "USD million", "currency": "USD", "period": "2022", "citation": cite(prior)}],
                    "calculations": [{"formula": "growth_percent", "first_fact_id": "current", "second_fact_id": "prior"}]}, 10
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    account = client.post("/v1/auth/local/register", json={"email": "recalc-candidates@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + account["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report",
        "text": "Revenue was 100 in 2023. Revenue was 80 in 2022.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue growth?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "recalc-candidates-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"], "require_counterarguments": False}).json()
    op = receipt["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    original = client.get(path, headers=headers).json()["versions"][-1]
    assert original["content"]["claims"][0]["suggested_calculations"][0]["calculation_index"] == 0
    edited = deepcopy(original["content"])
    edited["calculations"][0]["value"] = "30"
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": original["version_id"], "content": edited})
    assert saved.status_code == 201
    changed = saved.json()["versions"][-1]
    assert not changed["content"]["claims"][0].get("suggested_calculations")
    recalculated = client.post(path + "/calculations/recalculate", headers=headers,
        json={"parent_version_id": changed["version_id"], "calculation_index": 0})
    assert recalculated.status_code == 201
    versions = recalculated.json()["versions"]
    assert versions[-1]["content"]["calculations"][0]["value"] == "25.00"
    assert versions[-1]["content"]["claims"][0]["suggested_calculations"][0]["status"] == "requires_semantic_review"
    assert not versions[-2]["content"]["claims"][0].get("suggested_calculations")
    assert versions[-3]["content"]["claims"][0]["suggested_calculations"]


def test_partial_support_review_records_omissions_and_leaves_other_claims_unreviewed(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "partial-review-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            if system == REVIEW_SYSTEM_PROMPT:
                return {"reviews": [{"claim_index": 0, "verdict": "supports"}]}, 10
            cite = {"source_id": source_id, "quotation": "Revenue was 100"}
            return {"claims": [{"text": "Revenue was 100", "citations": [cite]},
                               {"text": "Revenue was reported", "citations": [cite] * 4}], "counterarguments": []}, 20
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    account = client.post("/v1/auth/local/register", json={"email": "partial-review-coverage@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + account["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "partial-review-coverage-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"], "support_review": True, "max_model_calls": 2,
              "max_total_tokens": 9000, "require_counterarguments": False}).json()
    op = receipt["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    versions = client.get(path, headers=headers).json()["versions"]
    content = versions[-1]["content"]
    assert content["run"]["status"] == "partial" and content["run"]["support_review_status"] == "partial"
    assert content["run"]["support_review_indexes"] == [0]
    assert content["run"]["support_review_omissions"] == [{"claim_index": 1, "reason": "not_reviewable"}]
    assert content["claims"][0]["support_review"]["verdict"] == "supports"
    assert "support_review" not in content["claims"][1]
    assert versions[-2]["content"]["run"]["support_review_omissions"] == [{"claim_index": 1, "reason": "not_reviewable"}]


def test_run_can_focus_on_saved_unique_quotation(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setattr(api, '_reserve_model_call', lambda *args, **kwargs: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *args, **kwargs: None)
    quote = 'The overlooked finding is 123 dollars.'
    prompts = []
    class Provider:
        name = 'focus-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            prompts.append(prompt)
            return {'claims': [{'text': 'The finding is 123 dollars.', 'citations': [{'source_id': source_id, 'quotation': quote}]}]}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    account = client.post('/v1/auth/local/register', json={'email': 'focus-run@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + account['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': 'Other material. ' * 500 + quote, 'permissions': {'store': True, 'model_input': True, 'display': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'What is the finding?', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id': topic['versions'][-1]['version_id'], 'content': {'claims': [{'text': 'Check finding', 'citations': [{'source_id': source_id, 'quotation': quote}]}]}})
    assert saved.status_code == 201, saved.text
    parent = saved.json()['versions'][-1]['version_id']
    payload = {'parent_version_id': parent, 'focus_citation': {'claim_index': 0, 'citation_index': 0}, 'require_counterarguments': False}
    request_headers = {**headers, 'Idempotency-Key': 'focus-test-key'}
    invalid = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'focus-invalid-key'}, json={**payload, 'focus_citation': {'claim_index': 9, 'citation_index': 0}})
    assert invalid.status_code == 422 and not prompts
    started = client.post(path + '/runs', headers=request_headers, json=payload)
    assert started.status_code == 202, started.text
    operation_id = started.json()['operation']['operation_id']
    for _ in range(100):
        state = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']['status']
        if state in {'completed', 'failed'}: break
        time.sleep(0.02)
    assert state == 'completed' and len(prompts) == 1 and quote in prompts[0]
    snapshot = client.get(path + '/runs/' + operation_id + '/inputs', headers=headers).json()
    assert snapshot['budget']['focus_citation'] == payload['focus_citation']
    replay = client.post(path + '/runs', headers=request_headers, json=payload)
    assert replay.status_code == 202 and replay.headers['Idempotency-Replayed'] == 'true' and len(prompts) == 1


def test_run_passes_persisted_scope_and_records_hash(monkeypatch):
    from hashlib import sha256
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setattr(api, '_reserve_model_call', lambda *a, **k: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *a, **k: None)
    prompts = []
    class Provider:
        name = 'scope-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            prompts.append(prompt)
            return {'claims': []}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'scope-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': 'Oceania revenue 4321.', 'permissions': {'store': True, 'display': True, 'model_input': True}}).json()['source_id']
    scope = 'Oceania segment in 2024'
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'What happened?', 'scope': scope, 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    started = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'scope-request-key'}, json={'parent_version_id': topic['versions'][-1]['version_id']})
    assert started.status_code == 202, started.text
    operation_id = started.json()['operation']['operation_id']
    for _ in range(100):
        status = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(0.02)
    assert status == 'completed' and len(prompts) == 1 and scope in prompts[0]
    inputs = client.get(path + '/runs/' + operation_id + '/inputs', headers=headers).json()
    assert inputs['scope_sha256'] == sha256(scope.encode()).hexdigest()
    assert scope not in str(inputs)


def test_historical_run_sends_cutoff_and_excludes_later_source(monkeypatch):
    import json
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setattr(api, '_reserve_model_call', lambda *a, **k: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *a, **k: None)
    prompts = []
    class Provider:
        name = 'cutoff-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            prompts.append(json.loads(prompt))
            return {'claims': []}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'cutoff-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    ids = []
    for title, date in [('Old', '2023-01-01T00:00:00Z'), ('Later', '2023-07-02T00:00:00Z')]:
        response = client.post('/v2/research/sources', headers=headers, json={'title': title, 'text': title + ' material.', 'published_at': date, 'permissions': {'store': True, 'display': True, 'model_input': True}})
        assert response.status_code == 201, response.text
        ids.append(response.json()['source_id'])
    cutoff = '2023-07-01T12:30:00.123456Z'
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'What was known?', 'as_of': cutoff, 'source_ids': ids}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    started = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'cutoff-request-key'}, json={'parent_version_id': topic['versions'][-1]['version_id']})
    assert started.status_code == 202, started.text
    op = started.json()['operation']['operation_id']
    for _ in range(100):
        status = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(0.02)
    assert status == 'completed' and len(prompts) == 1
    assert prompts[0]['research_cutoff'] == '2023-07-01T12:30:00.123456+00:00'
    assert [s['source_id'] for s in prompts[0]['sources']] == ['ref-1']
    assert 'Old material.' in prompts[0]['sources'][0]['text']
    assert all('Later material.' not in s['text'] for s in prompts[0]['sources'])
    segments = client.get(path, headers=headers).json()['versions'][-1]['content']['run']['selected_segments']
    assert segments[0]['source_id'] == ids[0] and segments[0]['model_source_id'] == 'ref-1'
    snapshot = client.get(path + '/runs/' + op + '/inputs', headers=headers).json()
    assert snapshot['as_of'] == prompts[0]['research_cutoff']


def test_source_discovery_only_sends_scope_when_explicitly_selected(monkeypatch):
    from uuid import uuid4
    from app.research_runs import discovery_query
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setattr(api, '_reserve_search_call', lambda *a, **k: None)
    queries = []
    class Search:
        supports_project_search = True
        def search(self, query, *, limit, deadline, cancel_event):
            queries.append(query)
            return ()
    monkeypatch.setattr(api, 'evidence_provider', Search())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'discovery-scope-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    scope = 'Oceania FY2024'
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Compare revenue', 'scope': scope}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    def run(parent, key, extra):
        response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': key}, json={'parent_version_id': parent, **extra})
        assert response.status_code == 202, response.text
        op = response.json()['operation']['operation_id']
        for _ in range(100):
            status = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
            if status in {'completed', 'failed'}: break
            time.sleep(0.02)
        assert status == 'completed'
        return op, client.get(path, headers=headers).json()['versions'][-1]['version_id']
    first_parent = topic['versions'][0]['version_id']
    first, parent = run(first_parent, 'scope-search-default', {})
    assert queries == ['Compare revenue']
    # An explicit false remains idempotent with requests predating the field.
    replay = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'scope-search-default'}, json={'parent_version_id': first_parent, 'include_scope_in_search': False})
    assert replay.status_code == 202 and replay.headers['Idempotency-Replayed'] == 'true'
    second, parent = run(parent, 'scope-search-opt-in', {'include_scope_in_search': True})
    assert queries == ['Compare revenue', 'Compare revenue Oceania FY2024']
    assert client.get(path + '/runs/' + second + '/inputs', headers=headers).json()['budget']['include_scope_in_search'] is True
    assert 'include_scope_in_search' not in client.get(path + '/runs/' + first + '/inputs', headers=headers).json()['budget']
    run(parent, 'question-override-search', {'question_override': 'Compare cash flow', 'include_scope_in_search': True})
    assert queries[-1] == 'Compare cash flow Oceania FY2024'
    bounded = discovery_query('问' * 80, '🌏' * 80, True)
    assert len(bounded) == 70 and bounded == '问' * 45 + ' ' + '🌏' * 24

@pytest.mark.parametrize('reported', [123, None])
def test_invalid_model_output_preserves_reported_usage_and_settles_budget(monkeypatch, reported):
    from app.phase3 import ProviderFailure
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    settlements = []
    class Provider:
        name = 'invalid-usage-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            raise ProviderFailure('invalid_provider_output', 'Invalid output.', retryable=False, reported_tokens=reported)
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    monkeypatch.setattr(api, '_reserve_model_call', lambda *args, **kwargs: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda request, op, usage: settlements.append(usage))
    client = TestClient(api.app)
    account = client.post('/v1/auth/local/register', json={'email': f'invalid-usage-{reported}@example.test',
        'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + account['access_token']}
    source = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': 'Revenue was 100.',
        'permissions': {'store': True, 'model_input': True, 'display': True}}).json()
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source['source_id']]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'invalid-usage'},
        json={'parent_version_id': topic['versions'][0]['version_id']})
    op = response.json()['operation']['operation_id']
    for _ in range(100):
        state = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
        if state in {'completed', 'failed'}: break
        time.sleep(0.02)
    assert state == 'completed'
    run = client.get(path, headers=headers).json()['versions'][-1]['content']['run']
    assert run['error'] == 'invalid_provider_output' and run['status'] == 'partial'
    assert run['total_tokens'] == reported and run['generation_tokens'] == reported
    assert settlements == [reported]


def test_conflicting_currency_is_rechecked_on_edit_and_recalculation(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'currency-conflict-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def fact(fid, value, year):
                return {'fact_id': fid, 'label': 'Revenue', 'value': value, 'unit': 'million',
                        'currency': 'USD', 'period': year, 'citation': {'source_id': source_id, 'quotation': 'EUR ' + value}}
            return {'facts': [fact('new', '100', '2023'), fact('old', '80', '2022')],
                    'calculations': [{'formula': 'growth_percent', 'first_fact_id': 'new', 'second_fact_id': 'old'}]}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    account = client.post('/v1/auth/local/register', json={'email': 'currency-conflict@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + account['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': 'Revenue EUR 100. Revenue EUR 80.',
        'permissions': {'store': True, 'model_input': True, 'display': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    receipt = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'currency-conflict'},
        json={'parent_version_id': topic['versions'][0]['version_id']}).json()
    op = receipt['operation']['operation_id']
    for _ in range(100):
        status = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(.02)
    assert status == 'completed'
    current = client.get(path, headers=headers).json()['versions'][-1]
    assert current['content']['run']['status'] == 'partial'
    assert all(f['currency_conflict_unverified'] for f in current['content']['facts'])
    assert not current['content']['calculations']
    content = deepcopy(current['content'])
    inputs = [{key: f[key] for key in ('fact_id', 'value', 'unit', 'currency', 'period')} for f in content['facts']]
    content['calculations'] = [{'formula_id': 'growth_percent', 'formula_version': '1', 'inputs': inputs,
                                'expression': '(first / second - 1) * 100', 'value': '25', 'unit': 'percent'}]
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id': current['version_id'], 'content': content})
    assert saved.status_code == 201
    latest = saved.json()['versions'][-1]
    assert latest['content']['calculations'][0]['input_status'] == 'unverifiable'
    recalc = client.post(path + '/calculations/recalculate', headers=headers,
                         json={'parent_version_id': latest['version_id'], 'calculation_index': 0})
    assert recalc.status_code == 422
    fixed = deepcopy(latest['content'])
    for fact in fixed['facts']:
        fact['currency'] = 'EUR'
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id': latest['version_id'], 'content': fixed})
    assert saved.status_code == 201
    latest = saved.json()['versions'][-1]
    assert all(not fact.get('currency_conflict_unverified') for fact in latest['content']['facts'])
    recalc = client.post(path + '/calculations/recalculate', headers=headers,
                         json={'parent_version_id': latest['version_id'], 'calculation_index': 0})
    assert recalc.status_code == 201 and recalc.json()['versions'][-1]['content']['calculations'][0]['value'] == '25.00'


def test_unverified_period_comparability_keeps_automatic_research_partial(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'period-grain-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def item(fid, val, period, quote):
                return {'fact_id':fid, 'label':'Metric', 'value':val, 'unit':'USD', 'currency':'USD', 'period':period,
                        'citation':{'source_id':source_id, 'quotation':quote}}
            return {'claims':[{'text':'Two values were reported.', 'citations':[{'source_id':source_id,'quotation':'Annual revenue was 100'}]}],
                    'facts':[item('annual','100','FY2024','Annual revenue was 100'), item('quarter','80','2023-Q4','Quarterly revenue was 80')],
                    'calculations':[{'formula':'growth_percent','first_fact_id':'annual','second_fact_id':'quarter'}]}, 10
    monkeypatch.setattr(api,'challenge_provider',Provider())
    client=TestClient(api.app)
    account=client.post('/v1/auth/local/register',json={'email':'period-grain@example.test','password':'correct horse battery staple!'}).json()
    headers={'Authorization':'Bearer '+account['access_token']}
    source_id=client.post('/v2/research/sources',headers=headers,json={'title':'Report','text':'Annual revenue was 100. Quarterly revenue was 80.',
        'permissions':{'store':True,'display':True,'model_input':True}}).json()['source_id']
    topic=client.post('/v2/research/topics',headers=headers,json={'question':'Compare values','source_ids':[source_id]}).json()
    path='/v2/research/topics/'+topic['topic_id']
    receipt=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'period-grain-test'},
        json={'parent_version_id':topic['versions'][0]['version_id'],'require_counterarguments':False}).json()
    op=receipt['operation']['operation_id']
    for _ in range(100):
        status=client.get(path+'/runs/'+op,headers=headers).json()['operation']['status']
        if status in {'completed','failed'}: break
        time.sleep(.02)
    assert status=='completed'
    content=client.get(path,headers=headers).json()['versions'][-1]['content']
    assert content['run']['status']=='partial'
    assert content['calculations'][0]['period_granularity_check']=='different_labels_verify_stock_or_flow'
    assert any('Period comparability is not established' in x for x in content['limitations'])


def test_reclassifying_findings_preserves_quotes_but_invalidates_review(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'role-edit-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            if system == REVIEW_SYSTEM_PROMPT:
                return {'reviews': [{'claim_index': 0, 'verdict': 'supports'}]}, 10
            return {'claims': [
                {'text': 'Cash was 100.', 'citations': [{'source_id': source_id, 'quotation': 'Cash was 100.'}]},
                {'text': 'Revenue was reported.', 'citations': [{'source_id': source_id, 'quotation': 'Revenue | $ | 100 | | $ | 90'}]}]}, 20
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'role-edit-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report',
        'text': 'Cash was 100.\n| [colspan=3] Revenue | $ | 100 | | [colspan=3] | $ | 90 |',
        'permissions': {'store': True, 'model_input': True, 'display': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Review results', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    receipt = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'role-edit'},
        json={'parent_version_id': topic['versions'][0]['version_id'], 'support_review': True, 'max_model_calls': 2, 'max_total_tokens': 9000}).json()
    op = receipt['operation']['operation_id']
    for _ in range(100):
        status = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(.02)
    assert status == 'completed'
    previous = client.get(path, headers=headers).json()['versions'][-1]
    before = previous['content']['claims']
    assert before[0]['support_review']['verdict'] == 'supports'
    assert before[1]['support_status'] == 'incomplete'
    content = deepcopy(previous['content'])
    for claim in content['claims']:
        claim['role'] = 'counterargument'
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id': previous['version_id'], 'content': content})
    assert saved.status_code == 201, saved.text
    latest = saved.json()['versions'][-1]
    for old, new in zip(before, latest['content']['claims']):
        assert new['role'] == 'counterargument' and new['text'] == old['text']
        assert new['citations'][0]['quotation'] == old['citations'][0]['quotation']
        assert new['authorship'] == 'user' and 'support_review' not in new
    assert latest['content']['claims'][1]['support_status'] == 'incomplete'
    assert saved.json()['versions'][-2]['content']['claims'] == before


def test_table_calculation_alignment_warning_keeps_run_partial(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'table-alignment-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def fact(fid, value, year):
                return {'fact_id': fid, 'label': 'Revenue', 'value': value, 'unit': 'million', 'currency': 'USD',
                        'period': year, 'citation': {'source_id': source_id, 'quotation': 'USD ' + value}}
            return {'claims': [{'text': 'Revenue was reported.', 'citations': [{'source_id': source_id, 'quotation': 'Revenue | USD 100 | USD 90'}]}],
                    'facts': [fact('a','100','2024'), fact('b','90','2023')],
                    'calculations': [{'formula': 'growth_percent', 'first_fact_id': 'a', 'second_fact_id': 'b'}]}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'alignment-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report',
        'text': '| Year Ended | 2024 | 2023 |\n| Revenue | USD 100 | USD 90 |',
        'permissions': {'store': True, 'model_input': True, 'display': True, 'export': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Review revenue', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    receipt = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'table-alignment'},
        json={'parent_version_id': topic['versions'][0]['version_id'], 'require_counterarguments': False}).json()
    op = receipt['operation']['operation_id']
    for _ in range(100):
        status = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(.02)
    assert status == 'completed'
    current = client.get(path, headers=headers).json()['versions'][-1]
    content = current['content']
    assert content['run']['status'] == 'partial'
    assert all(item['table_alignment_unverified'] for item in content['facts'])
    assert content['calculations'][0]['value'] == '11.11'
    exported = client.get(path + '/versions/' + current['version_id'] + '/export', headers=headers)
    assert exported.status_code == 200
    assert all(item['table_alignment_unverified'] for item in exported.json()['version']['content']['facts'])


def test_table_period_conflict_excludes_calculation_and_rechecks_edits(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'table-period-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def fact(fid, value, year):
                return {'fact_id': fid, 'label': 'Revenue', 'value': value, 'unit': 'million', 'currency': 'USD',
                        'period': year, 'citation': {'source_id': source_id, 'quotation': 'USD ' + value}}
            return {'claims': [{'text': 'Revenue was reported.', 'citations': [{'source_id': source_id, 'quotation': 'Revenue | USD 100 | USD 90'}]}],
                    'facts': [fact('a','100','2023'), fact('b','90','2024')],
                    'calculations': [{'formula': 'growth_percent', 'first_fact_id': 'a', 'second_fact_id': 'b'}]}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'period-column-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title':'Report', 'text':'| Year Ended | 2024 | 2023 |\n| Revenue | USD 100 | USD 90 |',
        'permissions': {'store': True, 'model_input': True, 'display': True, 'export': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question':'Revenue?', 'source_ids':[source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    receipt = client.post(path + '/runs', headers={**headers, 'Idempotency-Key':'period-column'}, json={'parent_version_id': topic['versions'][0]['version_id'], 'require_counterarguments':False}).json()
    op = receipt['operation']['operation_id']
    for _ in range(100):
        status = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
        if status in {'completed','failed'}: break
        time.sleep(.02)
    assert status == 'completed'
    current = client.get(path, headers=headers).json()['versions'][-1]
    content = current['content']
    assert content['run']['status'] == 'partial' and not content['calculations']
    assert [f['table_period_conflict']['column_year'] for f in content['facts']] == ['2024', '2023']
    exported = client.get(path + '/versions/' + current['version_id'] + '/export', headers=headers)
    assert exported.status_code == 200
    assert exported.json()['version']['content']['facts'][0]['table_period_conflict']['column_year'] == '2024'
    edited = deepcopy(content)
    inputs = [{k:f[k] for k in ('fact_id','value','unit','currency','period')} for f in edited['facts']]
    edited['calculations'] = [{'formula_id':'growth_percent','formula_version':'1','inputs':inputs, 'expression':'(first / second - 1) * 100','value':'11.11','unit':'percent'}]
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id':current['version_id'],'content':edited})
    assert saved.status_code == 201, saved.text
    latest = saved.json()['versions'][-1]
    assert latest['content']['calculations'][0]['input_status'] == 'unverifiable'
    assert client.post(path + '/calculations/recalculate', headers=headers, json={'parent_version_id':latest['version_id'],'calculation_index':0}).status_code == 422
    fixed = deepcopy(latest['content'])
    fixed['facts'][0]['period'],fixed['facts'][1]['period'] = '2024','2023'
    for fact in fixed['facts']: fact['table_period_conflict'] = {'declared_year':'2023','column_year':'2024'}
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id':latest['version_id'],'content':fixed})
    assert saved.status_code == 201, saved.text
    latest = saved.json()['versions'][-1]
    assert all(not fact.get('table_period_conflict') for fact in latest['content']['facts'])
    recalc = client.post(path + '/calculations/recalculate', headers=headers, json={'parent_version_id':latest['version_id'],'calculation_index':0})
    assert recalc.status_code == 201 and recalc.json()['versions'][-1]['content']['calculations'][0]['value'] == '11.11'
    assert all(f.get('table_alignment_unverified') for f in latest['content']['facts'])


def test_material_topic_accepts_versioned_run_questions_and_idempotency(monkeypatch):
    import json
    from hashlib import sha256
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    received = []
    class Provider:
        name = 'run-question-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            data = json.loads(prompt)
            received.append(data['question'])
            return {'claims':[{'text':'Cash was 100.', 'citations':[{'source_id': source_id, 'quotation':'Cash was 100.'}]}]}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email':f'run-question-{uuid4().hex}@example.test','password':'correct horse battery staple!'}).json()
    headers = {'Authorization':'Bearer '+auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title':'Report','text':'Cash was 100.',
        'permissions':{'store':True,'display':True,'model_input':True,'export':True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'source_ids':[source_id]}).json()
    assert topic['request']['question'] == ''
    path = '/v2/research/topics/' + topic['topic_id']
    def run(parent, key, question):
        body = {'parent_version_id':parent,'question_override':question,'require_counterarguments':False}
        response = client.post(path+'/runs', headers={**headers,'Idempotency-Key':key}, json=body)
        assert response.status_code == 202, response.text
        op = response.json()['operation']['operation_id']
        for _ in range(100):
            status = client.get(path+'/runs/'+op, headers=headers).json()['operation']['status']
            if status in {'completed','failed'}: break
            time.sleep(.02)
        assert status == 'completed'
        return body, op, client.get(path, headers=headers).json()
    first_body, first_op, updated = run(topic['versions'][0]['version_id'], 'question-one', '  What was cash?  ')
    first = updated['versions'][-1]
    assert received == ['What was cash?']
    assert first['content']['run']['question_override'] == 'What was cash?'
    inputs = client.get(path+'/runs/'+first_op+'/inputs', headers=headers).json()
    assert inputs['question_sha256'] == sha256(b'What was cash?').hexdigest()
    assert 'question_override' not in inputs['budget'] and 'What was cash?' not in str(inputs)
    replay = client.post(path+'/runs', headers={**headers,'Idempotency-Key':'question-one'}, json=first_body)
    assert replay.status_code == 202 and replay.json()['operation']['operation_id'] == first_op and len(received) == 1
    changed = {**first_body,'question_override':'A different question?'}
    assert client.post(path+'/runs', headers={**headers,'Idempotency-Key':'question-one'}, json=changed).status_code == 409
    _, _, updated = run(first['version_id'], 'question-two', 'What was profit?')
    assert received == ['What was cash?', 'What was profit?']
    assert updated['versions'][-2]['content']['run']['question_override'] == 'What was cash?'
    assert updated['versions'][-1]['content']['run']['question_override'] == 'What was profit?'
    assert updated['request']['question'] == ''
    exported = client.get(path+'/versions/'+first['version_id']+'/export', headers=headers).json()
    assert exported['version']['content']['run']['question_override'] == 'What was cash?'
    invalid = {'parent_version_id':updated['versions'][-1]['version_id'],'question_override':'   '}
    assert client.post(path+'/runs', headers={**headers,'Idempotency-Key':'question-empty'}, json=invalid).status_code == 422


def test_larger_provider_input_limit_requires_matching_research_budget(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    calls, reservations = [], []
    class Provider:
        name = "expanded-input-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=4000)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            calls.append(prompt)
            return {"claims": [{"text": "Revenue was 100", "citations": [{"source_id": source_id, "quotation": "Revenue was 100"}]}]}, 20
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: reservations.append(kwargs["token_budget"]))
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email":"expanded-context@example.test", "password":"correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + registered["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title":"Report", "text":"Revenue was 100.", "permissions":{"store":True,"display":True,"model_input":True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question":"Revenue?", "source_ids":[source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    parent = topic["versions"][-1]["version_id"]
    for limit, expected_calls in ((9999, 0), (10000, 1)):
        receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": f"expanded-{limit}"}, json={"parent_version_id":parent,"max_total_tokens":limit,"require_counterarguments":False})
        assert receipt.status_code == 202
        op = receipt.json()["operation"]["operation_id"]
        for _ in range(100):
            status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
            if status in ("completed", "failed"): break
            time.sleep(.02)
        assert status == "completed" and len(calls) == expected_calls
        result = client.get(path, headers=headers).json()["versions"][-1]
        assert result["content"]["run"]["provider_input_token_limit"] == 6000
        if not expected_calls: assert result["content"]["run"]["error"] == "provider_budget_exhausted"
        parent = result["version_id"]
    assert reservations == [10000]


def test_related_source_sentence_is_reviewable_and_recomputed_after_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    text = "Officials may adjust policy. They monitor labor market conditions and inflation pressures."
    first, second = "Officials may adjust policy.", "They monitor labor market conditions and inflation pressures."
    class Provider:
        name = "related-context-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            return {"claims":[{"text":"Officials may adjust policy and monitor labor market conditions.",
                    "citations":[{"source_id":source_id,"quotation":first}]}]}, 10
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    user = client.post("/v1/auth/local/register", json={"email":"related-context@example.test", "password":"correct horse battery staple!"}).json()
    headers = {"Authorization":"Bearer "+user["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title":"Report", "text":text,
        "permissions":{"store":True,"display":True,"model_input":True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question":"What do officials monitor?", "source_ids":[source_id]}).json()
    path = "/v2/research/topics/"+topic["topic_id"]
    response = client.post(path+"/runs", headers={**headers,"Idempotency-Key":"related-context-run"},
        json={"parent_version_id":topic["versions"][0]["version_id"], "require_counterarguments":False})
    assert response.status_code == 202
    op = response.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path+"/runs/"+op,headers=headers).json()["operation"]["status"]
        if status in {"completed","failed"}: break
        time.sleep(.02)
    assert status == "completed"
    version = client.get(path,headers=headers).json()["versions"][-1]
    content = version["content"]
    assert content["run"]["status"] == "partial"
    assert content["claims"][0]["citation_context_warnings"][0]["context_metric"] == "related unquoted sentence"
    edited = deepcopy(content)
    edited["claims"][0]["citations"].append({"source_id":source_id,"quotation":second})
    saved = client.post(path+"/versions",headers=headers,json={"parent_version_id":version["version_id"],"content":edited})
    assert saved.status_code == 201
    latest = saved.json()["versions"][-1]["content"]["claims"][0]
    assert not latest.get("citation_context_warnings") and latest["authorship"] == "user"
    assert content["claims"][0]["citation_context_warnings"]


def test_unquoted_dollar_figures_remain_partial_until_source_quotes_are_added(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    text = "Goods deficit narrowed $0.3 billion. Exports increased $5.8 billion; imports rose $7.1 billion."
    first, second = "Goods deficit narrowed $0.3 billion.", "Exports increased $5.8 billion; imports rose $7.1 billion."
    class Provider:
        name = "dollar-gap-test"
        policy = SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            return {"claims":[{"text":"Imports rose $7.1 billion and exports rose $5.8 billion.",
                    "citations":[{"source_id":source_id,"quotation":first}]}]},10
    monkeypatch.setattr(api,"challenge_provider",Provider())
    client=TestClient(api.app)
    user=client.post("/v1/auth/local/register",json={"email":"dollar-gap@example.test","password":"correct horse battery staple!"}).json()
    headers={"Authorization":"Bearer "+user["access_token"]}
    source_id=client.post("/v2/research/sources",headers=headers,json={"title":"Report","text":text,
        "permissions":{"store":True,"display":True,"model_input":True}}).json()["source_id"]
    topic=client.post("/v2/research/topics",headers=headers,json={"question":"Imports and exports?","source_ids":[source_id]}).json()
    path="/v2/research/topics/"+topic["topic_id"]
    receipt=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"dollar-gap-test"},
        json={"parent_version_id":topic["versions"][0]["version_id"],"require_counterarguments":False})
    assert receipt.status_code==202
    op=receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        state=client.get(path+"/runs/"+op,headers=headers).json()["operation"]["status"]
        if state in {"completed","failed"}:break
        time.sleep(.02)
    assert state=="completed"
    version=client.get(path,headers=headers).json()["versions"][-1];content=version["content"]
    assert content["run"]["status"]=="partial"
    assert content["claims"][0]["uncited_dollar_amounts"]==["$7.1 billion","$5.8 billion"]
    edited=deepcopy(content);edited["claims"][0]["citations"].append({"source_id":source_id,"quotation":second})
    saved=client.post(path+"/versions",headers=headers,json={"parent_version_id":version["version_id"],"content":edited})
    assert saved.status_code==201
    assert not saved.json()["versions"][-1]["content"]["claims"][0].get("uncited_dollar_amounts")
    assert content["claims"][0]["uncited_dollar_amounts"]


def test_quarter_evidence_gap_is_recomputed_after_adding_quote(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    text = "The actual ratio was 12.4 percent in the fourth quarter of 2022. The horizon ends in 2025:Q1."
    first, second = "The actual ratio was 12.4 percent in the fourth quarter of 2022.", "The horizon ends in 2025:Q1."
    class Provider:
        name = "quarter-gap-test"
        policy = SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            return {"claims":[{"text":"The actual ratio was 12.4 percent in 2022:Q4 and the horizon ends in 2025:Q1.",
                    "citations":[{"source_id":source_id,"quotation":first}]}]},10
    monkeypatch.setattr(api,"challenge_provider",Provider())
    client=TestClient(api.app)
    user=client.post("/v1/auth/local/register",json={"email":"quarter-gap@example.test","password":"correct horse battery staple!"}).json()
    headers={"Authorization":"Bearer "+user["access_token"]}
    source_id=client.post("/v2/research/sources",headers=headers,json={"title":"Report","text":text,
        "permissions":{"store":True,"display":True,"model_input":True}}).json()["source_id"]
    topic=client.post("/v2/research/topics",headers=headers,json={"question":"What was the period?","source_ids":[source_id]}).json()
    path="/v2/research/topics/"+topic["topic_id"]
    receipt=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"quarter-gap-test"},
        json={"parent_version_id":topic["versions"][0]["version_id"],"require_counterarguments":False})
    assert receipt.status_code==202
    op=receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        state=client.get(path+"/runs/"+op,headers=headers).json()["operation"]["status"]
        if state in {"completed","failed"}:break
        time.sleep(.02)
    assert state=="completed"
    version=client.get(path,headers=headers).json()["versions"][-1];content=version["content"]
    assert content["run"]["status"]=="partial" and content["claims"][0]["uncited_quarters"]==["2025:Q1"]
    edited=deepcopy(content);edited["claims"][0]["citations"].append({"source_id":source_id,"quotation":second})
    saved=client.post(path+"/versions",headers=headers,json={"parent_version_id":version["version_id"],"content":edited})
    assert saved.status_code==201
    assert not saved.json()["versions"][-1]["content"]["claims"][0].get("uncited_quarters")
    assert content["claims"][0]["uncited_quarters"]
