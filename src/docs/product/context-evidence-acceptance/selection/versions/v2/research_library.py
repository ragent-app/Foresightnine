"""Per-topic research library: passage chunking and bounded lexical (BM25) retrieval.

The caller passes only sources it is already allowed to read (authorization, ``as_of`` filtering and
simulated-data isolation are the caller's job); this module is pure computation: no network, no model
calls, no global state.  A library is cheap to keep per run and supports incremental updates
(``add_source`` / ``replace_source`` / ``remove_source``) for OCR-derived source versions.

Chunking (see ``_chunk_source``)
    * A source is split into *segments*: one per readable page of ``provenance['document_pages']``
      (status ``text`` or ``ocr``) or the whole text when there is no valid page map.  A passage never
      crosses a segment, so it carries exactly one 1-based ``page`` (``None`` without a page map).
    * Segments are cut on line boundaries into passages of about 600-1200 characters.  A passage is closed
      at a blank line once it holds at least ``MIN_PASSAGE_CHARS``, or before a line that would push it
      past ``MAX_PASSAGE_CHARS``.  Lines are never split, except a single non-table line longer than the
      maximum, which is cut at sentence punctuation or whitespace (or hard-cut when there is none).
    * Table rows (lines with at least two ``|``) are never split.  A passage containing table rows whose
      header row is not inside it carries that header row text as ``context`` (not part of the quote).
    * ``char_start``/``char_end`` are exact offsets into ``source.text``; ``text[char_start:char_end]``
      has no leading or trailing whitespace.

Scoring: BM25 (k1=1.2, b=0.75) over English words (lowercase, light stemming), numbers/percentages/dates
(kept exact; boosted) and CJK character bigrams.  Passages containing the whole query as a literal phrase
get a small multiplicative bonus.  Ties break on (source_id, char_start) so results are deterministic.
"""
from __future__ import annotations

from array import array
from collections import Counter
from dataclasses import dataclass
import logging
import math
import re
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Protocol, Sequence

from .research_diagnostics import safe_exception_diagnostic

if TYPE_CHECKING:  # pragma: no cover
    from .research_sources import ResearchSource

_logger = logging.getLogger(__name__)


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


_UNSET = _Unset()

MIN_PASSAGE_CHARS = 600
MAX_PASSAGE_CHARS = 1200
TARGET_PASSAGE_CHARS = 900
MAX_TABLE_ROW_CHARS = 4000       # a single table row longer than this is treated as ordinary text
BM25_K1 = 1.2
BM25_B = 0.75
NUMBER_BOOST = 2.0
PHRASE_BONUS = 1.25
RRF_K = 60
MAX_UNSEARCHED_RANGES = 50
_UNREADABLE_STATUSES = ("needs_ocr", "not_included", "failed")
_READABLE_STATUSES = frozenset({"text", "ocr"})

# --------------------------------------------------------------------------------------------- tokenizer

_CJK = "぀-ヿ㐀-䶿一-鿿豈-﫿가-힯ｦ-ﾟ"
_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
           "jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec")
_MONTH_NUM = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
              "nov": 11, "dec": 12}
_TOKEN_RE = re.compile(
    r"(?P<d_cjk>[0-9]{4}\s?[年년]\s?[0-9]{1,2}\s?[月월](?:\s?[0-9]{1,2}\s?[日일])?)"
    r"|(?P<d_en1>(?i:" + _MONTHS + r")\.?\s[0-9]{1,2}(?:st|nd|rd|th)?,?\s[0-9]{4}(?![0-9]))"
    r"|(?P<d_en2>[0-9]{1,2}(?:st|nd|rd|th)?\s(?i:" + _MONTHS + r")\.?,?\s[0-9]{4}(?![0-9]))"
    r"|(?P<d_iso>[0-9]{4}[-/][0-9]{1,2}[-/][0-9]{1,2}(?![0-9]))"
    r"|(?P<num>[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?%?)"
    r"|(?P<cjk>[" + _CJK + r"]+)"
    r"|(?P<word>[A-Za-z0-9À-ɏͰ-ϿЀ-ӿ]+)")
_TOKEN_FIND_RE = re.compile(re.sub(r"\(\?P<\w+>", "(?:", _TOKEN_RE.pattern))   # same tokens, no capture groups
_STOPWORDS = frozenset(
    "a an and are as at be been but by for from has have in is it its of on or that the their this to was were "
    "will with which who not no we our you your they he she his her than then there these those also into over "
    "such may can could would should about".split())
