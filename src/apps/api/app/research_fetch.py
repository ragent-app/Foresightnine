"""Bounded retrieval of public HTTPS documents (HTML, plain text, PDF)."""
from __future__ import annotations

import base64
import codecs
import io
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import tempfile
from time import monotonic
from typing import Callable, Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, field_validator

from .openai_compatible import (
    BinaryHttpResponse,
    HttpResponse,
    _TransportFailure,
    _TransportTimeout,
    _default_binary_transport,
    _validated_addresses,
    is_public_ip_address,
)
from .research_blob_store import configured_blob_store, is_valid_sha256
from .research_documents import (
    DEFAULT_MAX_TEXT_CHARS,
    TextPager,
    PdfPagesError,
    merge_ocr_into_document,
    extract_pdf_pages,
    pdf_extract_budget_seconds,
    pdf_page_count,
    page_map_from_text,
)
from .phase3 import ClientSearchEvidenceProvider, ProviderFailure
from .research_source_policy import is_fred_source
from .research_sources import SourcePermissions


PUBLIC_DOMAIN_OFFICIAL_HOSTS = frozenset({"www.bea.gov", "www.federalreserve.gov", "www.sec.gov"})
MAX_FETCH_BYTES = 5_000_000
MAX_EXTRACTED_TEXT_CHARS = 2_000_000
MAX_REDIRECT_HOPS = 4
_LARGE_MAX_BYTES_ENV = "FORESIGHTNINE_LARGE_DOCUMENT_MAX_BYTES"
_LARGE_SECONDS_ENV = "FORESIGHTNINE_LARGE_DOCUMENT_DOWNLOAD_SECONDS"
LARGE_DOCUMENT_MAX_BYTES = 200_000_000
LARGE_DOCUMENT_DOWNLOAD_SECONDS = 120.0
_HARD_LARGE_MAX_BYTES = 256_000_000


def large_document_max_bytes() -> int:
    try:
        value = int(os.environ.get(_LARGE_MAX_BYTES_ENV, ""))
    except ValueError:
        return LARGE_DOCUMENT_MAX_BYTES
    return value if 1 <= value <= _HARD_LARGE_MAX_BYTES else LARGE_DOCUMENT_MAX_BYTES


def large_document_download_seconds() -> float:
    try:
        value = float(os.environ.get(_LARGE_SECONDS_ENV, ""))
    except ValueError:
        return LARGE_DOCUMENT_DOWNLOAD_SECONDS
    return value if value > 0 else LARGE_DOCUMENT_DOWNLOAD_SECONDS

_ALLOWED_HOSTS_ENV = "FORESIGHTNINE_FETCH_ALLOWED_HOSTS"
_BLOCKED_HOSTS_ENV = "FORESIGHTNINE_FETCH_BLOCKED_HOSTS"
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_NON_PUBLIC_SUFFIXES = (".localhost", ".local", ".internal", ".test", ".invalid", ".arpa")
_SEC_FILING_PATH_RE = re.compile(
    r"/Archives/edgar/data/\d{1,10}/\d{18}/[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.html?"
)
_CHARSET_HEADER_RE = re.compile(r";\s*charset\s*=\s*['\"]?([A-Za-z0-9._-]+)['\"]?", re.IGNORECASE)
_META_CHARSET_RE = re.compile(r"<meta\b[^>]*\bcharset\s*=\s*['\"]?\s*([A-Za-z0-9._-]+)", re.IGNORECASE)
_HIDDEN_STYLE_RE = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!\s*important\s*)?(?:;|$)",
    re.IGNORECASE,
)
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_HIDDEN_CONTAINER_TAGS = frozenset({"ix:header", "ix:hidden", "template"})
_HARD_HIDDEN_TAGS = frozenset({"script", "style", "noscript", "svg"})
_BOILERPLATE_TAGS = frozenset({"nav", "footer", "aside"})
_BOILERPLATE_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary"})
_PRESERVE_CONTAINER_TAGS = frozenset({"main", "article", "table"})
_VOID_HTML_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr",
})

HTML_EXTRACTION_DISCLOSURE = (
    "Webpage text is extracted; table separators and span markers, when present, "
    "are generated. Extraction does not reproduce browser rendering; check the original document for layout and visibility."
)


def _parse_host_list(raw: str) -> frozenset[str]:
    return frozenset(
        item.strip().lower().rstrip(".")
        for item in raw.split(",")
        if item.strip() and item.strip().rstrip(".")
    )


def fetch_allowed_hosts() -> frozenset[str] | None:
    """Return optional operator-configured host allowlist, or None for open public HTTPS."""
    hosts = _parse_host_list(os.environ.get(_ALLOWED_HOSTS_ENV, ""))
    return hosts or None


def fetch_blocked_hosts() -> frozenset[str]:
    """Return operator-configured blocked hostnames."""
    return _parse_host_list(os.environ.get(_BLOCKED_HOSTS_ENV, ""))


def is_host_blocked(hostname: str) -> bool:
    """Return True if hostname or a parent domain matches the configured fetch blocklist."""
    host = hostname.lower().rstrip(".")
    return any(host == blocked or host.endswith("." + blocked) for blocked in fetch_blocked_hosts())


def is_sec_host(hostname: str) -> bool:
    """Return True if hostname is sec.gov or any subdomain of sec.gov."""
    try:
        host = hostname.rstrip(".").encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return False
    return host == "sec.gov" or host.endswith(".sec.gov")


