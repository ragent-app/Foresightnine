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


def _ico(*images):
    """A .ico with the given (width, bitcount, data) images."""
    header = b"\x00\x00\x01\x00" + len(images).to_bytes(2, "little")
    entries, blobs, offset = b"", b"", 6 + 16 * len(images)
    for width, bits, data in images:
        entries += bytes([width % 256, width % 256, 0, 0]) + (1).to_bytes(2, "little") + bits.to_bytes(2, "little") \
            + len(data).to_bytes(4, "little") + offset.to_bytes(4, "little")
        blobs += data
        offset += len(data)
    return header + entries + blobs


def test_a_multi_size_icon_is_cut_to_the_image_nearest_32_px():
    from app.research_favicons import smallest_icon
    big = _ico((16, 32, b"A" * 100), (32, 32, b"B" * 200), (256, 32, b"C" * 90000))
    data, kind = smallest_icon(big)
    assert kind == "image/x-icon" and len(data) == 22 + 200 and data.endswith(b"B" * 200)
    assert data[6] == 32 and int.from_bytes(data[18:22], "little") == 22
    png_inside = _ico((32, 32, PNG), (64, 32, b"D" * 10))
    assert smallest_icon(png_inside) == (PNG, "image/png")
    assert smallest_icon(b"<html>") is None
    assert smallest_icon(PNG + b"x" * 70000) is None, "a raster too large to serve is refused"


def test_icons_declared_by_the_home_page_are_found_small_first():
    from app.research_favicons import icon_links
    html = """<link rel="apple-touch-icon" href="/touch.png"><link rel='mask-icon' href='/m.svg'>
    <link href="/media/favicon.ico" rel="shortcut icon"><link rel=icon sizes=192x192 href=https://cdn.example.com/i.png>
    <link rel="stylesheet" href="/s.css">"""
    assert icon_links(html, "https://news.example.com/") == [
        "https://news.example.com/media/favicon.ico", "https://cdn.example.com/i.png", "https://news.example.com/touch.png"]


def test_without_favicon_ico_the_home_page_icon_is_used():
    from app.research_favicons import _fetch
    pages = {"https://n.example.com/": ("https://n.example.com/", b'<link rel="shortcut icon" href="/img/f.ico">'),
             "https://n.example.com/img/f.ico": ("https://n.example.com/img/f.ico", _ico((32, 32, b"E" * 50)))}
    asked = []
    def get(url, accept, limit):
        asked.append(url)
        return pages.get(url)
    data, kind = _fetch("n.example.com", get=get)
    assert kind == "image/x-icon" and data.endswith(b"E" * 50)
    assert asked == ["https://n.example.com/favicon.ico", "https://n.example.com/", "https://n.example.com/img/f.ico"]
