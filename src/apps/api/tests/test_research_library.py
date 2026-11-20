"""Tests for the per-topic research library (chunking + BM25 retrieval)."""
from __future__ import annotations

from datetime import datetime, timezone
import random
import resource
import time

import pytest

from app.research_library import (
    MAX_PASSAGE_CHARS, MIN_PASSAGE_CHARS, ResearchLibrary, VectorCandidate,
)
from app.research_sources import ResearchSource, SourcePermissions


def make_source(source_id: str, text: str, *, title: str | None = None, pages: list | None = None) -> ResearchSource:
    provenance = {}
    if pages is not None:
        provenance["document_pages"] = {"page_count": len(pages), "pages": pages, "version": 2, "method": "pdftotext_layout_paged"}
    return ResearchSource(
        source_id=source_id, owner_id="owner", title=title or source_id, text=text, kind="user_supplied",
        obtained_at=datetime(2026, 11, 20, tzinfo=timezone.utc), content_sha256="0" * 64,
        permissions=SourcePermissions(store=True, model_input=True), provenance=provenance)


def paged(page_texts: list[tuple[str, str]]) -> tuple[str, list]:
    """Build (text, pages) from [(status, text)]; needs_ocr/not_included pages carry no text."""
    text, pages = "", []
    for status, body in page_texts:
        if status in ("text", "ocr"):
            start = len(text)
            text += body
            pages.append([start, len(text), status])
            text += "\f"
        elif status == "needs_ocr":
            pages.append([len(text), len(text), status])
            text += "\f"
        else:
            pages.append([None, None, status])
    return text, pages


FILLER = "Management discussed general market conditions and the operating environment in detail. "


def prose(n_lines: int, tag: str = "") -> str:
    return "\n".join(f"{tag}{i}: {FILLER * 2}" for i in range(n_lines))


def test_chunking_offsets_lines_and_sizes():
    text = prose(60) + "\n\n" + prose(5, "tail")
    lib = ResearchLibrary([make_source("a", text)])
    passages = lib.passages("a")
    assert len(passages) > 3
    for p in passages:
        quote = text[p.char_start:p.char_end]
        assert quote == quote.strip() and quote
        assert len(quote) <= MAX_PASSAGE_CHARS
        # never mid-line: boundaries sit at line starts / ends
        assert p.char_start == 0 or text[p.char_start - 1] == "\n"
        assert p.char_end == len(text) or text[p.char_end] in "\n "
        assert p.page is None
    assert sum(1 for p in passages if p.char_end - p.char_start >= MIN_PASSAGE_CHARS) >= len(passages) - 3


def test_very_long_line_is_cut_inside_limit():
    text = "Sentence number one is here. " * 200   # one 5800-char line
    lib = ResearchLibrary([make_source("a", text)])
    passages = lib.passages("a")
    assert len(passages) >= 5
    assert all(p.char_end - p.char_start <= MAX_PASSAGE_CHARS for p in passages)
    assert all(text[p.char_end - 1] == "." for p in passages)


def test_english_search_and_snippet_is_exact_source_text():
    text = prose(30) + "\nThe company reported record quarterly revenue growth driven by data center demand.\n" + prose(30)
    lib = ResearchLibrary([make_source("a", text, title="Report")])
    hits = lib.search("quarterly revenue growth", top_k=3)
    assert hits and "quarterly revenue growth" in hits[0].snippet
    h = hits[0]
    assert text[h.snippet_start:h.snippet_end] == h.snippet
    assert h.char_start <= h.snippet_start and h.snippet_end <= h.char_end
    assert len(h.snippet) <= 360
    assert {"revenu", "growth", "quarterli"} & set(h.matched_terms) or h.matched_terms
    assert h.title == "Report" and "phrase" in h.reasons
    # light stemming: plural/inflected query still hits
    assert lib.search("revenues grew growing", top_k=1)[0].char_start == h.char_start


def test_chinese_and_mixed_language_queries():
    zh = prose(25) + "\n公司二零二四年度营业收入同比增长百分之十二，主要由数据中心业务驱动。\n" + prose(25)
    en = prose(20) + "\nThe board approved a share repurchase programme of USD 500 million.\n" + prose(20)
    ja = "当社の営業利益は前年度比で増加しました。\n" * 3 + prose(10)
    ko = "회사의 영업이익은 전년 대비 증가했습니다.\n" * 3 + prose(10)
    lib = ResearchLibrary([make_source("zh", zh), make_source("en", en), make_source("ja", ja), make_source("ko", ko)])
    assert lib.search("营业收入 同比增长", top_k=1)[0].source_id == "zh"
    assert lib.search("営業利益", top_k=1)[0].source_id == "ja"
    assert lib.search("영업이익", top_k=1)[0].source_id == "ko"
    mixed = lib.search("share repurchase 股份回购 营业收入", top_k=5)
    assert {h.source_id for h in mixed[:2]} == {"zh", "en"}
    zh_hit = next(h for h in mixed if h.source_id == "zh")
    assert "营业收入" in zh_hit.snippet


