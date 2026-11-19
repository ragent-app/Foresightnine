"""DoH validation behind fake-IP proxies is cached by record TTL (363 acceptance follow-up)."""
from __future__ import annotations

import json

import pytest

import app.openai_compatible as compatible


class FakeDoh:
    """Stands in for the public DoH HTTPS service; never touches the network."""

    def __init__(self, answers=None, fail=False, ttl=120):
        self.calls = 0
        self.fail = fail
        self.answers = answers or {"A": "1.1.1.1", "AAAA": "2606:4700:4700::1111"}  # public; never contacted
        self.ttl = ttl

    def connection(self, host, port, timeout=None, context=None):
        doh = self

        class Connection:
            def request(self, method, path, headers=None):
                doh.calls += 1
                self.record_type = "AAAA" if "type=AAAA" in path else "A"

            def getresponse(self):
                if doh.fail:
                    raise TimeoutError("doh timeout")
                record_type = self.record_type

                class Response:
                    status = 200

                    def read(self, limit):
                        value = doh.answers.get(record_type)
                        answer = [{"type": 1 if record_type == "A" else 28, "data": value, "TTL": doh.ttl}] if value else []
                        return json.dumps({"Status": 0, "Answer": answer}).encode()
                return Response()

            def close(self):
                pass
        return Connection()


@pytest.fixture(autouse=True)
def clean_cache():
    compatible._clear_doh_cache()
    yield
    compatible._clear_doh_cache()


def test_successful_answers_are_reused_until_they_expire(monkeypatch):
    doh = FakeDoh(ttl=120)
    monkeypatch.setattr(compatible, "HTTPSConnection", doh.connection)
    clock = [1000.0]
    monkeypatch.setattr(compatible, "monotonic", lambda: clock[0])
    first = compatible._public_doh_addresses("api.example.com")
    assert first == ("1.1.1.1", "2606:4700:4700::1111") and doh.calls == 2
    clock[0] += 119
    assert compatible._public_doh_addresses("API.example.com.") == first  # normalized hostname, cached
    assert doh.calls == 2
    clock[0] += 2
    compatible._public_doh_addresses("api.example.com")
    assert doh.calls == 4


@pytest.mark.parametrize(("ttl", "kept_for"), [(5, 30.0), (86400, 300.0)])
def test_cache_lifetime_is_bounded(monkeypatch, ttl, kept_for):
    doh = FakeDoh(ttl=ttl)
    monkeypatch.setattr(compatible, "HTTPSConnection", doh.connection)
    clock = [0.0]
    monkeypatch.setattr(compatible, "monotonic", lambda: clock[0])
    compatible._public_doh_addresses("api.example.com")
    clock[0] = kept_for - 0.5
    compatible._public_doh_addresses("api.example.com")
    assert doh.calls == 2
    clock[0] = kept_for + 0.5
    compatible._public_doh_addresses("api.example.com")
    assert doh.calls == 4


def test_failures_are_not_cached(monkeypatch):
    doh = FakeDoh(fail=True)
    monkeypatch.setattr(compatible, "HTTPSConnection", doh.connection)
    for _ in range(2):
        with pytest.raises(compatible._TransportFailure):
            compatible._public_doh_addresses("api.example.com")
    doh.fail = False
    assert compatible._public_doh_addresses("api.example.com")


def test_fake_ip_validation_uses_the_cache_and_still_rejects_private_answers(monkeypatch):
    doh = FakeDoh()
    monkeypatch.setattr(compatible, "HTTPSConnection", doh.connection)
    monkeypatch.setattr(compatible.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, "", ("198.18.0.10", 443))])
    for _ in range(3):
        assert compatible._validated_addresses("api.example.com", 443) == ("198.18.0.10",)
    assert doh.calls == 2
    private = FakeDoh(answers={"A": "10.0.0.5"})
    monkeypatch.setattr(compatible, "HTTPSConnection", private.connection)
    with pytest.raises(compatible._TransportFailure):
        compatible._validated_addresses("internal.example.com", 443)
