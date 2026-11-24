"""Website icons for the live activity line, fetched by the server and served same-origin."""
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app import main, research_favicons
from app.research_favicons import favicon, host_of, image_type, normalize_host
from app.research_progress import _activity_hosts

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


def test_only_public_domain_names_are_accepted():
    assert normalize_host("WWW.CNBC.com.") == "www.cnbc.com"
    for bad in ("localhost", "127.0.0.1", "10.0.0.1", "a", "x..com", "-a.com", "evil.com/path", "a.b:443", ""):
        assert normalize_host(bad) is None
    assert host_of("https://www.sec.gov/Archives/x.htm") == "www.sec.gov"
    assert host_of("file:///etc/passwd") is None


def test_only_raster_images_are_served():
    assert image_type(PNG) == "image/png"
    assert image_type(b"\x00\x00\x01\x00rest") == "image/x-icon"
    assert image_type(b"<svg onload=alert(1)>") is None
    assert image_type(b"<html>") is None


def test_answers_are_cached_including_none():
    calls = []
    def fetch(host):
        calls.append(host)
        return None if host == "none.example.com" else (PNG, "image/png")
    for _ in range(2):
        assert favicon("site.example.com", fetch=fetch) == (PNG, "image/png")
        assert favicon("none.example.com", fetch=fetch) is None
    assert calls == ["site.example.com", "none.example.com"]


def test_the_endpoint_requires_sign_in_and_refuses_non_public_hosts(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.setattr(research_favicons, "_fetch", lambda host: (PNG, "image/png") if host == "icons.example.com" else None)
    research_favicons._cache.clear()
    client = TestClient(main.app)
    assert client.get("/v2/research/favicons/icons.example.com").status_code == 401
    auth = client.post("/v1/auth/local/register", json={"email": f"{uuid4().hex}@example.test",
                       "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    ok = client.get("/v2/research/favicons/icons.example.com", headers=headers)
    assert ok.status_code == 200 and ok.content == PNG and ok.headers["content-type"] == "image/png"
    assert ok.headers["x-content-type-options"] == "nosniff"
    assert client.get("/v2/research/favicons/127.0.0.1", headers=headers).status_code == 404
    assert client.get("/v2/research/favicons/missing.example.com", headers=headers).status_code == 404


def test_the_current_step_names_the_sites_it_fetches_or_reads():
    checkpoint = {"acquired_lead_sources": {"https://www.cnbc.com/a": "s1", "https://nvidianews.nvidia.com/b": "s2"}}
    acquire = {"name": "acquire_sources", "arguments": json.dumps({"urls": ["https://www.cnbc.com/x", "https://www.cnbc.com/y", "https://apnews.com/z"], "rationale": "r"})}
    assert _activity_hosts(checkpoint, "acquire_sources", acquire) == ["www.cnbc.com", "apnews.com"]
    read = {"name": "read_sources", "arguments": {"reads": [{"source_id": "s2"}, {"source_id": "s1"}, {"source_id": "unknown"}]}}
    assert _activity_hosts(checkpoint, "read_sources", read) == ["nvidianews.nvidia.com", "www.cnbc.com"]
    assert _activity_hosts(checkpoint, "news_search", {"name": "news_search", "arguments": {}}) == []
    assert _activity_hosts(checkpoint, "acquire_sources", {"name": "acquire_sources", "arguments": "not json"}) == []