def validate_public_https_url(url: str, *, allow_fragment: bool = False, check_sec_path: bool = True) -> str:
    """Validate strict public HTTPS URL safety and return the normalized hostname."""
    if not isinstance(url, str) or not url.strip() or "\\" in url:
        raise ValueError("Only supported public HTTPS sources are available.")
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("Source URL port is invalid.") from error
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or (not allow_fragment and parts.fragment)
    ):
        raise ValueError("Only supported public HTTPS sources are available.")
    try:
        ClientSearchEvidenceProvider().validate_url(url)
    except ProviderFailure as error:
        raise ValueError("Source URL failed safety validation.") from error
    try:
        hostname = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as error:
        raise ValueError("Source hostname is invalid.") from error
    if (
        not hostname
        or len(hostname) > 253
        or hostname == "localhost"
        or hostname.endswith(_NON_PUBLIC_SUFFIXES)
    ):
        raise ValueError("Non-public hostnames are not permitted.")
    literal = ClientSearchEvidenceProvider._literal_address(hostname)
    if literal is not None:
        if not is_public_ip_address(literal):
            raise ValueError("Private or non-global IP addresses are not permitted.")
    else:
        labels = hostname.split(".")
        if len(labels) < 2 or any(not label for label in labels):
            raise ValueError("Source hostname must be a public domain name.")
        if all(re.fullmatch(r"(?:0x[0-9a-f]+|0[0-7]+|\d+)", label) for label in labels) and any(
            label.startswith("0x") or (len(label) > 1 and label.startswith("0")) for label in labels
        ):
            raise ValueError("Non-canonical IPv4 hostnames are not permitted.")
    normalized_netloc = f"[{hostname}]" if ":" in hostname else hostname
    normalized_url = urlunsplit((parts.scheme.lower(), normalized_netloc, parts.path, parts.query, parts.fragment))
    if is_fred_source(url) or is_fred_source(normalized_url):
        raise ValueError("FRED sources are not authorized.")
    if check_sec_path and is_sec_host(hostname):
        if hostname != "www.sec.gov" or parts.query or not _SEC_FILING_PATH_RE.fullmatch(parts.path):
            raise ValueError("Only direct www.sec.gov SEC filing HTML paths are available.")
    return hostname


class FetchSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    url: HttpUrl
    title: str = Field(min_length=1, max_length=500)
    permissions: SourcePermissions
    is_simulated: bool = False
    published_at: datetime | None = None
    event_at: datetime | None = None
    revised_at: datetime | None = None

    @field_validator("published_at", "event_at", "revised_at")
    @classmethod
    def publication_timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Timestamp must include a timezone.")
        return value

    @field_validator("url")
    @classmethod
    def allow_official_https_only(cls, value: HttpUrl) -> HttpUrl:
        validate_public_https_url(str(value), allow_fragment=False, check_sec_path=True)
        return value


class FetchFailure(ValueError):
    def __init__(self, code: str, detail: str | None = None):
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass
class _TableRowState:
    table_depth: int
    part_start: int
    has_nonempty: bool = False
    pending_empty: int = 0


