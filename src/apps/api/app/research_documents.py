"""Paged text extraction and page maps for large PDF documents.

``document_pages`` format (version 2; version 1 documents are the same minus ``ocr``/``ocr_usage``
and the ``ocr`` page status)::

    {"page_count": int,
     "pages": [[start, end, status], ...],   # one entry per page, in order
     "text_limit_reached": bool,
     "original_retained": bool,
     "method": "pdftotext_layout_paged" | "pdftotext_layout" | "html_paged",
     "version": 2,
     # optional keys, added by retention / ingestion OCR:
     "original_blob_sha256": "<hex>",
     "page_count_complete": false,           # html_paged only: parsing stopped (deadline) after the text limit
     "ocr": {"<page>": {"engine", "engine_version", "langs", "mean_confidence", "low_confidence_count"}},
     "ocr_usage": {"pages", "engines": {name: n}, "cached_pages", "llm_input_tokens", "llm_output_tokens"}}

``start``/``end`` are character offsets into the extracted text. ``status`` is
``text``, ``needs_ocr`` (start == end at the form-feed position),
``ocr`` (text recognised from the page image, offsets like ``text``), ``not_included`` or ``failed``
(start = end = None).

``html_paged`` documents (web pages, SEC filings, plain text) have no physical pages: the extracted text is split
into ~``HTML_PAGE_CHARS``-character pseudo-pages on line (paragraph, heading, table-row) boundaries, never inside a
line. Pages are joined with ``"\n"``, so ``text[start:end]`` is exactly the page text and the next page starts at
``end + 1``. All included pages have status ``text``.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
from time import monotonic
import unicodedata

PAGES_FORMAT_VERSION = 2
HTML_PAGE_CHARS = 8_000
TEXT_STATUSES = frozenset({"text", "ocr"})
CHUNK_PAGES = 20
CHUNK_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_TEXT_CHARS = 2_000_000
_GARBLED_FRACTION = 0.30
_PRLIMIT_ARGS = ("--as=402653184", "--cpu=60")


class PdfPagesError(Exception):
    """Extraction could not run (code: pdf_extractor_unavailable | pdf_unreadable | pdf_timeout)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PdfPagesResult:
    text: str
    document_pages: dict


def _env_number(name: str, default: float, *, minimum: float = 1.0) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value >= minimum else default


def pdf_extract_budget_seconds() -> float:
    return _env_number("FORESIGHTNINE_PDF_EXTRACT_SECONDS", 180.0)


def pdf_extract_workers() -> int:
    return int(_env_number("FORESIGHTNINE_PDF_EXTRACT_WORKERS", 2, minimum=1))


def classify_page_text(piece: str) -> str:
    """Return ``text`` or ``needs_ocr`` for one page's extracted text."""
    chars = [c for c in piece if not c.isspace()]
    if not chars:
        return "needs_ocr"
    bad = 0
    for c in chars:
        if c == "�" or unicodedata.category(c) in {"Co", "Cc", "Cs"}:
            bad += 1
    if bad / len(chars) > _GARBLED_FRACTION:
        return "needs_ocr"
    return "text"


def page_map_from_text(text: str, *, original_retained: bool) -> dict:
    """Page map for an already-extracted document whose pages are separated by form feeds."""
    pieces = text.split("\f")
    if pieces and pieces[-1] == "":
        pieces.pop()
    pages: list[list] = []
    offset = 0
    for piece in pieces:
        end = offset + len(piece)
        status = classify_page_text(piece)
        pages.append([offset, end, status] if status == "text" else [end, end, status])
        offset = end + 1
    return {"page_count": len(pages), "pages": pages, "text_limit_reached": False,
            "original_retained": bool(original_retained), "method": "pdftotext_layout",
            "version": PAGES_FORMAT_VERSION}


