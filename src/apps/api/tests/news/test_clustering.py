from __future__ import annotations

from datetime import datetime, timezone

from app.news.clustering import (
    ClusterInput,
    canonical_news_url,
    cluster_items,
    hamming,
    recluster_acquired_sources,
    simhash,
)
from app.research_sources import ResearchSource, SourcePermissions

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


def test_recluster_acquired_sources_merges_missed_wire_syndication_and_splits_title_false_positives():
    # 1. Pre-acquisition title-only: different headlines hide the fact that all three are the same Reuters wire copy.
    pre_syndicated = [
        ClusterInput("s_yahoo", "https://finance.yahoo.com/news/fed-pause-1.html", "Stocks climb as central bank pauses hikes", published_at=dt(22)),
        ClusterInput("s_reuters", "https://www.reuters.com/markets/us/fed-holds-2026-11-19/", "Fed holds rates steady", published_at=dt(20)),
        ClusterInput("s_local", "https://www.localdaily.co.uk/markets/rates", "Borrowing costs stay unchanged after Wednesday decision", published_at=dt(23)),
    ]
    pre_res = cluster_items(pre_syndicated)
    assert len(pre_res.clusters) == 3

    perms = SourcePermissions(store=True, model_input=True, display=True, export=False)
    acquired_syndicated = [
        ResearchSource(
            source_id="s_yahoo", owner_id="o", title="Stocks climb as central bank pauses hikes",
            text=BODY, url="https://finance.yahoo.com/news/fed-pause-1.html",
            kind="official", retrieval_method="web_html", published_at=dt(22), obtained_at=dt(23),
            content_sha256="h1", permissions=perms,
        ),
        ResearchSource(
            source_id="s_reuters", owner_id="o", title="Fed holds rates steady",
            text=BODY, url="https://www.reuters.com/markets/us/fed-holds-2026-11-19/",
            kind="official", retrieval_method="web_html", published_at=dt(20), obtained_at=dt(23),
            content_sha256="h2", permissions=perms,
        ),
        {
            "source_id": "s_local", "title": "Borrowing costs stay unchanged after Wednesday decision",
            "url": "https://www.localdaily.co.uk/markets/rates",
            "text": BODY,
            "published_at": dt(23).isoformat(),
        },
    ]
    post_res = recluster_acquired_sources(acquired_syndicated)
    assert len(post_res.clusters) == 1
    c = post_res.clusters[0]
    assert c.representative_id == "s_reuters"
    assert set(c.member_ids) == {"s_reuters", "s_yahoo", "s_local"}
    assert c.independent_source_count == 1
    assert c.syndicated_from == ("reuters",)
    assert len(set(post_res.item_cluster.values())) == 1
    assert post_res.item_cluster["s_reuters"] == c.cluster_id

    # 2. Pre-acquisition title-only false positive: identical generic titles merge pre-acquisition,
    #    then split after full bodies (>=200 chars) diverge.
    pre_false_merge = [
        ClusterInput("s_us", "https://newsone.example.com/q3-review", "Quarterly earnings review and market outlook", published_at=dt(10)),
        ClusterInput("s_eu", "https://newstwo.example.com/q3-review", "Quarterly earnings review and market outlook", published_at=dt(11)),
    ]
    assert len(cluster_items(pre_false_merge).clusters) == 1

    body_us = ("NEW YORK (Reuters) - U.S. semiconductor manufacturers reported surging artificial intelligence server "
               "shipments and expanding gross margins during the third fiscal quarter across North American data centers. ") * 2
    body_eu = ("FRANKFURT - European automotive suppliers faced softer electric vehicle demand in Germany and France "
               "as battery inventory adjustments weighed on industrial factory utilization across the eurozone. ") * 2
    post_split = recluster_acquired_sources([
        {"source_id": "s_us", "url": "https://newsone.example.com/q3-review", "title": "Quarterly earnings review and market outlook", "text": body_us, "published_at": dt(10)},
        {"source_id": "s_eu", "url": "https://newstwo.example.com/q3-review", "title": "Quarterly earnings review and market outlook", "text": body_eu, "published_at": dt(11)},
    ])
    assert len(post_split.clusters) == 2
    assert post_split.item_cluster["s_us"] != post_split.item_cluster["s_eu"]
    assert all(cl.independent_source_count == 1 for cl in post_split.clusters)


def test_recluster_acquired_sources_handles_empty_short_fallback_and_chinese_dateline():
    empty = recluster_acquired_sources([])
    assert empty.clusters == [] and empty.item_cluster == {}

    zh_body = "路透北京11月20日电 中国人民银行周四维持贷款市场报价利率不变，政策重心转向结构性信贷支持与跨境贸易结算便利化。" * 4
    res = recluster_acquired_sources([
        {"source_id": "zh_1", "url": "https://cn.reuters.com/pboc", "title": "央行维持LPR不变", "text": zh_body},
        {"source_id": "zh_2", "url": "https://finance.sina.com.cn/pboc", "title": "贷款市场报价利率维稳", "text": zh_body},
        {"source_id": "short_1", "url": None, "title": "Short upload", "text": "   " * 80 + "Too short"},
    ])
    assert res.item_cluster["zh_1"] == res.item_cluster["zh_2"]
    zh_cluster = next(c for c in res.clusters if "zh_1" in c.member_ids)
    assert zh_cluster.independent_source_count == 1 and zh_cluster.syndicated_from == ("reuters",)
    assert any("fewer than 200 characters" in note for note in res.notes)