@dataclass
class _TableCellState:
    row: _TableRowState | None
    part_start: int
    spans: list[str]


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []
        self.cell_depth = 0
        self.row_depth = 0
        self._table_depth = 0
        self._row_stack: list[_TableRowState] = []
        self._cell_stack: list[_TableCellState] = []
        self._scope_stack: list[tuple[str, bool]] = []

    def _boilerplate_suppressed(self) -> bool:
        return bool(self._scope_stack and self._scope_stack[-1][1])

    @staticmethod
    def _has_boilerplate_role(attrs: list[tuple[str, str | None]]) -> bool:
        for name, value in attrs:
            if name == "role" and isinstance(value, str):
                if any(token in _BOILERPLATE_ROLES for token in value.strip().lower().split()):
                    return True
        return False

    def _finish_cell(self) -> None:
        if not self._cell_stack:
            return
        cell = self._cell_stack.pop()
        row = cell.row
        if row is None:
            return
        content = "".join(self.parts[cell.part_start:])
        if not content.strip():
            # Suppress empty spacer cells and their contentless span markers;
            # any contiguous run of empty cells collapses at the next real cell.
            del self.parts[cell.part_start:]
            row.pending_empty += 1
            return
        prefix: list[str] = []
        if row.pending_empty:
            prefix.append(" | ")
        prefix.append(" | ")
        prefix.extend(cell.spans)
        self.parts[cell.part_start:cell.part_start] = prefix
        row.has_nonempty = True
        row.pending_empty = 0

    def _finish_row(self) -> None:
        if not self._row_stack:
            self.parts.append(" |\n")
            return
        row = self._row_stack.pop()
        while self._cell_stack and self._cell_stack[-1].row is row:
            self._finish_cell()
            self.cell_depth = max(0, self.cell_depth - 1)
        if row.has_nonempty or "".join(self.parts[row.part_start:]).strip():
            self.parts.append(" |\n")
        else:
            del self.parts[row.part_start:]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HARD_HIDDEN_TAGS:
            self.hidden += 1
        if self.hidden:
            return
        if tag in _PRESERVE_CONTAINER_TAGS:
            suppressed = False
        elif tag in _BOILERPLATE_TAGS or self._has_boilerplate_role(attrs):
            suppressed = True
        else:
            suppressed = self._boilerplate_suppressed()
        if tag not in _VOID_HTML_TAGS:
            self._scope_stack.append((tag, suppressed))
        if suppressed:
            return
        if tag == "table":
            self._table_depth += 1
        elif tag in {"td", "th"}:
            if self._row_stack and self._cell_stack and self._cell_stack[-1].row is self._row_stack[-1]:
                self._finish_cell()
                self.cell_depth = max(0, self.cell_depth - 1)
            self.cell_depth += 1
            spans: list[str] = []
            # These are extraction annotations, not source quotations. Keep
            # only bounded numeric spans; do not copy arbitrary attributes.
            for name, value in attrs:
                if (name in {"colspan", "rowspan"} and value and len(value) <= 2 and value.isascii() and
                        value.isdigit() and 1 < int(value) <= 99):
                    spans.append(f"[{name}={int(value)}] ")
            if self._row_stack:
                self._cell_stack.append(_TableCellState(
                    row=self._row_stack[-1],
                    part_start=len(self.parts),
                    spans=spans,
                ))
            else:
                self.parts.append(" | ")
                self.parts.extend(spans)
                self._cell_stack.append(_TableCellState(
                    row=None,
                    part_start=len(self.parts),
                    spans=[],
                ))
        elif tag == "tr":
            if self._row_stack and self._row_stack[-1].table_depth == self._table_depth:
                self._finish_row()
                self.row_depth = max(0, self.row_depth - 1)
            self.row_depth += 1
            row_part_start = len(self.parts)
            self.parts.append("\n")
            self._row_stack.append(_TableRowState(
                table_depth=self._table_depth,
                part_start=row_part_start,
            ))
        elif tag in {"p", "div", "br", "h1", "h2", "h3", "li"}:
            self.parts.append(" " if self.cell_depth else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HARD_HIDDEN_TAGS and self.hidden:
            self.hidden -= 1
            return
        if self.hidden:
            return
        scope_idx: int | None = None
        for idx in range(len(self._scope_stack) - 1, -1, -1):
            if self._scope_stack[idx][0] == tag:
                scope_idx = idx
                break
        if scope_idx is not None:
            was_suppressed = self._scope_stack[scope_idx][1]
            del self._scope_stack[scope_idx:]
            if was_suppressed:
                return
        elif self._boilerplate_suppressed():
            return
        if tag in {"td", "th"} and self.cell_depth:
            self.cell_depth -= 1
            self._finish_cell()
        elif tag == "tr":
            self._finish_row()
            self.row_depth = max(0, self.row_depth - 1)
        elif tag == "table" and self._table_depth:
            while self._row_stack and self._row_stack[-1].table_depth == self._table_depth:
                self._finish_row()
                self.row_depth = max(0, self.row_depth - 1)
            self._table_depth -= 1
        elif tag in {"p", "div", "h1", "h2", "h3", "li"}:
            self.parts.append(" " if self.cell_depth else "\n")

    def handle_data(self, data: str) -> None:
        if self.hidden or self._boilerplate_suppressed():
            return
        if self.row_depth and not self.cell_depth and not data.strip():
            return
        self.parts.append(re.sub(r"\s+", " ", data) if self.cell_depth else data)

    def close(self) -> None:
        super().close()
        while self._row_stack:
            self._finish_row()
        while self._cell_stack:
            self._finish_cell()
        self.cell_depth = 0
        self.row_depth = 0
        self._table_depth = 0
        self._scope_stack.clear()


class _SafeHtmlParser(_TextParser):
    """Omit XBRL headers and explicitly hidden subtrees without executing CSS."""

    _void = _VOID_HTML_TAGS

    def __init__(self) -> None:
        super().__init__()
        self.hidden_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.hidden_tags:
            if tag not in self._void:
                self.hidden_tags.append(tag)
            return
        hidden = tag in _HIDDEN_CONTAINER_TAGS or any(name == "hidden" for name, _ in attrs)
        for name, value in attrs:
            if name == "style" and isinstance(value, str):
                cleaned_style = _CSS_COMMENT_RE.sub("", value)
                hidden = hidden or bool(_HIDDEN_STYLE_RE.search(cleaned_style))
        if hidden:
            if tag not in self._void:
                self.hidden_tags.append(tag)
            return
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self.hidden_tags:
            # Closing an ancestor closes any unclosed nested tags inside the hidden subtree.
            if tag in self.hidden_tags:
                index = len(self.hidden_tags) - 1 - self.hidden_tags[::-1].index(tag)
                del self.hidden_tags[index:]
            return
        super().handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not self.hidden_tags:
            super().handle_data(data)


@dataclass(frozen=True)
class FetchedText:
    text: str
    response_sha256: str
    content_type: str
    original_html_base64: str | None = None
    pages: dict | None = None


@dataclass(frozen=True)
class FetchedPdf:
    text: str
    response_sha256: str
    content_type: str
    original_pdf_base64: str | None = None
    pages: dict | None = None


@dataclass(frozen=True)
class FetchedDocument:
    text: str
    response_sha256: str
    content_type: str
    final_url: str
    original_html_base64: str | None = None
    original_pdf_base64: str | None = None
    redirect_count: int = 0
    pages: dict | None = None


class _HashingSink:
    """File sink that counts bytes and hashes while the body streams in."""

    def __init__(self, path: Path):
        self.path = path
        self._file = path.open("wb")
        self._hash = sha256()
        self.size = 0

    def write(self, data: bytes) -> int:
        if self._file is None:
            raise _TransportFailure("sink closed")
        self._file.write(data)
        self._hash.update(data)
        self.size += len(data)
        return len(data)

    def reset(self) -> None:
        if self._file is None:
            raise _TransportFailure("sink closed")
        self._file.seek(0)
        self._file.truncate()
        self._hash = sha256()
        self.size = 0

    def hexdigest(self) -> str:
        return self._hash.hexdigest()

    def close(self) -> None:
        file, self._file = self._file, None
        if file is not None:
            file.close()


def _header(headers: Mapping[str, str], name: str) -> str:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value)
    return ""


