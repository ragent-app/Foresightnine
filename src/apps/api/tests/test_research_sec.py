from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from threading import Event
from time import monotonic

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

import app.main as api
import app.research_sec as research_sec
from app.openai_compatible import HttpResponse
from app.research_sec import SecConceptRequest, SecIdentityRequest, SecFailure, SecResult, fetch_sec_concept, pace_sec_request, search_sec_identities, reserve_sec_request
from app.research_analysis import _quoted_numbers
from app.storage import InMemoryDocumentRepository


def request(**changes):
    data = {"cik": "789019", "tag": "Revenues", "unit": "USD", "permissions": {"store": True}}
    return SecConceptRequest(**(data | changes))


def test_sec_data_preserves_units_filing_and_prevents_future_filing():
    payload = {"cik": 789019, "taxonomy": "us-gaap", "tag": "Revenues", "label": "Revenue",
        "entityName": "Example", "units": {"USD": [
            {"val": 100, "start": "2022-01-01", "end": "2022-12-31", "filed": "2023-02-01", "accn": "a", "form": "10-K"},
            {"val": 200, "start": "2023-01-01", "end": "2023-12-31", "filed": "2024-02-01", "accn": "b", "form": "10-K"}],
            "EUR": [{"val": 999, "end": "2022-12-31", "filed": "2023-02-01"}]}}
    calls = []
    def transport(method, url, headers, body, timeout):
        calls.append(url)
        assert headers["User-Agent"] == "Team test@example.test"
        assert url == "https://data.sec.gov/api/xbrl/companyconcept/CIK0000789019/us-gaap/Revenues.json"
        return HttpResponse(200, json.dumps(payload), {"content-type": "application/json"})
    result = fetch_sec_concept(request(filing_before="2024-01-01"), "Team test@example.test", transport)
    assert '"val":100' in result.text and '"unit":"USD"' in result.text
    assert '"accn":"a"' in result.text
    assert '"val":200' not in result.text and '"val":999' not in result.text
    assert "not exact public availability time" in result.text
    assert len(calls) == 1


def test_sec_requires_configured_contact_and_valid_paths():
    with pytest.raises(SecFailure, match="sec_contact_unconfigured"):
        fetch_sec_concept(request(), None, lambda *args: pytest.fail("must not fetch"))
    with pytest.raises(ValidationError):
        request(tag="../../private")
    with pytest.raises(ValidationError):
        request(cik="0")


def test_sec_decimal_observations_are_not_rounded_through_binary_float():
    # Deliberately provide JSON bytes rather than first constructing a Python
    # float, which would already have lost the information being tested.
    raw = ('{"cik":789019,"taxonomy":"us-gaap","tag":"Revenues","entityName":"Example",'
           '"units":{"USD":[{"val":1.0000000000000001,"filed":"2024-02-01",'
           '"end":"2023-12-31"},{"val":-0.000000000000000123456789,"filed":"2023-02-01",'
           '"end":"2022-12-31"}]}}')
    result = fetch_sec_concept(request(), "Team test@example.test",
        lambda *_args: HttpResponse(200, raw, {"content-type": "application/json"}))
    rows = [json.loads(line, parse_float=Decimal) for line in result.text.splitlines()[2:]]
    assert rows[0]["val"] == Decimal("1.0000000000000001")
    assert rows[1]["val"] == Decimal("-0.000000000000000123456789")
    assert all(isinstance(row["val"], Decimal) for row in rows)
    assert '"val":-0.000000000000000123456789' in result.text
    assert Decimal("-0.000000000000000123456789") in _quoted_numbers(result.text.splitlines()[-1])


def test_sec_extreme_decimal_exponents_are_bounded_and_disclosed():
    raw = ('{"cik":789019,"taxonomy":"us-gaap","tag":"Revenues",'
           '"units":{"USD":[{"val":1e-1001,"filed":"2024-02-01"},'
           '{"val":1.25,"filed":"2023-02-01"}]}}')
    result = fetch_sec_concept(request(), "Team test@example.test",
        lambda *_args: HttpResponse(200, raw, {"content-type": "application/json"}))
    assert "1 observations exceeded supported numeric precision and were omitted" in result.text
    assert '"val":1.25' in result.text and "1E-1001" not in result.text


