"""Rename, pin, soft delete, restore and purge of research topics (list management)."""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app import main
from app.main import app
from app.research_topics import DELETED_TOPIC_RETENTION

PERMISSIONS = {"store": True, "model_input": True, "display": True, "export": True}


def start(monkeypatch, label="lifecycle"):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    auth = client.post("/v1/auth/local/register", json={"email": f"{label}-{uuid4().hex}@example.test",
        "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Compare revenue"}).json()
    return client, headers, topic, "/v2/research/topics/" + topic["topic_id"]


def test_rename_and_pin_keep_the_question_and_the_version_chain(monkeypatch):
    client, headers, topic, path = start(monkeypatch)
    renamed = client.patch(path, headers=headers, json={"title": "  Revenue check  "})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "Revenue check"
    assert renamed.json()["request"]["question"] == "Compare revenue"

    pinned = client.patch(path, headers=headers, json={"pinned": True}).json()
    assert pinned["pinned_at"] is not None and pinned["title"] == "Revenue check"
    listed = client.get("/v2/research/topics", headers=headers).json()
    assert [(item["title"], item["pinned_at"] is not None) for item in listed] == [("Revenue check", True)]

    # Metadata lives beside the topic document, so appending a version still sees the expected revision.
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": topic["versions"][-1]["version_id"], "content": {}})
    assert saved.status_code == 201, saved.text
    assert saved.json()["title"] == "Revenue check"
    stored = main.RESEARCH_TOPICS.get(topic["topic_id"])
    assert stored is not None and stored.title is None and stored.pinned_at is None

    cleared = client.patch(path, headers=headers, json={"title": "", "pinned": False}).json()
    assert cleared["title"] is None and cleared["pinned_at"] is None
    assert client.patch(path, headers=headers, json={}).status_code == 422
    assert client.patch(path, headers=headers, json={"title": "x" * 201}).status_code == 422


def test_soft_delete_hides_the_topic_until_restored(monkeypatch):
    client, headers, topic, path = start(monkeypatch)
    deleted = client.delete(path, headers=headers)
    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body["topic_id"] == topic["topic_id"] and body["purge_after"] > body["deleted_at"]
    assert client.get("/v2/research/topics", headers=headers).json() == []
    assert client.get(path, headers=headers).status_code == 404
    assert client.get(path + "/runs", headers=headers).status_code == 404
    assert client.patch(path, headers=headers, json={"title": "x"}).status_code == 404

    restored = client.post(path + "/restore", headers=headers, json={})
    assert restored.status_code == 200, restored.text
    assert restored.json()["versions"] == topic["versions"]
    assert [item["topic_id"] for item in client.get("/v2/research/topics", headers=headers).json()] == [topic["topic_id"]]


def test_delete_is_refused_while_a_run_is_active(monkeypatch):
    client, headers, _, path = start(monkeypatch)
    monkeypatch.setattr(main, "_topic_has_active_run", lambda account_id, topic_id: True)
    refused = client.delete(path, headers=headers)
    assert refused.status_code == 409
    assert client.get(path, headers=headers).status_code == 200


def test_deleted_topics_are_purged_after_retention(monkeypatch):
    client, headers, topic, path = start(monkeypatch)
    deletion = client.delete(path, headers=headers).json()
    owner = topic["owner_id"]
    from datetime import datetime
    deleted_at = datetime.fromisoformat(deletion["deleted_at"])
    assert main.research_topics.list(owner, now=deleted_at + DELETED_TOPIC_RETENTION - timedelta(seconds=1)) == []
    assert main.RESEARCH_TOPICS.get(topic["topic_id"]) is not None
    main.research_topics.list(owner, now=deleted_at + DELETED_TOPIC_RETENTION)
    assert main.RESEARCH_TOPICS.get(topic["topic_id"]) is None
    assert main.RESEARCH_TOPIC_META.get(topic["topic_id"]) is None
    assert client.post(path + "/restore", headers=headers, json={}).status_code == 404


def test_other_accounts_cannot_manage_a_topic(monkeypatch):
    _, _, _, path = start(monkeypatch, "owner")
    client, other, _, _ = start(monkeypatch, "other")
    assert client.patch(path, headers=other, json={"title": "mine"}).status_code == 404
    assert client.delete(path, headers=other).status_code == 404
    assert client.post(path + "/restore", headers=other, json={}).status_code == 404