def _normalize_text_encoding(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = name.strip().strip("'\"").lower()
    if not cleaned or len(cleaned) > 40:
        return None
    if cleaned in {"gb2312", "gbk", "x-gbk", "csgb2312"}:
        cleaned = "gb18030"
    elif cleaned in {"utf8", "utf-8"}:
        cleaned = "utf-8-sig"
    try:
        codec = codecs.lookup(cleaned)
    except LookupError:
        return None
    if not getattr(codec, "_is_text_encoding", True):
        return None
    return cleaned


def _encoding_candidates(head: bytes, content_type_header: str, *, is_html: bool) -> list[str]:
    """Candidate encodings in priority order from BOM, header charset, meta charset (first 8 KB) and fallbacks."""
    candidates: list[str] = []
    if head.startswith(codecs.BOM_UTF8):
        candidates.append("utf-8-sig")
    elif head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        candidates.append("utf-16")
    header_match = _CHARSET_HEADER_RE.search(content_type_header)
    if header_match:
        normalized = _normalize_text_encoding(header_match.group(1))
        if normalized:
            candidates.append(normalized)
    if is_html:
        meta_match = _META_CHARSET_RE.search(head[:8192].decode("ascii", errors="ignore"))
        if meta_match:
            normalized = _normalize_text_encoding(meta_match.group(1))
            if normalized:
                candidates.append(normalized)
    candidates.extend(("utf-8-sig", "gb18030", "latin-1"))
    return list(dict.fromkeys(candidates))


def _decode_text_bytes(raw: bytes, content_type_header: str, *, is_html: bool) -> str:
    """Decode HTTP bytes using declared header/meta charset with safe UTF-8/GB18030/Latin-1 fallback."""
    for encoding in _encoding_candidates(raw[:8192], content_type_header, is_html=is_html):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


_TEXT_READ_CHUNK = 1 << 20
HTML_PARSE_SECONDS = 120.0
_MAX_UNDRAINED_PARTS = 1_000_000
_LINE_BREAKS = "\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"


def _iter_text_chunks(source: Path | bytes | str, encoding: str | None):
    """Yield decoded text chunks; strict incremental decoding so a bad byte aborts this encoding attempt."""
    if isinstance(source, str):
        yield source
        return
    decoder = codecs.getincrementaldecoder(encoding or "utf-8-sig")(errors="strict")
    handle = io.BytesIO(source) if isinstance(source, bytes) else source.open("rb")
    try:
        while chunk := handle.read(_TEXT_READ_CHUNK):
            text = decoder.decode(chunk)
            if text:
                yield text
        tail = decoder.decode(b"", final=True)
        if tail:
            yield tail
    finally:
        handle.close()


def _extract_text_once(source: Path | bytes | str, encoding: str | None, *, is_html: bool,
                       deadline: float) -> tuple[TextPager, bool]:
    """Return ``(pager, truncated_scan)``; the scan stops early only after the text limit when time runs out."""
    truncated = False
    pager = TextPager(max_text_chars=MAX_EXTRACTED_TEXT_CHARS)
    parser = _SafeHtmlParser() if is_html else None
    carry = ""
    leading = True
    blanks = 0

    def emit(text: str, *, final: bool) -> None:
        nonlocal carry, leading, blanks
        pieces = (carry + text).splitlines(keepends=True)
        carry = ""
        if pieces and not final and pieces[-1][-1] not in _LINE_BREAKS:
            carry = pieces.pop()
        for piece in pieces:
            line = piece.strip() if is_html else piece.rstrip()
            if is_html:
                if line:
                    pager.add_line(line)
                continue
            if not line.strip():
                blanks += 0 if leading else 1
                continue
            if leading:
                line, leading = line.lstrip(), False
            for _ in range(blanks):
                pager.add_line("")
            blanks = 0
            pager.add_line(line)

    def over_deadline() -> bool:
        return monotonic() > deadline

    for chunk in _iter_text_chunks(source, encoding):
        if parser is None:
            emit(chunk, final=False)
        else:
            try:
                parser.feed(chunk)
            except (ValueError, RecursionError) as error:
                raise FetchFailure("source_invalid", "HTML parsing failed.") from error
            # Rows and cells keep indexes into ``parts``; flush only between them so memory stays bounded.
            if parser.parts and not parser._row_stack and not parser._cell_stack:
                text = "".join(parser.parts)
                parser.parts.clear()
                emit(text, final=False)
            elif len(parser.parts) > _MAX_UNDRAINED_PARTS:
                # Everything is nested inside one open table cell, so nothing can be flushed safely.
                raise FetchFailure("source_too_large", "HTML text is nested inside one table cell and cannot be paged.")
        if over_deadline():
            if pager.limit_reached:
                truncated = True
                break
            raise FetchFailure("source_timeout", "Document text extraction exceeded its time budget.")
    if not truncated:
        if parser is not None:
            try:
                parser.close()
            except (ValueError, RecursionError) as error:
                raise FetchFailure("source_invalid", "HTML parsing failed.") from error
            emit("".join(parser.parts), final=True)
        else:
            emit("", final=True)
    return pager, truncated


def extract_text_pages(source: Path | bytes | str, content_type_header: str, *, is_html: bool,
                       deadline: float | None = None, original_retained: bool = False) -> tuple[str, dict]:
    """Extract HTML or plain text incrementally and return ``(text, document_pages)`` (``html_paged``).

    ``source`` is a file path (streamed in 1 MB chunks), raw bytes, or an already decoded string. Memory is
    bounded by the 2,000,000-character text limit plus one chunk, not by the document size. HTML hidden-subtree
    protections persist across chunk boundaries because one parser instance consumes all chunks.
    """
    if deadline is None:
        deadline = monotonic() + HTML_PARSE_SECONDS
    if isinstance(source, str):
        attempts: list[str | None] = [None]
    else:
        if isinstance(source, bytes):
            head = source[:8192]
        else:
            with source.open("rb") as handle:
                head = handle.read(8192)
        attempts = list(_encoding_candidates(head, content_type_header, is_html=is_html))
    pager: TextPager | None = None
    truncated = False
    for encoding in attempts:
        try:
            pager, truncated = _extract_text_once(source, encoding, is_html=is_html, deadline=deadline)
            break
        except UnicodeDecodeError:
            continue
    if pager is None:  # latin-1 never fails; defensive
        raise FetchFailure("source_invalid", "Document could not be decoded.")
    if pager.included_pages == 0:
        if pager.limit_reached:
            raise FetchFailure("source_too_large", "Extracted text exceeded 2,000,000 characters.")
        raise FetchFailure("source_invalid", "Document contained no extractable text.")
    return pager.finish(original_retained=original_retained,
                        page_count_complete=not truncated)


def _check_fetch_target(url: str, *, is_redirect: bool, check_dns: bool) -> str:
    """Enforce URL safety, FRED exclusion, operator blocklist/allowlist, and public DNS."""
    if is_fred_source(url):
        raise FetchFailure("source_blocked_policy", "FRED sources are not authorized.")
    try:
        hostname = validate_public_https_url(url, allow_fragment=False, check_sec_path=False)
    except ValueError as error:
        raise FetchFailure("source_unavailable" if is_redirect else "source_invalid", str(error)) from error
    if is_sec_host(hostname):
        raise FetchFailure("source_unavailable", "SEC URLs must use the dedicated SEC filing fetcher.")
    if is_host_blocked(hostname):
        raise FetchFailure("source_blocked_policy", "Host is blocked by fetch policy.")
    allowed_hosts = fetch_allowed_hosts()
    if allowed_hosts is not None and hostname not in allowed_hosts:
        raise FetchFailure("source_blocked_policy", "Host is not in the configured fetch allowlist.")
    if is_redirect and check_dns:
        port = urlsplit(url).port or 443
        try:
            _validated_addresses(hostname, port, allow_private=False)
        except _TransportFailure as error:
            raise FetchFailure("source_unavailable", "Redirect target failed public DNS validation.") from error
    return hostname


def _extract_html_text(html: str) -> str:
    parser = _SafeHtmlParser()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, RecursionError) as error:
        raise FetchFailure("source_invalid", "HTML parsing failed.") from error
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def _extract_pdf_document(request: FetchSourceRequest, raw: bytes, *, final_url: str,
                          redirect_count: int, deadline: float, ocr_usage=None) -> FetchedDocument:
    from .research_uploads import MAX_PDF_BYTES, PdfFailure, PdfUploadRequest, extract_pdf

    limit = min(MAX_FETCH_BYTES, MAX_PDF_BYTES)
    if len(raw) > limit:
        raise FetchFailure("source_too_large", "PDF exceeded 5 MB limit.")
    if not raw.startswith(b"%PDF-"):
        raise FetchFailure("source_invalid", "Invalid PDF header.")
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise FetchFailure("source_timeout")
    scanned_pages: int | None = None
    try:
        extracted = extract_pdf(
            PdfUploadRequest(
                title=request.title,
                pdf_base64=base64.b64encode(raw).decode("ascii"),
                permissions=request.permissions,
                text_order="layout",
            ),
            timeout_seconds=min(15.0, remaining),
        )
        text = extracted.text
        document_pages = page_map_from_text(text, original_retained=True)
    except (PdfFailure, ValidationError) as error:
        if isinstance(error, PdfFailure) and error.code == "pdf_timeout":
            raise FetchFailure("source_timeout") from error
        if isinstance(error, PdfFailure) and error.code == "pdf_extractor_unavailable":
            raise FetchFailure("source_unavailable", "pdf_extractor_unavailable") from error
        if isinstance(error, PdfFailure) and error.code == "pdf_no_extractable_text":
            scanned_pages = _small_pdf_page_count(raw)
            if not scanned_pages:
                raise FetchFailure("ocr_required", _ocr_detail(None)) from error
            text = "\f" * scanned_pages
            document_pages = page_map_from_text(text, original_retained=True)
        else:
            raise FetchFailure("source_invalid", "PDF extraction failed.") from error
    statuses = [entry[2] for entry in document_pages["pages"]]
    if "needs_ocr" in statuses:
        with tempfile.TemporaryDirectory(prefix="foresight-ocr-") as directory:
            path = Path(directory) / "source.pdf"
            path.write_bytes(raw)
            text, document_pages = _ingest_ocr(path, text, document_pages, title=request.title, url=final_url,
                                               usage=ocr_usage)
    if not any(entry[2] in {"text", "ocr"} for entry in document_pages["pages"]):
        raise FetchFailure("ocr_required", _ocr_detail(scanned_pages or document_pages["page_count"]))
    return FetchedDocument(
        text=text,
        response_sha256=sha256(raw).hexdigest(),
        content_type="application/pdf",
        final_url=final_url,
        original_pdf_base64=base64.b64encode(raw).decode("ascii"),
        redirect_count=redirect_count,
        pages=document_pages,
    )