def test_number_and_date_boost_exact_match():
    a = prose(10) + "\nOperating margin was 12.5% in fiscal 2024 compared with last year.\n" + prose(10)
    b = prose(10) + "\nOperating margin was 13.9% in fiscal 2023 compared with last year.\n" + prose(10)
    lib = ResearchLibrary([make_source("b", b), make_source("a", a)])
    hits = lib.search("operating margin 12.5%", top_k=2)
    assert hits[0].source_id == "a" and "number_or_date_exact" in hits[0].reasons
    assert "12.5%" in hits[0].matched_terms
    # dates: ISO, English and Chinese spellings all normalise to the same term
    d = ResearchLibrary([
        make_source("iso", "Balance sheet as of 2024-03-31 shows total assets.\n" + prose(8)),
        make_source("other", "Balance sheet as of 2023-12-31 shows total assets.\n" + prose(8)),
    ])
    assert d.search("balance sheet March 31, 2024", top_k=1)[0].source_id == "iso"
    assert d.search("2024年3月31日 total assets", top_k=1)[0].source_id == "iso"
    assert ResearchLibrary([make_source("n", "Revenue 1,234.5 million\n" + prose(3))]).search("1234.5")[0].matched_terms == ("1234.5",)


def test_page_attribution_and_page_boundaries():
    text, pages = paged([
        ("text", prose(12, "p1-") + "\nalpha unique-token-one finding\n"),
        ("text", prose(12, "p2-") + "\nbeta unique-token-two finding\n"),
        ("text", prose(3, "p3-")),
    ])
    lib = ResearchLibrary([make_source("doc", text, pages=pages)])
    for p in lib.passages("doc"):
        starts = [pg for pg in pages if pg[0] <= p.char_start < pg[1]]
        assert len(starts) == 1 and p.char_end <= starts[0][1]          # never crosses a page
        assert p.page == pages.index(starts[0]) + 1
    assert lib.search("unique-token-one")[0].page == 1
    assert lib.search("unique-token-two")[0].page == 2
    assert lib.coverage()["complete"] is True


def test_table_row_context_and_row_integrity():
    header = "| Metric | FY2024 | FY2023 |"
    rows = [f"| Segment {i} revenue (USD m) | {1000 + i} | {900 + i} |" for i in range(80)]
    text = "Selected financial data\n" + header + "\n|---|---|---|\n" + "\n".join(rows) + "\n\nClosing remarks.\n"
    lib = ResearchLibrary([make_source("t", text)])
    passages = lib.passages("t")
    assert len(passages) > 1
    later = [p for p in passages if p.char_start > text.index(rows[40]) and "|" in text[p.char_start:p.char_end]]
    assert later and all(p.context == header for p in later)
    first = passages[0]
    assert first.context == ""        # header row lies inside the first passage
    for p in passages:               # table rows stay intact
        for line in text[p.char_start:p.char_end].split("\n"):
            assert line in rows or line in (header, "|---|---|---|", "Selected financial data", "Closing remarks.")
    hit = lib.search("Segment 70 revenue 1070", top_k=1)[0]
    assert "| Segment 70 revenue (USD m) | 1070 | 970 |" in hit.snippet
    assert hit.context == header and header not in hit.snippet
    assert "table_header_context" in hit.reasons or "FY2024" not in hit.matched_terms
    # header terms are searchable through context
    assert lib.search("Segment 70 FY2023", top_k=1)[0].context == header


def test_exclusion_reporting_for_unextracted_pages():
    text, pages = paged([("text", prose(6, "x") + "\nreadable page fact\n"), ("needs_ocr", ""), ("needs_ocr", ""),
                         ("text", prose(4, "y")), ("not_included", ""), ("failed", "")])
    lib = ResearchLibrary([make_source("scan", text, pages=pages), make_source("plain", prose(6))],
                          ineligible={"secret": "model_input_not_permitted"})
    result = lib.search("readable page fact", source_ids=["scan", "ghost", "secret"])
    cov = result.coverage
    assert result[0].source_id == "scan"
    scan = cov["searched_sources"][0]
    assert scan["pages_total"] == 6
    assert scan["unsearched_pages"]["needs_ocr"] == {"count": 2, "ranges": [[2, 3]]}
    assert scan["unsearched_pages"]["not_included"]["count"] == 1
    assert scan["unsearched_pages"]["failed"]["count"] == 1
    reasons = {e["source_id"]: e["reason"] for e in cov["excluded_sources"]}
    assert reasons == {"ghost": "not_in_library", "secret": "model_input_not_permitted"}
    assert cov["complete"] is False and cov["unsearched_page_count"] == 4 and cov["notes"]
    assert lib.coverage()["excluded_sources"] == [{"source_id": "secret", "reason": "model_input_not_permitted"}]
    assert ResearchLibrary([make_source("plain", prose(3))]).coverage()["complete"] is True
    assert [h.source_id for h in lib.search("readable page fact", source_ids=["plain"])] == []