class TextPager:
    """Incrementally split extracted text lines into ``html_paged`` pseudo-pages.

    Feed one logical line at a time (no newlines inside). Memory holds only the included text (at most
    ``max_text_chars``); lines past the limit are only counted so ``page_count`` stays truthful.
    """

    def __init__(self, *, max_text_chars: int = DEFAULT_MAX_TEXT_CHARS, page_chars: int = HTML_PAGE_CHARS) -> None:
        self.max_text_chars = max_text_chars
        self.page_chars = page_chars
        self._page_texts: list[str] = []
        self._pages: list[list] = []
        self._offset = 0          # start offset of the next page (end of last page + 1 separator)
        self._lines: list[str] = []
        self._size = 0
        self.limit_reached = False
        self._excluded_pages = 0
        self._excluded_size = 0
        self.lines_seen = 0

    @property
    def included_pages(self) -> int:
        return len(self._pages) + (1 if self._lines else 0)

    def _close_page(self) -> None:
        if not self._lines:
            return
        page = "\n".join(self._lines)
        self._pages.append([self._offset, self._offset + len(page), "text"])
        self._page_texts.append(page)
        self._offset += len(page) + 1
        self._lines, self._size = [], 0

    def _count_excluded(self, line: str) -> None:
        self._excluded_size += len(line) + 1
        if self._excluded_size >= self.page_chars:
            self._excluded_pages += 1
            self._excluded_size = 0

    def add_line(self, line: str) -> None:
        self.lines_seen += 1
        if self.limit_reached:
            self._count_excluded(line)
            return
        grown = self._size + (1 if self._lines else 0) + len(line)
        if self._offset + grown > self.max_text_chars:
            self._close_page()
            self.limit_reached = True
            self._count_excluded(line)
            return
        self._lines.append(line)
        self._size = grown
        if grown >= self.page_chars:
            self._close_page()

    def finish(self, *, original_retained: bool = False, page_count_complete: bool = True) -> tuple[str, dict]:
        self._close_page()
        excluded = self._excluded_pages + (1 if self._excluded_size else 0)
        pages = list(self._pages) + [[None, None, "not_included"] for _ in range(excluded)]
        document_pages = {"page_count": len(pages), "pages": pages, "text_limit_reached": self.limit_reached,
                          "original_retained": bool(original_retained), "method": "html_paged",
                          "version": PAGES_FORMAT_VERSION}
        if not page_count_complete:
            document_pages["page_count_complete"] = False
        return "\n".join(self._page_texts), document_pages


def _valid_pages(document_pages: object) -> list | None:
    if not isinstance(document_pages, dict):
        return None
    pages = document_pages.get("pages")
    return pages if isinstance(pages, list) else None


def page_for_offset(document_pages: dict, offset: int) -> int | None:
    """1-based page containing a character offset, or None if no included page covers it."""
    pages = _valid_pages(document_pages)
    if pages is None or not isinstance(offset, int) or offset < 0:
        return None
    for number, entry in enumerate(pages, 1):
        start, end = entry[0], entry[1]
        if start is None or end is None or entry[2] not in TEXT_STATUSES:
            continue
        if start <= offset < end or (offset == end and start == end):
            return number
    return None


def page_range_to_chars(document_pages: dict, first_page: int, last_page: int) -> tuple[int, int] | None:
    """Character span covering included pages in the inclusive 1-based range, or None."""
    pages = _valid_pages(document_pages)
    if pages is None or first_page > last_page:
        return None
    starts: list[int] = []
    ends: list[int] = []
    for number in range(max(1, first_page), min(last_page, len(pages)) + 1):
        start, end, status = pages[number - 1]
        if start is None or end is None or status not in {"text", "ocr", "needs_ocr"}:
            continue
        starts.append(start)
        ends.append(end)
    if not starts:
        return None
    return min(starts), max(ends)


def _tools() -> tuple[str, str, str]:
    info, extractor, limiter = shutil.which("pdfinfo"), shutil.which("pdftotext"), shutil.which("prlimit")
    if not info or not extractor or not limiter:
        raise PdfPagesError("pdf_extractor_unavailable")
    return info, extractor, limiter