def _ingest_ocr(path: Path, text: str, document_pages: dict, *, title: str | None, url: str | None,
                usage=None) -> tuple[str, dict]:
    """OCR the earliest needs_ocr pages within the ingestion budget; unchanged when no engine is available.

    The vision engine runs only through ``usage`` hooks (reserve/settle of the research run); without them
    only local engines are used.
    """
    from .research_ocr import choose_ocr_languages, default_engines, ocr_pdf_pages

    pending = [number for number, entry in enumerate(document_pages["pages"], 1)
               if entry[2] == "needs_ocr" and entry[0] is not None]
    pending = pending[:int(_env_int("FORESIGHTNINE_OCR_INGEST_MAX_PAGES", 30))]
    engines = default_engines()
    if not pending or not engines:
        return text, document_pages
    langs = choose_ocr_languages(title=title, url=url, sample_text=text[:4000])
    deadline = monotonic() + _env_int("FORESIGHTNINE_OCR_INGEST_SECONDS", 90)
    results = ocr_pdf_pages(path, pending, langs=langs, deadline=deadline, engines=engines, usage=usage)
    if not results:
        return text, document_pages
    return merge_ocr_into_document(text, document_pages, results, max_text_chars=DEFAULT_MAX_TEXT_CHARS)


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value >= 0 else default