def test_replace_source_with_ocr_version_updates_index_and_coverage():
    text, pages = paged([("text", "intro text only\n" + prose(3)), ("needs_ocr", "")])
    lib = ResearchLibrary([make_source("v1", text, pages=pages), make_source("other", prose(5))])
    assert lib.search("scanned balance sheet figure 4242") == []
    assert lib.coverage(["v1"])["unsearched_page_count"] == 1
    text2, pages2 = paged([("text", "intro text only\n" + prose(3)),
                           ("ocr", "Scanned balance sheet figure 4242 recognised\n")])
    lib.replace_source("v1", make_source("v2", text2, pages=pages2))
    assert "v1" not in lib and "v2" in lib and len(lib) == 2
    hits = lib.search("scanned balance sheet figure 4242")
    assert hits[0].source_id == "v2" and hits[0].page == 2 and hits[0].extraction_method == "ocr"
    assert lib.coverage(["v2"])["complete"] is True
    assert lib.coverage(["v1"])["excluded_sources"][0]["reason"] == "not_in_library"
    # same-id replace and df bookkeeping stay consistent
    lib.replace_source("v2", make_source("v2", "completely different words about zebras\n" + prose(2)))
    assert lib.search("figure 4242") == [] and lib.search("zebras")[0].source_id == "v2"
    assert lib.remove_source("other") and lib.stats()["sources"] == 1
    with pytest.raises(ValueError):
        lib.add_source(make_source("v2", "dup"))


def test_deterministic_with_ties_and_insertion_order():
    same = prose(6) + "\nidentical needle sentence for ranking\n"
    sources = [make_source(f"s{i}", same) for i in range(5)]
    first = ResearchLibrary(sources).search("identical needle", top_k=5)
    second = ResearchLibrary(list(reversed(sources))).search("identical needle", top_k=5)
    assert [(h.source_id, h.char_start, h.score) for h in first] == [(h.source_id, h.char_start, h.score) for h in second]
    assert [h.source_id for h in first] == sorted(h.source_id for h in first)
    again = ResearchLibrary(sources).search("identical needle", top_k=5)
    assert [h.as_dict() for h in again] == [h.as_dict() for h in first]


def test_vector_scorer_extension_point_is_fused():
    class Fake:
        def __init__(self):
            self.indexed, self.removed = {}, []

        def index_source(self, source, passages):
            self.indexed[source.source_id] = len(passages)

        def remove_source(self, source_id):
            self.removed.append(source_id)

        def query(self, text, *, source_ids, limit):
            return [VectorCandidate("b", 0, 0.9)]

    fake = Fake()
    lib = ResearchLibrary([make_source("a", "gamma delta lexical match here\n"), make_source("b", "semantic neighbour text only\n")],
                          vector_scorer=fake)
    assert fake.indexed == {"a": 1, "b": 1}
    assert {h.source_id for h in lib.search("gamma delta")} == {"a", "b"}
    lib.remove_source("a")
    assert fake.removed == ["a"]


def _synthetic(seed: int, chars: int) -> str:
    rng = random.Random(seed)
    vocab = [f"w{rng.randrange(40000)}x" if i % 3 == 0 else "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randrange(3, 10)))
             for i in range(30000)]
    lines, total = [], 0
    while total < chars:
        line = " ".join(rng.choice(vocab) for _ in range(rng.randrange(8, 30))) + (f" {rng.randrange(10**6)}.{rng.randrange(100)}%" if rng.random() < 0.3 else "")
        lines.append(line)
        total += len(line) + 1
        if rng.random() < 0.05:
            lines.append("")
    return "\n".join(lines)[:chars]


def test_build_time_and_memory_bounds():
    texts = [_synthetic(i, 1_000_000) for i in range(20)]
    sources = [make_source(f"s{i}", t) for i, t in enumerate(texts)]
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started, cpu_started = time.perf_counter(), time.process_time()
    lib = ResearchLibrary(sources)
    build, cpu = time.perf_counter() - started, time.process_time() - cpu_started
    rss_delta_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss_before) / 1024
    started = time.perf_counter()
    for _ in range(10):
        hits = lib.search("w123x market revenue 4242.5%", top_k=10)
    per_search = (time.perf_counter() - started) / 10
    print(f"\nLIBRARY build={build:.2f}s cpu={cpu:.2f}s rss_delta={rss_delta_mb:.0f}MB search={per_search*1000:.0f}ms stats={lib.stats()}")
    assert lib.stats()["sources"] == 20 and hits is not None
    # Target 5 s on an idle host (measured ~0.65 s CPU per MB under contention, so ~13 s for 20 MB here); this host's 2 CPUs are shared with production (and the test runs niced), so
    # bound CPU time tightly and wall time loosely. The synthetic text is token-dense with a huge vocabulary.
    assert cpu < 20.0 and build < 40.0
    assert rss_delta_mb < 200
    assert per_search < 1.0