def pdf_page_count(path: Path, *, timeout_seconds: float = 30.0) -> int:
    """Page count via pdfinfo; raises PdfPagesError when unavailable or unreadable."""
    info, _, limiter = _tools()
    try:
        result = subprocess.run([limiter, *_PRLIMIT_ARGS, "--", info, str(path)], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as error:
        raise PdfPagesError("pdf_timeout") from error
    except OSError as error:
        raise PdfPagesError("pdf_extractor_unavailable") from error
    match = re.search(rb"^Pages:\s+(\d+)", result.stdout, re.MULTILINE)
    if result.returncode != 0 or not match or int(match.group(1)) < 1:
        raise PdfPagesError("pdf_unreadable")
    return int(match.group(1))


def _extract_chunk(extractor: str, limiter: str, path: Path, first: int, last: int,
                   timeout: float) -> list[str] | None:
    """Return per-page text for pages first..last, or None on failure/timeout."""
    if timeout <= 0:
        return None
    try:
        result = subprocess.run(
            [limiter, *_PRLIMIT_ARGS, "--", extractor, "-layout", "-enc", "UTF-8",
             "-f", str(first), "-l", str(last), str(path), "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    pieces = result.stdout.decode("utf-8", errors="replace").split("\f")
    if pieces and pieces[-1] == "":
        pieces.pop()
    expected = last - first + 1
    if len(pieces) > expected:
        return None
    # Missing trailing pages (should not happen) are reported as failed by the caller.
    return pieces


def extract_pdf_pages(path: Path, *, deadline: float, workers: int | None = None,
                      max_text_chars: int = DEFAULT_MAX_TEXT_CHARS) -> PdfPagesResult:
    """Extract text page by page in parallel chunks and build the page map."""
    _, extractor, limiter = _tools()
    budget_end = min(deadline, monotonic() + pdf_extract_budget_seconds())
    page_count = pdf_page_count(path, timeout_seconds=max(1.0, min(30.0, budget_end - monotonic())))
    workers = max(1, workers if workers is not None else pdf_extract_workers())
    chunks = [(first, min(first + CHUNK_PAGES - 1, page_count)) for first in range(1, page_count + 1, CHUNK_PAGES)]

    def run(chunk: tuple[int, int]) -> list[str] | None:
        return _extract_chunk(extractor, limiter, path, chunk[0], chunk[1],
                              min(CHUNK_TIMEOUT_SECONDS, budget_end - monotonic()))

    parts: list[str] = []
    length = 0
    pages: list[list] = []
    limit_reached = False
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="foresight-pdf-chunk") as pool:
        futures = [pool.submit(run, chunk) for chunk in chunks]
        for (first, last), future in zip(chunks, futures):
            if limit_reached:
                future.cancel()
                pages.extend([None, None, "not_included"] for _ in range(first, last + 1))
                continue
            pieces = future.result()
            for index in range(last - first + 1):
                if limit_reached:
                    pages.append([None, None, "not_included"])
                    continue
                if pieces is None or index >= len(pieces):
                    pages.append([None, None, "failed"])
                    continue
                piece = pieces[index]
                status = classify_page_text(piece)
                contribution = piece if status == "text" else ""
                if length + len(contribution) + 1 > max_text_chars:
                    limit_reached = True
                    pages.append([None, None, "not_included"])
                    continue
                start = length
                end = start + len(contribution)
                parts.append(contribution)
                parts.append("\f")
                length = end + 1
                pages.append([start, end, status])
    return PdfPagesResult(
        text="".join(parts),
        document_pages={"page_count": page_count, "pages": pages, "text_limit_reached": limit_reached,
                        "original_retained": False, "method": "pdftotext_layout_paged",
                        "version": PAGES_FORMAT_VERSION})


def merge_ocr_into_document(text: str, document_pages: dict, results: dict, *,
                            max_text_chars: int = DEFAULT_MAX_TEXT_CHARS) -> tuple[str, dict]:
    """Insert OCR text for ``needs_ocr`` pages, rebuild offsets and mark those pages ``ocr``.

    ``results`` maps 1-based page numbers to objects with ``text``, ``error``, ``engine``,
    ``engine_version``, ``langs``, ``mean_confidence``, ``low_confidence_count``, ``usage`` and
    ``cached``. Pages whose result failed, is empty or would exceed ``max_text_chars`` stay
    ``needs_ocr``. Token usage of every result (accepted or not) is totalled in ``ocr_usage``.
    """
    pages = document_pages["pages"]
    pieces = text.split("\f")
    if text.endswith("\f"):
        pieces.pop()
    piece_index = 0
    parts: list[str] = []
    length = 0
    new_pages: list[list] = []
    ocr_info: dict[str, dict] = dict(document_pages.get("ocr") or {})
    engines: dict[str, int] = {}
    cached_pages = 0
    for number, (start, end, status) in enumerate(pages, 1):
        if start is None or end is None:
            new_pages.append([start, end, status])
            continue
        piece = pieces[piece_index] if piece_index < len(pieces) else ""
        piece_index += 1
        result = results.get(number) if status == "needs_ocr" else None
        new_status = status
        if result is not None and result.error is None and result.text.strip():
            candidate = result.text.strip("\f")
            if length + len(candidate) + 1 <= max_text_chars:
                piece, new_status = candidate, "ocr"
                ocr_info[str(number)] = {
                    "engine": result.engine, "engine_version": result.engine_version, "langs": result.langs,
                    "mean_confidence": None if result.mean_confidence is None else round(result.mean_confidence, 1),
                    "low_confidence_count": result.low_confidence_count}
                engines[result.engine] = engines.get(result.engine, 0) + 1
                cached_pages += 1 if result.cached else 0
        new_start = length
        new_end = new_start + len(piece)
        parts.append(piece)
        length = new_end + 1
        new_pages.append([new_end, new_end, new_status] if new_status == "needs_ocr"
                         else [new_start, new_end, new_status])
    merged = "\f".join(parts) + ("\f" if parts and text.endswith("\f") else "")
    usage = dict(document_pages.get("ocr_usage") or {})
    prior_engines = dict(usage.get("engines") or {})
    for name, count in engines.items():
        prior_engines[name] = prior_engines.get(name, 0) + count
    input_tokens = sum((r.usage or {}).get("input_tokens", 0) for r in results.values())
    output_tokens = sum((r.usage or {}).get("output_tokens", 0) for r in results.values())
    updated = {**document_pages, "pages": new_pages, "version": PAGES_FORMAT_VERSION, "ocr": ocr_info,
               "ocr_usage": {"pages": sum(prior_engines.values()), "engines": prior_engines,
                             "cached_pages": usage.get("cached_pages", 0) + cached_pages,
                             "llm_input_tokens": usage.get("llm_input_tokens", 0) + input_tokens,
                             "llm_output_tokens": usage.get("llm_output_tokens", 0) + output_tokens}}
    return merged, updated