def _ocr_detail(page_count: int | None) -> str:
    pages = f"{page_count} pages" if page_count else "unknown page count"
    return f"PDF has no usable text layer ({pages}); it appears to be scanned and needs OCR."


def _small_pdf_page_count(raw: bytes) -> int | None:
    try:
        with tempfile.TemporaryDirectory(prefix="foresight-pdfinfo-") as directory:
            path = Path(directory) / "source.pdf"
            path.write_bytes(raw)
            return pdf_page_count(path, timeout_seconds=10.0)
    except (PdfPagesError, OSError):
        return None


def _extract_large_pdf(path: Path, *, response_sha256: str, final_url: str,
                       redirect_count: int, request: FetchSourceRequest | None = None,
                       ocr_usage=None) -> FetchedDocument:
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise FetchFailure("source_invalid", "Invalid PDF header.")
    try:
        result = extract_pdf_pages(path, deadline=monotonic() + pdf_extract_budget_seconds())
    except PdfPagesError as error:
        if error.code == "pdf_extractor_unavailable":
            raise FetchFailure("source_unavailable", "pdf_extractor_unavailable") from error
        if error.code == "pdf_timeout":
            raise FetchFailure("source_invalid", "PDF text extraction timed out locally.") from error
        raise FetchFailure("source_invalid", "PDF extraction failed.") from error
    text, document_pages = result.text, result.document_pages
    statuses = [entry[2] for entry in document_pages["pages"]]
    if "failed" in statuses and "text" not in statuses:
        raise FetchFailure("source_invalid", "PDF page extraction did not complete (local timeout or failure).")
    if "needs_ocr" in statuses:
        text, document_pages = _ingest_ocr(path, text, document_pages,
                                           title=getattr(request, "title", None), url=final_url, usage=ocr_usage)
    if not any(entry[2] in {"text", "ocr"} for entry in document_pages["pages"]):
        raise FetchFailure("ocr_required", _ocr_detail(document_pages["page_count"]))
    store = configured_blob_store()
    if store is not None and (request is None or request.permissions.store):
        try:
            digest = store.put_file(path)
        except OSError:
            digest = None
        if digest is not None and is_valid_sha256(digest):
            document_pages = {**document_pages, "original_retained": True, "original_blob_sha256": digest}
    return FetchedDocument(
        text=text,
        response_sha256=response_sha256,
        content_type="application/pdf",
        final_url=final_url,
        original_pdf_base64=None,
        redirect_count=redirect_count,
        pages=document_pages,
    )