_NUMBER_RE = re.compile(r"[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?%?")
_DATE_PARTS_RE = re.compile(r"[0-9]+|[A-Za-z]+")
_TOKEN_CACHE_LIMIT = 300_000


def _stem(word: str) -> str:
    if not word.isascii() or not word.isalpha() or len(word) < 4:
        return word
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith("sses"):
        word = word[:-2]
    elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    if word.endswith("ing") and len(word) > 5:
        word = word[:-3]
    elif word.endswith("ed") and len(word) > 4:
        word = word[:-2]
    if word.endswith("e") and len(word) > 4:
        word = word[:-1]
    return word


def _iso(year: int, month: int, day: int | None) -> str:
    return f"{year:04d}-{month:02d}" + (f"-{day:02d}" if day is not None else "")


def _expand_token(kind: str, raw: str) -> tuple[str, ...]:
    """Index terms produced by one regex token (kind = named group)."""
    if kind == "word":
        word = raw.lower()
        if word in _STOPWORDS or (len(word) == 1 and word.isalpha() and word.isascii()):
            return ()
        return (_stem(word),)
    if kind == "num":
        plain = raw.replace(",", "")
        if plain.endswith("%"):
            return (plain, plain[:-1])
        return (plain,)
    if kind == "cjk":
        if len(raw) == 1:
            return (raw,)
        return tuple(raw[i:i + 2] for i in range(len(raw) - 1))
    parts = _DATE_PARTS_RE.findall(raw)
    try:
        if kind == "d_iso":
            year, month, day = (int(p) for p in parts[:3])
        elif kind == "d_cjk":
            nums = [int(p) for p in parts if p.isdigit()]
            year, month, day = nums[0], nums[1], (nums[2] if len(nums) > 2 else None)
        else:  # english month names
            nums = [int(p) for p in parts if p.isdigit() and len(p) <= 2 or (p.isdigit() and len(p) == 4)]
            names = [p for p in parts if p.isalpha() and p.lower()[:3] in _MONTH_NUM]
            year = next(n for n in nums if n >= 1000)
            day = next(n for n in nums if n < 1000)
            month = _MONTH_NUM[names[0].lower()[:3]]
        if not (1 <= month <= 12) or (day is not None and not 1 <= day <= 31):
            return ()
    except (ValueError, StopIteration, IndexError):
        return ()
    terms = [_iso(year, month, day), str(year)]
    if kind != "d_iso" and day is not None:
        terms.append(str(day))
    return tuple(terms)


class _Tokenizer:
    """Tokenizer with a bounded per-instance cache (no module-level state)."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, ...]] = {}

    def _terms(self, raw: str, kind: str | None = None) -> tuple[str, ...]:
        terms = self._cache.get(raw)
        if terms is None:
            if kind is None:
                kind = _TOKEN_RE.fullmatch(raw).lastgroup  # type: ignore[union-attr]
            if len(self._cache) >= _TOKEN_CACHE_LIMIT:
                self._cache.clear()
            terms = self._cache[raw] = _expand_token(kind, raw)
        return terms

    def terms(self, text: str) -> list[str]:
        out: list[str] = []
        cache = self._cache
        for raw in _TOKEN_FIND_RE.findall(text):
            terms = cache.get(raw)
            if terms is None:
                terms = self._terms(raw)
            if terms:
                out.extend(terms)
        return out

    def term_counts(self, text: str, into: Counter | None = None) -> Counter:
        """Term frequencies of ``text`` (counting raw tokens first keeps the per-occurrence work in C)."""
        counts: Counter = into if into is not None else Counter()
        cache = self._cache
        for raw, n in Counter(_TOKEN_FIND_RE.findall(text)).items():
            terms = cache.get(raw)
            if terms is None:
                terms = self._terms(raw)
            for term in terms:
                counts[term] += n
        return counts

    def terms_with_spans(self, text: str, offset: int = 0) -> list[tuple[str, int, int]]:
        out: list[tuple[str, int, int]] = []
        for m in _TOKEN_RE.finditer(text):
            kind, raw = m.lastgroup, m.group()
            terms = self._terms(raw, kind)
            if kind == "cjk" and len(raw) > 1:
                for i, term in enumerate(terms):
                    out.append((term, offset + m.start() + i, offset + m.start() + i + 2))
            else:
                for term in terms:
                    out.append((term, offset + m.start(), offset + m.end()))
        return out


def _is_numeric_term(term: str) -> bool:
    return term[:1].isdigit() and term.isascii()


# ----------------------------------------------------------------------------------------------- chunking

_SEPARATOR_ROW_RE = re.compile(r"^\|?[\s:|+\-]*-[\s:|+\-]*\|?$")
_SENTENCE_END = "。！？；.!?;"


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) <= MAX_TABLE_ROW_CHARS and stripped.count("|") >= 2


def _is_separator_row(line: str) -> bool:
    return bool(_SEPARATOR_ROW_RE.match(line.strip()))


@dataclass(frozen=True)
class Passage:
    """One indexed passage; offsets are exact positions in ``ResearchSource.text``."""
    source_id: str
    index: int
    char_start: int
    char_end: int
    page: int | None
    context: str = ""


def _split_long_line(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Cut an over-long non-table line into pieces of at most MAX_PASSAGE_CHARS (sentence/space aware)."""
    pieces: list[tuple[int, int]] = []
    pos = start
    while end - pos > MAX_PASSAGE_CHARS:
        window_end = pos + MAX_PASSAGE_CHARS
        lo = pos + MIN_PASSAGE_CHARS // 2
        cut = -1
        for i in range(window_end, lo, -1):
            if text[i - 1] in _SENTENCE_END:
                cut = i
                break
        if cut < 0:
            for i in range(window_end, lo, -1):
                if text[i - 1].isspace():
                    cut = i
                    break
        if cut < 0:
            cut = window_end
        pieces.append((pos, cut))
        pos = cut
    pieces.append((pos, end))
    return pieces


