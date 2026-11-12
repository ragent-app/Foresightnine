"""Opt-in destructive integration test: use a disposable PostgreSQL database only."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(not os.getenv("FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN"), reason="disposable PostgreSQL not configured")
@pytest.mark.parametrize("phase", ["before_provider", "generation", "review", "result_commit"])
def test_interrupted_research_retains_inputs_and_cannot_replay_external_call(tmp_path, phase):
    state_file = tmp_path / "state.json"
    env = dict(os.environ, FORESIGHTNINE_STORAGE_MODE="postgres", FORESIGHTNINE_LOCAL_AUTH_MODE="mock",
               FORESIGHTNINE_POSTGRES_DSN=os.environ["FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN"],
               STAGEB_TEST_STATE=str(state_file))
    root = Path(__file__).resolve().parents[3]
    start = r'''
import json, os, sys
from threading import Event
from types import SimpleNamespace
from uuid import uuid4
sys.path.insert(0, "apps/api")
import app.main as api
from fastapi.testclient import TestClient
entered = Event()
class Provider:
    name = "restart-test"
    policy = SimpleNamespace(timeout_seconds=30, max_input_tokens=3000, max_output_tokens=1500)
    def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
        entered.set()
        Event().wait(30)
        raise AssertionError("process should be terminated")
api.challenge_provider = Provider()
client = TestClient(api.app)
registered = client.post("/v1/auth/local/register", json={"email": f"restart-{uuid4().hex}@example.test", "password": "correct horse battery staple!"})
assert registered.status_code == 201, registered.text
token = registered.json()["access_token"]
headers = {"Authorization": "Bearer " + token}
source = client.post("/v2/research/sources", headers=headers, json={"title":"Report", "text":"Revenue was 100.", "permissions":{"store":True,"model_input":True,"display":True}})
assert source.status_code == 201, source.text
topic = client.post("/v2/research/topics", headers=headers, json={"question":"Revenue?", "source_ids":[source.json()["source_id"]]})
assert topic.status_code == 201, topic.text
value = topic.json(); path = "/v2/research/topics/" + value["topic_id"] + "/runs"
payload = {"parent_version_id":value["versions"][0]["version_id"]}
started = client.post(path, headers={**headers, "Idempotency-Key":"restart-test-key"}, json=payload)
assert started.status_code == 202, started.text
assert entered.wait(10)
with open(os.environ["STAGEB_TEST_STATE"], "w") as f:
    json.dump({"token":token, "path":path, "payload":payload, "operation":started.json()["operation"]["operation_id"]}, f)
os._exit(0)
'''
    recover = r'''
import json, os, sys
from datetime import timedelta
sys.path.insert(0, "apps/api")
import app.main as api
from fastapi.testclient import TestClient
with open(os.environ["STAGEB_TEST_STATE"]) as f: state = json.load(f)
headers = {"Authorization":"Bearer " + state["token"]}
client = TestClient(api.app)
path = state["path"]; op = state["operation"]
assert client.get(path + "/" + op, headers=headers).json()["operation"]["status"] == "running"
assert api.operations.recover_expired_leases(now=api.lifecycle_clock.now() + timedelta(seconds=301)) >= 1
assert client.get(path + "/" + op, headers=headers).json()["operation"]["status"] == "failed"
topic = client.get(path.rsplit("/runs", 1)[0], headers=headers)
assert topic.status_code == 200 and len(topic.json()["versions"]) == 1
snapshot = client.get(path + "/" + op + "/inputs", headers=headers)
assert snapshot.status_code == 200 and snapshot.json()["parent_version_id"] == state["payload"]["parent_version_id"]
reservation = api.billing_ledger.usage.get("model:" + op)
assert reservation is not None and reservation.status == "reserved"
retry = client.post(path, headers={**headers, "Idempotency-Key":"restart-test-key"}, json=state["payload"])
assert retry.status_code == 202 and retry.headers["Idempotency-Replayed"] == "true"
assert retry.json()["operation"]["status"] == "failed"
assert client.delete("/v1/auth/local/account", headers=headers).status_code == 200
assert api.RESEARCH_RUN_INPUTS.get(op) is None
'''
    if phase == "before_provider":
        # Admission and its input snapshot are durable, but no provider call or
        # billing reservation has begun when the worker disappears.
        start = start.replace('api.challenge_provider = Provider()\n', '''api.challenge_provider = Provider()
def hold_before_reservation(*args, **kwargs):
    entered.set()
    Event().wait(30)
    raise AssertionError("process should be terminated")
api._reserve_model_call = hold_before_reservation
''')
        recover = recover.replace('assert reservation is not None and reservation.status == "reserved"',
                                  'assert reservation is None')
    elif phase == "review":
        start = start.replace('        entered.set()\n', '''        if "reviewing whether supplied evidence" not in system:
            citation = {"source_id": source_id, "quotation": "Revenue was 100"}
            return {"claims": [{"text": "Revenue was 100", "citations": [citation]}],
                    "counterarguments": [{"text": "Revenue alone does not establish profit", "citations": [citation]}],
                    "facts": [], "calculations": [], "limitations": []}, 55
        entered.set()
''')
        start = start.replace('assert source.status_code == 201, source.text', 'assert source.status_code == 201, source.text\nsource_id = source.json()["source_id"]')
        start = start.replace('payload = {"parent_version_id":value["versions"][0]["version_id"]}',
                              'payload = {"parent_version_id":value["versions"][0]["version_id"], "support_review":True, "max_model_calls":2, "max_total_tokens":9000}')
        recover = recover.replace('assert topic.status_code == 200 and len(topic.json()["versions"]) == 1', '''assert topic.status_code == 200 and len(topic.json()["versions"]) == 2
checkpoint = topic.json()["versions"][-1]["content"]
assert checkpoint["run"]["checkpoint"] == "generation" and checkpoint["run"]["status"] == "partial"
assert checkpoint["claims"][0]["citations"][0]["quotation"] == "Revenue was 100"''')
        recover = recover.replace('assert reservation is not None and reservation.status == "reserved"', '''assert reservation is not None and reservation.status == "settled" and reservation.actual_tokens == 55
review_reservation = api.billing_ledger.usage.get("model:" + op + ":support-review")
assert review_reservation is not None and review_reservation.status == "reserved"''')
    elif phase == "result_commit":
        # Exit while the result document has been written in the same
        # uncommitted PostgreSQL transaction as the operation's terminal state.
        start = start.replace('        entered.set()\n', '''        citation = {"source_id": source_id, "quotation": "Revenue was 100"}
        return {"claims": [{"text": "Revenue was 100", "citations": [citation]}],
                "counterarguments": [{"text": "Revenue alone does not establish profit", "citations": [citation]}],
                "facts": [], "calculations": [], "limitations": []}, 55
        entered.set()
''')
        start = start.replace('api.challenge_provider = Provider()\n', '''api.challenge_provider = Provider()
original_add_version = api.research_topics.add_version
def hold_after_result_write(*args, **kwargs):
    result = original_add_version(*args, **kwargs)
    entered.set()
    Event().wait(30)
    raise AssertionError("process should be terminated")
api.research_topics.add_version = hold_after_result_write
''')
        start = start.replace('assert source.status_code == 201, source.text', 'assert source.status_code == 201, source.text\nsource_id = source.json()["source_id"]')
        recover = recover.replace('assert reservation is not None and reservation.status == "reserved"',
                                  'assert reservation is not None and reservation.status == "settled" and reservation.actual_tokens == 55')
    for script in (start, recover):
        result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not os.getenv("FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN"), reason="disposable PostgreSQL not configured")
def test_uncommitted_admission_rolls_back_before_provider_and_allows_safe_retry(tmp_path):
    """Kill a process after snapshot write, before the admission transaction commits."""
    env = dict(os.environ, FORESIGHTNINE_STORAGE_MODE="postgres", FORESIGHTNINE_LOCAL_AUTH_MODE="mock",
               FORESIGHTNINE_POSTGRES_DSN=os.environ["FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN"],
               STAGEB_TEST_STATE=str(tmp_path / "admission.json"),
               STAGEB_PROVIDER_CALLS=str(tmp_path / "provider-calls"))
    root = Path(__file__).resolve().parents[3]
    start = r'''
import json, os, sys
from threading import Event, Thread
from types import SimpleNamespace
from uuid import uuid4
sys.path.insert(0, "apps/api")
import app.main as api
from fastapi.testclient import TestClient
entered = Event()
class Provider:
    name = "admission-crash-test"
    policy = SimpleNamespace(timeout_seconds=30, max_input_tokens=3000, max_output_tokens=1500)
    def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
        with open(os.environ["STAGEB_PROVIDER_CALLS"], "a") as f: f.write("unexpected\n")
        raise AssertionError("provider must not start before admission commits")
api.challenge_provider = Provider()
client = TestClient(api.app)
registered = client.post("/v1/auth/local/register", json={"email": f"admission-{uuid4().hex}@example.test", "password": "correct horse battery staple!"})
assert registered.status_code == 201, registered.text
token = registered.json()["access_token"]; headers = {"Authorization": "Bearer " + token}
source = client.post("/v2/research/sources", headers=headers, json={"title":"Report", "text":"Revenue was 100.", "permissions":{"store":True,"model_input":True,"display":True}})
assert source.status_code == 201, source.text
topic = client.post("/v2/research/topics", headers=headers, json={"question":"Revenue?", "source_ids":[source.json()["source_id"]]})
assert topic.status_code == 201, topic.text
value = topic.json(); path = "/v2/research/topics/" + value["topic_id"] + "/runs"
payload = {"parent_version_id": value["versions"][0]["version_id"]}
original_put = api.RESEARCH_RUN_INPUTS.put
def hold_after_snapshot(operation_id, value, **kwargs):
    result = original_put(operation_id, value, **kwargs)
    with open(os.environ["STAGEB_TEST_STATE"], "w") as f:
        json.dump({"token":token,"path":path,"payload":payload,"operation":operation_id,"source_id":source.json()["source_id"]}, f)
    entered.set()
    Event().wait(30)
    raise AssertionError("process should be terminated")
api.RESEARCH_RUN_INPUTS.put = hold_after_snapshot
Thread(target=lambda: client.post(path, headers={**headers,"Idempotency-Key":"admission-crash-key"}, json=payload), daemon=True).start()
assert entered.wait(10)
os._exit(0)
'''
    recover = r'''
import json, os, sys, time
from types import SimpleNamespace
sys.path.insert(0, "apps/api")
import app.main as api
from fastapi.testclient import TestClient
with open(os.environ["STAGEB_TEST_STATE"]) as f: state = json.load(f)
assert not os.path.exists(os.environ["STAGEB_PROVIDER_CALLS"])
headers = {"Authorization":"Bearer " + state["token"]}; path = state["path"]; op = state["operation"]
assert api.operations.store.get_operation(op) is None
assert api.RESEARCH_RUN_INPUTS.get(op) is None
assert api.billing_ledger.usage.get("model:" + op) is None
class Provider:
    name = "admission-retry-test"
    policy = SimpleNamespace(timeout_seconds=30, max_input_tokens=3000, max_output_tokens=1500)
    def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
        with open(os.environ["STAGEB_PROVIDER_CALLS"], "a") as f: f.write("called\n")
        citation = {"source_id":state["source_id"], "quotation":"Revenue was 100"}
        return {"claims":[{"text":"Revenue was 100", "citations":[citation]}],
                "counterarguments":[{"text":"Revenue is not profit", "citations":[citation]}],
                "facts":[], "calculations":[], "limitations":[]}, 55
api.challenge_provider = Provider(); client = TestClient(api.app)
request_headers = {**headers,"Idempotency-Key":"admission-crash-key"}
started = client.post(path, headers=request_headers, json=state["payload"])
assert started.status_code == 202, started.text
assert started.headers.get("Idempotency-Replayed") != "true"
assert started.json()["operation"]["operation_id"] == op
for _ in range(200):
    status = client.get(path + "/" + op, headers=headers).json()["operation"]["status"]
    if status in {"completed", "failed", "cancelled"}: break
    time.sleep(.05)
assert status == "completed", status
assert api.RESEARCH_RUN_INPUTS.get(op)["parent_version_id"] == state["payload"]["parent_version_id"]
assert len(client.get(path.rsplit("/runs", 1)[0], headers=headers).json()["versions"]) == 2
replay = client.post(path, headers=request_headers, json=state["payload"])
assert replay.status_code == 202 and replay.headers.get("Idempotency-Replayed") == "true"
with open(os.environ["STAGEB_PROVIDER_CALLS"]) as f: assert f.read().splitlines() == ["called"]
assert client.delete("/v1/auth/local/account", headers=headers).status_code == 200
'''
    for script in (start, recover):
        result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