def _fetch_document(
    request: FetchSourceRequest,
    *,
    transport: Callable[..., object] | None,
    timeout_seconds: float,
    accepted_types: frozenset[str],
    accept_header: str,
    max_redirects: int,
    ocr_usage=None,
) -> FetchedDocument:
    if transport is not None:
        return _fetch_document_impl(request, transport=transport, timeout_seconds=timeout_seconds,
                                    accepted_types=accepted_types, accept_header=accept_header,
                                    max_redirects=max_redirects, spool=None, ocr_usage=ocr_usage)
    # Default transport: stream the body to a temporary file so large PDFs never sit in memory.
    with tempfile.TemporaryDirectory(prefix="foresight-fetch-") as directory:
        spool = _HashingSink(Path(directory) / "download.bin")
        try:
            return _fetch_document_impl(request, transport=None, timeout_seconds=timeout_seconds,
                                        accepted_types=accepted_types, accept_header=accept_header,
                                        max_redirects=max_redirects, spool=spool, ocr_usage=ocr_usage)
        finally:
            spool.close()


def _fetch_document_impl(
    request: FetchSourceRequest,
    *,
    transport: Callable[..., object] | None,
    timeout_seconds: float,
    accepted_types: frozenset[str],
    accept_header: str,
    max_redirects: int,
    spool: _HashingSink | None,
    ocr_usage=None,
) -> FetchedDocument:
    if not request.permissions.store:
        raise FetchFailure("source_permission_unknown")
    if not 0 < timeout_seconds <= 15:
        raise FetchFailure("source_timeout")
    current_parts = urlsplit(str(request.url))
    current_url = urlunsplit((
        current_parts.scheme.lower(),
        current_parts.netloc.lower(),
        current_parts.path,
        current_parts.query,
        "",
    ))
    visited_urls = {current_url}
    using_default_transport = transport is None
    _check_fetch_target(current_url, is_redirect=False, check_dns=False)
    download_cap = large_document_max_bytes()
    # Streaming downloads are bounded by the overall download budget; the per-hop
    # timeout_seconds still bounds connect and response headers.
    deadline = monotonic() + (large_document_download_seconds() if spool is not None else timeout_seconds)
    if transport is not None:
        send = transport
    else:
        def send(method, address, headers, body, timeout):
            assert spool is not None
            spool.reset()
            return _default_binary_transport(
                method, address, headers, body, timeout, max_response_bytes=download_cap,
                response_timeout_seconds=min(timeout_seconds, timeout), sink=spool,
            )
    headers = {"Accept": accept_header, "User-Agent": "ForesightNine-Research/1.0"}
    redirect_count = 0
    response: object | None = None
    for hop in range(max_redirects + 1):
        if hop == 0:
            hop_timeout = timeout_seconds if spool is None else min(deadline - monotonic(), large_document_download_seconds())
        else:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise FetchFailure("source_timeout")
            hop_timeout = remaining if spool is not None else min(timeout_seconds, remaining)
        try:
            response = send("GET", current_url, headers, b"", hop_timeout)
        except _TransportTimeout as error:
            raise FetchFailure("source_timeout") from error
        except (_TransportFailure, OSError) as error:
            if isinstance(error, _TransportFailure) and str(error) == "response too large":
                raise FetchFailure("source_too_large", f"Response exceeded {download_cap:,} byte limit.") from error
            raise FetchFailure("source_unavailable") from error
        status_code = getattr(response, "status_code", 0)
        resp_headers = getattr(response, "headers", {})
        if status_code in _REDIRECT_STATUS_CODES:
            if hop >= max_redirects:
                raise FetchFailure("source_unavailable", "Redirect limit exceeded.")
            location = _header(resp_headers, "location").strip()
            if not location:
                raise FetchFailure("source_unavailable", "Redirect response missing Location header.")
            joined = urljoin(current_url, location)
            jp = urlsplit(joined)
            if jp.username is not None or jp.password is not None:
                raise FetchFailure("source_unavailable", "Redirect URL credentials are not permitted.")
            next_url = urlunsplit((jp.scheme.lower(), jp.netloc.lower(), jp.path, jp.query, ""))
            if next_url == current_url or next_url in visited_urls:
                raise FetchFailure("source_unavailable", "Redirect loop detected.")
            _check_fetch_target(next_url, is_redirect=True, check_dns=using_default_transport)
            current_url = next_url
            visited_urls.add(current_url)
            redirect_count += 1
            continue
        break
    assert response is not None
    status_code = getattr(response, "status_code", 0)
    resp_headers = getattr(response, "headers", {})
    if status_code in {401, 403}:
        raise FetchFailure("http_forbidden_or_challenge", f"HTTP {status_code}")
    if status_code in {404, 410}:
        raise FetchFailure("not_found", f"HTTP {status_code}")
    if status_code == 413:
        raise FetchFailure("source_too_large", "HTTP 413")
    if status_code == 429:
        raise FetchFailure("source_rate_limited", "HTTP 429")
    if not 200 <= status_code < 300:
        raise FetchFailure("source_unavailable", f"HTTP {status_code}")
    content_length = _header(resp_headers, "content-length").strip()
    content_type_header = _header(resp_headers, "content-type")
    content_type = content_type_header.split(";", 1)[0].lower().strip()
    # A streamed body lives in the spool file; an in-memory body (e.g. a patched transport) takes precedence.
    body = getattr(response, "body", None)
    streamed = spool is not None and spool.size > 0 and not (isinstance(body, (bytes, str)) and len(body) > 0)
    is_pdf = content_type == "application/pdf"
    length_limit = download_cap
    if content_length.isdigit() and int(content_length) > length_limit:
        raise FetchFailure("source_too_large", f"Response of {int(content_length):,} bytes exceeded {length_limit:,} byte limit.")
    if content_type not in accepted_types:
        raise FetchFailure("source_format_unsupported", f"Unsupported content-type: {content_type or 'unknown'}")
    spool_path: Path | None = None
    if streamed:
        assert spool is not None
        spool.close()
        if is_pdf and spool.size > MAX_FETCH_BYTES:
            return _extract_large_pdf(spool.path, response_sha256=spool.hexdigest(),
                                      final_url=current_url, redirect_count=redirect_count,
                                      request=request, ocr_usage=ocr_usage)
        if is_pdf or spool.size <= MAX_FETCH_BYTES:
            body = spool.path.read_bytes()
        else:
            spool_path = spool.path  # large HTML/text: parsed from the file, never read whole into memory
    if content_type == "application/pdf":
        if not isinstance(body, bytes):
            raise FetchFailure("source_invalid", "PDF response must be binary.")
        return _extract_pdf_document(request, body, final_url=current_url, redirect_count=redirect_count, deadline=deadline,
                                     ocr_usage=ocr_usage)
    is_html = content_type == "text/html"
    parse_deadline = monotonic() + HTML_PARSE_SECONDS
    if spool_path is not None:
        assert spool is not None
        size, digest, source = spool.size, spool.hexdigest(), spool_path
        raw: bytes | None = None
    elif isinstance(body, str):
        raw = body.encode("utf-8")
        size, digest, source = len(raw), sha256(raw).hexdigest(), body
    elif isinstance(body, bytes):
        raw = body
        size, digest, source = len(raw), sha256(raw).hexdigest(), body
    else:
        raise FetchFailure("source_invalid")
    if size > download_cap:
        raise FetchFailure("source_too_large", f"Response exceeded {download_cap:,} byte limit.")
    text, document_pages = extract_text_pages(source, content_type_header, is_html=is_html, deadline=parse_deadline)
    original_base64: str | None = None
    if is_html and size <= MAX_FETCH_BYTES:
        data = raw if raw is not None else spool_path.read_bytes()  # type: ignore[union-attr]
        original_base64 = base64.b64encode(data).decode("ascii")
        document_pages = {**document_pages, "original_retained": True}
    elif size > MAX_FETCH_BYTES:
        document_pages = _retain_original_blob(document_pages, spool_path, raw)
    return FetchedDocument(
        text=text,
        response_sha256=digest,
        content_type=content_type,
        final_url=current_url,
        original_html_base64=original_base64,
        redirect_count=redirect_count,
        pages=document_pages,
    )