def _segments(source_text: str, document_pages: Any) -> tuple[list[tuple[int, int, int | None, str | None]], dict]:
    """Readable segments ``(start, end, page, status)`` and unsearched page info."""
    length = len(source_text)
    pages = document_pages.get("pages") if isinstance(document_pages, Mapping) else None
    if not isinstance(pages, list) or not pages:
        return [(0, length, None, None)], {"page_count": None, "unsearched": {}}
    segments: list[tuple[int, int, int | None, str | None]] = []
    unsearched: dict[str, list[int]] = {}
    for number, entry in enumerate(pages, 1):
        try:
            start, end, status = entry[0], entry[1], entry[2]
        except (TypeError, IndexError, KeyError):
            unsearched.setdefault("failed", []).append(number)
            continue
        if status in _READABLE_STATUSES:
            if isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= length:
                if end > start:
                    segments.append((start, end, number, status))
            else:
                unsearched.setdefault("failed", []).append(number)
        else:
            unsearched.setdefault(status if status in _UNREADABLE_STATUSES else "failed", []).append(number)
    segments.sort(key=lambda s: s[0])
    return segments, {"page_count": len(pages), "unsearched": unsearched}


def _chunk_source(source_id: str, text: str, document_pages: Any
                  ) -> tuple[list[tuple[int, int, int | None, int, int, str | None]], dict]:
    """Return passages as ``(start, end, page, header_start, header_end, status)`` plus page coverage info.

    ``header_start == header_end`` means no table context.
    """
    segments, page_info = _segments(text, document_pages)
    passages: list[tuple[int, int, int | None, int, int, str | None]] = []
    for seg_start, seg_end, page, status in segments:
        cur_start = cur_end = -1          # open passage (cur_end excludes trailing whitespace)
        cur_ctx = (0, 0)
        cur_has_table = False
        header: tuple[int, int] | None = None
        prev_table = False                # previous non-blank line was a table row of the current table
        prev_row: tuple[int, int] | None = None

        def flush() -> None:
            nonlocal cur_start, cur_end, cur_ctx, cur_has_table
            if cur_start >= 0:
                passages.append((cur_start, cur_end, page, cur_ctx[0], cur_ctx[1], status))
            cur_start = cur_end = -1
            cur_ctx = (0, 0)
            cur_has_table = False

        pos = seg_start
        while pos <= seg_end:
            nl = text.find("\n", pos, seg_end)
            line_end = seg_end if nl < 0 else nl
            raw = text[pos:line_end]
            line_start = pos
            pos = line_end + 1
            stripped = raw.strip()
            if not stripped:
                prev_table = False
                if cur_start >= 0 and cur_end - cur_start >= MIN_PASSAGE_CHARS:
                    flush()
                continue
            ls = line_start + (len(raw) - len(raw.lstrip()))
            le = ls + len(stripped)
            table_row = _is_table_row(raw)
            if table_row:
                if not prev_table:
                    header = (ls, le)
                    prev_row = None
                elif _is_separator_row(raw) and prev_row is not None and header is not None:
                    header = prev_row
                prev_table = True
                prev_row = (ls, le)
                pieces = [(ls, le)]
            else:
                prev_table = False
                pieces = _split_long_line(text, ls, le) if le - ls > MAX_PASSAGE_CHARS else [(ls, le)]
            for ps, pe in pieces:
                if cur_start >= 0 and pe - cur_start > MAX_PASSAGE_CHARS:
                    flush()
                if cur_start < 0:
                    cur_start = ps
                cur_end = pe
                if table_row and not cur_has_table:
                    cur_has_table = True
                    if header is not None and header[0] < cur_start:
                        cur_ctx = header
                elif table_row and cur_has_table and header is not None and cur_ctx == (0, 0) and header[0] < cur_start:
                    cur_ctx = header
        flush()
    return passages, page_info


