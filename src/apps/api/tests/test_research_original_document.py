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
from app.research_analysis import SYSTEM_PROMPT, _select_windows, analyze
from app.research_sources import ResearchSource, SourcePermissions
from datetime import datetime, timezone
from app.research_uploads import _extract_text


PDF = Path(__file__).resolve().parents[3] / "docs/product/phase-a/source-snapshots/bea-gdp-2023-q4-advance.pdf"
FED_PDF = PDF.with_name("fed-fomc-2024-09-18.pdf")
BEA_2022_PDF = PDF.with_name("bea-gdp-2022-q4-advance.pdf")
HASH = "3cc68c16d15ce51e8a5c821c407e32b9f2185edbcadbaadfb1a3fed01ed9b31d"
QUOTE = "Real GDP increased 2.5 percent in 2023 (from the 2022 annual level to the 2023 annual level), compared"
QUARTER_QUOTE = "Real gross domestic product (GDP) increased at an annual rate of 3.3 percent in the fourth quarter of\n2023"


@pytest.mark.skipif(not FED_PDF.is_file() or not shutil.which("pdftotext") or not shutil.which("prlimit"),
                    reason="retained official document or PDF tools unavailable")
def test_chinese_policy_question_selects_english_decision_in_retained_fed_pdf():
    text = _extract_text(FED_PDF.read_bytes())
    report = ResearchSource(source_id="fed", owner_id="test", title="Federal Reserve 2024 September decision", text=text,
        kind="user_supplied", obtained_at=datetime.now(timezone.utc), content_sha256="test", retrieval_method="user_pdf",
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    quotation = "1/2 percentage point to 4-3/4 to 5 percent"
    def model(prompt):
        assert any(quotation in part["text"] for part in json.loads(prompt)["sources"])
        return {"claims": [{"text": "The target range was lowered.", "citations": [
            {"source_id": "fed", "quotation": quotation}]}]}, 1
    result = analyze("2024 年 9 月美联储决定的联邦基金目标区间和变化幅度是多少？", [report], None, model,
                     max_prompt_chars=3000 - len(SYSTEM_PROMPT.encode("utf-8")) - 512)
    citation = result.claims[0]["citations"][0]
    assert text[citation["location"]["character_start"]:citation["location"]["character_end"]] == quotation


@pytest.mark.skipif(not BEA_2022_PDF.is_file() or not shutil.which("pdftotext") or not shutil.which("prlimit"),
                    reason="retained official document or PDF tools unavailable")
def test_annual_and_quarterly_question_selects_both_distinct_passages():
    text = _extract_text(BEA_2022_PDF.read_bytes())
    question = ("According to the BEA advance estimate for 2022, report the full-year 2022 real GDP growth "
                "and the fourth-quarter annualized real GDP growth; distinguish their periods and note relevant limitations.")
    windows = _select_windows(text, question, 468, 2)
    assert len(windows) == 2
    assert any("2.9 percent in the fourth quarter" in passage for _, passage in windows)
    assert any("Real GDP increased 2.1 percent in 2022" in passage for _, passage in windows)
    report = ResearchSource(source_id="bea-dev", owner_id="test", title="BEA GDP 2022 advance estimate", text=text,
        kind="user_supplied", obtained_at=datetime.now(timezone.utc), content_sha256="test",
        permissions=SourcePermissions(store=True, model_input=True, display=True))
    def fixed_model(prompt):
        parts = json.loads(prompt)["sources"]
        assert any("2.9 percent in the fourth quarter" in part["text"] for part in parts)
        assert any("Real GDP increased 2.1 percent in 2022" in part["text"] for part in parts)
        return {}, 10
    result = analyze(question, [report], None, fixed_model,
                     max_prompt_chars=3000 - len(SYSTEM_PROMPT.encode("utf-8")) - 512)
    assert len(result.segments) == 2


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


@pytest.mark.parametrize("case,needles", [
    ("GDP_AQ", ("0.7 percent in the fourth quarter", "Real GDP increased 2.1 percent in 2025")),
    ("GDP_REV", ("0.7 percent in the fourth quarter", "Real GDP was revised down 0.7 percentage point")),
    ("FED_RATE", ("lower the target range for the federal funds rate by 1/4 percentage point to 3-1/2 to 3-3/4 percent",)),
    ("FED_DISS", ("Stephen I. Miran, who preferred to lower the target range for the federal funds rate by 1/2 percentage point",)),
])
@pytest.mark.skipif(not shutil.which("pdftotext") or not shutil.which("prlimit"), reason="PDF tools unavailable")
def test_relevant_original_pdf_passages_fit_bounded_prompt(case, needles):
    folder = Path(__file__).resolve().parents[3] / "docs/product/phase-a/37-cross-domain-evaluation"
    task = json.loads((folder / (case + "-input.json")).read_text())
    text = _extract_text((folder / task["source_file"]).read_bytes(), text_order=task["text_order"])
    assert sha256(text.encode()).hexdigest() == task["source_text_sha256"]
    report = ResearchSource(source_id="a" * 39, owner_id="test", title=task["source_title"], text=text,
        kind="user_supplied", obtained_at=datetime.now(timezone.utc),
        published_at=datetime.fromisoformat(task["published_at"].replace("Z", "+00:00")),
        content_sha256="test", retrieval_method="user_pdf", permissions=SourcePermissions(store=True, model_input=True, display=True))
    captured = []
    def model(prompt):
        captured.extend(item["text"] for item in json.loads(prompt)["sources"])
        return {}, 1
    result = analyze(task["question"], [report], None, model,
                     max_prompt_chars=3000 - len(SYSTEM_PROMPT.encode("utf-8")) - 512)
    normalized = [" ".join(part.split()) for part in captured]
    assert all(any(needle in part for part in normalized) for needle in needles)
    assert all(text[segment["character_start"]:segment["character_end"]] for segment in result.segments)


@pytest.mark.skipif(not shutil.which("pdftotext") or not shutil.which("prlimit"), reason="PDF tools unavailable")
def test_two_official_pdfs_replay_persists_candidate_warnings_and_review_omissions(monkeypatch):
    """Replay a saved response; this tests the pipeline, not model quality."""
    folder = Path(__file__).resolve().parents[3] / "docs/product/phase-a/67-two-source-selection-check"
    manifest = json.loads((folder / "manifest.json").read_text())
    recorded = json.loads((folder / "result.json").read_text())
    raw = json.loads((folder / "provider-output.json").read_text())["payload"]
    old_ids = recorded["source_ids"]
    ids = []
    review_indexes = []
    class Provider:
        name = "saved-two-source-response"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            if system == api.REVIEW_SYSTEM_PROMPT:
                review_indexes.extend(item["claim_index"] for item in json.loads(prompt)["claims"])
                # Provider-facing indexes are dense within this batch; the
                # persisted run maps them back to original claim indexes.
                assert review_indexes == list(range(len(review_indexes)))
                return {"reviews": [{"claim_index": index, "verdict": "supports"} for index in review_indexes]}, 20
            parts = json.loads(prompt)["sources"]
            assert any("74.4" in item["text"] for item in parts)
            assert any("88.2" in item["text"] for item in parts)
            serialized = json.dumps(raw, ensure_ascii=False)
            for old, new in zip(old_ids, ids):
                serialized = serialized.replace(old, new)
            return json.loads(serialized), 100
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    auth = client.post("/v1/auth/local/register", json={"email": "two-doc-replay@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    for item in manifest["sources"]:
        pdf = (folder / item["pdf"]).read_bytes()
        assert sha256(pdf).hexdigest() == item["pdf_sha256"]
        response = client.post("/v2/research/sources/pdf", headers=headers, json={"title": item["title"],
            "pdf_base64": base64.b64encode(pdf).decode("ascii"), "published_at": item["published_at"],
            "text_order": "layout", "permissions": {"store": True, "display": True, "model_input": True}})
        assert response.status_code == 201, response.text
        ids.append(response.json()["source_id"])
    task = json.loads((folder / "input.json").read_text())
    topic = client.post("/v2/research/topics", headers=headers, json={"question": task["question"], "source_ids": ids}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "two-doc-replay"}, json={
        "parent_version_id": topic["versions"][0]["version_id"], "max_model_calls": 2,
        "max_total_tokens": 10000, "support_review": True})
    assert receipt.status_code == 202, receipt.text
    op = receipt.json()["operation"]["operation_id"]
    for _ in range(300):
        state = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(.02)
    assert state == "completed"
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert content["run"]["status"] == "partial"
    assert content["amount_differences"][0]["claimed_amount"] == "74.4亿美元"
    assert content["claims"][2]["uncited_fact_values"] and content["claims"][3]["uncited_fact_values"]
    assert not any(content["claims"][i].get("support_review") for i in (0, 2, 3))
    assert not {0, 2, 3}.intersection(content["run"]["support_review_indexes"])
    assert len(content["run"]["support_review_indexes"]) == len(review_indexes)
    assert {entry["claim_index"] for entry in content["run"]["support_review_omissions"]} >= {0, 2, 3}
    difference = content["evidence_differences"][0]
    assert difference["matching_basis"] == "release_qualifiers_ignored"
    assert [fact["value"] for fact in difference["facts"]] == ["74.4", "88.2"]
    assert {fact["source_id"] for fact in difference["facts"]} == set(ids)
    assert all("发布于" in fact["period"] for fact in difference["facts"])


@pytest.mark.skipif(not shutil.which("pdftotext") or not shutil.which("prlimit"), reason="PDF tools unavailable")
@pytest.mark.parametrize("case,needles", [
    ("GDP_ESTIMATE", ("2.9 percent", "2.6 percent", "advance estimate")),
    ("FED_PURCHASES", ("$20 billion for treasury", "$10 billion for agency", "$40 billion per month", "$20 billion per month")),
])
def test_retrieval_keeps_requested_estimates_and_purchase_plans(case, needles):
    folder = Path(__file__).resolve().parents[3] / "docs/product/phase-a/70-multitask-evaluation"
    task = json.loads((folder / (case + "-input.json")).read_text())
    item = task["sources"][0]
    text = _extract_text((folder / item["file"]).read_bytes(), text_order="reading")
    assert sha256(text.encode()).hexdigest() == item["text_sha256"]
    report = ResearchSource(source_id=item["source_id"], owner_id="test", title=item["title"], text=text,
        kind="user_supplied", obtained_at=datetime.now(timezone.utc),
        published_at=datetime.fromisoformat(item["published_at"].replace("Z", "+00:00")),
        content_sha256="test", retrieval_method="user_pdf", permissions=SourcePermissions(store=True, model_input=True, display=True))
    captured = []
    def model(prompt):
        captured.extend(item["text"] for item in json.loads(prompt)["sources"])
        return {}, 1
    result = analyze(task["question"], [report], None, model,
                     max_prompt_chars=3000 - len(SYSTEM_PROMPT.encode("utf-8")) - 512)
    content = " ".join(" ".join(part.lower().split()) for part in captured)
    assert all(needle in content for needle in needles)
    assert all(text[segment["character_start"]:segment["character_end"]] for segment in result.segments)


@pytest.mark.skipif(not shutil.which("pdftotext") or not shutil.which("prlimit"), reason="PDF tools unavailable")
def test_contrary_evidence_selection_keeps_nominal_components_and_original_offsets():
    from app.research_analysis import _prompt_source_text
    folder=Path(__file__).resolve().parents[3]/"docs/product/phase-a/197-fresh-counterevidence"
    task=json.loads((folder/"tasks.json").read_text())[0]
    raw=(folder/task["source"]).read_bytes()
    manifest=json.loads((folder/"manifest.json").read_text())
    assert sha256(raw).hexdigest()==manifest["source_files"][task["source"]]
    text=_extract_text(raw,text_order="layout")
    report=ResearchSource(source_id=task["source_id"],owner_id="test",title=task["source"],text=text,
        kind="user_supplied",obtained_at=datetime.now(timezone.utc),published_at=datetime.fromisoformat(task["published_at"]),
        content_sha256=sha256(text.encode()).hexdigest(),retrieval_method="user_pdf",pdf_text_order="layout",
        permissions=SourcePermissions(store=True,model_input=True,display=True))
    captured=[]
    result=analyze(task["question"],[report],None,lambda prompt:(captured.append(json.loads(prompt)) or {},0),
                   max_prompt_chars=6000-len(SYSTEM_PROMPT.encode())-512)
    assert any("49.1 billion" in part["text"] and "10.0 billion" in part["text"] for part in captured[0]["sources"])
    assert len(result.segments)==len(captured[0]["sources"])
    for segment, sent in zip(result.segments,captured[0]["sources"]):
        original=text[segment["character_start"]:segment["character_end"]]
        assert _prompt_source_text(original,True)==sent["text"]


@pytest.mark.skipif(not shutil.which("pdftotext") or not shutil.which("prlimit"), reason="PDF tools unavailable")
def test_contrary_evidence_selection_keeps_qualitative_component_comparison():
    from app.research_analysis import _prompt_source_text
    folder=Path(__file__).resolve().parents[3]/"docs/product/phase-a/199-fresh-gdp-counterevidence"
    task=json.loads((folder/"tasks.json").read_text())[0]
    raw=(folder/task["source"]).read_bytes()
    manifest=json.loads((folder/"manifest.json").read_text())
    assert sha256(raw).hexdigest()==manifest["source_files"][task["source"]]
    text=_extract_text(raw,text_order="layout")
    report=ResearchSource(source_id=task["source_id"],owner_id="test",title=task["source"],text=text,
        kind="user_supplied",obtained_at=datetime.now(timezone.utc),published_at=datetime.fromisoformat(task["published_at"]),
        content_sha256=sha256(text.encode()).hexdigest(),retrieval_method="user_pdf",pdf_text_order="layout",
        permissions=SourcePermissions(store=True,model_input=True,display=True))
    captured=[]
    result=analyze(task["question"],[report],None,lambda prompt:(captured.append(json.loads(prompt)) or {},0),
                   max_prompt_chars=6000-len(SYSTEM_PROMPT.encode())-512)
    normalized=[" ".join(part["text"].split()) for part in captured[0]["sources"]]
    assert any("increase in services that was partly offset by a decrease in goods" in part for part in normalized)
    for segment, sent in zip(result.segments,captured[0]["sources"]):
        assert _prompt_source_text(text[segment["character_start"]:segment["character_end"]],True)==sent["text"]


@pytest.mark.skipif(not shutil.which("pdftotext") or not shutil.which("prlimit"), reason="PDF tools unavailable")
def test_company_name_prefix_warning_on_recorded_original_pdf():
    from app.research_evidence import flag_citation_subject_context
    folder=Path(__file__).resolve().parents[3]/"docs/product/phase-a/209-cross-domain-frozen"
    raw=(folder/"fed-2025-stress.pdf").read_bytes()
    manifest=json.loads((folder/"manifest.json").read_text())
    assert sha256(raw).hexdigest()==manifest["source_files"]["fed-2025-stress.pdf"]
    text=_extract_text(raw,text_order="reading")
    recorded=json.loads((folder/"BANK_2025-result.json").read_text())["content"]["claims"]
    source=ResearchSource(source_id="source_20900000000000000000000000000001",owner_id="test",title="Report",text=text,
        kind="user_supplied",obtained_at=datetime.now(timezone.utc),content_sha256="test",retrieval_method="user_pdf",
        permissions=SourcePermissions(store=True,display=True,model_input=True))
    warning=flag_citation_subject_context(recorded,[source])[0]["citation_context_warnings"][0]
    assert warning["context_metric"]=="named entity prefix"
    assert text[warning["context_character_start"]:warning["context_character_end"]]=="Bank "


@pytest.mark.skipif(not shutil.which("pdftotext") or not shutil.which("prlimit"), reason="PDF tools unavailable")
def test_company_name_prefix_enters_prompt_with_original_offsets():
    from app.research_analysis import _prompt_source_text
    folder=Path(__file__).resolve().parents[3]/"docs/product/phase-a/209-cross-domain-frozen"
    task=json.loads((folder/"tasks.json").read_text())[0]
    raw=(folder/task["source"]).read_bytes()
    manifest=json.loads((folder/"manifest.json").read_text())
    assert sha256(raw).hexdigest()==manifest["source_files"][task["source"]]
    text=_extract_text(raw,text_order="reading")
    source=ResearchSource(source_id=task["source_id"],owner_id="test",title=task["source"],text=text,
        kind="user_supplied",obtained_at=datetime.now(timezone.utc),content_sha256=sha256(text.encode()).hexdigest(),
        retrieval_method="user_pdf",permissions=SourcePermissions(store=True,display=True,model_input=True))
    captured=[]
    result=analyze(task["question"],[source],None,lambda prompt:(captured.append(json.loads(prompt)) or {},0),
                   max_prompt_chars=6000-len(SYSTEM_PROMPT.encode())-512)
    assert any("Bank of America Corporation" in item["text"] for item in captured[0]["sources"])
    assert len(result.segments)==len(captured[0]["sources"])
    for segment,item in zip(result.segments,captured[0]["sources"]):
        assert _prompt_source_text(text[segment["character_start"]:segment["character_end"]],True)==item["text"]


@pytest.mark.skipif(not shutil.which("pdftotext") or not shutil.which("prlimit"), reason="PDF tools unavailable")
def test_total_question_keeps_early_summary_and_component_evidence():
    from app.research_analysis import _prompt_source_text
    folder=Path(__file__).resolve().parents[3]/"docs/product/phase-a/214-pdf-order-paired"
    task=json.loads((folder/"tasks.json").read_text())[1];raw=(folder/task["source"]).read_bytes()
    manifest=json.loads((folder/"manifest.json").read_text())
    assert sha256(raw).hexdigest()==manifest["source_files"][task["source"]]
    text=_extract_text(raw,text_order="layout")
    source=ResearchSource(source_id=task["source_id"],owner_id="test",title=task["source"],text=text,
        kind="user_supplied",obtained_at=datetime.now(timezone.utc),content_sha256=sha256(text.encode()).hexdigest(),
        retrieval_method="user_pdf",permissions=SourcePermissions(store=True,display=True,model_input=True))
    captured=[]
    result=analyze(task["question"],[source],None,lambda prompt:(captured.append(json.loads(prompt)) or {},0),
                   max_prompt_chars=6000-len(SYSTEM_PROMPT.encode())-512)
    normalized=[" ".join(item["text"].split()) for item in captured[0]["sources"]]
    assert any("consumer spending increased $2.9 billion" in item for item in normalized)
    assert any("increase in services" in item and "decrease in goods" in item for item in normalized)
    assert len(result.segments)==len(captured[0]["sources"])
    for segment,item in zip(result.segments,captured[0]["sources"]):
        assert _prompt_source_text(text[segment["character_start"]:segment["character_end"]],True)==item["text"]