def _retain_original_blob(document_pages: dict, path: Path | None, raw: bytes | None) -> dict:
    """Store a >5 MB original in the content-addressed blob store when one is configured."""
    store = configured_blob_store()
    digest: str | None = None
    if store is not None:
        try:
            digest = store.put_file(path) if path is not None else store.put_bytes(raw or b"")
        except OSError:
            digest = None
    if digest is not None and is_valid_sha256(digest):
        return {**document_pages, "original_retained": True, "original_blob_sha256": digest}
    return {**document_pages, "original_retained": False}


def fetch_source_document(
    request: FetchSourceRequest,
    transport: Callable[..., HttpResponse | BinaryHttpResponse] | None = None,
    *,
    timeout_seconds: float = 15.0,
    ocr_usage=None,
) -> FetchedDocument:
    """Retrieve and extract a public HTTPS HTML, plain-text, or PDF document with bounded safe redirects.

    ``ocr_usage`` (``research_ocr.OcrUsageHooks``) enables vision-model OCR of scanned pages through the
    caller's reserve/settle accounting; without it only local OCR engines run.
    """
    return _fetch_document(
        request,
        transport=transport,
        timeout_seconds=timeout_seconds,
        accepted_types=frozenset({"text/html", "text/plain", "application/pdf"}),
        accept_header="text/html, text/plain, application/pdf;q=0.9",
        max_redirects=MAX_REDIRECT_HOPS,
        ocr_usage=ocr_usage,
    )


def fetch_official_text(
    request: FetchSourceRequest,
    transport: Callable[..., HttpResponse | BinaryHttpResponse] | None = None,
    *,
    timeout_seconds: float = 15.0,
) -> FetchedText:
    """Retrieve and extract bounded HTML or plain text without redirects."""
    document = _fetch_document(
        request,
        transport=transport,
        timeout_seconds=timeout_seconds,
        accepted_types=frozenset({"text/html", "text/plain"}),
        accept_header="text/html, text/plain",
        max_redirects=0,
    )
    return FetchedText(
        text=document.text,
        response_sha256=document.response_sha256,
        content_type=document.content_type,
        original_html_base64=document.original_html_base64,
        pages=document.pages,
    )


def fetch_official_pdf(
    request: FetchSourceRequest,
    *,
    timeout_seconds: float = 15.0,
    transport: Callable[..., object] | None = None,
) -> FetchedPdf:
    """Retrieve and extract one bounded PDF without redirects."""
    document = _fetch_document(
        request,
        transport=transport,
        timeout_seconds=timeout_seconds,
        accepted_types=frozenset({"application/pdf"}),
        accept_header="application/pdf",
        max_redirects=0,
    )
    return FetchedPdf(
        text=document.text,
        response_sha256=document.response_sha256,
        content_type="application/pdf",
        original_pdf_base64=document.original_pdf_base64,
        pages=document.pages,
    )
