"""Opt-in destructive integration test: use a disposable PostgreSQL database only."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(not os.getenv("FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN"), reason="disposable PostgreSQL not configured")
@pytest.mark.parametrize("phase", ["before_provider", "generation", "generation_checkpoint", "review", "isolated_review_second", "result_commit"])
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
    elif phase == "generation_checkpoint":
        # Generation was billed, but its checkpoint write is still in the
        # transaction when the process exits. Do not replay the provider.
        start = start.replace('        entered.set()\n', '''        citation = {"source_id": source_id, "quotation": "Revenue was 100"}
        return {"claims": [{"text": "Revenue was 100", "citations": [citation]}],
                "counterarguments": [{"text": "Revenue alone does not establish profit", "citations": [citation]}],
                "facts": [], "calculations": [], "limitations": []}, 55
        entered.set()
''')
        start = start.replace('api.challenge_provider = Provider()\n', '''api.challenge_provider = Provider()
original_add_version = api.research_topics.add_version
def hold_after_checkpoint_write(*args, **kwargs):
    result = original_add_version(*args, **kwargs)
    entered.set()
    Event().wait(30)
    raise AssertionError("process should be terminated")
api.research_topics.add_version = hold_after_checkpoint_write
''')
        start = start.replace('assert source.status_code == 201, source.text', 'assert source.status_code == 201, source.text\nsource_id = source.json()["source_id"]')
        start = start.replace('payload = {"parent_version_id":value["versions"][0]["version_id"]}',
                              'payload = {"parent_version_id":value["versions"][0]["version_id"], "support_review":True, "max_model_calls":2, "max_total_tokens":9000}')
        recover = recover.replace('assert reservation is not None and reservation.status == "reserved"',
                                  'assert reservation is not None and reservation.status == "settled" and reservation.actual_tokens == 55\nassert api.billing_ledger.usage.get("model:" + op + ":support-review") is None')
    elif phase == "review":
        start = start.replace('        entered.set()\n', '''        if system != api.REVIEW_SYSTEM_PROMPT:
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
    elif phase == "isolated_review_second":
        start = start.replace('        entered.set()\n', '''        if system != api.REVIEW_SYSTEM_PROMPT:
            citation = {"source_id": source_id, "quotation": "Revenue was 100"}
            return {"claims": [{"text": "Revenue was 100", "citations": [citation]},
                               {"text": "Revenue was reported", "citations": [citation]}],
                    "counterarguments": [], "facts": [], "calculations": [], "limitations": []}, 55
        self.review_count = getattr(self, "review_count", 0) + 1
        if self.review_count == 1:
            return {"reviews": [{"claim_index": 0, "verdict": "supports"}]}, 10
        entered.set()
''')
        start = start.replace('assert source.status_code == 201, source.text', 'assert source.status_code == 201, source.text\nsource_id = source.json()["source_id"]')
        start = start.replace('payload = {"parent_version_id":value["versions"][0]["version_id"]}',
                              'payload = {"parent_version_id":value["versions"][0]["version_id"], "support_review":True, "support_review_mode":"isolated", "max_model_calls":3, "max_total_tokens":9000, "require_counterarguments":False}')
        recover = recover.replace('assert topic.status_code == 200 and len(topic.json()["versions"]) == 1', '''assert topic.status_code == 200 and len(topic.json()["versions"]) == 5
checkpoint = topic.json()["versions"][-1]["content"]
assert checkpoint["run"]["checkpoint"] == "isolated_review" and checkpoint["run"]["status"] == "partial"
assert checkpoint["claims"][0]["support_review"]["verdict"] == "supports"
assert "support_review" not in checkpoint["claims"][1]
assert checkpoint["run"]["support_review_omissions"] == [{"claim_index": 1, "reason": "in_progress"}]''')
        recover = recover.replace('assert reservation is not None and reservation.status == "reserved"', '''assert reservation is not None and reservation.status == "settled" and reservation.actual_tokens == 55
first = api.billing_ledger.usage.get("model:" + op + ":support-review:0")
second = api.billing_ledger.usage.get("model:" + op + ":support-review:1")
assert first is not None and first.status == "settled" and first.actual_tokens == 10
assert second is not None and second.status == "reserved"''')
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
def test_committed_result_survives_process_exit_before_worker_cache_update(tmp_path):
    """Crash after terminal commit and replay the same request in a fresh process."""
    env = dict(os.environ, FORESIGHTNINE_STORAGE_MODE="postgres", FORESIGHTNINE_LOCAL_AUTH_MODE="mock",
               FORESIGHTNINE_POSTGRES_DSN=os.environ["FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN"],
               STAGEB_TEST_STATE=str(tmp_path / "committed.json"),
               STAGEB_TEST_MARKER=str(tmp_path / "committed-marker"),
               STAGEB_PROVIDER_CALLS=str(tmp_path / "provider-calls"))
    root = Path(__file__).resolve().parents[3]
    start = r'''
import json, os, sys
from threading import Event
from types import SimpleNamespace
from uuid import uuid4
sys.path.insert(0, "apps/api")
import app.main as api
from fastapi.testclient import TestClient
ready, entered = Event(), Event()
class Provider:
    name = "post-commit-crash-test"
    policy = SimpleNamespace(timeout_seconds=30, max_input_tokens=3000, max_output_tokens=1500)
    def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
        with open(os.environ["STAGEB_PROVIDER_CALLS"], "a") as f: f.write("called\n")
        entered.set()
        assert ready.wait(10)
        citation = {"source_id": source_id, "quotation": "Revenue was 100"}
        return {"claims":[{"text":"Revenue was 100", "citations":[citation]}],
                "counterarguments":[{"text":"Revenue alone does not establish profit", "citations":[citation]}],
                "facts":[], "calculations":[], "limitations":[]}, 55
api.challenge_provider = Provider()
client = TestClient(api.app)
reg = client.post("/v1/auth/local/register", json={"email":f"commit-{uuid4().hex}@example.test", "password":"correct horse battery staple!"})
assert reg.status_code == 201, reg.text
token = reg.json()["access_token"]; headers = {"Authorization":"Bearer " + token}
source = client.post("/v2/research/sources", headers=headers, json={"title":"Report", "text":"Revenue was 100.", "permissions":{"store":True,"model_input":True,"display":True}})
assert source.status_code == 201, source.text
source_id = source.json()["source_id"]
topic = client.post("/v2/research/topics", headers=headers, json={"question":"Revenue?", "source_ids":[source_id]})
assert topic.status_code == 201, topic.text
value = topic.json(); path = "/v2/research/topics/" + value["topic_id"] + "/runs"
payload = {"parent_version_id": value["versions"][0]["version_id"]}
original_finish = api.operations.store.finish
def exit_after_finish(**kwargs):
    result = original_finish(**kwargs)
    assert result[0]
    with open(os.environ["STAGEB_TEST_MARKER"], "w") as f: f.write("committed")
    os._exit(0)
api.operations.store.finish = exit_after_finish
started = client.post(path, headers={**headers, "Idempotency-Key":"post-commit-key"}, json=payload)
assert started.status_code == 202, started.text
assert entered.wait(10)
with open(os.environ["STAGEB_TEST_STATE"], "w") as f:
    json.dump({"token":token, "path":path, "payload":payload, "operation":started.json()["operation"]["operation_id"]}, f)
ready.set()
assert Event().wait(30), "worker did not exit after commit"
'''
    recover = r'''
import json, os, sys
sys.path.insert(0, "apps/api")
import app.main as api
from fastapi.testclient import TestClient
with open(os.environ["STAGEB_TEST_STATE"]) as f: state = json.load(f)
with open(os.environ["STAGEB_TEST_MARKER"]) as f: assert f.read() == "committed"
client = TestClient(api.app); headers = {"Authorization":"Bearer " + state["token"]}
path = state["path"]; op = state["operation"]
check = client.get(path + "/" + op, headers=headers)
assert check.status_code == 200 and check.json()["operation"]["status"] == "completed"
topic = client.get(path.rsplit("/runs", 1)[0], headers=headers)
assert topic.status_code == 200 and len(topic.json()["versions"]) == 2
assert topic.json()["versions"][-1]["content"]["claims"][0]["citations"][0]["quotation"] == "Revenue was 100"
usage = api.billing_ledger.usage.get("model:" + op)
assert usage is not None and usage.status == "settled" and usage.actual_tokens == 55
retry = client.post(path, headers={**headers, "Idempotency-Key":"post-commit-key"}, json=state["payload"])
assert retry.status_code == 202 and retry.headers["Idempotency-Replayed"] == "true"
assert retry.json()["operation"]["status"] == "completed"
with open(os.environ["STAGEB_PROVIDER_CALLS"]) as f: assert f.read() == "called\n"
assert len(client.get(path.rsplit("/runs", 1)[0], headers=headers).json()["versions"]) == 2
assert client.delete("/v1/auth/local/account", headers=headers).status_code == 200
'''
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


@pytest.mark.skipif(not os.getenv("FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN"), reason="disposable PostgreSQL not configured")
def test_corrected_fact_confirmation_and_calculation_survive_process_restart(tmp_path):
    """Versioned user evidence and confirmation survive a fresh PostgreSQL client process."""
    env=dict(os.environ,FORESIGHTNINE_STORAGE_MODE="postgres",FORESIGHTNINE_LOCAL_AUTH_MODE="mock",
             FORESIGHTNINE_POSTGRES_DSN=os.environ["FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN"],
             STAGEB_TEST_STATE=str(tmp_path/"fact-confirmation.json"))
    root=Path(__file__).resolve().parents[3]
    start=r'''
import json, os, sys
from uuid import uuid4
sys.path.insert(0,"apps/api")
from fastapi.testclient import TestClient
import app.main as api
client=TestClient(api.app)
r=client.post("/v1/auth/local/register",json={"email":f"persist-fact-{uuid4().hex}@example.test","password":"correct horse battery staple!"})
assert r.status_code==201,r.text
headers={"Authorization":"Bearer "+r.json()["access_token"]}
s=client.post("/v2/research/sources",headers=headers,json={"title":"Report","text":"Goods had a $36.7 billion decrease. Goods had a $40.0 billion decrease. Services increased $44.9 billion.",
 "permissions":{"store":True,"display":True,"model_input":True}});assert s.status_code==201,s.text
sid=s.json()["source_id"]
t=client.post("/v2/research/topics",headers=headers,json={"question":"Spending changes?","source_ids":[sid]});assert t.status_code==201,t.text
topic=t.json();path="/v2/research/topics/"+topic["topic_id"]
facts=[{"fact_id":"goods","label":"Goods change","value":"-40.0","unit":"billion USD","currency":"USD","period":"2023-03",
        "source_id":sid,"quotation":"a $36.7 billion decrease"},
       {"fact_id":"services","label":"Services change","value":"44.9","unit":"billion USD","currency":"USD","period":"2023-03",
        "source_id":sid,"quotation":"increased $44.9 billion"}]
r=client.post(path+"/versions",headers=headers,json={"parent_version_id":topic["versions"][0]["version_id"],"content":{"facts":facts}})
assert r.status_code==201,r.text
bad=r.json()["versions"][-1];assert bad["content"]["facts"][0]["source_value_warnings"]==["value_missing"]
content=bad["content"];content["facts"][0]["quotation"]="a $40.0 billion decrease";content["facts"][0].pop("location",None)
r=client.post(path+"/versions",headers=headers,json={"parent_version_id":bad["version_id"],"content":content})
assert r.status_code==201,r.text
corrected=r.json()["versions"][-1];assert corrected["content"]["facts"][0]["direction_context_unverified"]
r=client.post(path+"/facts/confirm-direction",headers=headers,json={"parent_version_id":corrected["version_id"],"fact_id":"goods"});assert r.status_code==201,r.text
confirmed=r.json()["versions"][-1]
r=client.post(path+"/calculations",headers=headers,json={"parent_version_id":confirmed["version_id"],"formula":"difference","first_fact_id":"services","second_fact_id":"goods"})
assert r.status_code==201,r.text
final=r.json()["versions"][-1];assert final["content"]["calculations"][0]["value"]=="84.90"
with open(os.environ["STAGEB_TEST_STATE"],"w") as f:json.dump({"token":headers["Authorization"],"path":path,"version":final["version_id"],"bad":bad["version_id"]},f)
'''
    recover=r'''
import json, os, sys
sys.path.insert(0,"apps/api")
from fastapi.testclient import TestClient
import app.main as api
with open(os.environ["STAGEB_TEST_STATE"]) as f:state=json.load(f)
client=TestClient(api.app);headers={"Authorization":state["token"]};path=state["path"]
r=client.get(path,headers=headers);assert r.status_code==200,r.text
versions=r.json()["versions"];final=versions[-1];assert final["version_id"]==state["version"]
assert next(v for v in versions if v["version_id"]==state["bad"])["content"]["facts"][0]["source_value_warnings"]==["value_missing"]
fact=next(f for f in final["content"]["facts"] if f["fact_id"]=="goods")
assert fact["quotation"]=="a $40.0 billion decrease" and fact["direction_confirmation"]["confirmed_by"]=="user"
assert api.direction_user_confirmed(fact)
assert final["content"]["calculations"][0]["value"]=="84.90" and final["content"]["calculations"][0]["input_status"]=="matched"
content=final["content"];content["facts"][0]["label"]="Different metric"
r=client.post(path+"/versions",headers=headers,json={"parent_version_id":final["version_id"],"content":content});assert r.status_code==201,r.text
changed=r.json()["versions"][-1];assert not changed["content"]["facts"][0].get("direction_confirmation")
assert changed["content"]["calculations"][0]["input_status"]=="unverifiable"
r=client.post(path+"/calculations/recalculate",headers=headers,json={"parent_version_id":changed["version_id"],"calculation_index":0});assert r.status_code==422,r.text
assert client.delete("/v1/auth/local/account",headers=headers).status_code==200
'''
    for script in (start,recover):
        result=subprocess.run([sys.executable,"-c",script],cwd=root,env=env,capture_output=True,text=True,timeout=60)
        assert result.returncode==0,result.stderr


@pytest.mark.skipif(not os.getenv("FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN"), reason="disposable PostgreSQL not configured")
def test_reextraction_and_selected_sources_survive_process_restart(tmp_path):
    """Source provenance and a filtered run remain durable across processes."""
    env=dict(os.environ,FORESIGHTNINE_STORAGE_MODE="postgres",FORESIGHTNINE_LOCAL_AUTH_MODE="mock",
             FORESIGHTNINE_POSTGRES_DSN=os.environ["FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN"],
             STAGEB_TEST_STATE=str(tmp_path/"source-selection.json"), STAGEB_PROVIDER_CALLS=str(tmp_path/"provider-calls"))
    root=Path(__file__).resolve().parents[3]
    start=r'''
import base64,json,os,sys,time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
sys.path.insert(0,"apps/api")
from fastapi.testclient import TestClient
import app.main as api
client=TestClient(api.app)
r=client.post("/v1/auth/local/register",json={"email":f"persist-pdf-{uuid4().hex}@example.test","password":"correct horse battery staple!"})
assert r.status_code==201,r.text
headers={"Authorization":"Bearer "+r.json()["access_token"]}
raw=Path("docs/product/phase-a/209-cross-domain-frozen/bea-2021-may.pdf").read_bytes()
r=client.post("/v2/research/sources/pdf",headers=headers,json={"title":"Report","text_order":"reading","pdf_base64":base64.b64encode(raw).decode(),
 "permissions":{"store":True,"display":True,"model_input":True,"export":True}});assert r.status_code==201,r.text
old=r.json()["source_id"]
r=client.post("/v2/research/sources/"+old+"/reextract",headers=headers,json={"text_order":"layout"});assert r.status_code==201,r.text
new=r.json()["source_id"];assert r.json()["derived_from_source_id"]==old
t=client.post("/v2/research/topics",headers=headers,json={"question":"Consumer spending?","source_ids":[old,new]});assert t.status_code==201,t.text
topic=t.json();path="/v2/research/topics/"+topic["topic_id"];parent=topic["versions"][0]["version_id"]
class Provider:
 name="persist-source-selection";policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
 def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
  with open(os.environ["STAGEB_PROVIDER_CALLS"],"a") as f:f.write("called\n")
  assert "Current-dollar consumer spending" in prompt
  return {"claims":[],"counterarguments":[],"facts":[],"calculations":[],"limitations":["Synthetic test result."]},10
api.challenge_provider=Provider()
payload={"parent_version_id":parent,"selected_source_ids":[new],"max_search_calls":0,"require_counterarguments":False}
r=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"persist-selection"},json=payload);assert r.status_code==202,r.text
op=r.json()["operation"]["operation_id"]
for _ in range(200):
 status=client.get(path+"/runs/"+op,headers=headers).json()["operation"]["status"]
 if status in {"completed","failed","cancelled"}:break
 time.sleep(.05)
assert status=="completed",status
with open(os.environ["STAGEB_TEST_STATE"],"w") as f:json.dump({"token":headers["Authorization"],"path":path,"old":old,"new":new,"op":op,"payload":payload},f)
'''
    recover=r'''
import json,os,sys
sys.path.insert(0,"apps/api")
from fastapi.testclient import TestClient
import app.main as api
with open(os.environ["STAGEB_TEST_STATE"]) as f:s=json.load(f)
client=TestClient(api.app);headers={"Authorization":s["token"]};path=s["path"]
r=client.get("/v2/research/sources/"+s["new"],headers=headers);assert r.status_code==200,r.text
assert r.json()["derived_from_source_id"]==s["old"] and r.json()["pdf_text_order"]=="layout"
old=client.get("/v2/research/sources/"+s["old"],headers=headers);assert old.status_code==200 and old.json()["pdf_text_order"]=="reading"
assert client.get("/v2/research/sources/"+s["new"]+"/original",headers=headers).status_code==200
topic=client.get(path,headers=headers).json();assert set(topic["versions"][-1]["source_ids"])=={s["old"],s["new"]}
inputs=client.get(path+"/runs/"+s["op"]+"/inputs",headers=headers).json()
assert [item["source_id"] for item in inputs["sources"]]==[s["new"]]
assert inputs["sources"][0]["derived_from_source_id"]==s["old"]
r=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"persist-selection"},json=s["payload"])
assert r.status_code==202 and r.headers.get("Idempotency-Replayed")=="true"
with open(os.environ["STAGEB_PROVIDER_CALLS"]) as f:assert f.read().splitlines()==["called"]
assert client.delete("/v1/auth/local/account",headers=headers).status_code==200
'''
    for script in (start,recover):
        result=subprocess.run([sys.executable,"-c",script],cwd=root,env=env,capture_output=True,text=True,timeout=60)
        assert result.returncode==0,result.stderr
