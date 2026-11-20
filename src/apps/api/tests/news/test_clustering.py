from __future__ import annotations

from datetime import datetime, timezone

from app.news.clustering import ClusterInput, canonical_news_url, cluster_items, hamming, simhash

BODY = ("NEW YORK (Reuters) - The U.S. Federal Reserve held interest rates steady on Wednesday and signalled that further cuts "
        "would depend on incoming data, as officials weighed sticky services inflation against a cooling labour market. ") * 3


def dt(h):
    return datetime(2026, 11, 19, h, 0, tzinfo=timezone.utc)


def test_canonical_url_rules():
    a = canonical_news_url("https://www.Example.com/news/story/?utm_source=x&fbclid=1&id=7#top")
    assert a == "https://example.com/news/story?id=7"
    assert canonical_news_url("https://m.example.com/news/story?id=7&gclid=z") == a
    assert canonical_news_url("https://example.com/news/story/amp?id=7") == a
    assert canonical_news_url("http://example.com:80/news/story.amp.html?id=7") == a
    assert canonical_news_url("notaurl") == "notaurl"


def test_utm_variants_same_cluster():
    items = [ClusterInput("1", "https://a.com/x?utm_source=tw", "Fed holds rates steady"),
             ClusterInput("2", "https://www.a.com/x/?utm_campaign=y&fbclid=2", "Fed holds rates steady")]
    r = cluster_items(items)
    assert len(r.clusters) == 1 and r.clusters[0].independent_source_count == 1


def test_syndicated_reuters_copies_count_once():
    items = [ClusterInput("yahoo", "https://finance.yahoo.com/news/fed-holds-123.html", "Fed holds rates steady - Yahoo Finance", BODY, published_at=dt(22)),
             ClusterInput("reuters", "https://www.reuters.com/markets/fed-holds-2026-11-19/", "Fed holds rates steady", BODY, published_at=dt(20)),
             ClusterInput("local", "https://www.localpaper.co.uk/biz/fed", "Fed holds interest rates steady, signals data dependence", BODY + " Local comment.", published_at=dt(23)),
             ClusterInput("other", "https://www.bbc.com/news/own-report", "Fed holds rates as inflation sticky",
                          "BBC's own reporting from Washington with different sentences entirely about officials and markets and bonds. " * 4, published_at=dt(21))]
    r = cluster_items(items)
    by_rep = {c.representative_id: c for c in r.clusters}
    assert "reuters" in by_rep
    c = by_rep["reuters"]
    assert set(c.member_ids) >= {"reuters", "yahoo", "local"}
    assert c.independent_source_count == 1 and c.syndicated_from == ("reuters",)
    assert r.item_cluster["other"] != r.item_cluster["reuters"] or c.independent_source_count == 1


def test_different_stories_similar_titles_stay_separate():
    items = [ClusterInput("a", "https://x.com/1", "Apple quarterly earnings beat estimates on strong iPhone sales"),
             ClusterInput("b", "https://y.com/2", "Microsoft quarterly earnings beat estimates on strong cloud sales"),
             ClusterInput("c", "https://z.com/3", "Fed raises rates by 25 basis points in November"),
             ClusterInput("d", "https://w.com/4", "Fed raises rates by 50 basis points in November")]
    r = cluster_items(items)
    assert len(r.clusters) == 4


def test_title_only_duplicates_and_independent_count():
    items = [ClusterInput("a", "https://x.com/1", "Fed holds rates steady in November meeting - X", seen_at=dt(1)),
             ClusterInput("b", "https://y.com/2", "Fed holds rates steady in November meeting | Y News", seen_at=dt(2)),
             ClusterInput("c", "https://sub.y.com/9", "Fed holds rates steady in November meeting", seen_at=dt(3))]
    r = cluster_items(items)
    assert len(r.clusters) == 1
    assert r.clusters[0].representative_id == "a" and r.clusters[0].independent_source_count == 2


def test_far_apart_dates_not_merged_title_only():
    a = ClusterInput("a", "https://x.com/1", "Fed holds rates steady in November meeting", published_at=dt(1))
    b = ClusterInput("b", "https://y.com/2", "Fed holds rates steady in November meeting", published_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert len(cluster_items([a, b]).clusters) == 2


def test_deterministic_and_order_independent():
    items = [ClusterInput(str(i), f"https://s{i}.com/p", "Fed holds rates steady in November meeting", seen_at=dt(i)) for i in range(1, 5)]
    r1, r2 = cluster_items(items), cluster_items(list(reversed(items)))
    assert r1.clusters[0].cluster_id == r2.clusters[0].cluster_id and r1.clusters[0].representative_id == "1"
    assert hamming(simhash(BODY), simhash(BODY)) == 0 and simhash("") is None


def test_chinese_wire_copies():
    body = "路透纽约11月19日电 美联储周三维持利率不变，并表示后续降息取决于数据，官员们权衡了居高不下的服务业通胀与降温的劳动力市场。" * 4
    items = [ClusterInput("a", "https://cn.reuters.com/a", "美联储维持利率不变并强调数据依赖", body),
             ClusterInput("b", "https://finance.sina.com.cn/b", "美联储维持利率不变并强调数据依赖", body)]
    r = cluster_items(items)
    assert len(r.clusters) == 1 and r.clusters[0].independent_source_count == 1
