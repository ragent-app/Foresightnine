from __future__ import annotations

import time

from app.news.published_date import extract_published_date as ex


def test_json_ld_wins_over_meta():
    html = '''<html><head><meta property="article:published_time" content="2026-11-18T00:00:00Z">
    <script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"NewsArticle",
    "datePublished":"2026-11-19T21:05:00+08:00","dateModified":"2026-11-20T01:00:00Z"}]}</script></head><body></body></html>'''
    r = ex(html)
    assert r.published.source == "json_ld.datePublished" and r.published.timezone == "offset"
    assert r.published.value.utcoffset().total_seconds() == 8 * 3600 and r.published.confidence == "high"
    assert r.modified.source == "json_ld.dateModified" and r.modified.timezone == "utc"


def test_meta_tags_and_modified():
    html = '<meta name="parsely-pub-date" content="2026-11-19T10:00:00Z"><meta property="article:modified_time" content="2026-11-19T12:00:00Z">'
    r = ex(html)
    assert r.published.source == "meta:parsely-pub-date" and r.modified.source == "meta:article:modified_time"
    assert ex('<meta name="DC.date.issued" content="2026-11-19">').published.precision == "date"
    assert ex('<meta name="pubdate" content="Thu, 19 Nov 2026 21:05:00 GMT">').published.timezone == "utc"


def test_time_tag():
    r = ex('<article><time itemprop="datePublished" datetime="2026-11-19T21:05:00-05:00">Nov 19</time></article>')
    assert r.published.source == "time_tag" and r.published.confidence == "medium"
    r = ex('<time datetime="2026-11-19T21:05:00Z">x</time>')
    assert r.published.source == "time_tag_unlabelled" and r.published.confidence == "low"


def test_chinese_visible_text_naive():
    r = ex('<html><body><h1>标题</h1><p class="info">发布时间：2026年11月19日 21:05 来源：新华社</p><p>正文 2025年1月1日</p></body></html>')
    p = r.published
    assert (p.value.year, p.value.month, p.value.day, p.value.hour, p.value.minute) == (2026, 11, 19, 21, 5)
    assert p.timezone == "timezone_unknown" and p.value.tzinfo is None and p.source == "visible_text:labelled"


def test_english_text_formats():
    assert ex("<p>Published Nov. 19, 2026 9:05 PM EST</p>").published.value.hour == 21
    assert ex("<p>Published Nov. 19, 2026 9:05 PM EST</p>").published.timezone == "offset"
    d = ex("<p>Posted 19 November 2026</p>").published
    assert d.value.month == 11 and d.precision == "date"
    assert ex("<p>Updated Nov 20, 2026</p><p>Posted November 19th, 2026</p>").modified.value.day == 20


def test_not_found_never_guessed():
    r = ex("<html><body><p>Hello world, no date here.</p></body></html>")
    assert r.published is None and r.reason == "no_date_found"
    assert ex("").reason == "empty_input" and ex(None).reason == "empty_input"
    assert ex('<meta name="date" content="9999-99-99"><p>nothing</p>').published is None


def test_script_text_not_treated_as_visible():
    assert ex("<script>var d='Published Nov 19, 2026';</script><p>hi</p>").published is None


def test_hostile_input_is_bounded():
    start = time.monotonic()
    nasty = "<div>" * 200_000 + "9" * 2_000_000 + "<time datetime=" + "a" * 500_000
    r = ex(nasty)
    r2 = ex("<script type='application/ld+json'>" + "[" * 100_000 + "</script>" + "年月日" * 100_000)
    r3 = ex("Nov " * 300_000 + "1, " * 100_000)
    assert r.published is None and r2.published is None and r3.truncated is False
    assert time.monotonic() - start < 10
    assert ex("x" * 2_000_000).truncated is True