def test_shared_gate_reserves_only_one_call_per_second():
    repository = InMemoryDocumentRepository()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: reserve_sec_request(repository, now), range(8)))
    assert sum(results) == 1
    assert not reserve_sec_request(repository, now + timedelta(milliseconds=999))
    assert reserve_sec_request(repository, now + timedelta(seconds=1))


def test_sec_endpoint_fails_closed_without_contact_and_records_provenance(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.delenv("FORESIGHTNINE_SEC_USER_AGENT", raising=False)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setattr(api, "_reserve_search_call", lambda request: None)
    client = TestClient(api.app)
    registration = client.post("/v1/auth/local/register", json={"email": "reader@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    body = {"cik": "789019", "tag": "Revenues", "permissions": {"store": True, "display": True}}
    path = "/v2/research/sources/sec-concept"
    assert client.post(path, headers=headers, json=body).status_code == 503
    monkeypatch.setenv("FORESIGHTNINE_SEC_USER_AGENT", "Test team contact@example.test")
    monkeypatch.setattr(api, "fetch_sec_concept", lambda *_args: SecResult(
        "https://data.sec.gov/api/xbrl/companyconcept/CIK0000789019/us-gaap/Revenues.json",
        "Example: Revenue", "2023 revenue 100 USD", "a" * 64))
    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 201, response.text
    assert response.json()["retrieval_method"] == "sec_companyconcept"
    assert response.json()["permissions"]["model_input"] is False
    assert response.json()["response_sha256"] == "a" * 64
    assert "contact@example.test" not in response.text
    assert client.post(path, headers=headers, json=body).status_code == 429


def test_sec_identity_candidates_are_sourced_and_do_not_assume_exchange():
    records = {"0": {"cik_str": 789019, "ticker": "EXM", "title": "Example Corporation"},
               "1": {"cik_str": 42, "ticker": "EXM.A", "title": "Example Other"},
               "2": {"cik_str": 0, "ticker": "EXM", "title": "Invalid"}}
    def transport(method, url, headers, body, timeout):
        assert url == "https://www.sec.gov/files/company_tickers.json"
        assert headers["User-Agent"] == "Team test@example.test"
        return HttpResponse(200, json.dumps(records), {"content-type": "application/json; charset=utf-8"})
    result = search_sec_identities(SecIdentityRequest(query="EXM"), "Team test@example.test", transport)
    assert [item["cik"] for item in result["candidates"]] == ["0000789019", "0000000042"]
    assert all(item["exchange"] is None for item in result["candidates"])
    assert "historical validity" in result["limitation"] and len(result["response_sha256"]) == 64
    with pytest.raises(SecFailure, match="sec_contact_unconfigured"):
        search_sec_identities(SecIdentityRequest(query="EXM"), None, lambda *_: pytest.fail("must not fetch"))
    with pytest.raises(SecFailure, match="source_invalid"):
        search_sec_identities(SecIdentityRequest(query="EXM"), "Team test@example.test",
            lambda *_: HttpResponse(200, '{}', {"content-type": "application/json"}))
    assert search_sec_identities(SecIdentityRequest(query="absent"), "Team test@example.test", transport)["candidates"] == []


def test_sec_identity_endpoint_requires_account_and_returns_unverified_candidates(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.delenv("FORESIGHTNINE_SEC_USER_AGENT", raising=False)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    client = TestClient(api.app)
    path = "/v2/research/identities/sec-search"
    assert client.post(path, json={"query": "EXM"}).status_code == 401
    registration = client.post("/v1/auth/local/register", json={"email": "identity@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    assert client.post(path, headers=headers, json={"query": "EXM"}).status_code == 503
    monkeypatch.setenv("FORESIGHTNINE_SEC_USER_AGENT", "Team test@example.test")
    monkeypatch.setattr(api, "_reserve_search_call", lambda _: None)
    monkeypatch.setattr(api, "reserve_sec_request", lambda _: True)
    monkeypatch.setattr(api, "search_sec_identities", lambda req, agent: {"candidates": [{"cik": "0000000042", "ticker": "EXM", "name": "Example", "exchange": None}], "limitation": "unknown exchange"})
    response = client.post(path, headers=headers, json={"query": "EXM"})
    assert response.status_code == 200
    assert response.json()["candidates"][0]["exchange"] is None
    assert "test@example.test" not in response.text


def test_pace_sec_request_waits_past_one_second_boundary_and_succeeds(monkeypatch):
    repository = InMemoryDocumentRepository()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert reserve_sec_request(repository, t0)

    clock = [100.0]
    utc_clock = [t0 + timedelta(milliseconds=200)]
    waits = []

    def fake_sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds
        utc_clock[0] += timedelta(seconds=seconds)

    monkeypatch.setattr(research_sec, "sleep", fake_sleep)
    assert pace_sec_request(
        repository,
        deadline=clock[0] + 20.0,
        now_fn=lambda: clock[0],
        utcnow_fn=lambda: utc_clock[0],
    )
    assert len(waits) == 1 and waits[0] == pytest.approx(0.82)
    assert repository.get("sec")["requested_at"] == utc_clock[0].isoformat()

    # Also verify real Event wait path when < 1s remains before slot reopens.
    repository.put("sec", {"requested_at": (utc_clock[0] - timedelta(milliseconds=950)).isoformat()})
    event = Event()
    real_now = monotonic()
    assert pace_sec_request(
        repository,
        cancel_event=event,
        deadline=real_now + 20.0,
        utcnow_fn=lambda: utc_clock[0] + timedelta(seconds=max(0.0, monotonic() - real_now)),
    )


def test_pace_sec_request_aborts_immediately_on_cancel_or_insufficient_deadline(monkeypatch):
    repository = InMemoryDocumentRepository()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert reserve_sec_request(repository, t0)

    waits = []
    monkeypatch.setattr(research_sec, "sleep", waits.append)

    # Pre-cancelled event returns False immediately without touching sleep.
    cancelled = Event()
    cancelled.set()
    assert not pace_sec_request(
        repository,
        cancel_event=cancelled,
        deadline=100.0,
        now_fn=lambda: 0.0,
        utcnow_fn=lambda: t0 + timedelta(seconds=5),
    )
    assert not waits

    # Insufficient deadline margin (< 15.0 + delay) aborts immediately without sleeping.
    assert not pace_sec_request(
        repository,
        deadline=15.5,
        now_fn=lambda: 0.0,
        utcnow_fn=lambda: t0 + timedelta(milliseconds=100),
    )
    assert not waits

    # Cancellation triggered during wait aborts without retrying.
    class CancellingEvent:
        def __init__(self):
            self.cancelled = False
            self.waits = []
        def is_set(self):
            return self.cancelled
        def wait(self, timeout):
            self.waits.append(timeout)
            self.cancelled = True
            return True

    cancelling = CancellingEvent()
    assert not pace_sec_request(
        repository,
        cancel_event=cancelling,
        deadline=50.0,
        now_fn=lambda: 0.0,
        utcnow_fn=lambda: t0,
    )
    assert len(cancelling.waits) == 1 and cancelling.waits[0] == pytest.approx(1.02)

    # Contention exceeding 4 attempts returns False after 3 waits.
    monkeypatch.setattr(research_sec, "reserve_sec_request", lambda *_a, **_kw: False)
    waits.clear()
    assert not pace_sec_request(
        repository,
        deadline=100.0,
        now_fn=lambda: 0.0,
        utcnow_fn=lambda: t0,
    )
    assert len(waits) == 3

