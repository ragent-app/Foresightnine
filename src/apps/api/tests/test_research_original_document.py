"""Mechanical API smoke test on a retained public BEA document.

The fixed model response is a test double, not a measurement of model quality.
Historical availability of the retrieved copy is not established by its hash.
"""
import base64
import json
from hashlib import sha256
from pathlib import Path
import shutil
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as api
from app.research_analysis import _select_windows
from app.research_uploads import _extract_text


PDF = Path(__file__).resolve().parents[3] / "docs/product/phase-a/source-snapshots/bea-gdp-2023-q4-advance.pdf"
FED_PDF = PDF.with_name("fed-fomc-2024-09-18.pdf")
HASH = "3cc68c16d15ce51e8a5c821c407e32b9f2185edbcadbaadfb1a3fed01ed9b31d"
QUOTE = "Real GDP increased 2.5 percent in 2023 (from the 2022 annual level to the 2023 annual level), compared"
QUARTER_QUOTE = "Real gross domestic product (GDP) increased at an annual rate of 3.3 percent in the fourth quarter of\n2023"


@pytest.mark.skipif(not FED_PDF.is_file() or not shutil.which("pdftotext") or not shutil.which("prlimit"),
                    reason="retained official document or PDF tools unavailable")
def test_chinese_policy_question_selects_english_decision_in_retained_fed_pdf():
    text = _extract_text(FED_PDF.read_bytes())
    windows = _select_windows(text, "2024 年 9 月美联储决定的联邦基金目标区间和变化幅度是多少？", 508, 2)
    assert any("1/2 percentage point to 4-3/4 to 5 percent" in passage for _, passage in windows)


@pytest.mark.skipif(not PDF.is_file() or not shutil.which("pdftotext") or not shutil.which("prlimit"),
                    reason="retained official document or PDF tools unavailable")
@pytest.mark.parametrize("question", ["What was 2023 annual real GDP growth, and how does it differ from the quarterly annualized rate?",
                                      "2023年全年实际GDP增长初值是多少？与季度折年率有什么区别？"])
def test_real_bea_pdf_is_searched_cited_and_reopened_without_a_live_model(monkeypatch, question):
    raw = PDF.read_bytes()
    assert sha256(raw).hexdigest() == HASH
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    calls = []
    source_id = ""
    class Provider:
        name = "fixed-test-response"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            calls.append(1)
            passages = [item["text"] for item in json.loads(prompt)["sources"]]
            assert any(QUOTE in passage for passage in passages), "the selected passages must include the annual figure"
            assert any(QUARTER_QUOTE in passage for passage in passages), "the selected passages must include the quarterly figure"
            citation = {"source_id": source_id, "quotation": QUOTE}
            return {"claims": [{"text": "Annual real GDP grew 2.5% in 2023.", "citations": [citation]}],
                    "counterarguments": [{"text": "The annual rate is not the quarterly rate.",
                                          "citations": [{"source_id": source_id, "quotation": QUARTER_QUOTE}]}],
                    "facts": [{"fact_id": "annual", "label": "Annual real GDP growth", "value": "2.5",
                               "unit": "percent", "period": "2023", "citation": citation}],
                    "calculations": [], "limitations": []}, 42
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    registration = client.post("/v1/auth/local/register", json={"email": "bea-original@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    uploaded = client.post("/v2/research/sources/pdf", headers=headers, json={"title": "BEA 2023 advance estimate",
        "pdf_base64": base64.b64encode(raw).decode("ascii"), "published_at": "2024-01-25T13:30:00Z",
        "permissions": {"store": True, "display": True, "model_input": True, "export": False}})
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["response_sha256"] == HASH
    source_id = uploaded.json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": question,
        "as_of": "2024-01-26T00:00:00Z", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    response = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "bea-real-doc-smoke-1"},
        json={"parent_version_id": topic["versions"][0]["version_id"], "max_search_calls": 0})
    assert response.status_code == 202
    op = response.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed" and calls == [1]
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert content["run"]["status"] == "partial"  # The rest of the PDF was not analyzed.
    fact = content["facts"][0]
    assert fact["value"] == "2.5" and fact["location"]["page"] == 2
    location = fact["location"]
    context = client.get(f"/v2/research/sources/{source_id}/context", headers=headers,
        params={"start": location["character_start"], "end": location["character_end"]})
    assert context.status_code == 200 and context.json()["selected"] == QUOTE
    assert context.json()["page_start"] == 2
    assert content["claims"][1]["citations"][0]["location"]["page"] == 1
    assert any("user-declared" in item for item in content["limitations"])
