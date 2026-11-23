"""Public run, cancellation, immutable reread, source permission and export lifecycle."""
from threading import Event, enumerate as active_threads
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as api
from tests.test_research_runtime import turn


def test_cancel_preserves_verified_question_part_and_reopen_observes_permissions(monkeypatch):
    client = TestClient(api.app)
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    registration = client.post("/v1/auth/local/register", json={
        "email": f"{uuid4().hex}@example.test", "password": "correct horse battery staple!",
    }).json()
    headers = {"Authorization": "Bearer " + registration["access_token"]}
    quote = "FY2025 revenue was 100 USD."
    source_id = client.post("/v2/research/sources", headers=headers, json={
        "title": "Annual disclosure", "text": quote,
        "permissions": {"store": True, "model_input": True, "display": True, "export": True},
    }).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={
        "question": "What was FY2025 revenue and diluted EPS?", "source_ids": [source_id],
    }).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    pending, release = Event(), Event()

    class Provider:
        name = "controlled-lifecycle"
        policy = SimpleNamespace(timeout_seconds=30, max_input_tokens=None, max_output_tokens=2048)
        calls = 0

        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return turn("read_sources", {"reads": [{"source_id": source_id}], "rationale": "Read disclosure."}, "read")
            if self.calls == 2:
                return turn("record_facts", {"facts": [{"source_id": source_id, "label": "Revenue", "value": "100",
                    "unit": "USD", "currency": "USD", "period": "FY2025", "quotation": quote}],
                    "rationale": "Save the disclosed figure."}, "fact")
            pending.set()
            release.wait(30)
            raise api.ProviderFailure("provider_cancelled", "interrupted", retryable=False)

    provider = Provider()
    monkeypatch.setattr(api, "challenge_provider", provider)
    op = None
    try:
        op = client.post(path + "/runs", headers={**headers, "Idempotency-Key": uuid4().hex}, json={
            "parent_version_id": topic["versions"][0]["version_id"], "discover_sources": False,
        }).json()["operation"]
        assert pending.wait(10), "research did not reach the third model request"
        control = "/v1/operations/" + op["operation_id"] + "/cancel"
        cancel_headers = {**headers, "Idempotency-Key": uuid4().hex}
        for _ in range(2):
            response = client.post(control, headers=cancel_headers, json={"cancel_token": op["cancel_token"]})
            assert response.status_code == 200 and response.json()["operation"]["status"] == "cancelled"
        versions = client.get(path, headers=headers).json()["versions"]
        assert len(versions) == 2 and provider.calls == 3
        version_path = path + "/versions/" + versions[-1]["version_id"]
        delivered = client.get(version_path, headers=headers).json()["content"]
        saved = delivered["turns"][-1]
        assert saved["status"] == "partial" and saved["reason"] == "provider_cancelled"
        assert any(p["status"] == "answered" and "100" in p["answer"] for p in saved["question_parts"])
        assert any(p["status"] == "unresolved" and "EPS" in p["question"] for p in saved["question_parts"])
        assert client.get(version_path + "/export", headers=headers).json()["version"]["content"] == delivered
        src = api.research_sources.get(topic["owner_id"], source_id)
        src.permissions.display = False
        api.research_sources.repository.put(source_id, src)
        hidden = client.get(version_path, headers=headers).json()["content"]
        assert not hidden["facts"] and not any(p["answer"] for p in hidden["turns"][-1]["question_parts"])
        assert client.get(version_path + "/export", headers=headers).status_code == 403
        assert provider.calls == 3
    finally:
        release.set()
        if op:
            for worker in active_threads():
                if worker.name == "provider-" + op["operation_id"]:
                    worker.join(10)
                    assert not worker.is_alive()
