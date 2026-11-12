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
        "text": "Revenue was 100 million in 2023. Revenue was 80 million in 2022.", "permissions": {"store": True,
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
    assert snapshot["budget"]["max_model_calls"] == 1
    assert snapshot["selection_method"] == "ranked_nonoverlapping_windows_v3"
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


@pytest.mark.parametrize("budget,second_verdict,expected_calls,expected_review", [
    (9000, "contradicts", 2, "complete"),
    (9000, "supports", 2, "complete"),
    (4500, "supports", 1, "budget_exhausted"),
    (9000, "invalid", 2, "failed"),
])
def test_optional_support_review_is_bounded_and_preserves_initial_research(monkeypatch, budget, second_verdict, expected_calls, expected_review):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    calls = []
    source_id = ""
    class Provider:
        name = "review-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            calls.append(system)
            if system == REVIEW_SYSTEM_PROMPT:
                assert "Revenue was 100" in prompt
                return {"reviews": [{"claim_index": 0, "verdict": "supports"},
                                    {"claim_index": 1, "verdict": second_verdict}]}, 12
            citation = {"source_id": source_id, "quotation": "Revenue was 100"}
            return {"claims": [{"text": "Revenue was 100", "citations": [citation]}],
                    "counterarguments": [{"text": "Revenue does not establish profit", "citations": [citation]}],
                    "limitations": [], "facts": [], "calculations": []}, 55
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    registration = client.post("/v1/auth/local/register", json={"email": f"review-{budget}-{second_verdict}@example.test",
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
    current = client.get(path, headers=headers).json()["versions"][-1]
    content, run = current["content"], current["content"]["run"]
    assert len(calls) == expected_calls == run["model_calls"]
    assert run["support_review_status"] == expected_review
    assert len(content["claims"]) == 2
    first_entry = api.billing_ledger.usage.get("model:" + op)
    assert first_entry.status == "settled" and first_entry.actual_tokens == 55
    second_entry = api.billing_ledger.usage.get("model:" + op + ":support-review")
    if expected_calls == 2:
        assert second_entry.status == "settled" and second_entry.actual_tokens == 12
        assert run["total_tokens"] == 67 and len(run["support_review_prompt_sha256"]) == 64
    else:
        assert second_entry is None and run["total_tokens"] == 55
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
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source["source_id"]]}).json()
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
    assert "sensitive model output" not in str(content)


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