# -------------------------------------------------------------------------------------- vector extension

@dataclass(frozen=True)
class VectorCandidate:
    source_id: str
    passage_index: int
    score: float


class VectorScorer(Protocol):
    """Extension point for a semantic scorer (not implemented here; no embeddings in this module).

    The library calls ``index_source`` after chunking a source and ``remove_source`` when it is replaced or
    removed, and merges ``query`` results into the lexical ranking with reciprocal-rank fusion.
    """

    def index_source(self, source: "ResearchSource", passages: Sequence[Passage]) -> None: ...

    def remove_source(self, source_id: str) -> None: ...

    def query(self, text: str, *, source_ids: Sequence[str], limit: int) -> Sequence[VectorCandidate]: ...


# ------------------------------------------------------------------------------------------------- results

@dataclass(frozen=True)
class Hit:
    source_id: str
    title: str
    page: int | None
    char_start: int              # passage span in source.text
    char_end: int
    score: float
    snippet: str                 # exact source text text[snippet_start:snippet_end], inside the passage
    snippet_start: int
    snippet_end: int
    matched_terms: tuple[str, ...]
    context: str = ""            # table header row; not part of the quotable passage text
    reasons: tuple[str, ...] = ()
    extraction_method: str | None = None   # "ocr" when the page text came from OCR
    lexical_score: float = 0.0
    vector_score: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id, "title": self.title, "page": self.page, "char_start": self.char_start,
            "char_end": self.char_end, "score": round(self.score, 6), "snippet": self.snippet,
            "snippet_start": self.snippet_start, "snippet_end": self.snippet_end,
            "matched_terms": list(self.matched_terms), "context": self.context, "reasons": list(self.reasons),
            "extraction_method": self.extraction_method,
        }


def _scorer_cache_stats(scorer: VectorScorer | None) -> tuple[int, int] | None:
    if scorer is None:
        return None
    cache_stats = getattr(scorer, "cache_stats", None)
    raw = cache_stats() if callable(cache_stats) else None
    if isinstance(raw, Mapping):
        hits = raw.get("hits")
        misses = raw.get("misses")
        if type(hits) is int and hits >= 0 and type(misses) is int and misses >= 0:
            return hits, misses
    cache = getattr(scorer, "cache", None)
    if cache is not None:
        hits = getattr(cache, "hits", None)
        misses = getattr(cache, "misses", None)
        if type(hits) is int and hits >= 0 and type(misses) is int and misses >= 0:
            return hits, misses
    return None


class HitList(list):
    """``list[Hit]`` that also carries the ``coverage`` metadata of the call that produced it."""

    coverage: dict[str, Any]

    def __init__(self, hits: Iterable[Hit] = (), coverage: dict[str, Any] | None = None) -> None:
        super().__init__(hits)
        self.coverage = coverage or {}


