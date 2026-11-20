from __future__ import annotations

import json
from datetime import datetime, timezone

from app.news.gdelt import GDELT_ATTRIBUTION, GdeltClient, RequestPacer, build_query, parse_seendate
from app.openai_compatible import BinaryHttpResponse

GOOD = {"articles": [
    {"url": "https://www.reuters.com/markets/fed-holds", "title": "Fed holds rates steady", "seendate": "20261119T210500Z",
     "domain": "reuters.com", "language": "English", "sourcecountry": "United States"},
    {"url": "https://news.sina.com.cn/a.html", "title": "美联储维持利率不变", "seendate": "20261119T223000Z",
     "domain": "news.sina.com.cn", "language": "Chinese", "sourcecountry": "China"},
    {"url": "", "title": "no url"},
    "garbage",
]}


class Recorder:
    def __init__(self, status=200, body: str | bytes = "", exc: Exception | None = None):
        self.status, self.body, self.exc, self.urls = status, body, exc, []

    def __call__(self, method, url, headers, body, timeout, **kw):
        self.urls.append(url)
        if self.exc:
            raise self.exc
        data = self.body if isinstance(self.body, bytes) else self.body.encode()
        return BinaryHttpResponse(self.status, data)


def client(transport):
    return GdeltClient(transport=transport, min_interval_seconds=0, pacer=RequestPacer(sleeper=lambda s: None), now=lambda: datetime(2026, 11, 20, tzinfo=timezone.utc))


def test_parses_articles_and_records_seen_at_not_published():
    t = Recorder(body=json.dumps(GOOD))
    r = client(t).search("fed rates", since=datetime(2026, 11, 19, tzinfo=timezone.utc), languages=["en", "zh"], limit=10)
    assert r.status == "partial" and len(r.data) == 2
    lead = r.data[0]
    assert lead.seen_at == datetime(2026, 11, 19, 21, 5, tzinfo=timezone.utc)
    assert lead.published_at is None and lead.language == "en" and lead.providers == ["gdelt"]
    assert r.data[1].language == "zh"
    assert GDELT_ATTRIBUTION in lead.attribution and GDELT_ATTRIBUTION in r.coverage.notes
    assert not r.permissions.export and r.provenance.response_sha256
    url = t.urls[0]
    assert "startdatetime=20261119000000" in url and "mode=ArtList" in url and "sourcelang%3Aenglish" in url
    r.to_wire()


def test_empty_cases():
    assert client(Recorder(body="")).search("fed rates").status == "empty"
    assert client(Recorder(body="{}")).search("fed rates").status == "empty"


def test_rate_limit_text_and_http_429():
    text = "Please limit requests to one every 5 seconds or contact kalev.leetaru5@gmail.com for larger queries."
    r = client(Recorder(body=text)).search("fed rates")
    assert r.status == "rate_limited" and r.retryable and r.reason == "gdelt_rate_limited"
    assert client(Recorder(status=429, body="x")).search("fed rates").status == "rate_limited"


def test_plain_text_query_rejection_malformed_and_http_errors():
    r = client(Recorder(body="The specified phrase is too short.")).search("ab")
    assert r.status == "failed" and r.reason == "gdelt_query_rejected" and not r.retryable
    r = client(Recorder(body='{"articles": [ {"url": ')).search("fed rates")
    assert r.reason == "gdelt_malformed_response" and r.retryable
    assert client(Recorder(status=503, body="")).search("fed rates").retryable
    assert client(Recorder(status=400, body="")).search("fed rates").retryable is False
    r = client(Recorder(exc=OSError("boom"))).search("fed rates")
    assert r.status == "failed" and r.reason == "gdelt_unavailable"
    assert client(Recorder()).search("   ").reason == "gdelt_query_invalid"


def test_control_characters_in_titles_tolerated():
    body = '{"articles": [{"url": "https://a.com/x", "title": "Tab\there", "seendate": "20261119T000000Z"}]}'
    assert client(Recorder(body=body)).search("fed rates").status == "ok"


def test_pacer_spaces_requests():
    now = [0.0]
    slept = []
    pacer = RequestPacer(clock=lambda: now[0], sleeper=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    c = GdeltClient(transport=Recorder(body="{}"), min_interval_seconds=5, pacer=pacer)
    c.search("fed rates"); c.search("fed rates")
    assert slept == [5.0]


def test_helpers():
    assert parse_seendate("20261119T210500Z").hour == 21 and parse_seendate("bad") is None
    q = build_query("fed", languages=["en", "zh", "xx"], domains=["reuters.com"])
    assert "sourcelang:english" in q and "sourcelang:chinese" in q and "domain:reuters.com" in q and "xx" not in q