def _ranges(numbers: Sequence[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for n in sorted(numbers):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


# ------------------------------------------------------------------------------------------------- library

class _SourceIndex:
    __slots__ = ("source", "starts", "ends", "pages", "hstarts", "hends", "lengths", "postings", "statuses",
                 "page_info", "text_len")

    def __init__(self, source: Any) -> None:
        self.source = source
        self.starts = array("I")
        self.ends = array("I")
        self.pages = array("I")      # 0 = no page
        self.hstarts = array("I")
        self.hends = array("I")
        self.lengths = array("I")
        self.postings: dict[str, array] = {}
        self.statuses: dict[int, str] = {}   # page -> status for OCR pages only
        self.page_info: dict = {}
        self.text_len = len(source.text)


class ResearchLibrary:
    """Lexical/hybrid index over the eligible sources of one research topic or run."""

    def __init__(self, sources: Iterable["ResearchSource"] = (), *,
                 ineligible: Mapping[str, str] | None = None,
                 vector_scorer: VectorScorer | None | _Unset = _UNSET) -> None:
        self._tok = _Tokenizer()
        self._indexes: dict[str, _SourceIndex] = {}
        self._df: Counter[str] = Counter()
        self._total_passages = 0
        self._total_length = 0
        self._ineligible: dict[str, str] = dict(ineligible or {})
        self._vector_explicit = vector_scorer is not _UNSET
        self._vector: VectorScorer | None = None if vector_scorer is _UNSET else vector_scorer  # type: ignore[assignment]
        for source in sources:
            self.add_source(source)

    @property
    def vector_scorer(self) -> VectorScorer | None:
        return self._vector

    @property
    def vector_scorer_explicit(self) -> bool:
        return self._vector_explicit

    def attach_vector_scorer(self, vector_scorer: VectorScorer | None) -> None:
        """Attach or replace the library's vector scorer and index any existing sources."""
        old = self._vector
        if old is vector_scorer:
            self._vector_explicit = True
            return
        if old is not None:
            for source_id in list(self._indexes):
                try:
                    old.remove_source(source_id)
                except Exception as error:
                    _logger.warning("vector_scorer_remove_failed metadata=%s", safe_exception_diagnostic(error))
        self._vector = vector_scorer
        self._vector_explicit = True
        if vector_scorer is not None:
            for index in self._indexes.values():
                try:
                    vector_scorer.index_source(index.source, self._passages(index))
                except Exception as error:
                    _logger.warning("vector_scorer_index_failed metadata=%s", safe_exception_diagnostic(error))

    # -- membership ---------------------------------------------------------------------------------
    @property
    def source_ids(self) -> list[str]:
        return sorted(self._indexes)

    def __len__(self) -> int:
        return len(self._indexes)

    def __contains__(self, source_id: object) -> bool:
        return source_id in self._indexes

    def indexed_source(self, source_id: str) -> "ResearchSource | None":
        index = self._indexes.get(source_id)
        return index.source if index else None

    def note_ineligible(self, source_id: str, reason: str = "not_eligible") -> None:
        """Remember a source the caller withheld so ``coverage`` can report why it was not searched."""
        self._ineligible[source_id] = reason

    def stats(self) -> dict[str, int]:
        return {"sources": len(self._indexes), "passages": self._total_passages, "terms": len(self._df),
                "indexed_characters": sum(sum(e - s for s, e in zip(i.starts, i.ends)) for i in self._indexes.values())}

    def add_source(self, source: "ResearchSource") -> None:
        if source.source_id in self._indexes:
            raise ValueError(f"source already in library: {source.source_id}; use replace_source")
        self._install(self._build(source))

    def replace_source(self, old_id: str, new_source: "ResearchSource") -> None:
        """Swap ``old_id`` for ``new_source`` (e.g. its OCR-derived version); the old index stays on failure."""
        if new_source.source_id != old_id and new_source.source_id in self._indexes:
            raise ValueError(f"source already in library: {new_source.source_id}")
        built = self._build(new_source)
        self._uninstall(old_id)
        self._install(built)

    def remove_source(self, source_id: str) -> bool:
        return self._uninstall(source_id)

    # -- build --------------------------------------------------------------------------------------
    def _build(self, source: Any) -> _SourceIndex:
        text = source.text or ""
        provenance = getattr(source, "provenance", None) or {}
        document_pages = provenance.get("document_pages") if isinstance(provenance, Mapping) else None
        index = _SourceIndex(source)
        chunks, page_info = _chunk_source(source.source_id, text, document_pages)
        index.page_info = page_info
        term_counts = self._tok.term_counts
        for pid, (start, end, page, hs, he, status) in enumerate(chunks):
            counts = term_counts(text[start:end])
            if he > hs:
                term_counts(text[hs:he], counts)
            index.starts.append(start)
            index.ends.append(end)
            index.pages.append(page or 0)
            index.hstarts.append(hs)
            index.hends.append(he)
            index.lengths.append(sum(counts.values()))
            if page is not None and status == "ocr":
                index.statuses[page] = "ocr"
            postings = index.postings
            for term, tf in counts.items():
                arr = postings.get(term)
                if arr is None:
                    arr = postings[term] = array("I")
                arr.append((pid << 8) | (tf if tf < 255 else 255))
        return index

    def _install(self, index: _SourceIndex) -> None:
        source_id = index.source.source_id
        self._indexes[source_id] = index
        self._ineligible.pop(source_id, None)
        df = self._df
        for term, arr in index.postings.items():
            df[term] += len(arr)
        self._total_passages += len(index.starts)
        self._total_length += sum(index.lengths)
        if self._vector is not None:
            try:
                self._vector.index_source(index.source, self._passages(index))
            except Exception as error:
                _logger.warning("vector_scorer_index_failed metadata=%s", safe_exception_diagnostic(error))

    def _uninstall(self, source_id: str) -> bool:
        index = self._indexes.pop(source_id, None)
        if index is None:
            return False
        df = self._df
        for term, arr in index.postings.items():
            remaining = df[term] - len(arr)
            if remaining > 0:
                df[term] = remaining
            else:
                del df[term]
        self._total_passages -= len(index.starts)
        self._total_length -= sum(index.lengths)
        if self._vector is not None:
            try:
                self._vector.remove_source(source_id)
            except Exception as error:
                _logger.warning("vector_scorer_remove_failed metadata=%s", safe_exception_diagnostic(error))
        return True

    def _passages(self, index: _SourceIndex) -> list[Passage]:
        text = index.source.text
        return [self._passage(index, pid, text) for pid in range(len(index.starts))]

    @staticmethod
    def _passage(index: _SourceIndex, pid: int, text: str) -> Passage:
        hs, he = index.hstarts[pid], index.hends[pid]
        return Passage(index.source.source_id, pid, index.starts[pid], index.ends[pid], index.pages[pid] or None,
                       text[hs:he] if he > hs else "")

    def passages(self, source_id: str) -> list[Passage]:
        index = self._indexes.get(source_id)
        return self._passages(index) if index else []

    # -- coverage -----------------------------------------------------------------------------------
    def coverage(self, source_ids: Sequence[str] | None = None) -> dict[str, Any]:
        """Which sources a search over ``source_ids`` covers, and what it cannot see."""
        requested = list(dict.fromkeys(source_ids)) if source_ids is not None else self.source_ids
        searched: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        unsearched_total = 0
        for source_id in sorted(requested):
            index = self._indexes.get(source_id)
            if index is None:
                reason = self._ineligible.get(source_id, "not_in_library")
                excluded.append({"source_id": source_id, "reason": reason})
                continue
            info = index.page_info.get("unsearched", {})
            unsearched = {status: {"count": len(pages), "ranges": _ranges(pages)[:MAX_UNSEARCHED_RANGES]}
                          for status, pages in sorted(info.items()) if pages}
            unsearched_total += sum(v["count"] for v in unsearched.values())
            entry: dict[str, Any] = {"source_id": source_id, "title": index.source.title,
                                     "passages": len(index.starts), "pages_total": index.page_info.get("page_count"),
                                     "unsearched_pages": unsearched}
            if not len(index.starts):
                entry["note"] = "no_extracted_text"
            searched.append(entry)
        if source_ids is None:
            for source_id, reason in sorted(self._ineligible.items()):
                excluded.append({"source_id": source_id, "reason": reason})
        notes: list[str] = []
        if unsearched_total:
            notes.append("Pages that are not extracted yet (needs_ocr), not included, or failed were NOT searched; "
                         "a miss does not mean the text is absent from those pages.")
        if excluded:
            notes.append("Excluded sources were not searched.")
        out: dict[str, Any] = {
            "searched_sources": searched, "excluded_sources": excluded, "unsearched_page_count": unsearched_total,
            "complete": not unsearched_total and not excluded, "notes": notes,
            "retrieval_mode": "hybrid_rrf" if self._vector is not None else "bm25_only",
        }
        cache_stats = _scorer_cache_stats(self._vector)
        if cache_stats is not None:
            out["embedding_cache_hits"] = cache_stats[0]
            out["embedding_cache_misses"] = cache_stats[1]
        return out

    # -- search -------------------------------------------------------------------------------------
    def search(self, query: str, *, source_ids: Sequence[str] | None = None, top_k: int = 10,
               max_chars_per_hit: int = 360, use_vector: bool = True) -> HitList:
        coverage = self.coverage(source_ids)
        if top_k <= 0 or not query or not query.strip():
            return HitList([], coverage)
        targets = [s["source_id"] for s in coverage["searched_sources"]]
        q_terms = self._tok.terms(query)
        weights: dict[str, float] = {}
        for term in q_terms:
            weights[term] = NUMBER_BOOST if _is_numeric_term(term) else 1.0
        scored = self._lexical(weights, targets)
        phrase = " ".join(query.lower().split())
        pool_limit = max(top_k * 5, 50)
        ranked = self._rank(scored, phrase, pool_limit)
        vector_scores: dict[tuple[str, int], float] = {}
        vector_active = False
        fallback_reason: str | None = None

        if use_vector and self._vector is not None and targets:
            try:
                raw_vec = list(self._vector.query(query, source_ids=targets, limit=pool_limit) or ())
            except Exception as error:
                _logger.warning("vector_scorer_query_failed metadata=%s", safe_exception_diagnostic(error))
                raw_vec = []
                fallback_reason = "vector_scorer_error"
            else:
                last_err = getattr(self._vector, "last_query_error", None)
                if not raw_vec and isinstance(last_err, str) and last_err:
                    fallback_reason = last_err
                else:
                    vector_active = True

            if vector_active:
                target_set = set(targets)
                valid_vec: list[tuple[tuple[str, int], float]] = []
                for cand in raw_vec:
                    sid = getattr(cand, "source_id", None)
                    pid = getattr(cand, "passage_index", None)
                    vscore = getattr(cand, "score", None)
                    if (
                        not isinstance(sid, str)
                        or sid not in target_set
                        or type(pid) is not int
                        or not (0 <= pid < len(self._indexes[sid].starts))
                        or isinstance(vscore, bool)
                        or not isinstance(vscore, (int, float))
                        or not math.isfinite(float(vscore))
                    ):
                        continue
                    valid_vec.append(((sid, pid), float(vscore)))
                valid_vec.sort(key=lambda item: (-item[1], item[0][0], self._indexes[item[0][0]].starts[item[0][1]]))
                dedup_vec: list[tuple[tuple[str, int], float]] = []
                seen_vec: set[tuple[str, int]] = set()
                for key0, vscore in valid_vec:
                    if key0 not in seen_vec:
                        seen_vec.add(key0)
                        dedup_vec.append((key0, vscore))
                dedup_vec = dedup_vec[:pool_limit]

                fused: dict[tuple[str, int], float] = {}
                for rank, (key0, _lex) in enumerate(ranked, 1):
                    fused[key0] = fused.get(key0, 0.0) + 1.0 / (RRF_K + rank)
                for rank, (key0, vscore) in enumerate(dedup_vec, 1):
                    fused[key0] = fused.get(key0, 0.0) + 1.0 / (RRF_K + rank)
                    vector_scores[key0] = vscore
                order = sorted(
                    fused.items(),
                    key=lambda kv: (-kv[1], kv[0][0], self._indexes[kv[0][0]].starts[kv[0][1]]),
                )
                lexical_of = {k: v for k, v in ranked}
                ranked = [(k, score) for k, score in order[:top_k]]
            else:
                ranked = ranked[:top_k]
                lexical_of = {k: v for k, v in ranked}
        else:
            ranked = ranked[:top_k]
            lexical_of = {k: v for k, v in ranked}

        coverage["retrieval_mode"] = "hybrid_rrf" if vector_active else "bm25_only"
        if fallback_reason:
            coverage["vector_fallback_reason"] = fallback_reason
        cache_stats = _scorer_cache_stats(self._vector)
        if cache_stats is not None:
            coverage["embedding_cache_hits"] = cache_stats[0]
            coverage["embedding_cache_misses"] = cache_stats[1]

        hits = [
            self._make_hit(
                key,
                score,
                lexical_of.get(key, 0.0),
                weights,
                phrase,
                max_chars_per_hit,
                vector_score=vector_scores.get(key),
            )
            for key, score in ranked
        ]
        return HitList(hits, coverage)

    def _lexical(self, weights: Mapping[str, float], targets: Sequence[str]) -> dict[tuple[str, int], float]:
        n = self._total_passages
        if not n or not weights:
            return {}
        avg = (self._total_length / n) or 1.0
        df = self._df
        scores: dict[tuple[str, int], float] = {}
        k1, b = BM25_K1, BM25_B
        for term in sorted(weights):
            term_df = df.get(term)
            if not term_df:
                continue
            idf = math.log(1.0 + (n - term_df + 0.5) / (term_df + 0.5)) * weights[term]
            for source_id in targets:
                index = self._indexes[source_id]
                arr = index.postings.get(term)
                if arr is None:
                    continue
                lengths = index.lengths
                for packed in arr:
                    pid, tf = packed >> 8, packed & 255
                    norm = k1 * (1.0 - b + b * lengths[pid] / avg)
                    key = (source_id, pid)
                    scores[key] = scores.get(key, 0.0) + idf * tf * (k1 + 1.0) / (tf + norm)
        return scores

    def _rank(self, scores: Mapping[tuple[str, int], float], phrase: str, limit: int
              ) -> list[tuple[tuple[str, int], float]]:
        def order(item: tuple[tuple[str, int], float]) -> tuple[float, str, int]:
            (source_id, pid), score = item
            return (-score, source_id, self._indexes[source_id].starts[pid])

        # bonus only needs the best candidates; take a generous pool first
        pool = sorted(scores.items(), key=order)[: max(limit * 2, 100)]
        if len(phrase) >= 3:
            boosted = []
            for key, score in pool:
                index = self._indexes[key[0]]
                span = index.source.text[index.starts[key[1]]:index.ends[key[1]]]
                if phrase in " ".join(span.lower().split()):
                    score *= PHRASE_BONUS
                boosted.append((key, score))
            pool = boosted
        pool.sort(key=order)
        return pool[:limit]

    def _make_hit(self, key: tuple[str, int], score: float, lexical: float, weights: Mapping[str, float],
                  phrase: str, max_chars: int, *, vector_score: float | None = None) -> Hit:
        source_id, pid = key
        index = self._indexes[source_id]
        text = index.source.text
        start, end = index.starts[pid], index.ends[pid]
        spans = self._tok.terms_with_spans(text[start:end], start)
        matched = [(t, s, e) for t, s, e in spans if t in weights]
        snip_start, snip_end = self._window(matched, weights, start, end, max_chars)
        passage_terms = {t for t, _, _ in matched}
        context = text[index.hstarts[pid]:index.hends[pid]] if index.hends[pid] > index.hstarts[pid] else ""
        context_terms: set[str] = set()
        if context:
            context_terms = {t for t in self._tok.terms(context) if t in weights} - passage_terms
        all_terms = passage_terms | context_terms
        reasons: list[str] = []
        if lexical > 0.0:
            reasons.append("lexical_bm25")
        if vector_score is not None:
            reasons.append("semantic_vector")
        if any(_is_numeric_term(t) for t in all_terms):
            reasons.append("number_or_date_exact")
        if context_terms:
            reasons.append("table_header_context")
        if len(phrase) >= 3 and phrase in " ".join(text[start:end].lower().split()):
            reasons.append("phrase")
        page = index.pages[pid] or None
        return Hit(source_id=source_id, title=index.source.title, page=page, char_start=start, char_end=end,
                   score=score, snippet=text[snip_start:snip_end], snippet_start=snip_start, snippet_end=snip_end,
                   matched_terms=tuple(sorted(all_terms)), context=context, reasons=tuple(reasons),
                   extraction_method="ocr" if page and index.statuses.get(page) == "ocr" else None,
                   lexical_score=lexical, vector_score=vector_score)

    @staticmethod
    def _window(matched: Sequence[tuple[str, int, int]], weights: Mapping[str, float], start: int, end: int,
                max_chars: int) -> tuple[int, int]:
        max_chars = max(20, int(max_chars))
        if end - start <= max_chars:
            return start, end
        if not matched:
            return start, start + max_chars
        best_i, best_score = 0, -1.0
        right = 0
        counts: Counter[str] = Counter()
        current = 0.0
        for left in range(len(matched)):
            while right < len(matched) and matched[right][2] - matched[left][1] <= max_chars:
                term = matched[right][0]
                if counts[term] == 0:
                    current += weights[term]
                counts[term] += 1
                right += 1
            if current > best_score:
                best_i, best_score = left, current
            term = matched[left][0]
            counts[term] -= 1
            if counts[term] == 0:
                current -= weights[term]
        first = matched[best_i][1]
        j = best_i
        last = matched[j][2]
        while j + 1 < len(matched) and matched[j + 1][2] - first <= max_chars:
            j += 1
            last = matched[j][2]
        pad = max(0, (max_chars - (last - first)) // 2)
        s = max(start, first - pad)
        e = min(end, s + max_chars)
        s = max(start, e - max_chars)
        return s, e
